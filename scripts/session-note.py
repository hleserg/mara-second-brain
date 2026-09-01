#!/usr/bin/env python3
"""Карточка сессии Claude Code из JSONL-транскрипта (ТЗ §6.1).

Механически, без LLM: на doctor генеративной модели нет, а приёмка этапа 2
требует, чтобы при отключённой сети сырьё всё равно писалось. Поэтому
карточка — это индекс (о чём, когда, где, чем), а смысл сессии остаётся в
сырье и ждёт дистилляции: `distilled: false` + задача в _system/queue/.

§13.10 «не копировать транскрипты в kb/»: в карточку идут только заголовок,
первый запрос и счётчики. Тела сообщений — нет.
"""
import json, os, re, sys, argparse
from datetime import datetime, timedelta, timezone
from collections import Counter

# Codex вклеивает AGENTS.md и окружение первым же user-сообщением. Это не то,
# «с чего началось» — настоящий запрос идёт следующим.
CODEX_INJECTED = ("# AGENTS.md instructions", "<environment_context>", "<user_instructions>")

def _blank():
    return {"title": None, "prompt": None, "cwd": None, "branch": None,
            "ts": [], "users": 0, "assists": 0, "tools": Counter(),
            "models": [], "cost": None, "duration": None, "sid": None,
            "source": "claude-code"}

def _lines(path):
    """Битые строки молча пропускаем: транскрипт живой файл, последняя строка
    может быть недописана."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try: yield json.loads(line)
            except ValueError: continue

def parse(path):
    for d in _lines(path):
        return parse_codex(path) if d.get("type") == "session_meta" else parse_claude(path)
    return _blank()

def parse_codex(path):
    """Rollout Codex (ТЗ §6.2). Хуков у Codex нет, карточку делает зеркалка."""
    f = _blank(); f["source"] = "codex"
    for d in _lines(path):
        if d.get("timestamp"): f["ts"].append(d["timestamp"])
        p = d.get("payload") or {}
        t = d.get("type")
        if t == "session_meta":
            f["sid"] = p.get("session_id"); f["cwd"] = p.get("cwd")
            g = p.get("git") or {}
            f["branch"] = g.get("branch") if isinstance(g, dict) else None
        elif t == "turn_context":
            f["cwd"] = p.get("cwd") or f["cwd"]
            if p.get("model") and p["model"] not in f["models"]: f["models"].append(p["model"])
        elif t == "response_item":
            pt = p.get("type")
            if pt == "message":
                text = " ".join(b.get("text", "") for b in p.get("content") or []).strip()
                if p.get("role") == "user" and not text.startswith(CODEX_INJECTED):
                    f["users"] += 1
                    if f["prompt"] is None: f["prompt"] = text
                elif p.get("role") == "assistant":
                    f["assists"] += 1
            elif pt in ("function_call", "custom_tool_call"):
                # reasoning с encrypted_content не трогаем вовсе (§6.1)
                f["tools"][p.get("name") or pt] += 1
    return f

def parse_claude(path):
    f = _blank()
    for d in _lines(path):
        t = d.get("type")
        f["sid"] = d.get("sessionId") or f["sid"]
        for k, j in (("cwd", "cwd"), ("branch", "gitBranch")):
            if d.get(j): f[k] = d[j]
        if d.get("timestamp"): f["ts"].append(d["timestamp"])
        if t == "ai-title":
            f["title"] = d.get("aiTitle")
        elif t == "cost-state":
            f["cost"] = d.get("totalCostUSD")
            f["duration"] = d.get("totalDuration")
            f["models"] = sorted(d.get("modelUsage") or {})
        elif t == "user":
            c = (d.get("message") or {}).get("content")
            # Настоящий запрос человека: content — строка. Результаты
            # инструментов приходят тем же type=user, но списком блоков.
            if isinstance(c, str) and not d.get("isMeta") and not d.get("isSidechain"):
                f["users"] += 1
                if f["prompt"] is None: f["prompt"] = c
        elif t == "assistant":
            f["assists"] += 1
            for b in (d.get("message") or {}).get("content") or []:
                # encrypted thinking пропускаем (§6.1) — сюда не попадает,
                # считаем только tool_use.
                if b.get("type") == "tool_use": f["tools"][b.get("name")] += 1
    return f

# Часовой пояс волта, а не машины: хук крутится на beta-pi (+05), волт живёт
# на doctor, а «что я делал вчера» считается по времени Сергея. Без этого одна
# и та же сессия попадала бы в разные сутки в зависимости от того, где хук.
TZ = timezone(timedelta(hours=float(os.environ.get("MARA_TZ_HOURS", 3))))

def _local(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)

def _now():
    return datetime.now(TZ)

def _clip(s, n):
    """Обрезка по границе слова: заголовок, разорванный посередине, потом
    попадает в эмбеддинги и в графы — пусть хотя бы читается."""
    if not s or len(s) <= n: return s
    cut = s[:n]
    return (cut[:cut.rfind(" ")] if " " in cut[n // 2:] else cut).rstrip(" ,.;:—-") + "…"

def _yaml_str(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')

def render(f, sid, raw_rel):
    ts = sorted(f["ts"])
    start = _local(ts[0]) if ts else _now()
    end = _local(ts[-1]) if ts else start
    prompt = re.sub(r"\s+", " ", (f["prompt"] or "").strip())
    title = f["title"] or _clip(prompt, 80) or "Сессия %s" % sid[:8]
    # /tmp и домашний каталог — не проект, а «запустил откуда попало».
    cwd = (f["cwd"] or "").rstrip("/")
    project = os.path.basename(cwd) if cwd and cwd not in ("", "/tmp", "/var/tmp",
                                                           os.path.expanduser("~")) else None

    fm = ["---",
          "title: " + _yaml_str(title),
          "type: session",
          "source: " + f["source"],
          "source_id: " + sid,
          "created: " + _now().isoformat(timespec="seconds"),
          "occurred: " + start.date().isoformat()]
    if project: fm.append("project: " + _yaml_str("[[%s]]" % project))
    fm += ["tags: [session, %s]" % f["source"],
           "sensitive: false",
           "distilled: false",
           "---", ""]

    span = "%s %s–%s" % (start.date().isoformat(), start.strftime("%H:%M"), end.strftime("%H:%M"))
    if f["duration"]: span += " (%d мин)" % round(f["duration"] / 60000)
    label = "Claude Code" if f["source"] == "claude-code" else "Codex"
    body = ["# " + title, "",
            "Сессия %s, %s." % (label, span)]
    if f["cwd"]: body.append("Каталог: `%s`%s." % (f["cwd"], ", ветка `%s`" % f["branch"] if f["branch"] else ""))
    body.append("")
    if prompt:
        body += ["**С чего началось:** " + _clip(prompt, 300), ""]
    body.append("- Ходов: %d запросов, %d шагов ассистента" % (f["users"], f["assists"]))
    if f["tools"]:
        body.append("- Инструменты: " + ", ".join("%s×%d" % (n, c) for n, c in f["tools"].most_common()))
    if f["models"]: body.append("- Модели: " + ", ".join(f["models"]))
    if f["cost"]: body.append("- Стоимость: $%.2f" % f["cost"])
    body += ["- Сырьё: `%s`" % raw_rel, "",
             "> Не дистиллировано. Карточка собрана механически, содержание сессии — в сырье,",
             "> задача стоит в `_system/queue/`.", ""]
    return "\n".join(fm + body)

def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"   # .tmp в том же каталоге: rename атомарен только на одной fs
    with open(tmp, "w", encoding="utf-8") as fh: fh.write(text)
    os.replace(tmp, path)

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("transcript")
    p.add_argument("--session-id")
    p.add_argument("--vault", help="корень волта (или спула той же формы); без него — в stdout")
    p.add_argument("--raw-rel", help="путь к сырью относительно волта; по умолчанию — как у claude-code")
    p.add_argument("--skip-empty", action="store_true",
                   help="молча выйти, если человек не сказал ни слова (оборванная сессия)")
    p.add_argument("--skip-existing", action="store_true",
                   help="не трогать уже готовую карточку (подбор пропущенных)")
    a = p.parse_args(argv)

    f = parse(a.transcript)
    if a.skip_empty and not f["users"]: return 0
    sid = a.session_id or f["sid"] or os.path.splitext(os.path.basename(a.transcript))[0]
    raw_rel = a.raw_rel or "raw/claude-code/%s/%s.jsonl" % (
        os.path.basename(os.path.dirname(os.path.abspath(a.transcript))), sid)
    note = render(f, sid, raw_rel)
    if not a.vault:
        sys.stdout.write(note); return 0
    if a.skip_existing and os.path.exists(os.path.join(a.vault, "kb/sessions", sid + ".md")):
        return 0
    write_atomic(os.path.join(a.vault, "kb/sessions", sid + ".md"), note)
    write_atomic(os.path.join(a.vault, "_system/queue", sid + ".json"),
                 json.dumps({"kind": "distill", "source": "claude-code", "source_id": sid,
                             "note": "kb/sessions/%s.md" % sid, "raw": raw_rel,
                             "queued": _now().isoformat(timespec="seconds")},
                            ensure_ascii=False, indent=2) + "\n")
    return 0

def self_check():
    import tempfile
    rec = lambda **k: json.dumps(k) + "\n"
    lines = (rec(type="user", sessionId="S1", cwd="/home/x/proj", gitBranch="main",
                 timestamp="2026-08-30T11:00:00.000Z", message={"content": "Почини синк"})
             + rec(type="assistant", sessionId="S1", timestamp="2026-08-30T11:00:05.000Z",
                   message={"content": [{"type": "thinking", "thinking": "..."},
                                        {"type": "tool_use", "name": "Bash"}]})
             + rec(type="user", sessionId="S1", timestamp="2026-08-30T11:00:06.000Z",
                   message={"content": [{"type": "tool_result", "content": "ok"}]})
             + rec(type="user", sessionId="S1", isMeta=True, message={"content": "мета"})
             + "{битый json\n"
             + rec(type="ai-title", sessionId="S1", aiTitle="Починка синка")
             + rec(type="cost-state", sessionId="S1", totalCostUSD=1.5, totalDuration=600000,
                   modelUsage={"claude-opus-5": {}}))
    with tempfile.TemporaryDirectory() as d:
        tp = os.path.join(d, "projects", "-home-x-proj", "S1.jsonl")
        os.makedirs(os.path.dirname(tp)); open(tp, "w").write(lines)
        f = parse(tp)
        assert f["users"] == 1, f["users"]              # tool_result и meta не считаем
        assert f["assists"] == 1 and f["tools"]["Bash"] == 1
        assert f["title"] == "Починка синка" and f["prompt"] == "Почини синк"
        v = os.path.join(d, "vault")
        main([tp, "--vault", v])
        note = os.path.join(v, "kb/sessions/S1.md")
        first = open(note, encoding="utf-8").read()
        assert "title: \"Починка синка\"" in first
        assert "occurred: 2026-08-30" in first          # дата события, не сегодня
        assert "distilled: false" in first
        assert "raw/claude-code/-home-x-proj/S1.jsonl" in first
        assert "project: \"[[proj]]\"" in first
        assert "Почини синк" in first and "tool_result" not in first
        main([tp, "--vault", v])                        # идемпотентность §6.1
        assert os.listdir(os.path.join(v, "kb/sessions")) == ["S1.md"]
        assert len(re.findall("^---$", open(note, encoding="utf-8").read(), re.M)) == 2
        assert json.load(open(os.path.join(v, "_system/queue/S1.json")))["source_id"] == "S1"

        # Codex: другой формат, тот же рендер
        cp = os.path.join(d, "rollout-x.jsonl")
        open(cp, "w").write(
            rec(type="session_meta", timestamp="2026-08-28T05:00:00.000Z",
                payload={"session_id": "C1", "cwd": "/home/x/proj"})
            + rec(type="response_item", timestamp="2026-08-28T05:00:01.000Z",
                  payload={"type": "message", "role": "user",
                           "content": [{"text": "# AGENTS.md instructions\nвклейка"}]})
            + rec(type="response_item", timestamp="2026-08-28T05:00:02.000Z",
                  payload={"type": "message", "role": "user",
                           "content": [{"text": "Собери прошивку"}]})
            + rec(type="response_item", payload={"type": "reasoning", "encrypted_content": "gAAA"})
            + rec(type="response_item", payload={"type": "custom_tool_call", "name": "shell"})
            + rec(type="response_item", timestamp="2026-08-28T05:10:00.000Z",
                  payload={"type": "message", "role": "assistant",
                           "content": [{"text": "готово"}]}))
        c = parse(cp)
        assert c["source"] == "codex" and c["sid"] == "C1"
        assert c["users"] == 1 and c["prompt"] == "Собери прошивку"   # вклейку не считаем
        assert c["assists"] == 1 and c["tools"]["shell"] == 1
        main([cp, "--vault", v, "--raw-rel", "raw/codex/pi/rollout-x.jsonl"])
        cn = open(os.path.join(v, "kb/sessions/C1.md"), encoding="utf-8").read()
        assert "source: codex" in cn and "occurred: 2026-08-28" in cn
        assert "raw/codex/pi/rollout-x.jsonl" in cn and "AGENTS.md" not in cn
        assert _clip("раз два три четыре", 12) == "раз два три…"
        assert _clip("короткое", 40) == "короткое"
    print("session-note self-check ok")

if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
