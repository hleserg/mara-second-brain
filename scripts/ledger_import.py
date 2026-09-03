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
import os, sys, glob, hashlib, argparse, importlib.util, tempfile

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


def событие(s):
    """`call/call_1` → `call_1`. Не звонок — значит события за карточкой нет."""
    s = (s or "").strip()
    return s[5:] or None if s.startswith("call/") else None


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
               hashlib.sha256(raw).hexdigest())


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
        for rel, fm, sha in карточки(vault, подкаталог):
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
            # в один, которое запрещает §4.4. Считаем и говорим вслух.
            if native in видели:
                итог["спорных"] += 1
                print("ledger_import: %s и %s несут один source_id %s — "
                      "перенесён первый" % (видели[native], rel, native),
                      file=sys.stderr)
                continue
            видели[native] = rel
            row = con.execute("select id from %s where source_native_id=?" % таблица,
                              (native,)).fetchone()
            новый = row is None
            итог[счётчик if новый else "обновлено"] += 1
            if dry_run:
                continue
            oid = mi.uuid7() if новый else row["id"]
            значения = {k: (_строка(fm.get(k)) or None) for k in поля}
            if "confidence" in значения:
                значения["confidence"] = _число(значения["confidence"])
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
    итог = run(mi.connect(a.root), a.vault, dry_run=a.dry_run)
    print("ledger_import%s: обязательств %d, разговоров %d, обновлено %d, спорных %d"
          % (" (проба)" if a.dry_run else "", итог["обязательств"],
             итог["разговоров"], итог["обновлено"], итог["спорных"]))
    return 1 if итог["спорных"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
