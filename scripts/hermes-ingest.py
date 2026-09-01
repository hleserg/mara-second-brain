#!/usr/bin/env python3
"""Транскрипты Мары в волт (ТЗ §6, §7). Гоняется на doctor.

Мара живёт на маке, её разговоры лежат в sqlite `~/.hermes/state.db`. Тянем их
сюда, кладём сырьём в `raw/hermes/` и делаем карточку тем же session-note.py,
что и для Claude Code с Codex — формат на выходе специально claude-code'овский,
чтобы не заводить третий парсер ради трёх полей.

Читаем по ssh и только select: база у Мары в WAL, писать в неё чужим процессом
нельзя, а читать на ходу можно.

    python3 scripts/hermes-ingest.py --vault /srv/vault
"""
import os, sys, json, argparse, subprocess
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=float(os.environ.get("MARA_TZ_HOURS", 3))))
# Без `active = 1`: компрессия контекста гасит старые сообщения, и живая
# сессия со временем «худела» бы прямо в сырье. Синтетические выжимки
# компрессии (_compressed_summary) наоборот не берём — это не то, что говорили.
Q = ('select session_id,role,content,timestamp from messages '
     "where role in ('user','assistant') and content is not null and content <> '' "
     'and _compressed_summary = 0 order by session_id, id')

def fetch(mac, db):
    """Сообщения с мака. sqlite3 -json есть в macOS начиная с Sonoma."""
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", mac,
                          "sqlite3 -json %s %s" % (db, json_arg(Q))],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if out.returncode:
        raise RuntimeError(out.stderr.decode("utf-8", "replace").strip()[:200])
    body = out.stdout.decode("utf-8", "replace").strip()
    return json.loads(body) if body else []

def json_arg(sql):
    """Кавычки внутри запроса свои, поэтому в шелл он едет в одинарных."""
    return "'" + sql.replace("'", "'\\''") + "'"

def as_claude(rows):
    """{sid: [строки jsonl]} в форме транскрипта Claude Code."""
    out = {}
    for r in rows:
        ts = datetime.fromtimestamp(r["timestamp"], TZ).isoformat(timespec="seconds")
        if r["role"] == "user":
            d = {"type": "user", "timestamp": ts, "message": {"content": r["content"]}}
        else:
            d = {"type": "assistant", "timestamp": ts,
                 "message": {"content": [{"type": "text", "text": r["content"]}]}}
        out.setdefault(r["session_id"], []).append(json.dumps(d, ensure_ascii=False))
    return out

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--mac", default=os.environ.get("MARA_MAC", "serg@192.168.1.80"))
    ap.add_argument("--db", default="~/.hermes/state.db")
    # Разговор с Марой — личное по умолчанию (§7.2: настроение, проблемы,
    # третьи лица). Решит владелец иначе — снимается этим флагом.
    ap.add_argument("--to-cloud", dest="sensitive", action="store_false",
                    help="разрешить дистилляцию разговоров с Марой в облаке")
    a = ap.parse_args(argv)

    sessions = as_claude(fetch(a.mac, a.db))
    made = 0
    for sid, lines in sessions.items():
        raw_rel = "raw/hermes/%s.jsonl" % sid
        raw = os.path.join(a.vault, raw_rel)
        text = "\n".join(lines) + "\n"
        # Сессия у Мары не кончается: session_reset выключен, а разговоры
        # группируются по пользователю — один sid живёт месяцами. Поэтому
        # сырьё переписываем всегда, а не только пока нет карточки, иначе в
        # волте остался бы первый час разговора и больше ничего.
        os.makedirs(os.path.dirname(raw), exist_ok=True)
        # Короче прежнего — значит что-то съело историю на той стороне.
        # Затирать длинное коротким не будем: сырьё восстановить неоткуда.
        if not (os.path.exists(raw) and len(text) < os.path.getsize(raw) * 0.9):
            tmp = raw + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh: fh.write(text)
            os.replace(tmp, raw)
        # Карточка — один раз: она про то, когда разговор начался. Для вечной
        # сессии это дата первого снимка, и это честнее, чем каждый час
        # переписывать occurred на сегодня.
        if os.path.exists(os.path.join(a.vault, "kb/sessions", sid + ".md")): continue
        subprocess.run([sys.executable, os.path.join(HERE, "session-note.py"), raw,
                        "--vault", a.vault, "--raw-rel", raw_rel, "--session-id", sid,
                        "--source", "hermes"] + (["--sensitive"] if a.sensitive else []) +
                       ["--skip-existing", "--skip-empty"], check=False)
        made += 1
    print("hermes-ingest: сессий %d, новых карточек %d" % (len(sessions), made))
    return 0

def self_check():
    rows = [{"session_id": "s1", "role": "user", "content": "привет", "timestamp": 1787920862.0},
            {"session_id": "s1", "role": "assistant", "content": "ну привет", "timestamp": 1787920938.0}]
    got = as_claude(rows)
    d = [json.loads(x) for x in got["s1"]]
    assert d[0]["type"] == "user" and d[0]["message"]["content"] == "привет"
    assert d[1]["message"]["content"][0]["text"] == "ну привет"
    # session-note должен уметь это прочитать — иначе весь смысл формата пропал
    sys.path.insert(0, HERE)
    from session_note_compat import parse, messages
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "s.jsonl")
    open(p, "w", encoding="utf-8").write("\n".join(got["s1"]) + "\n")
    f = parse(p)
    assert f["users"] == 1 and f["assists"] == 1, f
    assert list(messages(p)) == [("user", "привет"), ("assistant", "ну привет")]
    # старое сырьё не затирается усохшим: compression гасит сообщения, и
    # прогон после неё не должен уносить историю
    import tempfile as _t
    d = _t.mkdtemp(); raw = os.path.join(d, "s.jsonl")
    open(raw, "w", encoding="utf-8").write("x" * 1000)
    shrank = lambda t: os.path.exists(raw) and len(t) < os.path.getsize(raw) * 0.9
    assert shrank("y" * 100)          # усохло — не пишем
    assert not shrank("z" * 1000)     # столько же — пишем
    assert not shrank("z" * 5000)     # дописали — пишем
    # кавычка в запросе не разваливает шелл
    assert json_arg("select 'a'") == """'select '\\''a'\\'''"""
    print("hermes-ingest: самопроверка ок")
    return 0

if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
