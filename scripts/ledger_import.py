#!/usr/bin/env python3
"""Разовый перенос карточек волта в ledger (ТЗ §4.1, ADR-0001).

Первый шаг миграции власти: сегодня правда лежит в Markdown, и пересборка
проекции стёрла бы её. Перенос делает базу знающей ровно то, что знает волт, —
после этого переключение писателей становится обратимым, а до этого нет.

Шаг аддитивный и намеренно скучный: файлы не открываются на запись вообще,
проектор на ledger не переключается, ревизий не заводится. Кроме строк
объектов кладётся отпечаток файла — по нему будущая пересборка отличит свой
файл от поправленного руками.

Ключ — `source_id` карточки, а не путь: карточку могут переименовать в
Obsidian, и это не повод завести второе обязательство. Id объекта при повторном
запуске не меняется никогда (ТЗ §4.3).

    python3 scripts/ledger_import.py --dry-run     # посчитать, ничего не писать
    python3 scripts/ledger_import.py               # перенести
    python3 scripts/ledger_import.py --self-check
"""
import os, re, sys, glob, hashlib, argparse, importlib.util, sqlite3, tempfile
from collections import Counter

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
    итог = {"обязательств": 0, "разговоров": 0, "обновлено": 0, "спорных": 0}
    for подкаталог, вид, таблица, поля in ВИДЫ:
        счётчик = "обязательств" if вид == "commitment" else "разговоров"
        # карта своя на каждый вид: `source_native_id` уникален внутри таблицы,
        # а не поперёк. Одна общая карта означала бы, что обязательство с
        # `source_id: call/…`, поставленным руками, съедает разговор
        видели = {}
        for rel, fm, sha, _ in карточки(vault, подкаталог):
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
             "status": None, "due": None, "notes": []}
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


def self_check():
    with tempfile.TemporaryDirectory() as tmp:
        root, vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(root)
        os.makedirs(os.path.join(vault, "kb/commitments"))
        карточка = os.path.join(vault, "kb/commitments", "2026-09-03-smeta.md")
        шапка = ("title: прислать смету\nstatus: proposed\n"
                 "source_id: commitment/call_1/requests/1\norigin: call/call_1\n")
        with open(карточка, "w", encoding="utf-8") as fh:
            fh.write("---\n%s---\n\n- Обещание: прислать смету\n" % шапка)
        con = mi.connect(root)

        assert run(con, vault, dry_run=True)["обязательств"] == 1
        assert con.execute("select count(*) from commitments").fetchone()[0] == 0, \
            "проба не пишет"

        assert run(con, vault)["обязательств"] == 1
        r = con.execute("select * from commitments").fetchone()
        assert r["title"] == "прислать смету" and r["origin_event"] == "call_1"
        было = r["id"]

        assert run(con, vault)["обязательств"] == 0, "второй раз новых нет"
        assert con.execute("select id from commitments").fetchone()[0] == было, \
            "ТЗ §4.3: id не меняется"

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
    print("ledger_import%s: обязательств %d, разговоров %d, обновлено %d, спорных %d"
          % (" (проба)" if a.dry_run else "", итог["обязательств"],
             итог["разговоров"], итог["обновлено"], итог["спорных"]))
    if a.dry_run:
        сверка, замечания = история(a.vault)
        for z in замечания:
            print("ledger_import: " + z, file=sys.stderr)
        print("история правок: " + ", ".join(
            "%s %d" % (k, сверка[k]) for k in (
                "по пути правок", "рукой без события", "без правок",
                "без следа", "разошлось", "неразобрано строк")))
    return 1 if итог["спорных"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
