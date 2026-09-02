#!/usr/bin/env python3
"""Короткий дайджест звонка в телеграм (ТЗ §16).

Текст собирается шаблоном, без модели: пересказывать уже извлечённое незачем,
а один вызов LLM ради вежливой формулировки — это лишняя точка отказа и лишняя
дорога, по которой личный разговор может уехать наружу.

Отправка прямо в Bot API, а не через Мару. `ctx.inject_message` в Hermes
запускает полноценный ход модели: дайджест стоил бы вызова LLM и мог бы быть
переписан ею по дороге. И он должен доходить, когда Мара занята или лежит.
Ответ Серёги («это тоже задача, срок пятница») ловит инструмент Мары из
спеки 2: он поднимает последний дайджест через /v1/context/bootstrap.

Токен читается из /etc/mara/contextd.env, в волт и в git не попадает никогда.

    python3 scripts/call_digest.py --event call_<uuid>
    python3 scripts/call_digest.py --self-check
"""
import os, sys, json, uuid, argparse, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import call_project as cp

ENV_FILE = os.environ.get("MARA_ENV_FILE", "/etc/mara/contextd.env")
API = "https://api.telegram.org/bot%s/sendMessage"

# Порядок и названия разделов — из ТЗ §16 и совпадают с карточкой разговора.
SECTIONS = [("requests", "Попросили"), ("commitments", "Ты обещал"),
            ("decisions", "Решили"), ("changed_instructions", "Изменилось"),
            ("open_questions", "Неясно")]


КЛЮЧИ = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL")


def env(path=None):
    """Токен и канал: сначала из окружения, потом из env-файла.

    Окружение первым не для красоты: systemd читает EnvironmentFile от root и
    передаёт переменные процессу, а сам файл может быть недоступен sergey. Без
    этой строки открытие файла падало бы в OSError, словарь оставался пустым, и
    каждый дайджест молча становился no-transport — без единой ошибки в логе.

    Ключи не печатаются никуда.
    """
    out = {k: os.environ[k] for k in КЛЮЧИ if os.environ.get(k)}
    try:
        with open(path or ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass
    return out


def задач(n):
    """«1 задача», «2 задачи», «5 задач» — иначе строка режет глаз каждый день."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "%d задача" % n
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return "%d задачи" % n
    return "%d задач" % n


def line_of(item):
    text = item.get("new_state") or item.get("action") or ""
    if item.get("supersedes"):
        text = "%s → %s" % (item["supersedes"], text)
    due = " (до %s)" % item["due_at"] if item.get("due_at") else ""
    return "• %s%s · %s" % (text, due, cp.stamp(item))


def render(event, extraction, created_count):
    """(текст дайджеста, пункты для таблицы digests)."""
    day, _, human = cp.when(event)
    end = (event.get("ended") or "")[11:16]
    head = "Звонок · %s · %s%s" % (cp.contact(event), human, "–" + end if end else "")
    out, items = [head], []
    maybe = []
    for key, title in SECTIONS:
        rows = []
        for it in (extraction.get(key) or []):
            record = {"key": key, "action": it.get("action") or it.get("new_state"),
                      "disposition": it.get("disposition"), "due_at": it.get("due_at"),
                      "evidence": it.get("evidence")}
            items.append(record)
            (rows if it.get("disposition") == "task" else maybe).append(line_of(it))
        if rows:
            out += ["", title] + rows
    if created_count:
        out += ["", "Создано", "• " + задач(created_count)]
    if maybe:
        out += ["", "Возможно задача"] + maybe
    return "\n".join(out), items


def deliver(text, token, chat_id):
    """Отправить или честно сказать, что транспорта нет. Текст не теряется."""
    if not token or not chat_id:
        return "no-transport"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(API % token, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        ok = json.loads(r.read() or b"{}").get("ok", False)
    return "sent" if ok else "failed"


def run(event_id, root=None, env_file=None):
    root = root or mi.ROOT
    con = mi.connect(root)
    ev = mi.event_row(con, event_id)
    epath = mi.extraction_path(root, event_id)
    if not os.path.exists(epath):
        raise RuntimeError("нет извлечения %s" % epath)
    extraction = json.load(open(epath, encoding="utf-8"))
    created = len(cp.commitment_cards(ev, extraction, {}))
    text, items = render(ev, extraction, created)
    e = env(env_file)
    state = deliver(text, e.get("TELEGRAM_BOT_TOKEN"), e.get("TELEGRAM_HOME_CHANNEL"))
    did = str(uuid.uuid4())
    # один дайджест на событие: сбой отправки уводит работу в ретрай, и вторая
    # попытка должна заменить строку, а не положить рядом ещё одну
    con.execute("delete from digests where event_id=?", (event_id,))
    con.execute("insert into digests(id,event_id,chat_id,text,items_json,sent_at,state) "
                "values(?,?,?,?,?,?,?)",
                (did, event_id, e.get("TELEGRAM_HOME_CHANNEL"), text,
                 json.dumps(items, ensure_ascii=False), mi.now_iso(), state))
    print("call_digest: %s — %s, пунктов %d" % (event_id, state, len(items)))
    if state == "failed":
        raise RuntimeError("телеграм не принял дайджест")
    con.execute("update events set state='done' where id=?", (event_id,))
    return did


def self_check():
    event = {"id": "call_x", "occurred": "2026-09-02T14:05:00+03:00",
             "ended": "2026-09-02T14:23:11+03:00",
             "payload": {"contact_name": "Анна"}}
    extr = {"requests": [{"action": "прислать смету", "disposition": "task",
                          "due_at": "2026-09-04",
                          "evidence": [{"start_ms": 252000, "end_ms": 260000}]}],
            "open_questions": [{"action": "покрасить стены",
                                "disposition": "needs-review",
                                "evidence": [{"start_ms": 60000, "end_ms": 61000}]}]}
    text, items = render(event, extr, 1)
    assert text.startswith("Звонок · Анна · 14:05–14:23"), text[:60]
    assert "Попросили" in text and "04:12" in text
    assert "Возможно задача" in text and "покрасить стены" in text
    assert "1 задача" in text, text
    assert len(items) == 2
    assert задач(1) == "1 задача" and задач(3) == "3 задачи" and задач(11) == "11 задач"
    assert deliver("x", None, None) == "no-transport"
    print("call_digest self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="дайджест звонка в телеграм")
    ap.add_argument("--event")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--env-file", default=ENV_FILE)
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if not a.event:
        ap.error("нужен --event")
    mi.ROOT = a.root
    run(a.event, a.root, a.env_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
