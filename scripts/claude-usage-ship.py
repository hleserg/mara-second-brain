#!/usr/bin/env python3
"""Тики лимитов и снимок тулсета с клиентской машины в волт (бриф §2, §4, §8).

Ставится там, где живёт Claude Code (BetaPi, мак), кроном раз в пять минут.
Statusline дописывает сырой блоб в `~/.local/state/mara/ticks.raw` одним printf
— без jq и без сети, чтобы не тормозить строку состояния. Разбирает и ужимает
его этот скрипт, он же снимает тулсет и он же везёт всё на doctor.

Один писатель на файл (§8): сырьё лежит в папке своего хоста, никакая другая
машина в неё не пишет. `ticks.raw` забираем переименованием — statusline после
этого откроет по имени новый файл, и гонки за строку не будет.

    python3 scripts/claude-usage-ship.py                 # тики + тулсет + rsync
    python3 scripts/claude-usage-ship.py --no-push       # только локально
"""
import os, re, sys, json, glob, socket, hashlib, argparse, subprocess
from datetime import datetime, timezone

SCHEMA = 1          # тулсеты
TICK_SCHEMA = 2     # тики: с v2 серия одинаковых тиков ужимается в одну строку
HOME = os.path.expanduser("~")
STATE = os.environ.get("MARA_STATE", os.path.join(HOME, ".local/state/mara"))
RAW = os.environ.get("CLAUDE_USAGE_TICKS", os.path.join(STATE, "ticks.raw"))
SPOOL = os.path.join(STATE, "usage-spool")
DEST = os.environ.get("MARA_VAULT_SSH", "doctor:/srv/vault")
SUB = "Claude Usage/_data"


def host():
    return os.environ.get("MARA_HOST") or socket.gethostname()


def get(d, path, default=None):
    for k in path.split("."):
        if not isinstance(d, dict): return default
        d = d.get(k)
        if d is None: return default
    return d


def slim(epoch, d):
    """Блоб statusline → строка тика. Держим только то, что нужно счёту, но
    целиком prompt_cache: по нему видно, во что обходятся перестройки кэша."""
    cwd = get(d, "workspace.current_dir") or d.get("cwd") or ""
    rl = d.get("rate_limits") or {}
    return {
        "schema_version": TICK_SCHEMA,
        "ts": datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds"),
        "epoch": round(epoch, 3),
        "host": host(),
        "session_id": d.get("session_id") or "",
        "session_name": d.get("session_name") or "",
        "model_id": get(d, "model.id") or "",
        "model_name": get(d, "model.display_name") or "",
        "effort": get(d, "effort.level") or "",
        "cc_version": d.get("version") or "",
        "cwd": cwd,
        "project_key": ("-" + re.sub(r"[^A-Za-z0-9]+", "-", cwd).strip("-")) if cwd else "",
        # rate_limits в некоторых версиях отсутствует — это не повод падать
        # (бриф §1): тик пишем с null, интервал потом пометится как «без данных».
        "five_hour_pct": get(rl, "five_hour.used_percentage"),
        "five_hour_resets_at": get(rl, "five_hour.resets_at"),
        "seven_day_pct": get(rl, "seven_day.used_percentage"),
        "seven_day_resets_at": get(rl, "seven_day.resets_at"),
        "has_rate_limits": bool(rl),
        "ctx_used_tokens": get(d, "context_window.total_input_tokens"),
        "ctx_pct": get(d, "context_window.used_percentage"),
        "ctx_size": get(d, "context_window.context_window_size"),
        "exceeds_200k": d.get("exceeds_200k_tokens"),
        "cost_usd": get(d, "cost.total_cost_usd"),
        "duration_ms": get(d, "cost.total_duration_ms"),
        "api_duration_ms": get(d, "cost.total_api_duration_ms"),
        "lines_added": get(d, "cost.total_lines_added"),
        "lines_removed": get(d, "cost.total_lines_removed"),
        "cache_hit_ratio": get(d, "prompt_cache.hit_ratio"),
        "cache_write_tokens": get(d, "prompt_cache.cache_write_tokens"),
        "cache_misses": get(d, "prompt_cache.misses"),
        "fast_mode": d.get("fast_mode"),
        "thinking": get(d, "thinking.enabled"),
    }


# Ключ серии: пока это всё не менялось, тики одной сессии несут одно и то же.
RUN_KEY = ("session_id", "model_id", "effort", "cc_version", "cwd", "has_rate_limits",
           "five_hour_pct", "five_hour_resets_at", "seven_day_pct", "seven_day_resets_at",
           "fast_mode", "thinking")
# А это в серии меняется каждый ход; из серии интересно последнее значение.
RUN_LAST = ("session_name", "ctx_used_tokens", "ctx_pct", "ctx_size", "exceeds_200k",
            "cost_usd", "duration_ms", "api_duration_ms", "lines_added", "lines_removed",
            "cache_hit_ratio", "cache_write_tokens", "cache_misses")


def collapse(rows):
    """Серия одинаковых тиков одной сессии → одна строка с `ts_last` и `n`.

    Statusline срабатывает после каждого ответа — в активной работе это тик раз
    в пару секунд, а проценты лимита целые и меняются раз в минуты. Пятеро
    параллельных агентов дают 10 тиков в минуту, 12 МБ в сутки в волт, который
    коммитится и синкается. Серия несёт ровно один факт: «сессия жила отсюда
    досюда с такими процентами» — его и оставляем. Момент, когда процент
    сменился, сохраняется точно: смена рвёт серию.

    Ужимаем только внутри одной порции: файл append-only, дописанную строку
    прошлого прогона задним числом не расширить."""
    open_runs, out = {}, []
    for r in sorted(rows, key=lambda r: r["epoch"]):
        k, sid = tuple(r[f] for f in RUN_KEY), r["session_id"]
        run = open_runs.get(sid)
        if run and run["_key"] == k:
            run["ts_last"], run["epoch_last"], run["n"] = r["ts"], r["epoch"], run["n"] + 1
            for f in RUN_LAST: run[f] = r[f]
            continue
        run = dict(r, _key=k, ts_last=r["ts"], epoch_last=r["epoch"], n=1)
        open_runs[sid] = run
        out.append(run)
    for r in out: del r["_key"]
    return out


def ticks(raw=RAW, spool=SPOOL):
    """Забрать накопленное и разложить по месяцам.

    Возвращает (строк записано, тиков прочитано, строк не разобрано). Битые
    строки считаем вслух: statusline пишет один printf из многих процессов
    сразу, и хотя запись в O_APPEND атомарна до размера буфера, блоб в полтора
    килобайта — не та величина, о которую можно молча спотыкаться."""
    if not os.path.exists(raw): return 0, 0, 0
    take = raw + ".take"
    if os.path.exists(take):                 # прошлый прогон умер на полпути
        with open(take, "a", encoding="utf-8") as fh:
            fh.write(open(raw, encoding="utf-8", errors="replace").read())
        os.unlink(raw)
    else:
        os.rename(raw, take)
    rows, read, bad = {}, 0, 0
    for line in open(take, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line: continue
        ep, _, body = line.partition(" ")
        try: r = slim(float(ep), json.loads(body))
        except (ValueError, TypeError):
            bad += 1
            continue
        read += 1
        rows.setdefault(r["ts"][:7], []).append(r)
    d = os.path.join(spool, SUB, host())
    if rows and not os.path.isdir(d): os.makedirs(d)
    n = 0
    for month, rs in rows.items():
        with open(os.path.join(d, "ticks-%s.jsonl" % month), "a", encoding="utf-8") as fh:
            for r in collapse(rs):
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                n += 1
    os.unlink(take)
    return n, read, bad


def toolset(spool=SPOOL):
    """Снимок того, что подключено (§4). Хуки для этого не нужны: всё лежит в
    конфигах, а меняется редко — пишем строку, только когда хэш поменялся."""
    def read(p):
        try: return open(os.path.expanduser(p), encoding="utf-8", errors="replace").read()
        except OSError: return ""
    try: cj = json.loads(read("~/.claude.json") or "{}")
    except ValueError: cj = {}
    try: st = json.loads(read("~/.claude/settings.json") or "{}")
    except ValueError: st = {}
    skills = sorted({os.path.basename(p) for p in
                     glob.glob(os.path.expanduser("~/.claude/skills/*")) +
                     glob.glob(os.path.expanduser("~/.claude/plugins/*/skills/*"))
                     if os.path.isdir(p)})
    plugins = sorted(k for k, v in (st.get("enabledPlugins") or {}).items() if v)
    out = []
    for cwd, pv in sorted((cj.get("projects") or {}).items()):
        servers = sorted(set(cj.get("mcpServers") or {}) | set(pv.get("mcpServers") or {})
                         | set(pv.get("enabledMcpjsonServers") or []))
        t = {"schema_version": SCHEMA, "host": host(), "cwd": cwd,
             "project_key": "-" + re.sub(r"[^A-Za-z0-9]+", "-", cwd).strip("-"),
             "mcp_servers": servers, "skills": skills, "plugins": plugins,
             "claude_md_bytes": len(read("~/.claude/CLAUDE.md").encode())
                                + len(read(os.path.join(cwd, "CLAUDE.md")).encode()),
             "allowed_tools": len(pv.get("allowedTools") or [])}
        t["toolset_hash"] = hashlib.sha1(json.dumps(
            [t["cwd"], t["mcp_servers"], t["skills"], t["plugins"], t["claude_md_bytes"]],
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
        t["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out.append(t)
    d = os.path.join(spool, SUB, host())
    if not os.path.isdir(d): os.makedirs(d)
    p = os.path.join(d, "toolsets.jsonl")
    known = set()
    if os.path.exists(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            try: known.add(json.loads(line).get("toolset_hash"))
            except ValueError: pass
    new = [t for t in out if t["toolset_hash"] not in known]
    if new:
        with open(p, "a", encoding="utf-8") as fh:
            for t in new: fh.write(json.dumps(t, ensure_ascii=False) + "\n")
    return len(new)


def push(spool=SPOOL, dest=DEST):
    """Спул и живые транскрипты — на doctor. Транскрипты вообще-то возит
    SessionEnd-хук, но только в конце сессии, а панель должна видеть текущую."""
    ok = True
    if os.path.isdir(spool):
        ok &= subprocess.run(["rsync", "-a", spool + "/", dest + "/"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    tr = os.path.expanduser(os.environ.get("CLAUDE_PROJECTS", "~/.claude/projects"))
    if os.path.isdir(tr):
        ok &= subprocess.run(["rsync", "-a", "--include=*/", "--include=*.jsonl", "--exclude=*",
                              tr + "/", dest + "/raw/claude-code/"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args(argv)
    n, read, bad = ticks()
    t = toolset()
    sent = None if a.no_push else push()
    print("claude-usage-ship: тиков %d → строк %d%s, новых тулсетов %d%s"
          % (read, n, ", НЕ РАЗОБРАНО %d" % bad if bad else "", t,
             "" if sent is None else (", отправлено" if sent else ", ОТПРАВИТЬ НЕ ВЫШЛО")))
    return 0


def self_check():
    import tempfile
    d = tempfile.mkdtemp()
    raw = os.path.join(d, "ticks.raw")
    spool = os.path.join(d, "spool")
    blob = {"session_id": "s1", "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
            "effort": {"level": "xhigh"}, "version": "2.1.257",
            "workspace": {"current_dir": "/home/hleserg/x"},
            "cost": {"total_cost_usd": 1.25, "total_duration_ms": 10},
            "context_window": {"total_input_tokens": 176763, "used_percentage": 18},
            "prompt_cache": {"hit_ratio": 0.98},
            "rate_limits": {"five_hour": {"used_percentage": 28.0, "resets_at": 1788303000},
                            "seven_day": {"used_percentage": 5, "resets_at": 1788440400}}}
    open(raw, "w", encoding="utf-8").write(
        "1788296000.123 %s\n1788296300.5 %s\n" % (json.dumps(blob), json.dumps(blob)))
    # два одинаковых тика одной сессии — одна строка на выходе
    assert ticks(raw, spool) == (1, 2, 0)
    assert not os.path.exists(raw), "сырой файл должен быть забран переименованием"
    p = glob.glob(os.path.join(spool, SUB, "*", "ticks-*.jsonl"))
    assert len(p) == 1, p
    r = [json.loads(l) for l in open(p[0], encoding="utf-8")]
    assert r[0]["five_hour_pct"] == 28.0 and r[0]["seven_day_resets_at"] == 1788440400
    assert r[0]["project_key"] == "-home-hleserg-x", r[0]["project_key"]
    assert r[0]["ts"].startswith("2026-") and r[0]["epoch"] == 1788296000.123
    assert r[0]["ctx_pct"] == 18 and r[0]["cost_usd"] == 1.25
    assert (r[0]["n"], r[0]["epoch_last"]) == (2, 1788296300.5), r[0]

    # смена процента рвёт серию, момент смены сохраняется точно
    hi = json.loads(json.dumps(blob)); hi["rate_limits"]["five_hour"]["used_percentage"] = 29
    other = json.loads(json.dumps(blob)); other["session_id"] = "s2"
    open(raw, "w", encoding="utf-8").write("".join(
        "%s %s\n" % (e, json.dumps(b)) for e, b in
        [(1788296400.0, blob), (1788296410.0, other), (1788296420.0, blob),
         (1788296430.0, hi), (1788296440.0, hi)]))
    assert ticks(raw, spool) == (3, 5, 0)
    r = [json.loads(l) for l in open(p[0], encoding="utf-8")][-3:]
    # чужой тик посередине серию не рвёт: серии считаются внутри сессии
    assert [(x["session_id"], x["five_hour_pct"], x["n"]) for x in r] == \
           [("s1", 28.0, 2), ("s2", 28.0, 1), ("s1", 29, 2)], r
    assert r[2]["epoch"] == 1788296430.0, "момент смены процента должен быть точным"

    # повторный прогон на пустом месте ничего не ломает и не дублирует
    assert ticks(raw, spool) == (0, 0, 0)
    assert len(open(p[0], encoding="utf-8").read().strip().split("\n")) == 4
    # rate_limits нет вовсе — тик всё равно пишется, помеченный
    del blob["rate_limits"]
    open(raw, "w", encoding="utf-8").write("1788296600.0 %s\n" % json.dumps(blob))
    assert ticks(raw, spool) == (1, 1, 0)
    last = json.loads(open(p[0], encoding="utf-8").read().strip().split("\n")[-1])
    assert last["five_hour_pct"] is None and last["has_rate_limits"] is False
    # битая строка не роняет разбор, но её видно
    open(raw, "w", encoding="utf-8").write("не-число {}\n1788296700.0 {\n1788296800.0 %s\n"
                                           % json.dumps(blob))
    assert ticks(raw, spool) == (1, 1, 2)
    # тулсет пишется один раз на одинаковую конфигурацию
    a = toolset(spool)
    assert toolset(spool) == 0, "одинаковый тулсет должен писаться один раз"
    print("claude-usage-ship: самопроверка ок")
    return 0


if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
