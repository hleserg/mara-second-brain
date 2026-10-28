#!/usr/bin/env python3
"""Утро с кнопками (Т-У.5): блок утра уходит с doctor, под пунктами кнопки.

Текст утра тот же, что печатал крон Мары (`daily-summary.утро`, правила —
`docs/morning-brief.md`), но отправляет его doctor: кнопкам нужны Bot API
(`reply_markup` умеет только бот) и реестр (версия карточки для
`expected_version`), а то и другое живёт здесь. Крону Мары на маке остаётся
сводка за вчера (`daily-summary.py --no-morning`) — иначе утра два.

Под каждым пунктом — ряд кнопок «сделал / не моё / завтра»: та же команда,
что у правки словами (`mara.command.correction.v1`), нажатия принимает
`telegram_buttons.py`. В данных кнопки — код карточки (хвост id, как в
пакете) и версия из реестра: нажатие против изменившейся карточки — конфликт,
а не слепая правка. Пункт без `id:` в шапке или без строки в реестре с
непроверяемой версией не остаётся — версия тогда не едет вовсе, правка
пройдёт путём «проверить нечем» (п.3 ADR-0003).

Утро обязано говорить и «ничего» (правило 7 `docs/morning-brief.md`):
пустой день уходит одной строкой, без кнопок.

Отправка — строкой outbox вида `telegram_morning` с ключом-датой: повтор
крона за тот же день дубля не шлёт. Не доставлено — `skipped`, не копится:
утро, доехавшее к обеду, хуже никакого, а завтра будет новое; причина — в
`outbox.error`.

    python3 scripts/morning_send.py                  # утро в телеграм
    python3 scripts/morning_send.py --dry-run        # только напечатать
    python3 scripts/morning_send.py --self-check
"""
import argparse, datetime, importlib.util, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

ВИД = "telegram_morning"           # kind строки outbox
# подпись и буква действия; буква — в callback_data, разбирает telegram_buttons
ДЕЙСТВИЯ = (("сделал", "d"), ("не моё", "n"), ("завтра", "t"))


def сегодня(now=None):
    """Сегодняшний день владельца, `ГГГГ-ММ-ДД`."""
    now = now or datetime.datetime.now(mi.TZ)
    return now.astimezone(mi.TZ).date().isoformat()


def _сводка():
    """`daily-summary.py` с дефисом в имени обычным import не берётся."""
    spec = importlib.util.spec_from_file_location(
        "daily_summary", os.path.join(HERE, "daily-summary.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def кнопки(con, пункты):
    """Ряды inline-клавиатуры по пунктам утра: `N сделал / N не моё /
    N завтра`. В `callback_data` — буква действия, код карточки (последние
    восемь знаков id, как в пакете) и версия из реестра (`-`, если строки
    нет: правка пройдёт как непроверяемая, п.3 ADR-0003). Пункт без `id:`
    в шапке остаётся без кнопок — адресовать нажатие некуда.

    Правка рукой в Obsidian версию реестра не двигает (реестр —
    авторитет, ADR-0001, но правку рукой он забирает переносом, п.4):
    пункт, чья шапка разошлась со строкой реестра по статусу или сроку,
    остаётся без кнопок — «защищённый версией» клик переписал бы ручную
    правку молча (Codex), а отставшую шапку чинит `ledger_import.py
    --write-ids`, не кнопка. Правку рукой после отправки утра ловит сервер: при
    `expected_version` рассинхрон шапки со строкой — конфликт
    (`call_project._правка`), а не слепая правка."""
    ряды = []
    for n, fm in пункты:
        ид = str(fm.get("id") or "").strip()
        код = ид[-8:].lower()
        if len(ид) < 8 or not all(c in "0123456789abcdef" for c in код):
            continue
        row = (con.execute("select version, status, due from commitments "
                           "where id=?", (ид,)).fetchone()
               if con is not None else None)
        # срок целиком: правка рукой одного времени — тоже дрейф (Codex)
        if row and (str(row["status"] or "") != str(fm.get("status") or "")
                    or str(row["due"] or "") != str(fm.get("due") or "")):
            continue
        версия = str(row["version"]) if row else "-"
        ряды.append([{"text": "%d %s" % (n, имя),
                      "callback_data": "%s:%s:%s" % (буква, код, версия)}
                     for имя, буква in ДЕЙСТВИЯ])
    return ряды


def run(root=None, vault=None, env_file=None, now=None, dry_run=False):
    """Утренний прогон. Возвращает `already`, `dry-run` или исход отправки
    (`sent`, `failed`, `no-transport`, `not-private`)."""
    import call_digest as cd
    vault = vault or os.environ.get("VAULT", "/srv/vault")
    день = сегодня(now)
    con = mi.connect(root or mi.ROOT)
    текст, пункты = _сводка().утро(vault, день)
    e = cd.env(env_file)
    if dry_run:
        # превью работает и до настройки токенов: оператору надо видеть
        # текст и кнопки, не включая транспорт (Codex, круг 10)
        print(текст)
        for ряд in кнопки(con, пункты):
            print(" | ".join("%s → %s" % (к["text"], к["callback_data"]) for к in ряд))
        return "dry-run"
    # Кнопки живут на СВОЁМ боте (`MARA_BUTTONS_BOT_TOKEN`): нажатия —
    # обновления того бота, чьё сообщение, а очередь бота Hermes трогать
    # нельзя (Codex, см. telegram_buttons). Без полной настройки (оба
    # токена) утро от doctor не уходит вовсе: оно остаётся за кроном Мары,
    # и двух утр не бывает ни при вводе кнопок, ни при их откате (Codex,
    # круг 9); мёртвых кнопок это заодно тоже исключает.
    свой_бот = e.get("MARA_BUTTONS_BOT_TOKEN")
    if not (свой_бот and e.get("MARA_BUTTONS_TOKEN")):
        print("morning_send: кнопки не настроены — утро остаётся за кроном Мары")
        return "quiet"
    e = dict(e, TELEGRAM_BOT_TOKEN=свой_бот)
    ряды = кнопки(con, пункты)
    # проверка и вставка — одной транзакцией, как у вечернего напоминания:
    # два прогона разом не шлют оба
    with mi.транзакция(con):
        было = con.execute("select id, state, last_attempt from outbox where kind=? "
                           "and object_id=? and state in ('sent','sending')",
                           (ВИД, день)).fetchone()
        if было is None:
            con.execute("update outbox set state='skipped', error='пересобран' "
                        "where kind=? and object_id=? and state='pending'", (ВИД, день))
            payload = {"text": текст}
            if ряды:
                payload["buttons"] = ряды
            oid = mi.в_outbox(con, ВИД, payload, "morning", день)
        elif было["state"] == "sending" and _брошено(было["last_attempt"], now):
            con.execute("update outbox set state='skipped', error=? where id=? and "
                        "state='sending'",
                        ("брошено посреди отправки — возможно, доставлено", было["id"]))
    if было is not None:
        print("morning_send: %s уже отправлялось (%s)" % (день, было["state"]))
        return "already"
    try:
        итоги = mi.из_outbox(con, cd._отправитель(e), kind=ВИД, ид=oid)
        исход = итоги[0][1] if итоги else None
    except Exception as e:
        исход = "error: %s" % type(e).__name__
    if исход != "sent":
        with mi.транзакция(con):
            con.execute("update outbox set state='skipped', error=? where id=? and "
                        "state='pending'", (исход or "не отправлено", oid))
        print("morning_send: %s — не доставлено (%s), не копится"
              % (день, исход or "строку забрал другой прогон"), file=sys.stderr)
    print("morning_send: %s — %s, пунктов %d, с кнопками %d"
          % (день, исход, len(пункты), len(ряды)))
    return исход


def _брошено(last_attempt, now=None):
    """Как у вечернего напоминания: `sending` старше аренды — процесс умер."""
    try:
        t = datetime.datetime.fromisoformat(str(last_attempt))
    except ValueError:
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=mi.TZ)
    now = now or datetime.datetime.now(mi.TZ)
    return (now - t).total_seconds() > mi.АРЕНДА_OUTBOX_С


def self_check():
    import tempfile
    v = tempfile.mkdtemp()
    os.makedirs(os.path.join(v, "kb/commitments"))
    ид = "01999999-0000-7000-8000-00000000abcd"
    with open(os.path.join(v, "kb/commitments", "a.md"), "w", encoding="utf-8") as fh:
        fh.write("---\ntitle: прислать смету\nid: %s\nstatus: open\n"
                 "due: 2026-10-15\npromised_to: Анна\n---\n" % ид)
    with open(os.path.join(v, "kb/commitments", "b.md"), "w", encoding="utf-8") as fh:
        fh.write("---\ntitle: без id\nstatus: proposed\noccurred: 2026-10-15\n---\n")
    root = tempfile.mkdtemp()
    con = mi.connect(root)
    con.execute("insert into commitments(id, title, status, due, version, "
                "source_native_id) values(?,?,?,?,?,?)",
                (ид, "прислать смету", "open", "2026-10-15", 7, "sc-1"))
    con.commit()
    ds = _сводка()
    текст, пункты = ds.утро(v, "2026-10-15")
    assert ds.morning(v, "2026-10-15") == текст, "morning — тот же текст"
    assert [n for n, _ in пункты] == [1, 2], пункты
    ряды = кнопки(con, пункты)
    # пункт без id остаётся без ряда, версия — из реестра
    assert len(ряды) == 1 and ряды[0][0]["callback_data"] == "d:0000abcd:7", ряды
    assert [к["text"] for к in ряды[0]] == ["1 сделал", "1 не моё", "1 завтра"]
    con.execute("delete from commitments")
    con.commit()
    assert кнопки(con, пункты)[0][0]["callback_data"] == "d:0000abcd:-", "без строки — без версии"
    # пустое утро — текст есть (правило 7), кнопок нет
    пусто = tempfile.mkdtemp(); os.makedirs(os.path.join(пусто, "kb/commitments"))
    текст2, пункты2 = ds.утро(пусто, "2026-10-15")
    assert "ничего" in текст2 and not пункты2, текст2
    print("morning_send: самопроверка ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="утро с кнопками (Т-У.5)")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    mi.ROOT = a.root
    исход = run(a.root, a.vault, a.env_file, dry_run=a.dry_run)
    return 1 if исход == "failed" or str(исход).startswith("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
