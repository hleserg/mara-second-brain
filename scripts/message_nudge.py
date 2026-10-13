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
- исходящее (`outgoing`, у SMS `direction`) — пишет сам владелец;
- группа: «пишет Анна» в общем чате — не разговор с ней (`отправитель`);
- импорт истории (`via: export`), правки и надгробия — это не «пришло
  сейчас»; удалённое до шага сообщение тоже не напоминает;
- старое (`occurred` старше часа): догон TDLib после простоя отдаёт сотни
  сообщений, и напоминание про вчерашнее — не напоминание. Этот же порог
  гасит ретраи шага: через час после сообщения напоминать поздно.

«За день» — по дню владельца (`MARA_TZ_HOURS`) и по ключу отправителя (у
SMS — номер, у мессенджеров — подпись как её прислал источник): шаг `nudge`
на этого же отправителя сегодня уже стоял — второго нет, даже если первый
ничего не нашёл. Шаг лёгкий, и воркер берёт его раньше тяжёлых
(`contextd.такт`): за пачкой расшифровок он бы устарел. Узнавание — то же, что у
дайджеста (`context_pack.про_человека`): без нечёткого сопоставления, две
Анны в индексе — никого.

Отправка — строкой outbox своего вида (`telegram_nudge`, §5.2), но в отличие
от дайджеста не копится: нет транспорта — строка `skipped`, а не `pending`.
Напоминание, доехавшее после починки токена через день, хуже, чем никакое.
Своей находки у недоставленного напоминания нет: причина — в `outbox.error`,
а что транспорта нет, сверка скажет по недоставленным дайджестам звонков.
Доставка — не больше одного раза (`run`).

    python3 scripts/message_nudge.py --event msg_<uuid>
    python3 scripts/message_nudge.py --self-check
"""
import argparse, datetime, json, os, sys, time

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


ЛИЧНЫЕ = ("private", "secret")     # `chat_type` личного чата (TDLib, телефон)


def отправитель(source, payload):
    """`(подпись, имена, ключ)` отправителя или None.

    - SMS (`source: sms`, телефон): имя из книги и номер — оба ключи
      `про_человека`, номер сильнее имени; ключ дня — последние десять цифр
      номера. В подписи номер не печатается — только хвост.
    - Остальное — только личный чат (`chat_type` из `ЛИЧНЫЕ`): в группе
      «пишет Анна» — это не разговор с ней, и напоминание вне контекста ещё
      и съело бы дневной повод до её личного сообщения. Анонимный админ
      группы в TDLib подписан названием чата — тоже отсекается здесь.
      Ключ — подпись как её прислал источник."""
    p = payload or {}
    if source == "sms":
        имя = p.get("contact_name") if isinstance(p.get("contact_name"), str) else ""
        номер = p.get("number") if isinstance(p.get("number"), str) else ""
        цифры = "".join(c for c in номер if c.isdigit())[-10:]
        if not (имя.strip() or цифры):
            return None
        подпись = имя.strip() or "номер …%s" % цифры[-4:]
        return подпись, [x for x in (имя.strip(), номер) if x], \
            "tel:" + цифры if цифры else имя.strip()
    if p.get("chat_type") not in ЛИЧНЫЕ:
        return None
    имя = p.get("sender_name")
    if not (isinstance(имя, str) and имя.strip()):
        return None
    return имя.strip(), [имя.strip()], имя


def повод(source, payload, occurred, now=None):
    """Сообщение годится в повод: входящее, живое (не импорт, не правка, не
    надгробие), из личного разговора с узнаваемым отправителем и не старше
    `СВЕЖЕСТЬ_С`. Нечитаемое время — не повод: в отличие от дайджеста, здесь
    молчать дешевле, чем напомнить невпопад."""
    p = payload or {}
    if p.get("outgoing") or p.get("direction") == "outgoing":
        return False
    if p.get("via") == "export" or p.get("revision_of") or p.get("tombstone_of"):
        return False
    if отправитель(source, p) is None:
        return False
    t = _когда(occurred)
    now = time.time() if now is None else now
    return t is not None and -СВЕЖЕСТЬ_С <= now - t <= СВЕЖЕСТЬ_С


def начало_дня(now=None):
    """Полночь дня владельца (`mi.TZ`) в секундах эпохи."""
    t = datetime.datetime.fromtimestamp(time.time() if now is None else now, mi.TZ)
    return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def уже_сегодня(con, ключ, now=None):
    """Шаг напоминания на этого отправителя сегодня уже ставился. Ключ
    считается в питоне (`отправитель`): у SMS это номер, у мессенджеров —
    подпись; шагов за день — единицы, перебрать их дешевле, чем учить SQL
    обоим правилам."""
    for r in con.execute(
            "select e.source, e.payload_json from jobs j join events e "
            "on e.id = j.event_id where j.kind=? and %s >= ?" % mi.секунды("j.created"),
            (ШАГ, int(начало_дня(now)))):
        кто_там = отправитель(r["source"], json.loads(r["payload_json"] or "{}"))
        if кто_там and кто_там[2] == ключ:
            return True
    return False


def завести(con, event_id, data, now=None):
    """Приём: поставить шаг `nudge`, если сообщение — повод и первое за день
    от этого отправителя. Зовётся внутри транзакции приёма, после
    `put_event`. Возвращает id работы или None."""
    p = data.get("payload") or {}
    if data.get("kind") != "message":
        return None
    if not повод(data.get("source"), p, data.get("occurred_at"), now):
        return None
    if уже_сегодня(con, отправитель(data.get("source"), p)[2], now):
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


def обещано(vault, имена):
    """Открытое перед отправителем; любой сбой чтения волта — пусто."""
    import call_project as cp
    import context_pack
    try:
        return context_pack.про_человека(vault, имена, владелец=cp.OWNER)
    except Exception as e:
        print("message_nudge: обещания не прочитаны (%s)" % type(e).__name__,
              file=sys.stderr)
        return []


def _погасить(con, event_id, почему):
    """Ждущие строки этого события — в `skipped`: напоминание не копится."""
    with mi.транзакция(con):
        con.execute("update outbox set state='skipped', error=? where kind=? and "
                    "object_id=? and state='pending'", (почему, ВИД, event_id))


def run(event_id, root=None, env_file=None, vault=None, now=None):
    """Шаг воркера. Возвращает id строки outbox или None, если писать
    нечего.

    Исключение — когда телеграм отказал (`failed`) или транспорт бросил:
    ретрай имеет смысл, пока сообщение свежее. Устарело после сбоев — тоже
    исключение, а не тихий нуль: иначе постоянная поломка шага не дошла бы
    до DLQ и находки `dlq` в сверке.

    Доставка — не больше одного раза: строка, брошенная посреди отправки
    (`sending`, демон убит), могла и уйти — повтор её не шлёт, а закрывает."""
    import call_digest as cd
    root = root or mi.ROOT
    vault = vault or os.environ.get("VAULT", "/srv/vault")
    con = mi.connect(root)
    ev = mi.event_row(con, event_id)
    p = ev.get("payload") or {}
    if ev.get("kind") != "message" or not повод(ev.get("source"), p, ev.get("occurred"), now):
        _погасить(con, event_id, "устарело")
        сбои = con.execute("select max(attempts) from jobs where event_id=? and kind=? "
                           "and state='ready'", (event_id, ШАГ)).fetchone()[0]
        if сбои:
            raise RuntimeError("напоминание устарело после сбоев шага: %d" % сбои)
        print("message_nudge: %s — не повод (не входящее или старше %d мин)"
              % (event_id, СВЕЖЕСТЬ_С // 60))
        return None
    if mi.message_state(con, ev["source"], ev["source_id"]) is None:
        print("message_nudge: %s — сообщение удалено" % event_id)
        return None
    было = con.execute("select id, state from outbox where kind=? and object_id=? and "
                       "state in ('sent','sending')", (ВИД, event_id)).fetchone()
    if было:
        if было["state"] == "sending":
            with mi.транзакция(con):
                con.execute("update outbox set state='skipped', error=? where id=? and "
                            "state='sending'",
                            ("брошено посреди отправки — возможно, доставлено", было["id"]))
        print("message_nudge: %s — уже отправлялось (%s)" % (event_id, было["state"]))
        return None
    подпись, имена, _ = отправитель(ev.get("source"), p)
    пункты = обещано(vault, имена)
    if not пункты:
        print("message_nudge: %s — открытого нет" % event_id)
        return None
    with mi.транзакция(con):
        con.execute("update outbox set state='skipped', error='пересобран' "
                    "where kind=? and object_id=? and state='pending'", (ВИД, event_id))
        oid = mi.в_outbox(con, ВИД, {"text": текст(подпись, пункты)}, "event", event_id)
    # `_отправитель` дайджеста: тот же транспорт и та же застава §8.3
    итоги = mi.из_outbox(con, cd._отправитель(cd.env(env_file)), kind=ВИД, ид=oid)
    исход = итоги[0][1] if итоги else None
    if исход == "failed":
        raise RuntimeError("телеграм не принял напоминание")
    if исход != "sent":
        # не копим: через день оно бессмысленно (см. шапку модуля)
        _погасить(con, event_id, исход or "не отправлено")
        print("message_nudge: %s — не доставлено (%s), не копится" % (event_id, исход),
              file=sys.stderr)
    print("message_nudge: %s — %s, пунктов %d" % (event_id, исход, len(пункты)))
    return oid


def self_check():
    now = _когда("2026-09-02T14:05:00+03:00")
    т = "2026-09-02T14:04:00+03:00"
    p = {"sender_name": "Анна Петрова", "chat_type": "private", "text": "привет"}
    assert повод("telegram", p, т, now)
    assert повод("telegram", p, "2026-09-02T11:05:00+00:00", now), "сдвиг читается"
    assert not повод("telegram", p, "2026-09-02T13:04:59+03:00", now), "старше часа"
    assert not повод("telegram", p, "вчера", now) and not повод("telegram", p, None, now)
    for лишнее in ({"outgoing": True}, {"via": "export"}, {"revision_of": "7/2"},
                   {"chat_type": "group"}, {"chat_type": None}, {"sender_name": "  "}):
        assert not повод("telegram", dict(p, **лишнее), т, now), лишнее
    смс = {"number": "+7 999 000-00-01", "contact_name": "Анна", "direction": "incoming"}
    assert отправитель("sms", смс) == ("Анна", ["Анна", "+7 999 000-00-01"],
                                      "tel:9990000001")
    assert отправитель("sms", {"number": "89990000001"})[0] == "номер …0001"
    assert повод("sms", смс, т, now)
    assert not повод("sms", dict(смс, direction="outgoing"), т, now)
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
