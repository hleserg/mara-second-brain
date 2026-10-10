#!/usr/bin/env python3
"""Разовый перенос карточек волта в ledger (ТЗ §4.1, ADR-0001).

Первый шаг миграции власти: сегодня правда лежит в Markdown, и пересборка
проекции стёрла бы её. Перенос делает базу знающей ровно то, что знает волт, —
после этого переключение писателей становится обратимым, а до этого нет.

Шаг аддитивный и намеренно скучный: файлы не открываются на запись вообще,
проектор на ledger не переключается, ревизий не заводится. Кроме строк
объектов кладётся отпечаток файла — по нему будущая пересборка отличит свой
файл от поправленного руками.

История правок переносится тоже (Т2.0, шаг 3а плана миграций): каждая строка
журнала «Правки:» ложится в `corrections` под детерминированным id, а статус,
которому в журнале нет объяснения, получает отметку об этом — иначе после
смены авторитета база утверждала бы, что 192 отмены 21.09 и 28.09 взялись
из ниоткуда. Ревизий из журнала не восстанавливаем: в нём нет версий, и
придумывать их задним числом — выдумывать данные (ADR-0003, «Откат и
миграция»); `version` у перенесённых строк остаётся 1, ревизии начнутся с
первой доменной команды Т2.3.

Ключ — `source_id` карточки, а не путь: карточку могут переименовать в
Obsidian, и это не повод завести второе обязательство. Id объекта при повторном
запуске не меняется никогда (ТЗ §4.3).

    python3 scripts/ledger_import.py --dry-run     # посчитать, ничего не писать
    python3 scripts/ledger_import.py               # перенести
    python3 scripts/ledger_import.py --write-ids   # Т2.2: id из реестра в шапки карточек
    python3 scripts/ledger_import.py --self-check

Идентичность (Т2.2, ADR-0002). Id объекта рождается один раз и живёт в
реестре; карточка несёт его копию полем `id:` в шапке. Откуда он берётся,
по старшинству: строка реестра по `source_id` → поле `id:` карточки (волт,
восстановленный без базы) → новый uuid7. Перенос и проектор зовут одну и ту
же `перенести_карточку`, так что у карточки, которую `call_project` только
что записал, строка в реестре появляется тем же вызовом, а не ночным кроном.
`--write-ids` — разовый шаг на doctor с паузой писателей
(`docs/migration-plan.md` §4 шаг 4): вписывает `id:` из реестра в карточки,
у которых его нет; откат — git волта.

Evidence (ADR-0004 п.5, обратный путь). Проектор пишет строки `evidence_refs`
из извлечения, а в шапку карточки — их копию списком `evidence`. Реестр,
восстановленный из копии старее волта, этих строк не имеет; полный перенос
восстанавливает их из шапки — только те, чей сегмент есть в расшифровке
события-источника и интервал в его границах (`_evidence_из_шапки`), и
только у обязательств без единой строки от модели: реестр со строками —
авторитет, шапка его не переписывает.
"""
import os, re, sys, glob, json, uuid, hashlib, argparse, importlib.util, sqlite3, tempfile
from collections import Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
from vault_common import locked

VAULT = os.environ.get("MARA_VAULT", os.environ.get("VAULT", "/srv/vault"))


def _brief():
    """mara-brief.py с дефисом в имени обычным import не берётся."""
    spec = importlib.util.spec_from_file_location(
        "mara_brief", os.path.join(HERE, "mara-brief.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mb = _brief()

# (каталог, тип, таблица, колонки фронтматтера)
ВИДЫ = (
    ("kb/commitments", "commitment", "commitments",
     ("title", "status", "owner", "promised_to", "due", "due_explicit",
      "created", "occurred", "valid_from", "confidence", "supersedes",
      "classification", "extractor", "prompt_version", "extraction_id")),
    ("kb/conversations", "conversation", "conversations",
     ("title", "occurred", "valid_from", "created", "classification",
      "extraction_id")),
)


# Префиксы `source_id`/`origin`, за которыми стоит строка в `events`. Их
# ровно два, и оба ставит `call_project.py`: `call/` (разговору и
# обязательству из звонка) и `correction/` (обязательству из поправки —
# сразу в оба поля). Остальные значения этих полей — `commitment/…`,
# `person-…`, `vault:…` — события за собой не несут.
#
# Номеров строк тут нет намеренно: они переезжают от любой правки соседа, а
# утверждение остаётся. Искать — `grep -nE '"(source_id|origin)"'` по
# `call_project.py`.
СОБЫТИЙНЫЕ = ("call", "correction")


def событие(s):
    """`call/call_1` → `call_1`. Не событийный префикс — события нет."""
    голова, _, хвост = (s or "").strip().partition("/")
    return хвост or None if голова in СОБЫТИЙНЫЕ else None


def _число(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _строка(v):
    """Значение фронтматтера строкой. Список — через запятую.

    Разбор фронтматтера отдаёт что положили: `supersedes: [a, b]` приезжает
    списком, и дальше он ронял либо `.strip()`, либо саму вставку («type
    'list' is not supported»). Падение посреди прогона — это не «плохая
    карточка не перенеслась», это «всё, что после неё по алфавиту, не
    перенеслось тоже».
    """
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or None
    return str(v)


def _карточка(vault, p):
    """(rel, fm, sha256 файла, текст) одной карточки; None — не читается."""
    try:
        with open(p, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        # битый симлинк или каталог с именем `*.md`: перенос разовый и
        # руками, останавливать его из-за одного мусорного имени незачем
        print("ledger_import: %s не читается (%s) — пропущен"
              % (os.path.relpath(p, vault), e), file=sys.stderr)
        return None
    # BOM от винды и CRLF из синка: без них `frontmatter` не матчит шапку
    # вовсе и возвращает пустоту, а карточка молча заводится объектом со
    # всеми полями NULL и ключом по пути
    текст = raw.decode("utf-8", "replace").lstrip("\ufeff").replace("\r\n", "\n")
    fm, _ = mb.frontmatter(текст)
    return (os.path.relpath(p, vault), fm, hashlib.sha256(raw).hexdigest(), текст)


def карточки(vault, подкаталог):
    for p in sorted(glob.glob(os.path.join(vault, подкаталог, "*.md"))):
        к = _карточка(vault, p)
        if к:
            yield к


def ключ(fm, rel):
    """`source_native_id` карточки: объявленный `source_id`, иначе путь."""
    return (_строка(fm.get("source_id")) or "").strip() or "vault:" + rel


def вид_по_пути(rel):
    for подкаталог, вид, таблица, поля in ВИДЫ:
        if rel.startswith(подкаталог + "/"):
            return вид, таблица, поля
    return None


def run(con, vault=None, dry_run=False):
    """Перенести всё, что есть. Возвращает счётчики."""
    vault = vault or VAULT
    итог = {"обязательств": 0, "разговоров": 0, "обновлено": 0, "спорных": 0,
            "правок": 0, "evidence": 0}
    for подкаталог, вид, таблица, поля in ВИДЫ:
        счётчик = "обязательств" if вид == "commitment" else "разговоров"
        # карта своя на каждый вид: `source_native_id` уникален внутри таблицы,
        # а не поперёк. Одна общая карта означала бы, что обязательство с
        # `source_id: call/…`, поставленным руками, съедает разговор
        видели = {}
        for rel, fm, sha, текст in карточки(vault, подкаталог):
            if not fm:
                # шапки нет вовсе: завести объект со всеми полями NULL и
                # ключом по пути хуже, чем не заводить — такая строка потом
                # даёт дубль при первом же переименовании файла
                print("ledger_import: %s без фронтматтера — пропущена" % rel,
                      file=sys.stderr)
                итог["спорных"] += 1
                continue
            native = ключ(fm, rel)
            # копия карточки в Obsidian наследует source_id. Молча заменить
            # первую строку второй — это ровно то схлопывание двух объектов
            # в один без доказательства, что это одно событие, — а его
            # прямо требует ТЗ §4.3. Считаем и говорим вслух.
            if native in видели:
                итог["спорных"] += 1
                print("ledger_import: %s и %s несут один source_id %s — "
                      "перенесён первый" % (видели[native], rel, native),
                      file=sys.stderr)
                continue
            исход = _перенести(con, rel, fm, sha, текст, вид, таблица, поля,
                               dry_run, evidence_из_шапки=True)
            if исход is None:
                итог["спорных"] += 1
                continue
            # Ниже заставы, а не выше: карточка, ушедшая в спор, не перенесена
            # ни во что, и сообщение «перенесён первый» о ней было бы ложью.
            видели[native] = rel
            новый, правок, ссылок = исход
            итог[счётчик if новый else "обновлено"] += 1
            итог["правок"] += правок
            итог["evidence"] += ссылок
    if not dry_run:
        # перенос меняет `projections` (хеши, версии) — манифест и контрольная
        # точка за ним (§4.8/§5.2, Т2.6); проба ничего не пишет
        import vault_manifest
        vault_manifest.записать(con, vault)
    return итог


# Кто пишет строку «статус без следа» и ревизию переноса: не человек и не
# модель, а сам перенос. Строка нужна, потому что после смены авторитета
# журнал в волте никто не перечитает, и статус без строки в базе выглядел бы
# объяснённым.
ПЕРЕНОС = "ledger_import"
ПЕРЕНОС_АКТОР = ("import", ПЕРЕНОС, "перенос из волта")


def _id_занят(con, таблица, в_шапке):
    """Ключ объекта, которому уже принадлежит id из шапки; None — свободен.

    Копия карточки в Obsidian с убранным `source_id`, волт новее
    восстановленной базы: вставка с таким id упала бы `UNIQUE constraint
    failed` и унесла всё, что после по алфавиту (ревью PR #117, P2-5). Это
    спор, как и остальные разошедшиеся ключи: говорим и не трогаем.
    """
    if not в_шапке:
        return None
    r = con.execute("select source_native_id from %s where id=?" % таблица,
                    (в_шапке,)).fetchone()
    return r[0] if r else None


def _спор(con, rel, native, вид, таблица):
    """(строка по ключу, проекция по пути, текст спора или None).

    Карточку могли завести без `source_id` — ключом тогда стал путь.
    Когда `source_id` наконец проставили, ключ сменился, и по одной строке
    перенос завёл бы второй объект, а первый остался бы вообще без файла:
    запись проекции перевесила бы её на новый id. Поэтому спрашиваем ещё и
    проекцию по пути, вместе с ключом объекта, который за ней стоит.

    Соединение обычное, не `left`: у проекции, чью строку объекта снесли
    руками, ответа нет вовсе, и это правильный ответ. При `left join`
    вернулась бы пара из пустот, карточка спорила бы вечно — вместо того
    чтобы просто завестись заново. Мутант «join → left join» на этом и
    ловится.

    Два точных ключа — объявленный `source_id` и путь — разошлись. Так
    выглядят сразу несколько случаев: карточке дописали `source_id`,
    карточку переименовали, на месте удалённой завели новую, `source_id` из
    карточки убрали. Развести их нечем.

    Первые две редакции этой правки пробовали слить объекты там, где «и так
    понятно»: сперва по одному пути, потом по пути и совпавшему заголовку.
    Оба раза ревью приводило вход, на котором слияние затирало строку
    ledger, которую `main` не терял, — а заголовок ещё и совпадает ровно в
    самом опасном случае: путь карточки складывается из даты и
    `slug(...)[:40]` (`call_project.py`, `commitment_cards` и `_завести`),
    то есть две карточки сходятся на одном пути как раз при одинаковом
    заголовке.

    Значит правило простое и без исключений: разошлись ключи — спорим. ТЗ
    §4.3, последний пункт: «две разные записи с одинаковым текстом не
    дедуплицируются без доказательства, что это одно событие».
    Доказательства здесь нет ни в базе, ни в файле, а цена ошибки
    несимметрична: на `main` терялась проекция и оставалась сирота, которую
    видно и можно пришить руками, а слияние стирает саму строку — и
    восстановить её нечем.
    """
    if con is None:
        return None, None, None
    row = con.execute("select id from %s where source_native_id=?" % таблица,
                      (native,)).fetchone()
    проекция = con.execute(
        "select o.id, o.source_native_id from projections p "
        "join %s o on o.id = p.object_id "
        "where p.path=? and p.object_kind=?" % таблица,
        (rel, вид)).fetchone()
    if проекция is None:
        спор = None
    elif row is None:
        спор = "ключ сменился"
    elif проекция["id"] != row["id"]:
        спор = "по ключу стоит другой объект"
    else:
        спор = None
    return row, проекция, спор


def _перенести(con, rel, fm, sha, текст, вид, таблица, поля, dry_run=False,
               актор=ПЕРЕНОС_АКТОР, evidence_из_шапки=False):
    """Одна карточка → строка объекта, проекция, история.

    Возвращает `(новый ли объект, записано строк истории, восстановлено
    ссылок evidence)`, либо None, если карточка спорная и не перенесена.
    Правила спора — ниже по тексту, они писались кровью трёх кругов ревью и
    исключений не имеют.

    `evidence_из_шапки` — обратный путь ссылок (`_evidence_из_шапки`), его
    включает только полный перенос `run`: проектор кладёт строки
    `evidence_refs` сам, из извлечения, сразу за `перенести_карточку`.

    `актор` — `(actor_type, actor_id, reason)` для ревизии: кто принёс
    изменение. Перенос — сам перенос, проектор — `call_project`, правка
    словами — владелец; без этого ревизии от всех трёх путей выглядели бы
    одинаково, и журнал отвечал бы на вопрос «что», но не «кто».
    """
    native = ключ(fm, rel)
    row, проекция, спор = _спор(con, rel, native, вид, таблица)
    if спор:
        print("ledger_import: %s стоит за объектом %s (ключ %s), а "
              "карточка объявила source_id %s — %s, не сливаем" %
              (rel, проекция["id"], проекция["source_native_id"],
               native, спор), file=sys.stderr)
        return None
    прежний = row["id"] if row else None
    # Т2.2: id по старшинству — реестр, потом шапка карточки, потом новый.
    # Шапка идёт второй ради волта, восстановленного без базы: id в ней —
    # тот самый, что был в реестре, и выдать новый значило бы потерять его.
    # Разошлись — верим реестру и говорим: шапку поправит `--write-ids`.
    в_шапке = (_строка(fm.get("id")) or "").strip() or None
    if прежний and в_шапке and в_шапке != прежний:
        print("ledger_import: %s несёт id %s, в реестре %s — верю реестру"
              % (rel, в_шапке, прежний), file=sys.stderr)
    новый = прежний is None
    if новый and con is not None:
        занят = _id_занят(con, таблица, в_шапке)
        if занят:
            print("ledger_import: %s несёт id %s, а в реестре он у объекта с "
                  "ключом %s (здесь %s) — не сливаем"
                  % (rel, в_шапке, занят, native), file=sys.stderr)
            return None
    if dry_run:
        # проба показывает и ссылки: сколько восстановила бы, сколько отвергла
        # (ранбук восстановления, шаг 4а: «сначала проба»); только чтение
        ссылок = (_evidence_из_шапки(con, rel, прежний or в_шапке, fm,
                                     событие(_строка(fm.get("origin"))), dry_run=True)
                  if evidence_из_шапки and вид == "commitment" and con is not None else 0)
        return новый, 0, ссылок
    oid = прежний or в_шапке or mi.uuid7()
    значения = {k: (_строка(fm.get(k)) or None) for k in поля}
    if "confidence" in значения:
        сырое = значения["confidence"]
        значения["confidence"] = _число(сырое)
        # Пустой её делает `_число`, а не база: `real` в SQLite —
        # affinity, и «высокая» легла бы в такую колонку как есть.
        # Молчать нельзя, иначе карточка выглядит перенесённой
        # целиком. И не спорная — одно поле, набранное руками, не
        # повод ронять весь прогон.
        if сырое is not None and значения["confidence"] is None:
            print("ledger_import: %s — confidence %r не число, "
                  "перенесено пустым" % (rel, сырое), file=sys.stderr)
    значения["id"] = oid
    значения["source_native_id"] = native
    значения["origin_event"] = (
        событие(_строка(fm.get("origin"))) if вид == "commitment"
        else событие(_строка(fm.get("source_id"))))
    # Объект, ревизия, проекция и история — одной транзакцией (§5.2, Т2.1в):
    # падение между `update … version=N` и строкой `revisions` оставляло бы
    # версию без ревизии навсегда (ревью PR #117, P3-8). Внутри чужой
    # транзакции — savepoint: откат ровно этого шага (ревью PR #118, P2).
    with mi.транзакция(con):
        _записать_объект(con, таблица, вид, oid, значения, новый, актор)
        правок = _проекция_и_история(con, rel, вид, oid, sha, fm, текст)
        ссылок = (_evidence_из_шапки(con, rel, oid, fm, значения.get("origin_event"))
                  if evidence_из_шапки and вид == "commitment" else 0)
    return новый, правок, ссылок


# Строка списка `evidence` во фронтматтере, как её рисует проектор
# (`call_project._evidence_список`): `<segment_id> <start_ms>-<end_ms>`.
ССЫЛКА = re.compile(r"^(\S+) (\d+)-(\d+)$")


def _evidence_из_шапки(con, rel, oid, fm, событие, dry_run=False):
    """Обратный путь evidence (ADR-0004 п.5, хвост Т2.4/Т2.6): реестр
    восстановлен из копии старее волта, и у обязательства нет ни одной
    строки `evidence_refs` от модели, а в шапке карточки лежит список
    `evidence`, который проектор нарисовал из тех же строк. Тогда строки
    восстанавливаются из шапки — с той же сверкой, что у проектора
    (`call_project._сверить_с_реестром`): сегмент принадлежит расшифровке
    события-источника карточки (`origin`), интервал в его границах; иначе
    ссылка без референта — не evidence (п.1) и не восстанавливается, о ней
    говорится в stderr. Реестр со строками — авторитет: шапка его не
    переписывает, расхождение называет `vault_drift`. След — `audit_events`
    `evidence_restored` (сколько восстановлено, сколько отвергнуто), только
    когда что-то восстановлено: иначе карточка с навсегда потерянными
    сегментами писала бы аудит каждым прогоном. Возвращает число строк.

    Ссылки, которые реестр **отозвал** (`call_project._отозвать_evidence`:
    пункт при повторной проекции ушёл в ревью, строки удалены с аудитом
    `evidence_withdrawn`, карточка в волте осталась со старым списком), не
    восстанавливаются: отсутствие строк здесь — решение реестра, а не потеря
    (ревью PR #131, P2). `dry_run` — только счёт, без записи и аудита; `oid`
    None (объекта в реестре нет) — строк и отзыва у него быть не может."""
    список = fm.get("evidence")
    if isinstance(список, str):
        список = список.split(",")
    if not isinstance(список, list):
        return 0
    список = [str(x).strip() for x in список if str(x).strip()]
    if not список:
        return 0
    if oid is not None:
        if con.execute("select 1 from evidence_refs where object_kind='commitment' and "
                       "object_id=? and producer='model' limit 1", (oid,)).fetchone():
            return 0
        if con.execute("select 1 from audit_events where object_kind='commitment' and "
                       "object_id=? and action='evidence_withdrawn' limit 1",
                       (oid,)).fetchone():
            print("ledger_import: %s — ссылки evidence отозваны реестром, из шапки не "
                  "восстанавливаются" % rel, file=sys.stderr)
            return 0
    когда, принято, отвергнуто = mi.now_iso(), 0, []
    for ссылка in список:
        m = ССЫЛКА.match(ссылка)
        seg = None
        if m and событие:
            seg = con.execute(
                "select s.start_ms, s.end_ms from transcript_segments s join transcripts t "
                "on t.id=s.transcript_id where s.id=? and t.event_id=?",
                (m.group(1), событие)).fetchone()
        if seg is None or not (seg["start_ms"] <= int(m.group(2)) <= int(m.group(3))
                               <= seg["end_ms"]):
            отвергнуто.append(ссылка)
            continue
        if not dry_run:
            con.execute("insert into evidence_refs(id,object_kind,object_id,kind,segment_id,"
                        "start_ms,end_ms,producer,created) values(?,?,?,?,?,?,?,?,?)",
                        (mi.uuid7(), "commitment", oid, "audio", m.group(1),
                         int(m.group(2)), int(m.group(3)), "model", когда))
        принято += 1
    if отвергнуто:
        print("ledger_import: %s — ссылок evidence без сегмента в реестре: %d "
              "(не восстановлены)" % (rel, len(отвергнуто)), file=sys.stderr)
    if принято and not dry_run:
        mi.audit(con, "evidence_restored", ("import", ПЕРЕНОС), "commitment", oid,
                 {"restored": принято, "rejected": len(отвергнуто), "from": "frontmatter"},
                 когда)
    return принято


def _проекция_и_история(con, rel, вид, oid, sha, fm, текст):
    # путь мог смениться при переименовании: у объекта ровно одна проекция
    con.execute("delete from projections where object_id=? and path<>?",
                (oid, rel))
    # upsert, не `insert or replace`: тот заводил строку заново и обнулял
    # `ledger_version`/`projector_version`/`manifest_hash` из миграции 2
    # (ревью PR #117, P2-2) — колонки проектора Т2.6, которые перенос не
    # ведёт и трогать не вправе
    # `ledger_version` — версия объекта, которую эта проекция отражает
    # (§4.8, Т2.6); `projector_version` ставит проектор, `manifest_hash` —
    # `vault_manifest.записать` в конце прогона, когда все строки на месте
    версия = (con.execute("select version from commitments where id=?", (oid,)).fetchone()
              or [None])[0] if вид == "commitment" else None
    con.execute("insert into projections"
                "(path,object_kind,object_id,content_sha256,written,ledger_version) "
                "values(?,?,?,?,?,?) on conflict(path) do update set "
                "object_kind=excluded.object_kind, object_id=excluded.object_id, "
                "content_sha256=excluded.content_sha256, written=excluded.written, "
                "ledger_version=excluded.ledger_version",
                (rel, вид, oid, sha, mi.now_iso(), версия))
    return правки_в_базу(con, oid, fm, текст) if вид == "commitment" else 0


def _записать_объект(con, таблица, вид, oid, значения, новый, актор):
    """Строка объекта: вставка или обновление только изменившихся полей.

    ADR-0003 п.1–2: `version` живёт в реестре и растёт на единицу на каждое
    принятое изменение, а `revisions` хранит только то, что изменилось, до
    и после. `insert or replace` этого не умеет: он заводит строку заново с
    `version` по умолчанию — то есть молча откатывает счётчик на 1 при
    каждом переносе. Перерисовка без изменений версию не трогает.
    Версия и ревизии есть у обязательств; у разговоров колонки `version`
    нет (их не правят), им — обычное обновление.
    """
    actor_type, actor_id, причина = актор
    имена = sorted(значения)
    # Событие аудита — той же транзакцией, что объект и ревизия (§5.2, Т2.5):
    # имена полей и версия, без значений — они в `revisions`.
    if новый:
        con.execute("insert into %s(%s) values(%s)"
                    % (таблица, ",".join(имена), ",".join("?" * len(имена))),
                    [значения[k] for k in имена])
        когда = mi.now_iso()
        поля = sorted(k for k, v in значения.items() if v is not None and k != "id")
        if вид == "commitment":
            con.execute(
                "insert into revisions(object_kind,object_id,version,changed_json,"
                "actor_type,actor_id,reason,origin_event,occurred) "
                "values(?,?,1,?,?,?,?,?,?)",
                (вид, oid, json.dumps({k: [None, значения[k]] for k in поля},
                                      ensure_ascii=False),
                 actor_type, actor_id, причина, значения.get("origin_event"), когда))
        mi.audit(con, "object.created", актор, вид, oid,
                 {"version": 1 if вид == "commitment" else None, "fields": поля,
                  "reason": причина}, когда)
        return
    старое = dict(con.execute("select * from %s where id=?" % таблица, (oid,)).fetchone())
    # `created` у существующего объекта не переносится: проектор ставит в
    # шапку `mi.now_iso()` при каждой перерисовке, и перенос этого поля
    # давал бы ложную ревизию и version+1 на каждую повторную проекцию.
    # Время рождения объекта — то, что легло первым.
    изменилось = {k: [старое.get(k), v] for k, v in значения.items()
                  if k not in ("id", "created") and старое.get(k) != v}
    if not изменилось:
        return
    поля_sql = ", ".join("%s=?" % k for k in sorted(изменилось))
    args = [изменилось[k][1] for k in sorted(изменилось)]
    когда = mi.now_iso()
    версия = None
    if вид == "commitment":
        версия = (старое.get("version") or 1) + 1
        con.execute("update %s set %s, version=?, updated=? where id=?"
                    % (таблица, поля_sql), args + [версия, когда, oid])
        con.execute(
            "insert into revisions(object_kind,object_id,version,changed_json,"
            "actor_type,actor_id,reason,origin_event,occurred) values(?,?,?,?,?,?,?,?,?)",
            (вид, oid, версия, json.dumps(изменилось, ensure_ascii=False),
             actor_type, actor_id, причина, значения.get("origin_event"), когда))
    else:
        con.execute("update %s set %s where id=?" % (таблица, поля_sql), args + [oid])
    mi.audit(con, "object.updated", актор, вид, oid,
             {"version": версия, "fields": sorted(изменилось), "reason": причина}, когда)


def перенести_карточку(con, vault, rel, актор=ПЕРЕНОС_АКТОР):
    """Одна карточка по пути — для проектора, сразу после записи файла.

    Возвращает id объекта или None (не наш каталог, не читается, спорная).
    Тот же код, что у полного переноса: у карточки, которую `call_project`
    только что записал, строка в реестре появляется этим вызовом, а не
    ночным кроном, и id в её шапке — тот, что в реестре.
    """
    вид = вид_по_пути(rel)
    if not вид:
        return None
    к = _карточка(vault, os.path.join(vault, rel))
    if not к or not к[1]:
        return None
    rel, fm, sha, текст = к
    вид, таблица, поля = вид
    if _перенести(con, rel, fm, sha, текст, вид, таблица, поля, актор=актор) is None:
        return None
    return con.execute("select id from %s where source_native_id=?" % таблица,
                       (ключ(fm, rel),)).fetchone()[0]


ШАПКА = re.compile(r"^---\n(.*?)\n---\n", re.S)


def _с_id(текст, oid):
    """Текст карточки с `id: <oid>` в шапке: строка заменяется на месте, а
    новой встаёт сразу за `title:` — остальные байты не трогаются, как в
    `call_project._шапка`: Basic Memory дописывает в карточки свои ключи."""
    m = ШАПКА.match(текст)
    if not m:
        return None
    строки = m.group(1).split("\n")
    for i, l in enumerate(строки):
        if l.startswith("id:"):
            строки[i] = "id: " + oid
            break
    else:
        после = next((i for i, l in enumerate(строки) if l.startswith("title:")), -1)
        строки.insert(после + 1, "id: " + oid)
    return "---\n" + "\n".join(строки) + "\n---\n" + текст[m.end():]


def вписать_id(con, vault, dry_run=False):
    """Т2.2: id из реестра — в шапку каждой карточки, где его нет или он
    другой. Пишет волт, поэтому только под флоком и только с паузой
    писателей на doctor. Возвращает (вписано, пропущено без строки).

    Пишется нормализованный текст: BOM снят, CRLF → LF (так читает
    `_карточка`). Для карточки из винды или синка это правка байтов сверх
    строки `id:`; откат — git волта, и в плане это названо (ревью P3-6).
    Спорная карточка (та же `_спор`, что у переноса) не правится: у неё
    путь занят чужой проекцией, и `update projections … path` упал бы на
    ключе посреди прогона (ревью P3-11). Считается в «без строки»."""
    вписано, без_строки = 0, 0
    with locked(vault):
        for подкаталог, вид, таблица, _ in ВИДЫ:
            for rel, fm, sha, текст in карточки(vault, подкаталог):
                if not fm:
                    continue
                row, _, спор = _спор(con, rel, ключ(fm, rel), вид, таблица)
                if row is None or спор:
                    без_строки += 1
                    continue
                if (_строка(fm.get("id")) or "").strip() == row["id"]:
                    continue
                новый = _с_id(текст, row["id"])
                if новый is None:
                    continue
                вписано += 1
                if dry_run:
                    continue
                p = os.path.join(vault, rel)
                tmp = p + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(новый)
                os.replace(tmp, p)
                # отпечаток проекции — на новые байты, иначе следующая
                # сверка сочтёт нашу же правку чужой
                with open(p, "rb") as fh:
                    sha = hashlib.sha256(fh.read()).hexdigest()
                # по объекту, не по пути: переименованная до ночного переноса
                # карточка держит проекцию под старым путём, и обновление по
                # пути не нашло бы ни строки (ревью P3-6)
                con.execute("update projections set content_sha256=?, path=? "
                            "where object_id=? and object_kind=?",
                            (sha, rel, row["id"], вид))
    return вписано, без_строки


# Строку журнала пишет `call_project._поправить`:
#   - 2026-09-21T15:08, Мара, correction/<id>: статус a → b; срок x → y; заметка
# и рукой — без события, как «чистый лист» 21.09: `- <когда>, <кто>: <заметка>`.
СТРОКА = re.compile(r"^- (\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}), ([^,:]+?)"
                    r"(?:, correction/(\S+?))?: (.*)$")
ПЕРЕХОД = re.compile(r"^(статус|срок) (.+?) → (\S+)$")


def журнал(текст):
    """Журнал «Правки:» из тела карточки → (записи, неразобранные строки)."""
    _, _, хвост = текст.partition("\nПравки:\n")
    записи, мусор = [], []
    for l in хвост.splitlines():
        l = l.strip()
        if not l or l == "Правки:":
            continue
        m = СТРОКА.match(l)
        if not m:
            мусор.append(l)
            continue
        з = {"when": m.group(1), "who": m.group(2), "event": m.group(3),
             "status": None, "due": None, "notes": [], "raw": l}
        for часть in filter(None, (x.strip() for x in m.group(4).split(";"))):
            п = ПЕРЕХОД.match(часть)
            if п:
                з["status" if п.group(1) == "статус" else "due"] = п.group(2, 3)
            else:
                з["notes"].append(часть)
        записи.append(з)
    return записи, мусор


def история(vault):
    """Сверка Т2.0: чем объяснён статус каждого обязательства. Ничего не пишет.

    Статус без следа в журнале — это правка мимо пути правок (21.09 и 28.09
    так закрыто 192 обязательства). Переносить такой статус в реестр можно,
    но объяснять его нечем, и сказать об этом надо до смены авторитета.
    """
    итог, замечания = Counter(), []
    for rel, fm, _, текст in карточки(vault, "kb/commitments"):
        статус = _строка(fm.get("status")) or "proposed"
        записи, мусор = журнал(текст)
        итог["неразобрано строк"] += len(мусор)
        for l in мусор:
            замечания.append("%s: строка журнала не разобрана: %s" % (rel, l[:80]))
        переходы = [з for з in записи if з["status"]]
        if переходы:
            последний = переходы[-1]
            if последний["status"][1] != статус:
                вид = "разошлось"
                замечания.append("%s: разошлось — в шапке %s, журнал говорит %s"
                                 % (rel, статус, последний["status"][1]))
            else:
                вид = ("по пути правок" if последний["event"]
                       else "рукой без события")
        elif статус == "proposed":
            вид = "без правок"
        elif статус == "open" and (_строка(fm.get("origin")) or "").startswith(
                "correction/"):
            вид = "по пути правок"          # заведена правкой: `_завести`
        else:
            вид = "рукой без события" if записи else "без следа"
        итог[вид] += 1
    return итог, замечания


# Пространство имён для id строк `corrections`, которые пишет перенос.
# Id детерминированный — uuid5 от объекта и строки журнала, — а не uuid7:
# перенос запускают дважды (проба, потом всерьёз), и повтор обязан лечь в
# те же строки, а не удвоить историю. Побочно по id видно, своя строка или
# чужая: всё, что в `corrections` объекта не входит в множество ожидаемых
# id, перенос не писал — это либо доменная команда Т2.5, либо строка от
# прежней редакции журнала. Сверка такие считает и называет, но не трогает.
ПРОСТРАНСТВО = uuid.UUID("6d617261-0000-5000-8000-6c6564676572")  # «mara…ledger»


def _id_правки(oid, ключ):
    return str(uuid.uuid5(ПРОСТРАНСТВО, "%s\x00%s" % (oid, ключ)))


def _когда(минуты):
    """`2026-09-21T15:08` журнала → ISO со сдвигом, как `events.occurred`.

    Журнал пишет `now_iso()[:16]` (`call_project._поправить`), то есть время
    в `MARA_TZ_HOURS`; сдвиг отрезан ради ширины строки, а не потерян.
    Возвращаем его, чтобы колонка держала и UTC, и исходный сдвиг (§5.1).
    """
    try:
        return datetime.fromisoformat(минуты.replace(" ", "T")).replace(
            tzinfo=mi.TZ).isoformat(timespec="seconds")
    except ValueError:
        return минуты


def правки_из_карточки(oid, fm, текст):
    """Строки `corrections` для одного обязательства. Ничего не пишет.

    Одна строка журнала → по строке на каждое изменённое поле (`status`,
    `due`); заметка без перехода — строка с полем `note`. Заметки рядом с
    переходом уходят в `reason` каждой строки перехода — это и есть «почему».
    Актор везде человек: строка с событием — слова владельца через
    `mara_correction` (`origin_event` его и несёт), строка без события —
    правка рукой в волте. «Мара» в журнале — кто записал, а не кто решил.

    Статус в шапке, которого журнал не объясняет (`история()`: «без следа»,
    «рукой без события», «разошлось»), получает одну строку с `old_json`
    null от актора `ledger_import` и причиной словами. Это не выдуманная
    история, а запись факта: на момент переноса шапка говорила так, и
    объяснения этому в волте не было.
    """
    статус = _строка(fm.get("status")) or "proposed"
    записи, _ = журнал(текст)
    строки = []
    for n, з in enumerate(записи):
        # ключ id — номер строки и её текст: поправленная руками строка даёт
        # новую строку в базе, а прежняя остаётся и становится «чужой» для
        # сверки; одинаковые строки подряд различает номер
        ключ = "%d\x00%s\x00" % (n, з["raw"])
        общее = {"actor_type": "human", "actor_id": з["who"],
                 "origin_event": з["event"], "occurred": _когда(з["when"])}
        причина = ("правка через mara_correction" if з["event"]
                   else "правка рукой в волте")
        if з["notes"]:
            причина += ": " + "; ".join(з["notes"])
        переходы = [(поле, з[поле]) for поле in ("status", "due") if з[поле]]
        if not переходы:
            строки.append(dict(общее, id=_id_правки(oid, ключ + "note"),
                               field="note", old_json=None,
                               new_json=json.dumps("; ".join(з["notes"]),
                                                   ensure_ascii=False),
                               reason=причина))
        for поле, (было, стало) in переходы:
            # «не был» и «?» — так `_поправить` печатает пустоту
            строки.append(dict(общее, id=_id_правки(oid, ключ + поле),
                               field=поле,
                               old_json=(None if было in ("не был", "?")
                                         else json.dumps(было, ensure_ascii=False)),
                               new_json=json.dumps(стало, ensure_ascii=False),
                               reason=причина))
    переходы_статуса = [з for з in записи if з["status"]]
    if переходы_статуса and переходы_статуса[-1]["status"][1] != статус:
        почему = ("шапка расходится с журналом: журнал говорит %s"
                  % переходы_статуса[-1]["status"][1])
    elif переходы_статуса or статус == "proposed":
        почему = None
    elif статус == "open" and (_строка(fm.get("origin")) or "").startswith(
            "correction/"):
        почему = None                       # заведена правкой: `_завести`
    else:
        почему = "статус из шапки без строки в журнале «Правки:»"
    if почему:
        # Ключ отметки несёт статус и причину: владелец поменял статус рукой
        # ещё раз — повторный перенос пишет новую отметку, а не молчит об
        # этом из-за `insert or ignore` по прежнему id (ревью PR #117,
        # P2-1). Прежняя отметка остаётся: она про прежний момент и
        # по-прежнему верна. Время — момент переноса, не `valid_from`
        # карточки: тот ставит только `_поправить`, а у статуса без
        # журнала настоящего времени нет, и выдавать за него дату создания
        # карточки значило бы выдумывать (P2-3). Повтор ту же отметку не
        # сдвигает: id детерминирован, `insert or ignore`.
        строки.append({"id": _id_правки(oid, "head/status/%s/%s" % (статус, почему)),
                       "field": "status", "old_json": None,
                       "new_json": json.dumps(статус, ensure_ascii=False),
                       "actor_type": "import", "actor_id": ПЕРЕНОС,
                       "origin_event": None,
                       "reason": почему + "; время правки неизвестно, "
                                 "здесь — момент переноса",
                       "occurred": mi.now_iso()})
    return строки


def правки_в_базу(con, oid, fm, текст):
    """Записать историю обязательства. Возвращает число новых строк.

    `insert or ignore` по детерминированному id: повтор переноса не плодит
    строк, а правка строки журнала руками даёт новую строку рядом со
    старой — старую перенос не трогает, о ней скажет сверка.
    """
    новых = 0
    for r in правки_из_карточки(oid, fm, текст):
        новых += con.execute(
            "insert or ignore into corrections(id,object_kind,object_id,field,"
            "old_json,new_json,actor_type,actor_id,reason,origin_event,occurred) "
            "values(?,'commitment',?,?,?,?,?,?,?,?,?)",
            (r["id"], oid, r["field"], r["old_json"], r["new_json"],
             r["actor_type"], r["actor_id"], r["reason"], r["origin_event"],
             r["occurred"])).rowcount
    return новых


def сверка(con, vault):
    """«Карточек столько же, сколько строк» (migration-plan.md §4 шаг 3а,
    §5 предусловия Т2.8). Только читает. Возвращает (счётчики, замечания).

    Три вопроса, каждый — счётчиком и строкой на каждое расхождение: у
    каждой карточки есть строка объекта; статус в строке тот же, что в
    шапке; история в `corrections` — ровно та, что в журнале. Строки
    `corrections`, которых перенос не писал (id вне ожидаемого множества),
    считаются «чужими»: до Т2.5 это след прежней редакции журнала, после —
    доменные команды, и в обоих случаях не перенесённое.
    """
    итог, замечания = Counter(), []
    видели, сошлись = {}, set()
    for rel, fm, _, текст in карточки(vault, "kb/commitments"):
        итог["карточек"] += 1
        if not fm:
            итог["без строки"] += 1
            замечания.append("%s: без шапки, строки в базе нет" % rel)
            continue
        native = ключ(fm, rel)
        # Спорные карточки (дубль `source_id`, разошедшиеся ключи) перенос
        # не трогал — сверять их с чужой строкой значит выдавать неперенесённую
        # карточку за расхождение переноса (ревью PR #117, P3-1). Они
        # считаются отдельно и сходиться не дают.
        if native in видели:
            итог["спорных"] += 1
            замечания.append("%s: спорная — тот же source_id, что у %s"
                             % (rel, видели[native]))
            continue
        row, проекция, спор = _спор(con, rel, native, "commitment", "commitments")
        if спор:
            итог["спорных"] += 1
            замечания.append("%s: спорная — %s (за объектом %s стоит ключ %s)"
                             % (rel, спор, проекция["id"], проекция["source_native_id"]))
            continue
        видели[native] = rel
        if row is None:
            занят = _id_занят(con, "commitments", (_строка(fm.get("id")) or "").strip())
            if занят:
                итог["спорных"] += 1
                замечания.append("%s: спорная — id из шапки у объекта с ключом %s"
                                 % (rel, занят))
                continue
            итог["без строки"] += 1
            замечания.append("%s: строки в базе нет (ключ %s)" % (rel, native))
            continue
        row = con.execute("select id, status from commitments where id=?",
                          (row["id"],)).fetchone()
        сошлись.add(row["id"])
        итог["строк"] += 1
        статус = _строка(fm.get("status")) or "proposed"
        if (row["status"] or "proposed") != статус:
            итог["статус разошёлся"] += 1
            замечания.append("%s: в шапке %s, в базе %s"
                             % (rel, статус, row["status"]))
        ожидаемые = {r["id"] for r in правки_из_карточки(row["id"], fm, текст)}
        # Отметки переноса о прежних состояниях шапки — своя история, не
        # чужая: статус менялся рукой дважды, и обе отметки верны про свой
        # момент. Чужое — только то, чего перенос не писал никогда. Маска по
        # актору `import` накроет и отметки прежней редакции ключа (до
        # ab1168d, без статуса в ключе) — на doctor их нет, перенос там ещё
        # не запускался; появятся — считать своими, они про свой момент.
        строки_базы = con.execute(
            "select id, actor_type from corrections where object_kind='commitment' "
            "and object_id=?", (row["id"],)).fetchall()
        в_базе = {r[0] for r in строки_базы}
        свои_отметки = {r[0] for r in строки_базы if r[1] == "import"}
        итог["правок ожидается"] += len(ожидаемые)
        нет, чужие = ожидаемые - в_базе, в_базе - ожидаемые - свои_отметки
        итог["правок нет в базе"] += len(нет)
        итог["правок чужих"] += len(чужие)
        if нет or чужие:
            замечания.append("%s: правок из журнала нет в базе %d, чужих в базе %d"
                             % (rel, len(нет), len(чужие)))
    # Строки, за которыми в этом прогоне не встало ни одной карточки, — а не
    # «без проекции»: у удалённой из волта карточки проекция остаётся, и по
    # ней сверка зеленила объект, который проектор потом воскресил бы (Codex
    # по #117, P1).
    итог["строк без карточки"] = con.execute(
        "select count(*) from commitments").fetchone()[0] - len(сошлись)
    return итог, замечания


def строка_сверки(итог):
    return "сверка Т2.0: " + ", ".join(
        "%s %d" % (k, итог[k]) for k in (
            "карточек", "строк", "спорных", "без строки", "строк без карточки",
            "статус разошёлся", "правок ожидается", "правок нет в базе",
            "правок чужих"))


def сошлось(итог):
    return not any(итог[k] for k in ("спорных", "без строки", "строк без карточки",
                                     "статус разошёлся", "правок нет в базе",
                                     "правок чужих"))


def self_check():
    with tempfile.TemporaryDirectory() as tmp:
        root, vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(root)
        os.makedirs(os.path.join(vault, "kb/commitments"))
        os.makedirs(os.path.join(vault, ".git"))        # флок `locked()` живёт там
        карточка = os.path.join(vault, "kb/commitments", "2026-09-03-smeta.md")
        шапка = ("title: прислать смету\nstatus: open\n"
                 "source_id: commitment/call_1/requests/1\norigin: call/call_1\n")
        with open(карточка, "w", encoding="utf-8") as fh:
            fh.write("---\n%s---\n\n- Обещание: прислать смету\n\nПравки:\n"
                     "- 2026-09-21T15:08, Мара, correction/c1: статус proposed → open\n"
                     % шапка)
        con = mi.connect(root)

        assert run(con, vault, dry_run=True)["обязательств"] == 1
        assert con.execute("select count(*) from commitments").fetchone()[0] == 0, \
            "проба не пишет"

        итог = run(con, vault)
        assert итог["обязательств"] == 1 and итог["правок"] == 1, итог
        r = con.execute("select * from commitments").fetchone()
        assert r["title"] == "прислать смету" and r["origin_event"] == "call_1"
        было = r["id"]
        п = con.execute("select * from corrections").fetchone()
        assert (п["object_id"], п["field"], п["origin_event"]) == \
            (было, "status", "c1") and json.loads(п["new_json"]) == "open", dict(п)

        итог = run(con, vault)
        assert итог["обязательств"] == 0 and итог["правок"] == 0, \
            "второй раз ни новых объектов, ни новых правок"
        assert con.execute("select id from commitments").fetchone()[0] == было, \
            "ТЗ §4.3: id не меняется"
        счёт, замечания = сверка(con, vault)
        assert сошлось(счёт) and not замечания, (dict(счёт), замечания)
        # Т2.2: id из реестра попадает в шапку, и после этого перенос верит ей
        assert вписать_id(con, vault) == (1, 0)
        with open(карточка, encoding="utf-8") as fh:
            assert "\nid: %s\n" % было in fh.read(), "id не вписан"
        assert вписать_id(con, vault) == (0, 0), "второй раз вписывать нечего"
        assert перенести_карточку(con, vault, "kb/commitments/2026-09-03-smeta.md") == было
        счёт, _ = сверка(con, vault)
        assert сошлось(счёт), dict(счёт)

        ids = [mi.uuid7() for _ in range(200)]
        assert ids == sorted(ids) and len(set(ids)) == 200, "uuid7 монотонен"
    print("ledger_import self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="перенос карточек волта в ledger")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    ap.add_argument("--write-ids", action="store_true", dest="write_ids",
                    help="Т2.2: вписать id из реестра в шапки карточек")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if a.write_ids:
        # Только после переноса и только всерьёз: без строк в реестре
        # вписывать нечего, а проба тут — `--dry-run` вместе с флагом.
        if not os.path.exists(os.path.join(a.root, "contextd.db")):
            print("ledger_import --write-ids: базы в %s нет — сначала перенос"
                  % a.root, file=sys.stderr)
            return 2
        con = mi.connect(a.root)
        вписано, без_строки = вписать_id(con, a.vault, dry_run=a.dry_run)
        print("ledger_import --write-ids%s: вписано %d, без строки в реестре %d"
              % (" (проба)" if a.dry_run else "", вписано, без_строки))
        return 0
    # Проба — вопрос «что бы перенеслось», а не команда завести каталог
    # блобов со схемой: `mi.connect` создаёт и то и другое (mara_ingest.py,
    # `def connect`). Базы нет — значит новым будет всё, и это правда.
    без_базы = a.dry_run and not os.path.exists(
        os.path.join(a.root, "contextd.db"))
    if без_базы:
        print("ledger_import: базы в %s нет — считаем всё новым" % a.root,
              file=sys.stderr)
    # Пробе и база нужна только на чтение: `mi.connect` прогоняет схему и
    # миграции, то есть проба на doctor докатила бы базу до кода дерева мимо
    # Г4 (docs/migration-plan.md).
    if без_базы:
        con = None
    elif a.dry_run:
        con = sqlite3.connect("file:%s?mode=ro" % os.path.join(
            a.root, "contextd.db"), uri=True)
        con.row_factory = sqlite3.Row
    else:
        con = mi.connect(a.root)
    итог = run(con, a.vault, dry_run=a.dry_run)
    print("ledger_import%s: обязательств %d, разговоров %d, обновлено %d, "
          "спорных %d, правок %d, evidence восстановлено %d"
          % (" (проба)" if a.dry_run else "", итог["обязательств"],
             итог["разговоров"], итог["обновлено"], итог["спорных"],
             итог["правок"], итог["evidence"]))
    if a.dry_run:
        история_, замечания = история(a.vault)
        for z in замечания:
            print("ledger_import: " + z, file=sys.stderr)
        print("история правок: " + ", ".join(
            "%s %d" % (k, история_[k]) for k in (
                "по пути правок", "рукой без события", "без правок",
                "без следа", "разошлось", "неразобрано строк")))
    # Сверка «карточек столько же, сколько строк» — после записи всерьёз и у
    # пробы поверх живой базы (там она отвечает на вопрос «что разойдётся»).
    # На пробе расхождение кодом не считается: до первого переноса истории
    # в базе нет по определению.
    if con is not None and con.execute(
            "select 1 from sqlite_master where name='corrections'").fetchone() is None:
        # проба поверх базы до миграции 2 (doctor до Т2.8): таблицы истории
        # ещё нет, и сверять историю нечем — говорим это, а не падаем
        print("сверка Т2.0: в базе нет таблицы corrections (схема до миграции 2) "
              "— история не сверяется", file=sys.stderr)
    elif con is not None:
        счёт, замечания = сверка(con, a.vault)
        for z in замечания:
            print("ledger_import: " + z, file=sys.stderr)
        print(строка_сверки(счёт))
        if not a.dry_run and not сошлось(счёт):
            return 1
    return 1 if итог["спорных"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
