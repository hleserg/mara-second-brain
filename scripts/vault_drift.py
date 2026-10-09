#!/usr/bin/env python3
"""Детектор расхождений проекции и реестра (Т2.6, ТЗ §4.8: «drift detector:
ручные изменения выявляются, не затираются молча»).

Только читает — базу открывает в режиме `mode=ro`, чтобы опечатка в
`--root` не завела пустую базу. Сравнивает то, что лежит в волте (карточки
`kb/commitments`, `kb/conversations`), с тем, что реестр о них помнит
(`projections`, `commitments`/`conversations`, `evidence_refs`), и называет
каждое расхождение строкой с ключом счётчика. Пересборка волта из реестра
— следующий слой Т2.6 и гейт Г4/Т2.8: пока авторитет у волта, пересборка
стирала бы правки владельца, а детектор — нет.

Что считается (ключи счётчиков):

- «изменены после переноса» — файл не тот, что реестр видел последним
  (`projections.content_sha256`): правка рукой, которую следующий
  `ledger_import.py` ещё не забрал (крона у переноса нет, он запускается
  руками — RUNBOOK). Шапка и evidence такой карточки с реестром **не**
  сравниваются: расхождение объяснено правкой файла, и говорить о нём
  второй раз значило бы каждую правку статуса рукой называть поломкой.
  Норма дня до Т2.8; расхождение только с `--strict`.
- «карточек без проекции» — файл есть, строки `projections` нет: ещё не
  переносилась. Норма до переноса, расхождение с `--strict`.
- «переименованы» — проекция без файла, но карточка с тем же `id:` в шапке
  лежит под другим путём: переименована рукой, перенос перевесит проекцию.
  Норма до переноса, расхождение с `--strict`.
- «проекций без файла» — реестр помнит карточку, файла нет и карточки с её
  `id` нет: удалена руками, объект остался. Расхождение.
- «проекций без объекта» — строка проекции указывает на объект, которого
  нет. Расхождение.
- «шапка разошлась» — файл тот же, что видел реестр, а `title`/`status`/
  `due` в строке объекта другие: реестр ушёл вперёд (правка словами или
  перенос другой карточки), проекция отстала. Расхождение.
- «evidence разошлось» — файл тот же, а список `evidence` во фронтматтере
  обязательства не совпадает со строками `evidence_refs` от производителя
  `model` (только их проектор и рисует; строки `human`/`rule`, когда
  появятся, карточка не перечисляет). Расхождение.

Спорные карточки (`source_id` в шапке не тот, что у объекта за проекцией)
здесь не видны — это компетенция `ledger_import.сверка`, которая считает
их отдельно.

    python3 scripts/vault_drift.py --check [--strict] [--vault V --root R]
"""
import os, sys, argparse, sqlite3
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import ledger_import as li

ПОЛЯ = {"commitment": ("title", "status", "due"), "conversation": ("title",)}
УМОЛЧАНИЯ = {"status": "proposed"}
РАСХОЖДЕНИЯ = ("проекций без файла", "проекций без объекта", "шапка разошлась",
               "evidence разошлось")
СТРОГО = ("изменены после переноса", "карточек без проекции", "переименованы")


class ВолтНеПрочитан(RuntimeError):
    pass


def _норм(поле, v):
    v = li._строка(v)
    v = v.strip() if isinstance(v, str) else v
    return (v or None) or УМОЛЧАНИЯ.get(поле)


def _evidence_шапки(fm):
    v = fm.get("evidence")
    if isinstance(v, list):
        return {str(x).strip() for x in v if str(x).strip()}
    if isinstance(v, str) and v.strip():
        return {x.strip() for x in v.split(",") if x.strip()}
    return set()


def проверить(con, vault):
    """→ (счётчики, замечания как список `(ключ, текст)`). Ничего не пишет.

    Волта нет или в нём нет ни одного каталога карточек — `ВолтНеПрочитан`:
    иначе каждая проекция выглядела бы «удалённой руками»."""
    if not vault or not any(os.path.isdir(os.path.join(vault, под))
                            for под, *_ in li.ВИДЫ):
        raise ВолтНеПрочитан("волт не прочитан: %s — нет каталогов карточек" % vault)
    итог, замечания = Counter(), []

    def заметить(ключ, текст):
        итог[ключ] += 1
        замечания.append((ключ, текст))

    на_диске = {}
    for подкаталог, вид, таблица, _ in li.ВИДЫ:
        for rel, fm, sha, текст in li.карточки(vault, подкаталог):
            на_диске[rel] = (вид, таблица, fm, sha)
    итог["карточек"] = len(на_диске)
    проекции = {r["path"]: dict(r) for r in con.execute(
        "select path, object_kind, object_id, content_sha256 from projections")}
    итог["проекций"] = len(проекции)
    id_без_проекции = set()
    for rel, (вид, таблица, fm, sha) in на_диске.items():
        p = проекции.get(rel)
        if p is None:
            заметить("карточек без проекции",
                     "%s: проекции в реестре нет — ещё не переносилась" % rel)
            ид = (li._строка(fm.get("id")) or "").strip()
            if ид:
                id_без_проекции.add(ид)
            continue
        # Объект — раньше хеша: проекция на отсутствующий объект — поломка
        # реестра при любом состоянии файла, и правка рукой её не объясняет;
        # иначе за «изменены после переноса» (не находка сверки) она молчала бы
        # до `--strict` (Codex по #123).
        row = con.execute("select * from %s where id=?" % таблица,
                          (p["object_id"],)).fetchone()
        if row is None:
            заметить("проекций без объекта",
                     "%s: проекция указывает на объект %s, которого нет"
                     % (rel, p["object_id"]))
            continue
        if p["content_sha256"] != sha:
            заметить("изменены после переноса",
                     "%s: файл изменён после последнего переноса — правка рукой, "
                     "ещё не перенесена" % rel)
            continue                  # дальше сравнивать нечего: файл ушёл вперёд
        for поле in ПОЛЯ[вид]:
            в_шапке, в_базе = _норм(поле, fm.get(поле)), _норм(поле, row[поле])
            if в_шапке != в_базе:
                заметить("шапка разошлась", "%s: %s в шапке %r, в реестре %r — реестр "
                         "ушёл вперёд, проекция отстала" % (rel, поле, в_шапке, в_базе))
        if вид == "commitment":
            в_реестре = {"%s %d-%d" % (r["segment_id"], r["start_ms"], r["end_ms"])
                         for r in con.execute(
                             "select segment_id, start_ms, end_ms from evidence_refs "
                             "where object_kind='commitment' and object_id=? "
                             "and producer='model' and segment_id is not null",
                             (row["id"],))}
            в_шапке = _evidence_шапки(fm)
            if в_шапке != в_реестре:
                заметить("evidence разошлось", "%s: evidence в шапке %d, в реестре %d, "
                         "общих %d" % (rel, len(в_шапке), len(в_реестре),
                                       len(в_шапке & в_реестре)))
    for rel, p in проекции.items():
        if rel in на_диске:
            continue
        if p["object_id"] in id_без_проекции:
            заметить("переименованы", "%s: файла нет, карточка с тем же id лежит под "
                     "другим путём — переименована рукой, перенос перевесит проекцию" % rel)
        else:
            заметить("проекций без файла", "%s: реестр помнит карточку, файла нет — "
                     "удалена руками, объект %s остался" % (rel, p["object_id"]))
    return итог, замечания


def расхождение(итог, strict=False):
    return any(итог[k] for k in РАСХОЖДЕНИЯ + (СТРОГО if strict else ()))


def строка(итог):
    return "дрейф проекции: " + ", ".join(
        "%s %d" % (k, итог[k]) for k in ("карточек", "проекций") + СТРОГО + РАСХОЖДЕНИЯ)


def только_чтение(root):
    """База в режиме `mode=ro` (как `ledger_import --dry-run`): опечатка в
    `--root` даёт ошибку, а не пустую базу в новом каталоге."""
    con = sqlite3.connect("file:%s?mode=ro" % os.path.join(root, "contextd.db"), uri=True)
    con.row_factory = sqlite3.Row
    return con


def self_check():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root, vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(root)
        os.makedirs(os.path.join(vault, "kb/commitments"))
        os.makedirs(os.path.join(vault, ".git"))
        p = os.path.join(vault, "kb/commitments/2026-09-03-smeta.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: прислать смету\nstatus: open\n"
                     "source_id: commitment/call_1/requests/1\norigin: call/call_1\n---\n"
                     "\n- Обещание: прислать смету\n")
        con = mi.connect(root)
        итог, _ = проверить(con, vault)
        assert итог["карточек без проекции"] == 1 and not расхождение(итог)
        assert расхождение(итог, strict=True)
        li.run(con, vault)
        итог, зам = проверить(con, vault)
        assert not расхождение(итог, strict=True) and not зам, (dict(итог), зам)
        con.execute("update commitments set status='done'")
        итог, _ = проверить(con, vault)
        assert итог["шапка разошлась"] == 1 and расхождение(итог), dict(итог)
        os.remove(p)
        итог, _ = проверить(con, vault)
        assert итог["проекций без файла"] == 1, dict(итог)
        try:
            проверить(con, os.path.join(tmp, "нет"))
            raise AssertionError("отсутствующий волт должен быть отказом")
        except ВолтНеПрочитан:
            pass
    print("vault_drift self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="дрейф проекции волта против реестра")
    ap.add_argument("--check", action="store_true", help="сверить и напечатать")
    ap.add_argument("--strict", action="store_true",
                    help="неперенесённые правки — тоже расхождение (после Т2.8)")
    ap.add_argument("--vault", default=li.VAULT)
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if not a.check:
        ap.error("нужен --check")
    try:
        con = только_чтение(a.root)
        итог, замечания = проверить(con, a.vault)
    except (ВолтНеПрочитан, sqlite3.OperationalError) as e:
        print("vault_drift: %s" % e, file=sys.stderr)
        return 2
    print(строка(итог))
    for _, з in замечания:
        print("  " + з)
    return 1 if расхождение(итог, a.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
