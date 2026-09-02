#!/usr/bin/env python3
"""Извлечённое → карточки волта (ТЗ §10).

Две сущности становятся первоклассными: `kb/conversations/` — сам разговор,
`kb/commitments/` — кто кому что должен. Волт остаётся источником правды,
SQLite только очередь.

Фронтматтер строго плоский. Разбор в этом репозитории регэкспный
(`vault_common`, `frontmatter-migrate`), вложенная карта распарсилась бы в
мусор молча, и обнаружилось бы это через месяц на битой сводке. Всё
вложенное из ТЗ §7 живёт в JSON-манифесте блоба, а сюда попадают плоские
ключи вроде `retention_audio_until`.

    python3 scripts/call_project.py --event call_<uuid> --vault /srv/vault
    python3 scripts/call_project.py --self-check
"""
import os, sys, re, json, hashlib, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
from vault_common import canon_map, linkify, locked, scrub, yaml_str

OWNER = os.environ.get("MARA_OWNER", "sergey")
CONV_DIR = "kb/conversations"
COMM_DIR = "kb/commitments"

# Разделы карточки и дайджеста названы одинаково: человек читает то же самое
# в телеграме и в обсидиане, и не гадает, куда что переехало.
SECTIONS = [("requests", "Попросили"), ("commitments", "Ты обещал"),
            ("decisions", "Решили"), ("changed_instructions", "Изменилось"),
            ("open_questions", "Неясно")]

TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
            "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
            "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
            "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
            "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}


def slug(text, default="unknown"):
    """Латиница для имени файла: у остальных карточек репозитория она такая же."""
    out = "".join(TRANSLIT.get(ch, ch) for ch in (text or "").lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return out or default


def stamp(item):
    """Метка времени первого спана: «04:12». По ней открывают место в записи."""
    ev = (item.get("evidence") or [{}])[0]
    ms = int(ev.get("start_ms") or 0)
    return "%02d:%02d" % (ms // 60000, (ms % 60000) // 1000)


def when(event):
    """Дата и время разговора как (2026-09-02, 1405, 14:05)."""
    occ = event.get("occurred") or mi.now_iso()
    day, _, rest = occ.partition("T")
    hhmm = (rest[:5] or "00:00")
    return day, hhmm.replace(":", ""), hhmm


def contact(event):
    p = event.get("payload") or {}
    return p.get("contact_name") or p.get("number") or "неизвестный номер"


def is_owner(name, canon):
    n = (name or "").strip().lower()
    return n == OWNER or (canon or {}).get(n) == OWNER


def people_line(extraction, canon):
    """Строка «Люди:» — единственное, что читает entity-link.py."""
    names = [n for n in (extraction.get("people_mentioned") or [])
             if n and not is_owner(n, canon)]
    return "Люди: " + ", ".join(linkify(names, canon)) if names else None


def projects_line(extraction, canon):
    names = [p for p in (extraction.get("projects_mentioned") or []) if p]
    return "Проекты: " + ", ".join(linkify(names, canon)) if names else None


def frontmatter(pairs, lists=()):
    """Плоский фронтматтер в порядке §4 плюс новые ключи хвостом."""
    out = ["---"]
    out += ["%s: %s" % (k, v) for k, v in pairs if v is not None]
    for key, values in lists:
        if values:
            out.append("%s:" % key)
            out += ["  - %s" % v for v in values]
    out.append("---")
    return "\n".join(out)


def body_of(card_text):
    return card_text.split("---", 2)[2].lstrip("\n")


def conversation_card(event, extraction, canon):
    """(путь относительно волта, текст карточки) для одного разговора."""
    day, hhmm, human = when(event)
    who = contact(event)
    path = "%s/%s-%s-%s.md" % (CONV_DIR, day, hhmm, slug(who))

    lines = []
    for key, title in SECTIONS:
        items = extraction.get(key) or []
        if not items:
            continue
        lines.append("## %s" % title)
        for it in items:
            text = it.get("new_state") or it.get("action") or ""
            due = " (до %s)" % it["due_at"] if it.get("due_at") else ""
            mark = "" if it.get("disposition") == "task" else " · на проверку"
            lines.append("- %s%s%s · %s" % (scrub(text), due, mark, stamp(it)))
        lines.append("")
    for line in (people_line(extraction, canon), projects_line(extraction, canon)):
        if line:
            lines.append(scrub(line))
    body = "\n".join(lines).rstrip() + "\n"

    fm = frontmatter(
        [("title", yaml_str("Звонок · %s · %s" % (who, human))),
         ("type", "conversation"),
         ("source", "phone"),
         ("source_id", "call/" + event["id"]),
         ("created", mi.now_iso()),
         ("occurred", event.get("occurred")),
         ("sensitive", "true"),
         ("distilled", "true"),
         ("domain", "personal"),
         ("classification", event.get("classification") or "personal"),
         ("storage_scope", "vault-sync"),
         ("model_scope", "local-only"),
         ("cloud_allowed", "false"),
         ("retention_audio_until", (event.get("payload") or {}).get("audio_until")),
         ("content_sha256", hashlib.sha256(body.encode("utf-8")).hexdigest()),
         ("source_revision", "1"),
         ("pipeline_version", str(mi.PIPELINE_VERSION)),
         ("valid_from", event.get("ended") or event.get("occurred"))],
        lists=[("audience", ["mara"])])
    return path, fm + "\n" + body


def commitment_cards(event, extraction, canon):
    """Карточки обязательств: только то, что перешло порог и сказано прямо."""
    day, hhmm, _ = when(event)
    conv = "%s-%s-%s" % (day, hhmm, slug(contact(event)))
    who = contact(event)
    out = []
    for key in ("requests", "commitments", "changed_instructions"):
        for n, it in enumerate(extraction.get(key) or [], 1):
            if it.get("disposition") != "task":
                continue                      # «возможно задача» живёт в дайджесте
            action = it.get("action") or it.get("new_state") or ""
            path = "%s/%s-%s.md" % (COMM_DIR, day, slug(action)[:40])
            owner = OWNER if key != "requests" else (it.get("owner") or OWNER)
            body = ["- Обещание: %s" % scrub(action),
                    "- Откуда: [[%s]] · %s" % (conv, stamp(it))]
            if it.get("deadline_phrase"):
                body.append("- Прозвучало о сроке: «%s»" % scrub(it["deadline_phrase"]))
            if it.get("supersedes"):
                body.append("- Отменяет: %s" % scrub(it["supersedes"]))
            body.append("")
            body.append("Люди: " + ", ".join(
                linkify([x for x in [it.get("promised_to") or it.get("requester") or who]
                         if x and not is_owner(x, canon)], canon)))
            text = "\n".join(body).rstrip() + "\n"
            fm = frontmatter(
                [("title", yaml_str(action[:80])),
                 ("type", "commitment"),
                 ("source", "phone"),
                 ("source_id", "commitment/%s/%s/%d" % (event["id"], key, n)),
                 ("created", mi.now_iso()),
                 ("occurred", event.get("occurred")),
                 ("sensitive", "true"),
                 ("distilled", "true"),
                 ("status", "proposed"),
                 ("owner", owner),
                 ("promised_to", it.get("promised_to") or it.get("requester") or who),
                 ("due", it.get("due_at")),
                 ("due_explicit", "true" if it.get("deadline_explicit") else "false"),
                 ("origin", "call/" + event["id"]),
                 ("classification", event.get("classification") or "personal"),
                 ("model_scope", "local-only"),
                 ("cloud_allowed", "false"),
                 ("confidence", "%.2f" % float(it.get("confidence") or 0)),
                 ("supersedes", yaml_str(it["supersedes"]) if it.get("supersedes") else None),
                 ("pipeline_version", str(mi.PIPELINE_VERSION)),
                 ("valid_from", event.get("ended") or event.get("occurred"))],
                lists=[("audience", ["mara"])])
            out.append((path, fm + "\n" + text))
    return out


def person_card(event, canon):
    """Карточка человека из разрешённого контакта журнала звонков.

    Единственное исключение из правила «людей автоматика не заводит»
    (см. шапку entity-link.py). Правило существует затем, что имя, выдернутое
    из текста, легко оказывается опечаткой, должностью или чужим Петей — и
    граф зарастает фантомами. Здесь имя даёт адресная книга телефона, а не
    расшифровка, поэтому фантома не будет. Имени из текста разговора это
    послабление по-прежнему не касается.
    """
    p = event.get("payload") or {}
    name = p.get("contact_name")
    if not name or p.get("contact_source") != "call-log":
        return None
    key = slug(name)
    if (canon or {}).get(name.lower()) or (canon or {}).get(key):
        return None                       # уже есть в реестре
    fm = frontmatter(
        [("title", yaml_str(name)),
         ("type", "person"),
         ("source", "phone"),
         ("source_id", "person-" + key),
         ("created", mi.now_iso()),
         ("occurred", (event.get("occurred") or "")[:10] or None),
         ("sensitive", "false"),
         ("distilled", "false")],
        lists=[("aliases", [name] + ([p["number"]] if p.get("number") else []))])
    body = "Заведён автоматически из контакта в журнале звонков.\n"
    return "entities/people/%s.md" % key, fm + "\n" + body


def all_cards(event, extraction, canon):
    """Всё, что рождает один звонок: разговор и обязательства из него."""
    cards = [conversation_card(event, extraction, canon)]
    person = person_card(event, canon)
    if person:
        cards.append(person)
    return cards + commitment_cards(event, extraction, canon)


def write_cards(vault, cards):
    """Атомарно и под общим флоком: рядом ходят автокоммит и bisync."""
    written = []
    with locked(vault):
        for rel, text in cards:
            path = os.path.join(vault, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
            written.append(rel)
    return written


def run(event_id, vault, root=None):
    root = root or mi.ROOT
    con = mi.connect(root)
    ev = mi.event_row(con, event_id)
    epath = mi.extraction_path(root, event_id)
    if not os.path.exists(epath):
        raise RuntimeError("нет извлечения %s" % epath)
    extraction = json.load(open(epath, encoding="utf-8"))
    blob = con.execute("select audio_until from blobs where sha256=?",
                       (ev["blob_sha256"],)).fetchone()
    if blob:
        ev["payload"]["audio_until"] = blob["audio_until"]
    canon = canon_map(vault)
    written = write_cards(vault, all_cards(ev, extraction, canon))
    con.execute("update events set state='projected' where id=?", (event_id,))
    # пакет для Мары пересобираем сразу: обязательство, о котором она узнает
    # только после ночного крона, — это обязательство, о котором она не узнает
    # (ТЗ §15). Писатель у _system/context один — context_pack, кто бы ни звал.
    import context_pack
    context_pack.build_now(vault)
    print("call_project: %s — карточек %d" % (event_id, len(written)))
    return written


def self_check():
    import tempfile
    event = {"id": "call_x", "occurred": "2026-09-02T14:05:00+03:00",
             "ended": "2026-09-02T14:23:11+03:00", "classification": "personal",
             "payload": {"contact_name": "Анна"}}
    extr = {"requests": [{"action": "прислать смету", "disposition": "task",
                          "confidence": 0.9, "due_at": "2026-09-04",
                          "deadline_explicit": True, "explicit": True,
                          "evidence": [{"start_ms": 252000, "end_ms": 260000}]}],
            "commitments": [], "decisions": [], "open_questions": [],
            "changed_instructions": [], "constraints": [], "followups": [],
            "people_mentioned": ["Анна", "Серёж"], "projects_mentioned": []}
    path, text = conversation_card(event, extr, {"серёж": "sergey"})
    assert path == "kb/conversations/2026-09-02-1405-anna.md", path
    assert "sensitive: true" in text and "cloud_allowed: false" in text
    assert "04:12" in text, "метка времени спана потерялась"
    people = [l for l in text.splitlines() if l.startswith("Люди: ")][0]
    assert "sergey" not in people, "себя в собеседники не записываем"
    for line in text.split("---", 2)[1].strip().splitlines():
        assert not (line.startswith("  ") and not line.strip().startswith("- ")), \
            "вложенная карта в фронтматтере: %r" % line
    cards = commitment_cards(event, extr, {})
    assert len(cards) == 1 and "due: 2026-09-04" in cards[0][1]
    vault = tempfile.mkdtemp()
    os.makedirs(os.path.join(vault, ".git"))
    assert len(write_cards(vault, all_cards(event, extr, {}))) == 2
    print("call_project self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="карточки разговора и обязательств")
    ap.add_argument("--event")
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if not a.event:
        ap.error("нужен --event")
    mi.ROOT = a.root
    run(a.event, a.vault, a.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
