#!/usr/bin/env python3
"""Напоминание по сообщению (Т-У.4): пишет человек, перед которым есть
открытое обещание, — Мара говорит владельцу «ты обещал: …».

У звонка напоминание едет разделом дайджеста («Обещано раньше»). У
сообщений дайджеста нет, а напоминание на каждое сообщение было бы шумом:
переписка — десятки сообщений подряд. Поэтому повод — **первое за день**
свежее входящее сообщение человека: решает приём (`завести`, в той же
транзакции, что событие), шаг `nudge` ставится только тогда. Остальное —
шаг воркера (`run`): кто это по волту, что перед ним открыто, отправка.

Не повод:
- исходящее (`outgoing`) — пишет сам владелец;
- импорт истории (`via: export`) и правки/удаления (`revision_of`, без
  отправителя) — это не «пришло сейчас»;
- старое (`occurred` старше часа): догон TDLib после простоя отдаёт сотни
  сообщений, и напоминание про вчерашнее — не напоминание. Этот же порог
  гасит ретраи шага: через час после сообщения напоминать поздно.

«За день» — по дню владельца (`MARA_TZ_HOURS`) и по подписи отправителя как
её прислал источник: шаг `nudge` на этого же отправителя сегодня уже стоял —
второго нет, даже если первый ничего не нашёл. Узнавание — то же, что у
дайджеста (`context_pack.про_человека`): без нечёткого сопоставления, две
Анны в индексе — никого.

Отправка — строкой outbox своего вида (`telegram_nudge`, §5.2), но в отличие
от дайджеста не копится: нет транспорта — строка `skipped`, а не `pending`.
Напоминание, доехавшее после починки токена через день, хуже, чем никакое.
Что транспорта нет, и так скажет сверка по дайджестам.

    python3 scripts/message_nudge.py --event msg_<uuid>
    python3 scripts/message_nudge.py --self-check
"""
import argparse, datetime, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

ВИД = "telegram_nudge"             # kind строки outbox
ШАГ = "nudge"                      # kind работы
СВЕЖЕСТЬ_С = 3600
МАКС = 5                           # строк обещаний, остальное — числом


def _когда(s):
    """Время строки в секундах эпохи; нечитаемое — None."""
    try:
        t = datetime.datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=mi.TZ)
    return t.timestamp()


def кто(payload):
    """Подпись отправителя как её прислал источник, или пустая строка."""
    s = (payload or {}).get("sender_name")
    return s.strip() if isinstance(s, str) else ""


def повод(payload, occurred, now=None):
    """Сообщение годится в повод: входящее, живое (не импорт и не правка),
    с отправителем и не старше `СВЕЖЕСТЬ_С`. Нечитаемое время — не повод:
    в отличие от дайджеста, здесь молчать дешевле, чем напомнить невпопад."""
    p = payload or {}
    if p.get("outgoing") or p.get("via") == "export" or p.get("revision_of"):
        return False
    if not кто(p):
        return False
    t = _когда(occurred)
    now = time.time() if now is None else now
    return t is not None and -СВЕЖЕСТЬ_С <= now - t <= СВЕЖЕСТЬ_С


def начало_дня(now=None):
    """Полночь дня владельца (`mi.TZ`) в секундах эпохи."""
    t = datetime.datetime.fromtimestamp(time.time() if now is None else now, mi.TZ)
    return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def уже_сегодня(con, отправитель, now=None):
    """Шаг напоминания на этого отправителя сегодня уже ставился."""
    return con.execute(
        "select 1 from jobs j join events e on e.id = j.event_id "
        "where j.kind=? and e.kind='message' "
        "and json_extract(e.payload_json, '$.sender_name') = ? "
        "and %s >= ? limit 1" % mi.секунды("j.created"),
        (ШАГ, отправитель, int(начало_дня(now)))).fetchone() is not None


def завести(con, event_id, data, now=None):
    """Приём: поставить шаг `nudge`, если сообщение — повод и первое за день
    от этого отправителя. Зовётся внутри транзакции приёма, после
    `put_event`. Возвращает id работы или None."""
    p = data.get("payload") or {}
    if data.get("kind") != "message":
        return None
    if not повод(p, data.get("occurred_at"), now):
        return None
    # отправителя сравниваем как он лёг в `payload_json`: без `.strip()`,
    # иначе «Анна » и «Анна» были бы двумя людьми только для этой проверки
    if уже_сегодня(con, p.get("sender_name"), now):
        return None
    return mi.add_job(con, event_id, ШАГ)


def текст(отправитель, пункты):
    """Сообщение владельцу. Строки — как в пакете (`context_pack.строка`)
    без имени: оно в шапке. Пол отправителя неизвестен — без «ему/ей»."""
    import context_pack
    шапка = "Пишет «%s». Ты обещал:" % context_pack.данные(отправитель, 60)
    строки = ["• " + context_pack.строка(dict(it, promised_to=None))[2:]
              for it in пункты[:МАКС]]
    if len(пункты) > МАКС:
        строки.append("• …и ещё %d" % (len(пункты) - МАКС))
    return "\n".join([шапка] + строки)


def обещано(vault, отправитель):
    """Открытое перед отправителем; любой сбой чтения волта — пусто."""
    import call_project as cp
    import context_pack
    try:
        return context_pack.про_человека(vault, [отправитель], владелец=cp.OWNER)
    except Exception as e:
        print("message_nudge: обещания не прочитаны (%s)" % type(e).__name__,
              file=sys.stderr)
        return []


def run(event_id, root=None, env_file=None, vault=None, now=None):
    """Шаг воркера. Возвращает id строки outbox или None, если писать
    нечего. Исключение — только когда телеграм отказал (`failed`): ретрай
    имеет смысл, пока сообщение свежее."""
    import call_digest as cd
    root = root or mi.ROOT
    vault = vault or os.environ.get("VAULT", "/srv/vault")
    con = mi.connect(root)
    ev = mi.event_row(con, event_id)
    p = ev.get("payload") or {}
    if ev.get("kind") != "message" or not повод(p, ev.get("occurred"), now):
        print("message_nudge: %s — не повод (не входящее или старше %d мин)"
              % (event_id, СВЕЖЕСТЬ_С // 60))
        return None
    if con.execute("select 1 from outbox where kind=? and object_id=? and "
                   "state in ('sent','sending')", (ВИД, event_id)).fetchone():
        print("message_nudge: %s — уже отправлено" % event_id)
        return None
    пункты = обещано(vault, кто(p))
    if not пункты:
        print("message_nudge: %s — открытого нет" % event_id)
        return None
    with mi.транзакция(con):
        con.execute("update outbox set state='skipped', error='пересобран' "
                    "where kind=? and object_id=? and state='pending'", (ВИД, event_id))
        oid = mi.в_outbox(con, ВИД, {"text": текст(кто(p), пункты)}, "event", event_id)
    итоги = mi.из_outbox(con, cd._отправитель(cd.env(env_file)), kind=ВИД, ид=oid)
    исход = итоги[0][1] if итоги else None
    if исход == "failed":
        raise RuntimeError("телеграм не принял напоминание")
    if исход != "sent":
        # не копим: через день оно бессмысленно (см. шапку модуля)
        with mi.транзакция(con):
            con.execute("update outbox set state='skipped', error=? where id=? "
                        "and state='pending'", (исход or "не отправлено", oid))
        print("message_nudge: %s — не доставлено (%s), не копится" % (event_id, исход),
              file=sys.stderr)
    print("message_nudge: %s — %s, пунктов %d" % (event_id, исход, len(пункты)))
    return oid


def self_check():
    now = _когда("2026-09-02T14:05:00+03:00")
    p = {"sender_name": "Анна Петрова", "text": "привет"}
    assert повод(p, "2026-09-02T14:04:00+03:00", now)
    assert повод(p, "2026-09-02T11:05:00+00:00", now), "сдвиг читается"
    assert not повод(p, "2026-09-02T13:04:59+03:00", now), "старше часа"
    assert not повод(p, "вчера", now) and not повод(p, None, now)
    assert not повод(dict(p, outgoing=True), "2026-09-02T14:04:00+03:00", now)
    assert not повод(dict(p, via="export"), "2026-09-02T14:04:00+03:00", now)
    assert not повод(dict(p, revision_of="7/2"), "2026-09-02T14:04:00+03:00", now)
    assert not повод({"sender_name": "  "}, "2026-09-02T14:04:00+03:00", now)
    t = текст("Анна", [{"title": "прислать смету", "due": "2026-09-04",
                        "promised_to": "Анна", "id": None}] * 7)
    assert t.splitlines()[0] == "Пишет «Анна». Ты обещал:", t
    assert t.splitlines()[1] == "• «прислать смету» — до 2026-09-04", t
    assert t.splitlines()[-1] == "• …и ещё 2" and len(t.splitlines()) == 7, t
    print("message_nudge self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="напоминание по сообщению (Т-У.4)")
    ap.add_argument("--event")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if not a.event:
        ap.error("нужен --event")
    mi.ROOT = a.root
    run(a.event, a.root, a.env_file, a.vault)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
