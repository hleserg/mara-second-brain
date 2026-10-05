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
import os, sys, re, json, glob, hashlib, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import context_pack
import ledger_import as li
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


# --- идентичность карточки (Т2.2, ADR-0002) ---------------------------------
#
# У каждого разговора и обязательства — свой uuid7, и он не зависит ни от
# имени файла, ни от заголовка: в шапке лежит полем `id:`, в реестре — ключом
# строки. Кто выдаёт id, решает зовущий: чистым построителям карточек (тесты,
# самопроверка) хватает `_новый`, проектору `run()` нужен `_из_реестра` —
# иначе повторная проекция того же звонка выдала бы второй id, и стабильность
# держалась бы на том, что проекцию не повторяют.

def _новый(вид, native):
    return mi.uuid7()


def _из_реестра(con):
    """Id по `source_id`, если объект уже есть в реестре; иначе новый."""
    def ид(вид, native):
        таблица = "commitments" if вид == "commitment" else "conversations"
        row = con.execute("select id from %s where source_native_id=?" % таблица,
                          (native,)).fetchone()
        return row["id"] if row else mi.uuid7()
    return ид


def _свободный(vault, con):
    """Путь карточки, который не затрёт чужую.

    Два звонка одному человеку в одну минуту (полевой тест R12 §16) дают
    один путь; §2.2 п.1 — именно эта перезапись. Различитель — последние
    восемь знаков id, не первые (ADR-0002: первые — старшие биты миллисекунд,
    у объектов одного прогона они одинаковы) и не позиционный `-2` (он
    зависит от порядка обхода и переезжает при пересборке). Занят ли путь,
    спрашиваем у реестра (`projections`), а до него — у файла: карточка,
    которую реестр ещё не видел, тоже чужая, если у неё другой `source_id`.
    """
    def вольный(вид, rel, oid, native):
        занят = False
        if con is not None:
            row = con.execute("select object_id from projections where path=?",
                              (rel,)).fetchone()
            if row:
                занят = row["object_id"] != oid
        if not занят and vault and os.path.exists(os.path.join(vault, rel)):
            with open(os.path.join(vault, rel), encoding="utf-8") as fh:
                fm, _ = context_pack.mb.frontmatter(fh.read())
            занят = ((fm.get("id") or "") != oid
                     and (fm.get("source_id") or "") != native)
        return rel[:-3] + "--" + oid[-8:] + ".md" if занят else rel
    return вольный


def _как_есть(вид, rel, oid, native):
    return rel


def conversation_card(event, extraction, canon, ид=_новый, вольный=_как_есть):
    """(путь относительно волта, текст карточки) для одного разговора."""
    day, hhmm, human = when(event)
    who = contact(event)
    native = "call/" + event["id"]
    oid = ид("conversation", native)
    path = вольный("conversation", "%s/%s-%s-%s.md" % (CONV_DIR, day, hhmm, slug(who)),
                   oid, native)

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
         ("id", oid),
         ("type", "conversation"),
         ("source", "phone"),
         ("source_id", native),
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


def commitment_cards(event, extraction, canon, ид=_новый, вольный=_как_есть,
                     conv=None):
    """Карточки обязательств: только то, что перешло порог и сказано прямо.

    `conv` — имя файла разговора без расширения, на который ссылается
    «Откуда»: его даёт `all_cards`, потому что у разговора путь мог получить
    различитель, и ссылка обязана вести на него, а не на соседа.
    """
    day, hhmm, _ = when(event)
    conv = conv or "%s-%s-%s" % (day, hhmm, slug(contact(event)))
    who = contact(event)
    out = []
    for key in ("requests", "commitments", "changed_instructions"):
        for n, it in enumerate(extraction.get(key) or [], 1):
            if it.get("disposition") != "task":
                continue                      # «возможно задача» живёт в дайджесте
            action = it.get("action") or it.get("new_state") or ""
            native = "commitment/%s/%s/%d" % (event["id"], key, n)
            oid = ид("commitment", native)
            path = вольный("commitment",
                           "%s/%s-%s.md" % (COMM_DIR, day, slug(action)[:40]),
                           oid, native)
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
                 ("id", oid),
                 ("type", "commitment"),
                 ("source", "phone"),
                 ("source_id", native),
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


def all_cards(event, extraction, canon, ид=_новый, вольный=_как_есть):
    """Всё, что рождает один звонок: разговор и обязательства из него."""
    conv_path, conv_text = conversation_card(event, extraction, canon, ид, вольный)
    cards = [(conv_path, conv_text)]
    person = person_card(event, canon)
    if person:
        cards.append(person)
    conv = os.path.basename(conv_path)[:-3]
    return cards + commitment_cards(event, extraction, canon, ид, вольный, conv)


def write_cards(vault, cards):
    """Атомарно и под общим флоком: рядом ходят автокоммит и bisync."""
    written = []
    with locked(vault):
        for rel, text in cards:
            _atomic(os.path.join(vault, rel), text)
            written.append(rel)
    return written


def _atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


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
    written = write_cards(vault, all_cards(ev, extraction, canon,
                                           _из_реестра(con), _свободный(vault, con)))
    # Реестр узнаёт о карточке тем же прогоном, а не ночным переносом: id
    # в шапке и ключ строки — одно и то же с первой секунды (Т2.2).
    for rel in written:
        li.перенести_карточку(con, vault, rel,
                              актор=("projector", "call_project", "проекция звонка"))
    con.execute("update events set state='projected' where id=?", (event_id,))
    # пакет для Мары пересобираем сразу: обязательство, о котором она узнает
    # только после ночного крона, — это обязательство, о котором она не узнает
    # (ТЗ §15). Писатель у _system/context один — context_pack, кто бы ни звал.
    context_pack.build_now(vault)
    print("call_project: %s — карточек %d" % (event_id, len(written)))
    return written

# --- правка обязательства словами Серёги (ТЗ §16) ---------------------------
#
# «Это тоже задача, срок пятница», «сделал», «отмени» — Мара зовёт инструмент
# mara_correction, плагин шлёт событие kind=correction, contextd применяет его
# здесь синхронно. Историю не переписываем: фронтматтер — текущая проекция,
# тело — журнал правок с датой и ссылкой на событие. YAML руками никто не
# правит, и Мара тоже.

СТАТУСЫ = ("open", "done", "cancelled")
ДАТА = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check_correction(payload):
    """Граница доверия: аргументы придумывает модель. Ошибка — строкой, чтобы
    Мара переспросила Серёгу, а не оставила 500 в логе."""
    item = str(payload.get("item") or "").strip()
    if not item or len(item) > 200:
        return "нужно название обязательства, до 200 знаков"
    if payload.get("status") not in (None, "") + СТАТУСЫ:
        return "статус — один из: " + ", ".join(СТАТУСЫ)
    if payload.get("due") and not ДАТА.match(str(payload["due"])):
        return "срок нужен как YYYY-MM-DD"
    if not (payload.get("status") or payload.get("due") or payload.get("note")):
        return "нечего править: ни статуса, ни срока, ни заметки"
    return None


def _карточки(vault):
    out = []
    for p in sorted(glob.glob(os.path.join(vault, COMM_DIR, "*.md"))):
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        fm, _ = context_pack.mb.frontmatter(text)
        out.append({"path": p, "rel": os.path.relpath(p, vault), "fm": fm, "text": text})
    return out


def _слова(s):
    return set(re.findall(r"\w+", s))


def _похожие(item, cards):
    """Карточки по названию: точное совпадение, потом вхождение, потом общие
    слова. Внутри яруса открытые важнее закрытых. Два равных кандидата — не
    «берём первый», а вопрос Серёге: правка не должна лечь в чужую карточку молча."""
    q = context_pack.mb.clean(item).lower()
    qw = _слова(q)
    ярусы = ([], [], [])
    for c in cards:
        t = context_pack.mb.clean(c["fm"].get("title") or "").lower()
        if not t:
            continue
        tw = _слова(t)
        if t == q:
            ярусы[0].append(c)
        elif q in t or t in q:
            ярусы[1].append(c)
        elif qw and len(qw & tw) / max(len(qw), len(tw)) >= 0.5:
            ярусы[2].append(c)
    for ярус in ярусы:
        if ярус:
            открытые = [c for c in ярус if c["fm"].get("status") in context_pack.OPEN]
            return открытые or ярус
    return []


def _шапка(text, **новое):
    """Строки фронтматтера правим на месте, остальные байты не трогаем: Basic
    Memory дописывает в карточки свои ключи, и пересборка из словаря их бы
    выбросила. Нет ключа — дописываем в конец шапки."""
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    head = m.group(1).split("\n")
    for k, v in новое.items():
        line = "%s: %s" % (k, v)
        for i, l in enumerate(head):
            if l.startswith(k + ":"):
                head[i] = line
                break
        else:
            head.append(line)
    return "---\n" + "\n".join(head) + "\n---\n" + text[m.end():]


def _поправить(card, status, due, note, когда, event_id):
    fm, title = card["fm"], card["fm"].get("title") or "?"
    новое, журнал = {}, []
    if status and status != fm.get("status"):
        новое["status"] = status
        журнал.append("статус %s → %s" % (fm.get("status") or "?", status))
    if due and due != fm.get("due"):
        новое["due"], новое["due_explicit"] = due, "true"
        журнал.append("срок %s → %s" % (fm.get("due") or "не был", due))
    if note:
        журнал.append(note)
    out = {"found": True, "card": card["rel"], "title": title, "changed": новое}
    if not журнал:
        out["text"] = "«%s» уже так" % title
        return out
    if новое:
        новое["valid_from"] = когда
    text = _шапка(card["text"], **новое) if новое else card["text"]
    if "\nПравки:\n" not in text:
        text = text.rstrip("\n") + "\n\nПравки:\n"
    text += "- %s, Мара, correction/%s: %s\n" % (когда[:16], event_id, "; ".join(журнал))
    _atomic(card["path"], text)
    out["text"] = "«%s»: %s" % (title, "; ".join(журнал))
    return out


def _завести(vault, item, due, note, когда, event):
    """Новая задача словами Серёги. Поля те же, что у карточки из звонка, чтобы
    context_pack и сводки видели её как любую другую."""
    day, stem = когда[:10], slug(item)[:40]
    oid = mi.uuid7()
    rel = "%s/%s-%s.md" % (COMM_DIR, day, stem)
    # занятый путь — различитель из id, как у проектора (ADR-0002), а не
    # позиционный `-2`: тот переезжал при пересборке
    if os.path.exists(os.path.join(vault, rel)):
        rel = "%s/%s-%s--%s.md" % (COMM_DIR, day, stem, oid[-8:])
    fm = frontmatter(
        [("title", yaml_str(item[:80])),
         ("id", oid),
         ("type", "commitment"),
         ("source", "mara"),
         ("source_id", "correction/%s" % event["id"]),
         ("created", когда),
         ("occurred", event.get("occurred_at") or когда),
         ("sensitive", "true"),
         ("distilled", "true"),
         ("status", "open"),
         ("owner", OWNER),
         ("due", due),
         ("due_explicit", "true" if due else "false"),
         ("origin", "correction/%s" % event["id"]),
         ("classification", "personal"),
         ("model_scope", "local-only"),
         ("cloud_allowed", "false"),
         ("confidence", "1.00"),
         ("pipeline_version", str(mi.PIPELINE_VERSION)),
         ("valid_from", когда)],
        lists=[("audience", ["mara"])])
    body = ["- Обещание: %s" % item,
            "- Откуда: сказано Маре, %s" % когда[:16]]
    if note:
        body.append("- Заметка: %s" % note)
    _atomic(os.path.join(vault, rel), fm + "\n" + "\n".join(body) + "\n")
    return {"found": False, "created": rel, "title": item,
            "text": "завёл «%s»%s" % (item, " до " + due if due else "")}


def apply_correction(vault, event, con=None):
    """Событие kind=correction → карточка. Возвращает, что сделано, с полем
    `text` для Мары. Пакет для Мары пересобирается сразу, как после звонка.

    `con` — реестр: записанная или заведённая карточка тут же переносится
    в него (`ledger_import.перенести_карточку`), чтобы строка и шапка не
    расходились до ночного крона. Без `con` (тесты правки словами) волт
    остаётся единственным, кого правка касается — как и до Т2.2."""
    p = event.get("payload") or {}
    item = scrub(str(p.get("item") or "").strip())
    status, due = p.get("status") or None, p.get("due") or None
    note = scrub(str(p.get("note") or "").strip()) or None
    когда = mi.now_iso()
    with locked(vault):
        cards = _карточки(vault)
        found = _похожие(item, cards)
        if len(found) > 1:
            names = [c["fm"].get("title") for c in found]
            out = {"found": False, "ambiguous": names,
                   "text": "подходят несколько, уточни: " + "; ".join(names)}
        elif found:
            out = _поправить(found[0], status, due, note, когда, event.get("id"))
        elif status == "open":
            out = _завести(vault, item, due, note, когда, event)
        else:
            открытые = [c["fm"].get("title") for c in cards
                        if c["fm"].get("status") in context_pack.OPEN]
            out = {"found": False, "open": открытые,
                   "text": "не нашёл «%s» среди открытых: %s"
                           % (item, "; ".join(открытые) or "список пуст")}
    rel = out.get("card") if out.get("changed") else out.get("created")
    if con is not None and rel:
        # актор — владелец: правка словами это его решение, Мара лишь записала
        li.перенести_карточку(con, vault, rel, актор=(
            "human", "owner", "correction/%s" % event.get("id")))
    # вне флока: build_now берёт его сам, а flock второго дескриптора ждал бы первого
    out["pack_sha256"] = context_pack.build_now(vault)
    return out


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
    # класс символов, а не один вход: на этом стоит потолок перечисления в
    # сверке (`contextd_reconcile`, обход `kb/`) — точка, пробел, NFD и
    # заглавные в имени карточки появиться не могут
    assert re.fullmatch(r"[a-z0-9-]+", slug("Привет, Мир! №1 ёж.md / é")), slug(
        "Привет, Мир! №1 ёж.md / é")
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
