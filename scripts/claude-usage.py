#!/usr/bin/env python3
"""Учёт расхода лимитов Claude Code. Ни одного вызова модели.

Разведка (§10.1 брифа) показала, что почти всё уже лежит в транскриптах
`~/.claude/projects/**/<session>.jsonl`, а их на doctor приносит существующий
SessionEnd-хук — 535 файлов в `raw/claude-code/`, включая мак. Возить нечего,
хуки заводить не на что: `assistant` несёт usage по каждому ответу, модель,
effort, версию, ветку и sidechain; `user.toolUseResult` — результат инструмента
и его размер; `system.compactMetadata` — компакции; `cost-state` — деньги,
посчитанные самим Claude Code (прайс-таблица не нужна).

Чего в транскриптах нет вовсе — процентов лимита подписки. Их отдаёт только
statusline, поэтому тики пишет отдельный шаг (см. `claude-usage-tick.sh`).

    python3 scripts/claude-usage.py scan --out /srv/vault/"Claude Usage"/_data
    python3 scripts/claude-usage.py status
"""
import os, re, sys, json, glob, argparse, collections
from datetime import datetime, timezone

SCHEMA = 1
# Транскрипт не знает, на какой машине писался. Единственный надёжный признак —
# корень рабочего каталога. Догадка, и помечена как догадка.
HOSTS = [("/home/hleserg", "BetaPi"), ("/Users/serg", "mac"), ("/home/sergey", "doctor")]
# Результат инструмента крупнее этого попадает в лог поимённо: из таких и
# состоит «куда ушёл контекст», остальные интересны только суммой.
HEAVY = 20000
MCP = re.compile(r"^mcp__([^_]+(?:_[^_]+)*?)__")


def kind(name):
    """builtin | mcp | skill | agent — и имя сервера, если это MCP."""
    m = MCP.match(name or "")
    if m: return "mcp", m.group(1)
    if name == "Skill": return "skill", ""
    if name == "Agent": return "agent", ""
    return "builtin", ""


def host_of(cwd, project_key=""):
    """Каталог проекта — запасной признак: у сессии в /tmp своего cwd нет, но имя
    папки транскрипта его помнит («-home-hleserg-…»)."""
    for pre, h in HOSTS:
        if (cwd or "").startswith(pre): return h
        if project_key.startswith(pre.replace("/", "-")): return h
        if ("-" + pre.replace("/", "-")) in project_key: return h
    return "unknown"


def ts_of(d):
    t = d.get("timestamp")
    return t if isinstance(t, str) else ""


def epoch_of(ts):
    """ISO → epoch. Тики штампованы epoch'ом, ходы — строкой; свести их на одну
    ось иначе нельзя."""
    try: return datetime.fromisoformat((ts or "").replace("Z", "+00:00")).timestamp()
    except ValueError: return 0.0


def size(x):
    """Байты, которые результат инструмента занял в контексте."""
    if x is None: return 0
    if isinstance(x, str): return len(x.encode("utf-8"))
    try: return len(json.dumps(x, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError): return len(str(x).encode("utf-8"))


def parse(path):
    """Транскрипт → (запись сессии, тяжёлые вызовы инструментов, ходы).

    Ход — это `(время, сессия, модель, токены)` одного ответа. Агрегатору он
    нужен поштучно: интервал между тиками надо разложить по сессиям, а сумма по
    сессии тут не поможет.

    Дедуп по `message.id`: одно и то же сообщение встречается в файле не раз
    (резюме компакции, ретраи), и без дедупа токены задваиваются."""
    s = {"schema_version": SCHEMA, "session_id": os.path.basename(path)[:-6],
         "transcript": path, "started_at": "", "ended_at": "", "cc_version": "",
         "effort": "", "cwd": "", "git_branch": "", "project_key": os.path.basename(os.path.dirname(path)),
         "n_turns": 0, "n_sidechain_turns": 0, "n_synthetic": 0, "n_tool_calls": 0, "n_compactions": 0,
         "tokens_in": 0, "tokens_out": 0, "tokens_cache_write": 0, "tokens_cache_read": 0,
         "tokens_thinking": 0, "first_turn_input_tokens": 0, "dropped_tokens": 0,
         "api_usd_equiv": 0.0, "unknown_model_cost": False, "duration_ms": 0,
         "api_duration_ms": 0, "tool_duration_ms": 0, "lines_added": 0, "lines_removed": 0,
         "models": {}, "tools": {}, "mcp_servers": {}, "skills": {},
         "tool_result_bytes": 0, "advisor_models": {}}
    seen, heavy, use, turns = set(), [], {}, []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line: continue
        try: d = json.loads(line)
        except ValueError: continue
        t = d.get("type")
        if d.get("sessionId"): s["session_id"] = d["sessionId"]
        if t == "assistant":
            m = d.get("message") or {}
            mid = m.get("id")
            if mid and mid in seen: continue
            if mid: seen.add(mid)
            u = m.get("usage") or {}
            ts = ts_of(d)
            if ts and (not s["started_at"] or ts < s["started_at"]): s["started_at"] = ts
            if ts > s["ended_at"]: s["ended_at"] = ts
            for k, f in (("cc_version", "version"), ("effort", "effort"),
                         ("cwd", "cwd"), ("git_branch", "gitBranch")):
                if d.get(f): s[k] = d[f]
            if d.get("advisorModel"):
                s["advisor_models"][d["advisorModel"]] = s["advisor_models"].get(d["advisorModel"], 0) + 1
            # `<synthetic>` — не ответ модели, а заглушка Claude Code (обрыв,
            # ошибка API). На маке из таких состоят 506 пустых сессий OpenClaw,
            # и без отсева они утопили бы любой счёт сессий.
            if (m.get("model") or "") in ("", "<synthetic>"):
                s["n_synthetic"] += 1
                continue
            s["n_sidechain_turns" if d.get("isSidechain") else "n_turns"] += 1
            got = (u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                   u.get("cache_creation_input_tokens") or 0, u.get("cache_read_input_tokens") or 0)
            s["tokens_in"] += got[0]; s["tokens_out"] += got[1]
            s["tokens_cache_write"] += got[2]; s["tokens_cache_read"] += got[3]
            s["tokens_thinking"] += (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
            turns.append({"epoch": epoch_of(ts), "session_id": s["session_id"],
                          "model": m["model"], "sidechain": bool(d.get("isSidechain")),
                          "in": got[0], "out": got[1], "cw": got[2], "cr": got[3]})
            # §4А: первый ответ — это системный промпт, описания инструментов и
            # CLAUDE.md. По нему и меряется, во что обходится сам тулсет.
            if not s["first_turn_input_tokens"]: s["first_turn_input_tokens"] = got[0] + got[2]
            if d.get("attributionSkill"):
                s["skills"][d["attributionSkill"]] = s["skills"].get(d["attributionSkill"], 0) + 1
            for b in m.get("content") or []:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"): continue
                n = b.get("name") or "?"
                s["n_tool_calls"] += 1
                s["tools"][n] = s["tools"].get(n, 0) + 1
                k, srv = kind(n)
                if srv: s["mcp_servers"][srv] = s["mcp_servers"].get(srv, 0) + 1
                if k == "skill":
                    sk = (b.get("input") or {}).get("skill") or "?"
                    s["skills"][sk] = s["skills"].get(sk, 0) + 1
                use[b.get("id")] = (n, ts)
        elif t == "user" and d.get("toolUseResult") is not None:
            n = size(d["toolUseResult"])
            s["tool_result_bytes"] += n
            tid = next((b.get("tool_use_id") for b in (d.get("message") or {}).get("content") or []
                        if isinstance(b, dict) and b.get("tool_use_id")), None)
            name, when = use.get(tid, ("?", ts_of(d)))
            if n >= HEAVY:
                heavy.append({"schema_version": SCHEMA, "ts": when or ts_of(d),
                              "session_id": s["session_id"], "tool_name": name,
                              "kind": kind(name)[0], "mcp_server": kind(name)[1],
                              "result_bytes": n})
        elif t == "system" and d.get("compactMetadata"):
            s["n_compactions"] += 1
            s["dropped_tokens"] = d["compactMetadata"].get("cumulativeDroppedTokens") or s["dropped_tokens"]
        elif t == "cost-state":
            # Деньги считает сам Claude Code, включая модели советника и
            # подагентов. Берём последнее состояние, оно накопительное.
            s["api_usd_equiv"] = d.get("totalCostUSD") or 0.0
            s["unknown_model_cost"] = bool(d.get("hasUnknownModelCost"))
            for f, k in (("totalDuration", "duration_ms"), ("totalAPIDuration", "api_duration_ms"),
                         ("totalToolDuration", "tool_duration_ms"),
                         ("totalLinesAdded", "lines_added"), ("totalLinesRemoved", "lines_removed")):
                if d.get(f): s[k] = d[f]
            s["models"] = {k: v for k, v in (d.get("modelUsage") or {}).items()}
    s["host"] = host_of(s["cwd"], s["project_key"])
    s["model_ids"] = sorted(s["models"])
    s["tokens_total"] = s["tokens_in"] + s["tokens_out"] + s["tokens_cache_write"] + s["tokens_cache_read"]
    s["day"] = s["started_at"][:10]
    # session_id узнаётся по ходу разбора; проставим его ходам задним числом
    for t in turns: t["session_id"] = s["session_id"]
    return (s, heavy, turns) if s["n_turns"] or s["n_sidechain_turns"] else (None, [], [])


def transcripts(roots):
    for r in roots:
        for p in sorted(glob.glob(os.path.join(os.path.expanduser(r), "*", "*.jsonl"))):
            yield p


def dump(path, rows):
    tmp = path + ".tmp"                      # атомарно: рядом крутится sync (§8)
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows: fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def scan(roots, out):
    sessions, heavy, turns = [], [], []
    for p in transcripts(roots):
        try: s, h, t = parse(p)
        except OSError: continue
        if s: sessions.append(s); heavy.extend(h); turns.extend(t)
    sessions.sort(key=lambda s: s["started_at"])
    heavy.sort(key=lambda h: h["ts"])
    turns.sort(key=lambda t: t["epoch"])
    if out:
        d = os.path.join(out, "derived")
        if not os.path.isdir(d): os.makedirs(d)
        dump(os.path.join(d, "sessions.jsonl"), sessions)
        dump(os.path.join(d, "heavy-results.jsonl"), heavy)
    return sessions, heavy, turns


def human(n):
    for u, k in (("млрд", 1e9), ("млн", 1e6), ("тыс", 1e3)):
        if n >= k: return "%.1f %s" % (n / k, u)
    return str(int(n))


def status(sessions):
    by_model = collections.Counter(); cost = collections.Counter()
    by_host = collections.Counter(); tok = collections.Counter()
    for s in sessions:
        by_host[s["host"]] += 1
        for m, v in s["models"].items():
            by_model[m] += (v.get("inputTokens", 0) + v.get("outputTokens", 0)
                            + v.get("cacheReadInputTokens", 0) + v.get("cacheCreationInputTokens", 0))
            cost[m] += v.get("costUSD", 0.0)
        for k in ("tokens_in", "tokens_out", "tokens_cache_write", "tokens_cache_read"): tok[k] += s[k]
    days = sorted({s["day"] for s in sessions if s["day"]})
    print("сессий: %d, с %s по %s, машины: %s"
          % (len(sessions), days[0] if days else "?", days[-1] if days else "?",
             ", ".join("%s %d" % (h, n) for h, n in by_host.most_common())))
    print("токенов: вход %s, выход %s, запись кэша %s, чтение кэша %s"
          % tuple(human(tok[k]) for k in ("tokens_in", "tokens_out", "tokens_cache_write", "tokens_cache_read")))
    print("эквивалент по ценам API: $%.2f" % sum(cost.values()))
    print("\n%-34s %10s %10s" % ("модель", "токенов", "$"))
    for m, n in by_model.most_common():
        print("%-34s %10s %10.2f" % (m, human(n), cost[m]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("scan", "status"))
    ap.add_argument("--roots", nargs="+", default=[os.environ.get(
        "CLAUDE_PROJECTS", "~/.claude/projects")])
    ap.add_argument("--out", help="каталог _data; без него ничего не пишем")
    a = ap.parse_args(argv)
    sessions, heavy, _ = scan(a.roots, a.out if a.cmd == "scan" else None)
    if a.cmd == "scan":
        print("claude-usage: сессий %d, тяжёлых результатов %d%s"
              % (len(sessions), len(heavy), ", записано в " + a.out if a.out else ""))
        return 0
    return status(sessions)


def self_check():
    import tempfile
    d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "-home-hleserg-x"))
    p = os.path.join(d, "-home-hleserg-x", "s1.jsonl")
    rows = [
        {"type": "assistant", "sessionId": "s1", "timestamp": "2026-08-31T10:00:00Z",
         "cwd": "/home/hleserg/x", "version": "2.1.257", "effort": "xhigh",
         "gitBranch": "main", "advisorModel": "claude-fable-5",
         "message": {"id": "m1", "model": "claude-opus-5", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash"},
             {"type": "tool_use", "id": "t2", "name": "mcp__basic-memory__search"}],
          "usage": {"input_tokens": 10, "output_tokens": 5,
                    "cache_creation_input_tokens": 100, "cache_read_input_tokens": 7,
                    "output_tokens_details": {"thinking_tokens": 3}}}},
        # тот же message.id — дубль, токены не должны удвоиться
        {"type": "assistant", "sessionId": "s1", "timestamp": "2026-08-31T10:00:01Z",
         "message": {"id": "m1", "model": "claude-opus-5",
                     "usage": {"input_tokens": 10, "output_tokens": 5}}},
        {"type": "user", "toolUseResult": {"stdout": "x" * 30000},
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
        {"type": "user", "toolUseResult": "мелочь",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t2"}]}},
        {"type": "system", "compactMetadata": {"trigger": "auto", "cumulativeDroppedTokens": 333821}},
        {"type": "cost-state", "totalCostUSD": 1.5, "totalLinesAdded": 12,
         "hasUnknownModelCost": False, "totalDuration": 900,
         "modelUsage": {"claude-opus-5": {"inputTokens": 10, "outputTokens": 5, "costUSD": 1.5}}},
    ]
    open(p, "w", encoding="utf-8").write("\n".join(json.dumps(r) for r in rows) + "\n")
    s, heavy, turns = parse(p)
    assert s["session_id"] == "s1" and s["host"] == "BetaPi", s
    assert host_of("", "-tmp-claude-1000--home-hleserg-x-scratchpad") == "BetaPi"
    assert s["n_turns"] == 1, "дубль по message.id не отсеян: %d" % s["n_turns"]
    assert (s["tokens_in"], s["tokens_out"], s["tokens_cache_write"], s["tokens_cache_read"]) == (10, 5, 100, 7)
    assert s["tokens_thinking"] == 3 and s["first_turn_input_tokens"] == 110
    assert s["n_tool_calls"] == 2 and s["tools"]["Bash"] == 1
    assert s["mcp_servers"] == {"basic-memory": 1}, s["mcp_servers"]
    assert s["advisor_models"] == {"claude-fable-5": 1}
    assert s["n_compactions"] == 1 and s["dropped_tokens"] == 333821
    assert s["api_usd_equiv"] == 1.5 and s["lines_added"] == 12
    assert s["effort"] == "xhigh" and s["cc_version"] == "2.1.257"
    # тяжёлый результат попал поимённо и привязан к своему инструменту,
    # мелкий — только в сумму
    assert len(heavy) == 1 and heavy[0]["tool_name"] == "Bash", heavy
    assert heavy[0]["result_bytes"] > 30000
    assert s["tool_result_bytes"] > 30000
    assert kind("mcp__basic-memory__search") == ("mcp", "basic-memory")
    assert kind("Read") == ("builtin", "") and kind("Skill")[0] == "skill"
    # сессия из одних заглушек — не сессия
    sy = os.path.join(d, "-home-hleserg-x", "s3.jsonl")
    open(sy, "w").write(json.dumps({"type": "assistant", "sessionId": "s3",
        "message": {"id": "z", "model": "<synthetic>", "usage": {}}}) + "\n")
    assert parse(sy) == (None, [], [])

    # пустой транскрипт сессией не считается
    e = os.path.join(d, "-home-hleserg-x", "s2.jsonl")
    open(e, "w").write('{"type":"summary"}\n')
    assert parse(e) == (None, [], [])
    # ход попал в ленту один раз (дубль по message.id тут тоже должен отсеяться)
    assert len(turns) == 1 and turns[0]["model"] == "claude-opus-5", turns
    assert turns[0]["epoch"] == epoch_of("2026-08-31T10:00:00Z") > 0
    assert (turns[0]["in"], turns[0]["cr"]) == (10, 7)
    out = tempfile.mkdtemp()
    ss, hh, tt = scan([d], out)
    assert len(ss) == 1 and os.path.exists(os.path.join(out, "derived/sessions.jsonl"))
    print("claude-usage: самопроверка ок")
    return 0


if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
