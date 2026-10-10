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
import os, sys, re, json, glob, hashlib, argparse, contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import context_pack
import call_extract
import ledger_import as li
import vault_manifest
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


def _ммсс(ms):
    return "%02d:%02d" % (ms // 60000, (ms % 60000) // 1000)


def stamp(item):
    """Метка времени первой ссылки: «04:12–04:37» у ссылки на сегмент
    реестра, «04:12» у старой, где конца не было. По ней открывают место в
    записи (ADR-0004 п.5). Ссылок нет (все отклонены реестром) — пустая
    строка, а не «00:00»: выдуманная метка хуже отсутствующей (Codex по
    #122, круг 2)."""
    ev = item.get("evidence") or []
    if not ev:
        return ""
    a = int(ev[0].get("start_ms") or 0)
    b = ev[0].get("end_ms")
    if ev[0].get("segment_id") and isinstance(b, int) and b > a:
        return "%s–%s" % (_ммсс(a), _ммсс(b))
    return _ммсс(a)


def метка(item):
    """« · 04:12–04:37» для строки списка; пусто, если метки нет."""
    s = stamp(item)
    return " · " + s if s else ""


def _код_сегмента(item):
    """Хвост `segment_id` первой ссылки — восемь знаков (ADR-0002), чтобы
    сторож пересборки и владелец могли сослаться на строку реестра."""
    ev = (item.get("evidence") or [{}])[0]
    sid = ev.get("segment_id")
    return " · #%s" % sid[-8:] if isinstance(sid, str) and sid else ""


def _evidence_список(item):
    """Список `evidence` во фронтматтер: машиночитаемо, чтобы проекция была
    полной и пересобираемой (§4.8) — только ссылки на сегменты реестра."""
    return ["%s %d-%d" % (e["segment_id"], e["start_ms"], e["end_ms"])
            for e in item.get("evidence") or []
            if isinstance(e.get("segment_id"), str) and e.get("segment_id")]


# Списки извлечения, пункты которых проекция рисует со ссылкой на запись:
# три первых дают карточки обязательств, остальные — строки в карточке
# разговора (`conversation_card`). Сверка — для всех (Codex по #122).
СО_ССЫЛКАМИ = ("requests", "commitments", "changed_instructions",
               "decisions", "open_questions", "constraints", "followups")


def _сверить_с_реестром(con, event_id, extraction):
    """ADR-0004 п.3 и п.5 на пути проекции: до рендера каждая ссылка с
    `segment_id` сверяется с реестром — сегмент принадлежит расшифровке
    этого события, интервал (если есть) в его границах; без интервала
    ссылка равна сегменту целиком (п.1). Возвращает копию извлечения, в
    которой у пунктов остались только принятые ссылки (`evidence`), а
    отклонённые отложены в `evidence_rejected`; пункт `task` с хотя бы одной
    отклонённой уходит в `needs-review` (п.3) и карточки не получает.
    Карточка рисуется по принятому — а не по тексту модели, который реестр
    мог отвергнуть (ревью PR #122). Ссылки без `segment_id` (извлечения до
    Т2.4) сверить нечем — остаются как есть, строк реестра не дают."""
    out = json.loads(json.dumps(extraction))
    for key in СО_ССЫЛКАМИ:
        for it in out.get(key) or []:
            принятые, отклонённые = [], []
            for e in it.get("evidence") or []:
                sid = e.get("segment_id") if isinstance(e, dict) else None
                if not isinstance(sid, str) or not sid:
                    принятые.append(e)                  # legacy — не сверяем
                    continue
                seg = con.execute(
                    "select s.start_ms, s.end_ms from transcript_segments s join transcripts t "
                    "on t.id=s.transcript_id where s.id=? and t.event_id=?",
                    (sid, event_id)).fetchone()
                a, b = e.get("start_ms"), e.get("end_ms")
                if seg is not None and a is None and b is None:
                    a, b = seg["start_ms"], seg["end_ms"]
                if (seg is None or type(a) is not int or type(b) is not int
                        or not seg["start_ms"] <= a <= b <= seg["end_ms"]):
                    отклонённые.append({"segment_id": sid,
                                        "start_ms": a if type(a) is int else None,
                                        "end_ms": b if type(b) is int else None})
                    continue
                принятые.append(dict(e, start_ms=a, end_ms=b))
            it["evidence"], it["evidence_rejected"] = принятые, отклонённые
            if отклонённые and it.get("disposition") == "task":
                it["disposition"] = "needs-review"
    return out


def _evidence_в_реестр(con, oid, item, когда):
    """ADR-0004 п.1, п.5: строки `evidence_refs` обязательства — по одной на
    принятую ссылку с `segment_id` (сверка — `_сверить_с_реестром`, до
    рендера; отклонённые уже в аудите). Повтор проекции заменяет строки с `producer =
    model`, а не кладёт рядом; их `id` при этом выдаются заново — на них
    никто не ссылается, стабильный id (ADR-0002) у объекта, а не у ссылки.
    Ссылки без `segment_id` строк не дают: ссылка без референта — не
    evidence. Возвращает число записанных."""
    con.execute("delete from evidence_refs where object_kind='commitment' and object_id=? "
                "and producer='model'", (oid,))
    n = 0
    for e in item.get("evidence") or []:
        sid = e.get("segment_id")
        if not isinstance(sid, str) or not sid:
            continue
        con.execute("insert into evidence_refs(id,object_kind,object_id,kind,segment_id,"
                    "start_ms,end_ms,producer,created) values(?,?,?,?,?,?,?,?,?)",
                    (mi.uuid7(), "commitment", oid, "audio", sid, e["start_ms"], e["end_ms"],
                     "model", когда))
        n += 1
    return n


def _отозвать_evidence(con, vault, event_id, extraction, когда):
    """Повторная проекция (база восстановлена старее извлечения, расшифровка
    переделана): пункт, прежде бывший карточкой, теперь в ревью — карточка в
    этот прогон не рисуется, но его объект, проекция и строки `evidence_refs`
    от прошлого прогона остались и выдают отвергнутое за каноническое
    (Codex по #122). Отзываем то, что реестр вправе отозвать сам: строки
    `evidence_refs` с `producer = model`, с аудитом `evidence_withdrawn` на
    объект. Объект и файл карточки остаются: карточка — территория владельца
    (он мог её править), расхождение проекции с реестром — работа сторожа
    пересборки (Т2.6), а не молчаливого удаления."""
    native = {"commitment/%s/%s/%d" % (event_id, key, n)
              for key in ("requests", "commitments", "changed_instructions")
              for n, it in enumerate(extraction.get(key) or [], 1)
              if it.get("disposition") == "task"}
    for row in con.execute("select id, source_native_id from commitments where "
                           "source_native_id like ?", ("commitment/%s/%%" % event_id,)):
        if row["source_native_id"] in native:
            continue
        with mi.транзакция(con):
            n = con.execute("delete from evidence_refs where object_kind='commitment' and "
                            "object_id=? and producer='model'", (row["id"],)).rowcount
            if n:
                mi.audit(con, "evidence_withdrawn", ("rule", "call_project"), "commitment",
                         row["id"], {"event": event_id, "refs": n,
                                     "why": "пункт ушёл в ревью при повторной проекции"},
                         когда)
                print("call_project: %s — у объекта %s отозвано ссылок evidence: %d, "
                      "карточка осталась, разберёт сторож пересборки"
                      % (event_id, row["id"], n), file=sys.stderr)


def _пункты(extraction):
    """Ключ `source_id` карточки обязательства → пункт извлечения, из
    которого она сделана (та же нумерация, что в `commitment_cards`)."""
    return {"commitment/%s/%s/%d" % (extraction.get("event_id"), key, n): it
            for key in ("requests", "commitments", "changed_instructions")
            for n, it in enumerate(extraction.get(key) or [], 1)}


def when(event):
    """Дата и время разговора как (2026-09-02, 1405, 14:05)."""
    occ = event.get("occurred") or mi.now_iso()
    day, _, rest = occ.partition("T")
    hhmm = (rest[:5] or "00:00")
    return day, hhmm.replace(":", ""), hhmm


def contact(event):
    p = event.get("payload") or {}
    return p.get("contact_name") or p.get("number") or "неизвестный номер"


# Заголовок карточки и дайджеста по исходу (Т4.3): у состоявшегося и
# неизвестного — прежний «Звонок», чтобы карточки, нарисованные до этого,
# остались байт в байт теми же.
ЗАГОЛОВОК = {"missed": "Пропущенный звонок", "no-answer": "Недозвон"}


def заголовок(event, extraction=None):
    return ЗАГОЛОВОК.get(mi.исход_звонка(event.get("payload"), extraction), "Звонок")


def строка_исхода(event, extraction=None):
    """«Исход: не дозвонился (исходящий, 0 с)» — только у несостоявшихся;
    у состоявшегося, неизвестного и сомнительного — None, не печатается."""
    p = event.get("payload") or {}
    код = mi.исход_звонка(p, extraction)
    if код in (None, "answered"):
        return None
    направление = {"incoming": "входящий", "outgoing": "исходящий",
                   "missed": "пропущенный"}[p["direction"]]
    try:
        сек = max(0, int(p.get("duration_s")))
    except (TypeError, ValueError):
        сек = 0
    return "Исход: %s (%s, %d с)" % (mi.ИСХОДЫ[код], направление, сек)


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


def _создан(con):
    """`created` объекта из реестра, если он там есть: повторная проекция
    того же звонка не переписывает дату создания карточки (перенос её
    намеренно не обновляет, и пересборка из реестра берёт её же — иначе
    каждая перерисовка давала бы «разошлось» по одной строке `created:`,
    ревью PR #125). Нет объекта — None, и карточка получает «сейчас»."""
    def создан(вид, oid):
        таблица = "commitments" if вид == "commitment" else "conversations"
        row = con.execute("select created from %s where id=?" % таблица, (oid,)).fetchone()
        return row["created"] if row else None
    return создан


def _никогда(вид, oid):
    return None


def _из_реестра(con):
    """Id по `source_id`, если объект уже есть в реестре; иначе новый.

    Шапку существующего файла проектор не читает: при базе, восстановленной
    старше волта, повторная проекция выдаст новый id, а `_свободный` по
    совпавшему `source_id` перепишет файл. Это ручной сценарий после
    восстановления; порядок там — сначала `ledger_import.py` (он верит id из
    шапки), потом проекции (ревью P3-10)."""
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
    выдано = {}                       # путь → id, внутри одного прогона

    def вольный(вид, rel, oid, native):
        # одно действие и просьбой, и обещанием — у извлечения моделью обычное
        # дело: путь из даты и slug один, а в `projections` и на диске его ещё
        # нет; без памяти о выданном второй файл затёр бы первый молча
        # (ревью P2-6)
        занят = выдано.get(rel, oid) != oid
        if not занят and con is not None:
            row = con.execute("select object_id from projections where path=?",
                              (rel,)).fetchone()
            if row:
                занят = row["object_id"] != oid
        if not занят and vault and os.path.exists(os.path.join(vault, rel)):
            with open(os.path.join(vault, rel), encoding="utf-8") as fh:
                fm, _ = context_pack.mb.frontmatter(fh.read())
            занят = ((fm.get("id") or "") != oid
                     and (fm.get("source_id") or "") != native)
        if занят:
            rel = rel[:-3] + "--" + oid[-8:] + ".md"
        выдано[rel] = oid
        return rel
    return вольный


def _как_есть(вид, rel, oid, native):
    return rel


def conversation_card(event, extraction, canon, ид=_новый, вольный=_как_есть,
                      создан=_никогда):
    """(путь относительно волта, текст карточки) для одного разговора."""
    day, hhmm, human = when(event)
    who = contact(event)
    native = "call/" + event["id"]
    oid = ид("conversation", native)
    created = создан("conversation", oid) or mi.now_iso()
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
            lines.append("- %s%s%s%s" % (scrub(text), due, mark, метка(it)))
        lines.append("")
    for line in (строка_исхода(event, extraction), people_line(extraction, canon),
                 projects_line(extraction, canon)):
        if line:
            lines.append(scrub(line))
    body = "\n".join(lines).rstrip() + "\n"

    # `outcome` — только у несостоявшегося звонка: у состоявшегося поля нет,
    # и карточки, нарисованные до Т4.3, остаются байт в байт теми же
    fm = frontmatter(
        [("title", yaml_str("%s · %s · %s" % (заголовок(event, extraction), who, human))),
         ("id", oid),
         ("type", "conversation"),
         ("source", "phone"),
         ("source_id", native),
         ("created", created),
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
         # Т5.0: из какой ревизии извлечения карточка (как у обязательства)
         ("extraction_id", extraction.get("extraction_id")),
         ("valid_from", event.get("ended") or event.get("occurred")),
         ("outcome", None if mi.звонок_состоялся(event.get("payload"), extraction)
          else mi.исход_звонка(event.get("payload"), extraction))],
        lists=[("audience", ["mara"])])
    return path, fm + "\n" + body


def commitment_cards(event, extraction, canon, ид=_новый, вольный=_как_есть,
                     conv=None, создан=_никогда):
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
                    "- Откуда: [[%s]]%s%s" % (conv, метка(it), _код_сегмента(it))]
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
                 ("created", создан("commitment", oid) or mi.now_iso()),
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
                 # ADR-0004 п.4: чем и по какой версии извлечено
                 ("extractor", extraction.get("extractor")),
                 ("prompt_version", extraction.get("prompt_version")),
                 # Т5.0: из какой ревизии извлечения карточка (строка
                 # `extractions`); у извлечений до миграции 6 поля нет
                 ("extraction_id", extraction.get("extraction_id")),
                 ("cloud_allowed", "false"),
                 ("confidence", "%.2f" % float(it.get("confidence") or 0)),
                 ("supersedes", yaml_str(it["supersedes"]) if it.get("supersedes") else None),
                 ("pipeline_version", str(mi.PIPELINE_VERSION)),
                 ("valid_from", event.get("ended") or event.get("occurred"))],
                lists=[("audience", ["mara"]), ("evidence", _evidence_список(it))])
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


def all_cards(event, extraction, canon, ид=_новый, вольный=_как_есть, создан=_никогда):
    """Всё, что рождает один звонок: разговор и обязательства из него."""
    conv_path, conv_text = conversation_card(event, extraction, canon, ид, вольный, создан)
    cards = [(conv_path, conv_text)]
    person = person_card(event, canon)
    if person:
        cards.append(person)
    conv = os.path.basename(conv_path)[:-3]
    return cards + commitment_cards(event, extraction, canon, ид, вольный, conv, создан)


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
        # fsync до rename: контрольная точка проекций (§5.2, манифест) ставится
        # после карточек, и после сбоя питания они обязаны нести байты, а не
        # только имена — иначе манифест с хешами файлов, которых нет (ревью)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    vault_manifest.fsync_каталога(os.path.dirname(path))


def run(event_id, vault, root=None):
    root = root or mi.ROOT
    con = mi.connect(root)
    ev = mi.event_row(con, event_id)
    # из реестра (ревизия, миграция 6), файл — только у извлечений до неё
    extraction = call_extract.прочитать_извлечение(con, root, event_id)
    if extraction is None:
        raise RuntimeError("нет извлечения %s" % mi.extraction_path(root, event_id))
    blob = con.execute("select audio_until from blobs where sha256=?",
                       (ev["blob_sha256"],)).fetchone()
    if blob:
        ev["payload"]["audio_until"] = blob["audio_until"]
    canon = canon_map(vault)
    # evidence сверяется с реестром до рендера: карточка рисует только
    # принятые ссылки, пункт с отклонённой — в ревью, не в карточку (ADR п.3)
    extraction = _сверить_с_реестром(con, event_id, extraction)
    когда = mi.now_iso()
    # каждая отклонённая ссылка — строка аудита (ADR п.3), и у пункта, который
    # из-за неё карточки не получил, тоже: объекта нет, адрес — событие
    with mi.транзакция(con):
        for key in СО_ССЫЛКАМИ:
            for n, it in enumerate(extraction.get(key) or [], 1):
                for о in it.get("evidence_rejected") or []:
                    mi.audit(con, "evidence_rejected", ("rule", "call_project"), "event",
                             event_id, dict(о, list=key, item=n, why="сегмент не из "
                                            "расшифровки события или интервал за границами"),
                             когда)
    cards = all_cards(ev, extraction, canon, _из_реестра(con), _свободный(vault, con),
                      _создан(con))
    written = write_cards(vault, cards)
    _отозвать_evidence(con, vault, event_id, extraction, когда)
    # Реестр узнаёт о карточке тем же прогоном, а не ночным переносом: id
    # в шапке и ключ строки — одно и то же с первой секунды (Т2.2). Вместе
    # с объектом — его evidence (ADR-0004 п.5): строки `evidence_refs` одной
    # транзакцией с переносом, по пункту извлечения, из которого карточка.
    пункты = _пункты(dict(extraction, event_id=event_id))
    тексты = dict(cards)
    спорные = []
    for rel in written:
        if not li.вид_по_пути(rel):
            continue
        with mi.транзакция(con):
            oid = li.перенести_карточку(
                con, vault, rel, актор=("projector", "call_project", "проекция звонка"))
            if oid is None:
                спорные.append(rel)
                continue
            fm, _ = context_pack.mb.frontmatter(тексты.get(rel, ""))
            пункт = пункты.get(fm.get("source_id"))
            if пункт is not None:
                _evidence_в_реестр(con, oid, пункт, когда)
            # §4.8: какой версией проектора нарисована проекция
            con.execute("update projections set projector_version=? where path=?",
                        (mi.PIPELINE_VERSION, rel))
    if спорные:
        # По построению `_свободный` сюда не попасть: путь либо свободен, либо
        # свой. Попали — значит реестр и волт разошлись так, как код не
        # предвидел, и молчать в stderr подпроцесса, который воркер при коде
        # 0 выбрасывает, нельзя (ревью P3-7): работа уходит в ретрай и DLQ,
        # а там её видно.
        raise RuntimeError("карточки записаны, но в реестр не легли (спор): %s"
                           % ", ".join(спорные))
    # §4.8/§5.2: манифест с хешами — после карточек, контрольная точка — после
    # него; под флоком волта, как сами карточки — рядом правка словами
    with locked(vault):
        vault_manifest.записать(con, vault, когда)
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
    if payload.get("id") not in (None, ""):
        ид = payload["id"].strip().lstrip("#") if isinstance(payload["id"], str) else ""
        if not (ид and len(ид) <= 36 and КОД.match(ид)):
            return "id — код #xxxxxxxx из списка или полный id карточки"
    if payload.get("expected_version") not in (None, ""):
        if not _целое(payload["expected_version"]):
            return "expected_version — целое число от 1"
    return None


def _целое(v):
    """Версия из недоверенного аргумента: целое ≥ 1 или None.

    `isdecimal`, а не `isdigit`: у «²» второе истинно, а `int()` падает —
    обрыв без ответа (ревью PR #118)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 1 else None
    if isinstance(v, str) and v.isdecimal() and int(v) >= 1:
        return int(v)
    return None


# Регистр не важен: `_по_коду` сравнивает в нижнем, и принимать надо так же
КОД = re.compile(r"^[0-9a-f-]{8,36}$", re.I)


def _id_карточки(card, адреса):
    """Id карточки: из шапки, а без неё — из реестра по пути проекции.

    Карточка без `id:` в шапке (до `--write-ids`, заведённая руками) в
    реестре уже есть — `перенести_карточку` даёт ей id, но шапку не правит.
    Id из ответа обязан быть адресом и для неё (ревью PR #118, P2). Список
    в шапке (рукописный YAML) — не id."""
    ид = card["fm"].get("id")
    if isinstance(ид, str) and ид.strip():
        return ид.strip()
    return адреса.get(card["rel"])


def _адреса(con):
    """путь карточки → id объекта, по проекциям реестра."""
    if con is None:
        return {}
    return {r["path"]: r["object_id"] for r in con.execute(
        "select path, object_id from projections where object_kind='commitment'")}


def _по_коду(cards, ид, адреса):
    """Карточки по id: полный или последние восемь знаков (код из пакета).

    Код короткий — восемь шестнадцатеричных знаков хвоста uuid7 (ADR-0002:
    хвост случайный, в отличие от головы с миллисекундами). Два совпадения
    на один код среди сотен карточек маловероятны, но возможны — тогда это
    тот же «подходят несколько», что и по словам.
    """
    ид = ид.lstrip("#").lower()
    out = []
    for c in cards:
        свой = (_id_карточки(c, адреса) or "").lower()
        if _совпал(свой, ид):
            out.append(c)
    return out


def _совпал(свой, ид):
    return bool(свой) and (свой == ид or (len(ид) >= 8 and свой.endswith(ид)))


def _строка_реестра(con, card, адреса):
    oid = _id_карточки(card, адреса)
    return con.execute("select id, version, title, status, due from commitments "
                       "where id=?", (oid,)).fetchone() if oid else None


def _конфликт(con, card, ожидали, payload, когда, адреса):
    """ADR-0003 п.4: версия в реестре не та, что ждал вызывающий.

    Возвращает ответ формы §4.5 или None, если конфликта нет. Проверять есть
    по чему только у карточки с id и строкой в реестре: без строки честно
    говорим `version_checked: False` и правим как раньше (п.3 ADR, legacy).
    Очередь ревью — таблица `alerts` (kind `version_conflict`, state `open`):
    своей таблицы конфликтов в схеме нет, а `alerts` для того и заведена,
    чтобы Control Plane показал открытое и дал владельцу разобрать.
    """
    row = _строка_реестра(con, card, адреса)
    if row is None or row["version"] == ожидали:
        return None
    oid = row["id"]
    текущее = {"title": row["title"], "status": row["status"], "due": row["due"]}
    # открытая тревога на ту же версию объекта уже есть — повтор правки в
    # другую минуту не плодит очередь (ревью PR #118)
    было = con.execute(
        "select id from alerts where kind='version_conflict' and state='open' and "
        "object_kind='commitment' and object_id=? and "
        "json_extract(detail_json, '$.current_version')=?",
        (oid, row["version"])).fetchone()
    cid = было["id"] if было else mi.uuid7()
    if not было:
        con.execute("insert into alerts(id,kind,severity,state,object_kind,object_id,"
                    "opened,detail_json) values(?,?,?,?,?,?,?,?)",
                    (cid, "version_conflict", "warn", "open", "commitment", oid, когда,
                     json.dumps({"expected_version": ожидали,
                                 "current_version": row["version"],
                                 "current": текущее, "attempted_patch": payload},
                                ensure_ascii=False)))
    return {"found": True, "error": "version_conflict", "entity_id": oid,
            "card": card["rel"], "title": row["title"],
            "expected_version": ожидали, "current_version": row["version"],
            "current": текущее, "attempted_patch": payload, "conflict_id": cid,
            "text": "«%s» уже изменилась: версия %d, а не %d (статус %s) — не правил, "
                    "конфликт %s в очереди ревью"
                    % (row["title"], row["version"], ожидали, row["status"], cid[-8:])}


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


def _норма(s):
    """Заголовок — как его видит Мара в пакете (`context_pack.данные`, без
    обрезки): искать по словам надо то же самое, иначе у карточки без `id`
    правка словами промахивалась бы на `_`, `#`, `<>` (Codex по #129)."""
    return context_pack.данные(s, 10 ** 6).lower()


def _похожие(item, cards):
    """Карточки по названию: точное совпадение, потом вхождение, потом общие
    слова. Внутри яруса открытые важнее закрытых. Два равных кандидата — не
    «берём первый», а вопрос Серёге: правка не должна лечь в чужую карточку молча."""
    # Мара цитирует заголовок таким, каким его показал пакет: обрезанным по
    # `MAX_TITLE` с «…» на конце. Хвостовое многоточие — не слово, а обрезанный
    # показ сравнивается с обрезанным же заголовком карточки (Codex по #129,
    # круг 2): иначе длинный заголовок без `id` правке словами недоступен.
    # Запрос — как есть (NFKC внутри `данные` делает из «…» «...», и
    # заголовок с буквальным многоточием сравнивается с таким же), а без
    # хвостового многоточия — только с реально обрезанным показом: иначе
    # «Позвонить…» точно совпадал бы с «Позвонить» (Codex, круги 3–4).
    # Многоточие снимается до нормализации, после неё его уже не узнать.
    q = _норма(item)
    q_без = _норма(re.sub(r"(…|\.\.\.)\s*$", "", str(item or "")))
    if not q:
        # Пустой запрос — подстрока любого заголовка: единственная открытая
        # карточка закрылась бы по «#» (Codex, круг 3). Не нашли — и всё.
        return []
    qw = _слова(q)
    ярусы = ([], [], [])
    for c in cards:
        сырой = c["fm"].get("title") or ""
        t = _норма(сырой)
        if not t:
            continue
        показ = context_pack.данные(сырой, context_pack.MAX_TITLE).lower()
        обрезан = показ.endswith("…")
        tw = _слова(t)
        if t == q or (обрезан and q_без and q_без == показ[:-1].strip()):
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
    out["applied"] = True        # записано — и журнал, даже если шапка та же
    return out


def карточка_правки(item, due, note, когда, event, oid):
    """Текст карточки, заведённой словами владельца: чистый рендер, без
    диска — им же пересборка (`vault_rebuild`) рисует такую карточку из
    события правки в реестре."""
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
    return fm + "\n" + "\n".join(body) + "\n"


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
    _atomic(os.path.join(vault, rel), карточка_правки(item, due, note, когда, event, oid))
    return {"found": False, "created": rel, "title": item,
            "text": "завёл «%s»%s" % (item, " до " + due if due else "")}


def _вернуть_карточку(vault, out, found):
    """Снять с диска то, что записала правка: заведённую карточку убрать,
    поправленной вернуть прежний текст (он в `found[0]["text"]`)."""
    if out.get("created"):
        try:
            os.remove(os.path.join(vault, out["created"]))
        except FileNotFoundError:
            pass
    elif out.get("applied") and found:
        _atomic(found[0]["path"], found[0]["text"])


def _в_реестр(con, vault, event, p, out, found, адреса, проверено, слияние, когда):
    """Хвост правки в реестре: перенос записанной карточки и событие аудита —
    одной транзакцией (§5.2, Т2.5: «след правки сохраняется»). Аудит пишется
    на любой исход, включая отказы: конфликт, «подходят несколько», «не
    нашёл» — ревизии их не видят, а след нужен и им.

    `applied`, а не `changed`: правка «только заметка» меняет журнал, а не
    шапку, и по `changed` реестр её не видел (Codex по #117, P1).
    Перенос — под тем же флоком, что и запись: иначе вторая правка успевала
    бы изменить файл до того, как первая прочитает его в реестр, и две
    правки ложились бы одной ревизией с чужим актором (Codex по #117, P2).
    """
    eid = event.get("id")
    rel = out.get("card") if out.get("applied") else out.get("created")
    причина = "correction/%s" % eid
    # ADR-0003 п.3: правка, версию которой проверить было нечем или не
    # просили (нет `expected_version` или строки в реестре), записывается с
    # `legacy_title_match` в причине ревизии — долг виден в данных, а не
    # только в ответе (`version_checked`)
    if out.get("applied") and not проверено:
        причина += "; legacy_title_match"
    with mi.транзакция(con):
        if rel:
            # актор — владелец: правка словами это его решение, Мара лишь записала
            oid = li.перенести_карточку(con, vault, rel, актор=("human", "owner", причина))
            if not oid:
                # перенос отверг карточку (спор по ключу или занятому id, см.
                # `ledger_import._спор`) — это отказ команды, а не её успех:
                # без исключения транзакция записала бы аудит `applied`, а
                # файл остался бы с правкой без строки (Codex по #120, круг 3).
                # `call_project.run` тот же None считает ошибкой.
                raise RuntimeError("правка %s: карточка %s спорная, реестр её не принял"
                                   % (eid, rel))
            # §4.8: карточку правки рисует тот же проектор — та же версия
            con.execute("update projections set projector_version=? where path=?",
                        (mi.PIPELINE_VERSION, rel))
            row = con.execute("select version from commitments where id=?",
                              (oid,)).fetchone()
            # id и версия в ответе — чтобы следующая правка пришла с ними
            # (ADR-0003 п.3: сперва id в ответ, потом expected_version
            # обязателен)
            out["id"], out["version"] = oid, row["version"] if row else None
        elif "id" not in out and out.get("found") and len(found) == 1:
            # «уже так»: ничего не писали, но адрес и версия у карточки есть
            row = _строка_реестра(con, found[0], адреса)
            if row:
                out["id"], out["version"] = row["id"], row["version"]
        исход = ("conflict" if out.get("error") == "version_conflict"
                 else "ambiguous" if out.get("ambiguous")
                 else "created" if out.get("created")
                 else "applied" if out.get("applied")
                 else "noop" if out.get("found")
                 else "not_found")
        mi.audit(con, "correction", ("human", "owner"), "commitment",
                 out.get("id") or out.get("entity_id"),
                 {"event": eid, "outcome": исход,
                  "fields": [k for k in ("status", "due", "note") if p.get(k)],
                  "expected_version": _целое(p.get("expected_version")),
                  "version_checked": проверено,
                  "merged": "commutative" if слияние else None,
                  "conflict_id": out.get("conflict_id"),
                  "ambiguous_ids": out.get("ambiguous_ids")}, когда)


def заметка(raw):
    """Заметка правки — одной строкой и с одиночным «; »: журнал «Правки:»
    режется по «;» и читается построчно (`ledger_import.журнал`), и заметка
    с переносом или двойной точкой с запятой не пережила бы круг волт →
    реестр → пересборка (ревью PR #125). Одна на запись и на пересборку
    (`vault_rebuild._из_правки`) — иначе заведённая карточка расходилась бы
    сама с собой (Codex по #125)."""
    note = re.sub(r"\s*;+\s*", "; ", " ".join(str(raw or "").split())).strip("; ")
    return scrub(note) or None


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
    note = заметка(p.get("note"))
    ид = p["id"].strip() if isinstance(p.get("id"), str) else ""
    ожидали = _целое(p.get("expected_version"))
    когда = mi.now_iso()
    # Реестр — одной транзакцией на всю команду: тревога конфликта, перенос
    # карточки и строка аудита ложатся вместе или никак (§5.2, ревью PR
    # #120, P3-1). Замок записи берётся уже под флоком волта, а не до него,
    # и держится миллисекунды — файл карточки пишется внутри, но он мал.
    with locked(vault):
        # что записано на диск — видно снаружи и до возврата из `_правка`:
        # исключение из переноса или из `commit` должно знать, что откатывать
        записано = {}
        try:
            with (mi.транзакция(con) if con is not None else contextlib.nullcontext()):
                out = _правка(con, vault, event, p, item, status, due, note,
                              ид, ожидали, когда, записано)
        except BaseException:
            # Реестр откатился — откатываем и файл: иначе карточка уже с
            # правкой, а строки нет, и повтор видит «уже так», так что реестр
            # не догонит никогда (Codex по #120, P1). Ловим снаружи
            # транзакции: упавший `commit` — тоже откат (Codex, круг 2).
            # Файл — не транзакция, поэтому прежний текст возвращаем руками.
            if записано:
                _вернуть_карточку(vault, записано["out"], записано["found"])
            raise
        # §5.2: карточка и строка легли — манифест и контрольная точка следом,
        # ещё под флоком: правка словами меняет проекцию, как и проектор звонка.
        # Диск отказал — правка уже принята и ответ с id нужен Маре; манифест
        # догонит следующая проекция, а до неё сверка это назовёт (ревью)
        if con is not None and (out.get("applied") or out.get("created")):
            try:
                vault_manifest.записать(con, vault, когда)
            except OSError as e:
                out["manifest_error"] = e.__class__.__name__
    # вне флока: build_now берёт его сам, а flock второго дескриптора ждал бы первого
    out["pack_sha256"] = context_pack.build_now(vault)
    return out


def _правка(con, vault, event, p, item, status, due, note, ид, ожидали, когда,
            записано):
    """Тело команды под флоком и транзакцией: найти, решить, записать файл,
    перенести в реестр. Возвращает ответ; в `записано` кладёт ответ и
    найденные карточки, как только файл на диске тронут."""
    cards = _карточки(vault)
    адреса = _адреса(con)
    # код из пакета — точный адрес, поиск по словам ему не нужен; нет
    # такого кода — не угадываем по словам, а говорим
    found = _по_коду(cards, ид, адреса) if ид else _похожие(item, cards)
    конфликт, проверено, слияние = None, False, False
    if len(found) == 1 and ожидали is not None and con is not None:
        fm = found[0]["fm"]
        # ADR-0003 п.5: правка, после которой ничего не меняется, — не
        # конфликт, а «уже так»; версию проверяем только у настоящего
        # изменения. Заметка — коммутативна: только `status` и `due`
        # двигают версию, а с ними она не пересекается, так что правка
        # «одна заметка» на несовпавшую версию принимается, а не идёт
        # в ревью.
        меняет_шапку = ((status and status != fm.get("status"))
                        or (due and due != fm.get("due")))
        row = _строка_реестра(con, found[0], адреса)
        проверено = row is not None
        if меняет_шапку or (note and row is not None and ожидали > row["version"]):
            # версия больше текущей — такой нет; это не «старая
            # заметка», а чужое представление о карточке (ревью PR
            # #120, P3-4)
            конфликт = _конфликт(con, found[0], ожидали, dict(p), когда, адреса)
        elif note and row is not None:
            слияние = row["version"] != ожидали
    if конфликт:
        out = конфликт
    elif len(found) > 1:
        names = [c["fm"].get("title") for c in found]
        out = {"found": False, "ambiguous": names,
               "ambiguous_ids": [_id_карточки(c, адреса) for c in found],
               "text": "подходят несколько, уточни: " + "; ".join(names)}
    elif found:
        out = _поправить(found[0], status, due, note, когда, event.get("id"))
        # ADR-0003 п.3: без строки в реестре версию проверить нечем
        out["version_checked"] = проверено
        if слияние:
            out["merged"] = "commutative"
    elif ид:
        out = {"found": False, "text": "не нашёл карточку с кодом #%s" % ид.lstrip("#")}
    elif status == "open":
        out = _завести(vault, item, due, note, когда, event)
    else:
        открытые = [c["fm"].get("title") for c in cards
                    if c["fm"].get("status") in context_pack.OPEN]
        out = {"found": False, "open": открытые,
               "text": "не нашёл «%s» среди открытых: %s"
                       % (item, "; ".join(открытые) or "список пуст")}
    записано.update(out=out, found=found)
    if con is not None:
        _в_реестр(con, vault, event, p, out, found, адреса, проверено, слияние, когда)
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
