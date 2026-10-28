#!/usr/bin/env python3
"""Напоминание по сроку (Т-У.2): вечером накануне — «завтра срок: …».

Утро дня срока уже есть: блок «Сегодня с датой» утренней сводки
(`daily-summary.morning`, Т-У.1). Вечер — здесь. Крон doctor в 20:00
(`install/mara.cron`) берёт открытые карточки `kb/commitments` со сроком на
завтра по дню владельца (`MARA_TZ_HOURS`) и пишет владельцу в телеграм тем
же транспортом, что дайджест звонка и напоминание по сообщению: только
личный чат (§8.3, `call_digest.deliver`).

Правила утра (`docs/morning-brief.md`) действуют и здесь:
- не больше трёх пунктов, остальное — числом: длинный список одинаково
  срочного при СДВГ даёт паралич, а не действие;
- пустой вечер — тишина. Утро обязано говорить и «ничего» (тишина там
  неотличима от поломки), а вечер — повод, а не отчёт: «завтра ничего»
  каждый вечер — шум, к которому привыкают и перестают читать.

Срок и отбор — как у утра (`daily-summary.живые`): дата в начале значения,
неподтверждённое старше трёх дней не напоминается. Авторитет — реестр
(ADR-0001), но читатель здесь волт: срок и статус из шапки те же, что в
строке, пока `vault_drift.py --check` чист, а правку рукой в Obsidian
шапка показывает раньше, чем её заберёт перенос.

Отправка — строкой outbox вида `telegram_due` с ключом — датой срока
(`object_id`): повтор после успешной отправки дубль не шлёт. Доставка — «хотя
бы раз», как у дайджеста: если транспорт упал уже после того, как телеграм
принял сообщение, повтор тем же вечером пришлёт второе. Не копится, как напоминание Т-У.4: нет транспорта — строка
`skipped`. «Завтра срок», доехавшее послезавтра, хуже никакого.

    python3 scripts/due_reminder.py                  # завтрашние сроки, в телеграм
    python3 scripts/due_reminder.py --dry-run        # только напечатать
    python3 scripts/due_reminder.py --self-check
"""
import argparse, datetime, importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import context_pack as cp

ВИД = "telegram_due"               # kind строки outbox
МАКС = 3                           # пунктов, остальное — числом (docs/morning-brief.md)


def завтра(now=None):
    """Завтрашний день владельца, `ГГГГ-ММ-ДД`."""
    now = now or datetime.datetime.now(mi.TZ)
    return (now.astimezone(mi.TZ) + datetime.timedelta(days=1)).date().isoformat()


def _сводка():
    """`daily-summary.py` с дефисом в имени обычным import не берётся."""
    spec = importlib.util.spec_from_file_location(
        "daily_summary", os.path.join(HERE, "daily-summary.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def к_сроку(vault, день):
    """Карточки со сроком `день` в форме `поля()`. Отбор — утренний
    (`daily-summary.живые` на день срока): неподтверждённое старше трёх
    дней утро прячет, и вечер о нём не напоминает (ревью #174). Срок —
    дата в начале значения, как у утра: `2026-10-15T18:00` — тоже 15-е.
    Порядок: сначала со временем (оно и есть самое срочное), потом по
    дате и заголовку — чтобы в обрезку «…и ещё N» уходило не оно."""
    пункты = []
    for fm in _сводка().живые(vault, день):
        срок = str(fm.get("due") or "")
        if срок[:10] != день:
            continue
        it = cp.поля(fm)
        заголовок = cp.данные(it["title"], cp.MAX_TITLE)
        if заголовок:
            пункты.append(((len(срок) == 10, срок, заголовок), it))
    return [it for _, it in sorted(пункты, key=lambda p: p[0])]


def брошено(last_attempt, now=None):
    """Строка `sending` старше аренды outbox — процесс умер посреди
    отправки; моложе — её прямо сейчас шлёт другой прогон."""
    try:
        t = datetime.datetime.fromisoformat(str(last_attempt))
    except ValueError:
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=mi.TZ)
    now = now or datetime.datetime.now(mi.TZ)
    return (now - t).total_seconds() > mi.АРЕНДА_OUTBOX_С


def текст(пункты):
    """Сообщение владельцу. Пункт — как в пакете (`context_pack.строка`),
    без срока (он в шапке) и без кода карточки: отвечать будут Маре
    словами, а не кодом."""
    строки = ["• " + cp.строка(dict(it, due=None, id=None))[2:] for it in пункты[:МАКС]]
    if len(пункты) > МАКС:
        строки.append("• …и ещё %d — тоже завтра" % (len(пункты) - МАКС))
    return "\n".join(["⏰ Завтра срок:"] + строки
                     + ["Ответь Маре словами: «сделал», «перенеси на пятницу»."])


def run(root=None, vault=None, env_file=None, now=None, dry_run=False):
    """Вечерний прогон. Возвращает исход: `quiet` (сроков на завтра нет),
    `already` (за эту дату уже отправлено), `dry-run` или исход отправки
    (`sent`, `failed`, `no-transport`, `not-private`)."""
    import call_digest as cd
    vault = vault or os.environ.get("VAULT", "/srv/vault")
    день = завтра(now)
    пункты = к_сроку(vault, день)
    if not пункты:
        print("due_reminder: на %s сроков нет — молчим" % день)
        return "quiet"
    сообщение = текст(пункты)
    if dry_run:
        print(сообщение)
        return "dry-run"
    con = mi.connect(root or mi.ROOT)
    # проверка и вставка — одной транзакцией: иначе два прогона разом оба
    # видели бы «ещё не слали» и слали оба (ревью #174)
    with mi.транзакция(con):
        было = con.execute("select id, state, last_attempt from outbox where kind=? "
                           "and object_id=? and state in ('sent','sending')",
                           (ВИД, день)).fetchone()
        if было is None:
            con.execute("update outbox set state='skipped', error='пересобран' "
                        "where kind=? and object_id=? and state='pending'", (ВИД, день))
            oid = mi.в_outbox(con, ВИД, {"text": сообщение}, "due", день)
        elif было["state"] == "sending" and брошено(было["last_attempt"], now):
            # процесс убит между захватом и пометкой: сообщение могло и уйти,
            # повтор его не шлёт, а закрывает строку (как `message_nudge`)
            con.execute("update outbox set state='skipped', error=? where id=? and "
                        "state='sending'",
                        ("брошено посреди отправки — возможно, доставлено", было["id"]))
    if было is not None:
        print("due_reminder: на %s уже отправлялось (%s)" % (день, было["state"]))
        return "already"
    # транспорт и застава §8.3 — дайджеста; исключение транспорта строку
    # оставляет `pending`, и ниже она гасится, как любой неуспех
    try:
        итоги = mi.из_outbox(con, cd._отправитель(cd.env(env_file)), kind=ВИД, ид=oid)
        исход = итоги[0][1] if итоги else None
    except Exception as e:
        исход = "error: %s" % type(e).__name__
    if исход != "sent":
        # не копим: завтрашний срок, напомненный послезавтра, — не напоминание
        with mi.транзакция(con):
            con.execute("update outbox set state='skipped', error=? where id=? and "
                        "state='pending'", (исход or "не отправлено", oid))
        print("due_reminder: %s — не доставлено (%s), не копится"
              % (день, исход or "строку забрал другой прогон"), file=sys.stderr)
    print("due_reminder: %s — %s, пунктов %d" % (день, исход, len(пункты)))
    return исход


def self_check():
    import tempfile
    v = tempfile.mkdtemp()
    os.makedirs(os.path.join(v, "kb/commitments"))

    def card(name, **fm):
        with open(os.path.join(v, "kb/commitments", name), "w", encoding="utf-8") as fh:
            fh.write("---\n%s\n---\n" % "\n".join("%s: %s" % kv for kv in fm.items()))

    now = datetime.datetime(2026, 10, 14, 20, 0, tzinfo=mi.TZ)
    assert завтра(now) == "2026-10-15"
    card("a.md", title="прислать смету", status="open", due="2026-10-15", promised_to="Анна",
         id="01999999-0000-7000-8000-00000000abcd")
    card("b.md", title="закрыто", status="done", due="2026-10-15")
    card("c.md", title="послезавтра", status="open", due="2026-10-16")
    card("d.md", title="перезвонить", status="proposed", due="2026-10-15",
         promised_to="+79990000000", occurred="2026-10-14")
    card("e.md", title="давнее неподтверждённое", status="proposed", due="2026-10-15",
         occurred="2026-09-01")
    пункты = к_сроку(v, "2026-10-15")
    assert [it["title"] for it in пункты] == ["перезвонить", "прислать смету"], пункты
    t = текст(пункты)
    assert t.splitlines()[0] == "⏰ Завтра срок:", t
    assert "• «прислать смету» · «Анна»" in t and "#" not in t and "2026" not in t, t
    assert "79990000000" not in t, "номер вместо имени не едет"
    assert run(vault=v, now=datetime.datetime(2026, 10, 20, 20, 0, tzinfo=mi.TZ)) == "quiet"
    print("due_reminder self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="напоминание по сроку накануне (Т-У.2)")
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
    # отказ телеграма — ненулевой код: в журнале крона его видно
    return 1 if исход == "failed" or str(исход).startswith("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
