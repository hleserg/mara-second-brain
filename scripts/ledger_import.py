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
    python3 scripts/ledger_import.py --self-check
"""
import os, re, sys, glob, json, uuid, hashlib, argparse, importlib.util, sqlite3, tempfile
from collections import Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

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
      "classification")),
    ("kb/conversations", "conversation", "conversations",
     ("title", "occurred", "valid_from", "created", "classification")),
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


def карточки(vault, подкаталог):
    for p in sorted(glob.glob(os.path.join(vault, подкаталог, "*.md"))):
        try:
            with open(p, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            # битый симлинк или каталог с именем `*.md`: перенос разовый и
            # руками, останавливать его из-за одного мусорного имени незачем
            print("ledger_import: %s не читается (%s) — пропущен"
                  % (os.path.relpath(p, vault), e), file=sys.stderr)
            continue
        # BOM от винды и CRLF из синка: без них `frontmatter` не матчит шапку
        # вовсе и возвращает пустоту, а карточка молча заводится объектом со
        # всеми полями NULL и ключом по пути
        текст = raw.decode("utf-8", "replace").lstrip("\ufeff").replace("\r\n", "\n")
        fm, _ = mb.frontmatter(текст)
        yield (os.path.relpath(p, vault), fm,
               hashlib.sha256(raw).hexdigest(), текст)


def run(con, vault=None, dry_run=False):
    """Перенести всё, что есть. Возвращает счётчики."""
    vault = vault or VAULT
    итог = {"обязательств": 0, "разговоров": 0, "обновлено": 0, "спорных": 0,
            "правок": 0}
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
            native = (_строка(fm.get("source_id")) or "").strip() or "vault:" + rel
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
            row = con.execute("select id from %s where source_native_id=?" % таблица,
                              (native,)).fetchone() if con else None
            # Карточку могли завести без `source_id` — ключом тогда стал путь.
            # Когда `source_id` наконец проставили, ключ сменился, и по одному
            # `row` перенос завёл бы второй объект, а первый остался бы вообще
            # без файла: `insert or replace into projections` перевесил бы
            # проекцию на новый id. Поэтому спрашиваем ещё и проекцию по пути,
            # вместе с ключом объекта, который за ней стоит.
            #
            # Соединение обычное, не `left`: у проекции, чью строку объекта
            # снесли руками, ответа нет вовсе, и это правильный ответ. При
            # `left join` вернулась бы пара из пустот, карточка спорила бы
            # вечно — вместо того чтобы просто завестись заново. Мутант
            # «join → left join» на этом и ловится.
            проекция = con.execute(
                "select o.id, o.source_native_id from projections p "
                "join %s o on o.id = p.object_id "
                "where p.path=? and p.object_kind=?" % таблица,
                (rel, вид)).fetchone() if con else None
            # Два точных ключа — объявленный `source_id` и путь — разошлись.
            # Так выглядят сразу несколько случаев: карточке дописали
            # `source_id`, карточку переименовали, на месте удалённой завели
            # новую, `source_id` из карточки убрали. Развести их нечем.
            #
            # Первые две редакции этой правки пробовали слить объекты там, где
            # «и так понятно»: сперва по одному пути, потом по пути и
            # совпавшему заголовку. Оба раза ревью приводило вход, на котором
            # слияние затирало строку ledger, которую `main` не терял, — а
            # заголовок ещё и совпадает ровно в самом опасном случае: путь
            # карточки складывается из даты и `slug(...)[:40]`
            # (`call_project.py:159`, `:391`), то есть две карточки сходятся на
            # одном пути как раз при одинаковом заголовке.
            #
            # Значит правило простое и без исключений: разошлись ключи —
            # спорим. ТЗ §4.3, последний пункт (`TZ-master.md:229`): «две
            # разные записи с одинаковым текстом не дедуплицируются без
            # доказательства, что это одно событие». Доказательства здесь нет
            # ни в базе, ни в файле, а цена ошибки несимметрична: на `main`
            # терялась проекция и оставалась сирота, которую видно и можно
            # пришить руками, а слияние стирает саму строку — и восстановить
            # её нечем.
            if проекция is None:
                спор = None
            elif row is None:
                спор = "ключ сменился"
            elif проекция["id"] != row["id"]:
                спор = "по ключу стоит другой объект"
            else:
                спор = None
            if спор:
                итог["спорных"] += 1
                print("ledger_import: %s стоит за объектом %s (ключ %s), а "
                      "карточка объявила source_id %s — %s, не сливаем" %
                      (rel, проекция["id"], проекция["source_native_id"],
                       native, спор), file=sys.stderr)
                continue
            # Ниже заставы, а не выше: карточка, ушедшая в спор, не перенесена
            # ни во что, и сообщение «перенесён первый» о ней было бы ложью.
            видели[native] = rel
            прежний = row["id"] if row else None
            новый = прежний is None
            итог[счётчик if новый else "обновлено"] += 1
            if dry_run:
                continue
            oid = прежний or mi.uuid7()
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
            имена = sorted(значения)
            con.execute("insert or replace into %s(%s) values(%s)"
                        % (таблица, ",".join(имена), ",".join("?" * len(имена))),
                        [значения[k] for k in имена])
            # путь мог смениться при переименовании: у объекта ровно одна проекция
            con.execute("delete from projections where object_id=? and path<>?",
                        (oid, rel))
            con.execute("insert or replace into projections"
                        "(path,object_kind,object_id,content_sha256,written) "
                        "values(?,?,?,?,?)", (rel, вид, oid, sha, mi.now_iso()))
            if вид == "commitment":
                итог["правок"] += правки_в_базу(con, oid, fm, текст)
    return итог


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
# Кто пишет строку «статус без следа»: не человек и не модель, а сам перенос.
# Строка нужна, потому что после смены авторитета журнал в волте никто не
# перечитает, и статус без строки в базе выглядел бы объяснённым.
ПЕРЕНОС = "ledger_import"


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
        строки.append({"id": _id_правки(oid, "head/status"), "field": "status",
                       "old_json": None,
                       "new_json": json.dumps(статус, ensure_ascii=False),
                       "actor_type": "import", "actor_id": ПЕРЕНОС,
                       "origin_event": None, "reason": почему,
                       "occurred": (_строка(fm.get("valid_from"))
                                    or _строка(fm.get("created"))
                                    or mi.now_iso())})
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
    for rel, fm, _, текст in карточки(vault, "kb/commitments"):
        итог["карточек"] += 1
        if not fm:
            итог["без строки"] += 1
            замечания.append("%s: без шапки, строки в базе нет" % rel)
            continue
        native = (_строка(fm.get("source_id")) or "").strip() or "vault:" + rel
        row = con.execute("select id, status from commitments where "
                          "source_native_id=?", (native,)).fetchone()
        if row is None:
            итог["без строки"] += 1
            замечания.append("%s: строки в базе нет (ключ %s)" % (rel, native))
            continue
        итог["строк"] += 1
        статус = _строка(fm.get("status")) or "proposed"
        if (row["status"] or "proposed") != статус:
            итог["статус разошёлся"] += 1
            замечания.append("%s: в шапке %s, в базе %s"
                             % (rel, статус, row["status"]))
        ожидаемые = {r["id"] for r in правки_из_карточки(row["id"], fm, текст)}
        в_базе = {r[0] for r in con.execute(
            "select id from corrections where object_kind='commitment' "
            "and object_id=?", (row["id"],))}
        итог["правок ожидается"] += len(ожидаемые)
        нет, чужие = ожидаемые - в_базе, в_базе - ожидаемые
        итог["правок нет в базе"] += len(нет)
        итог["правок чужих"] += len(чужие)
        if нет or чужие:
            замечания.append("%s: правок из журнала нет в базе %d, чужих в базе %d"
                             % (rel, len(нет), len(чужие)))
    итог["строк без карточки"] = con.execute(
        "select count(*) from commitments where id not in "
        "(select object_id from projections where object_kind='commitment')"
    ).fetchone()[0]
    return итог, замечания


def строка_сверки(итог):
    return "сверка Т2.0: " + ", ".join(
        "%s %d" % (k, итог[k]) for k in (
            "карточек", "строк", "без строки", "строк без карточки",
            "статус разошёлся", "правок ожидается", "правок нет в базе",
            "правок чужих"))


def сошлось(итог):
    return not any(итог[k] for k in ("без строки", "строк без карточки",
                                     "статус разошёлся", "правок нет в базе",
                                     "правок чужих"))


def self_check():
    with tempfile.TemporaryDirectory() as tmp:
        root, vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(root)
        os.makedirs(os.path.join(vault, "kb/commitments"))
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

        ids = [mi.uuid7() for _ in range(200)]
        assert ids == sorted(ids) and len(set(ids)) == 200, "uuid7 монотонен"
    print("ledger_import self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="перенос карточек волта в ledger")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
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
          "спорных %d, правок %d"
          % (" (проба)" if a.dry_run else "", итог["обязательств"],
             итог["разговоров"], итог["обновлено"], итог["спорных"],
             итог["правок"]))
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
