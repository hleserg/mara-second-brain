#!/usr/bin/env python3
"""Детектор расхождений проекции и реестра (Т2.6, ТЗ §4.8: «drift detector:
ручные изменения выявляются, не затираются молча»).

Только читает. Сравнивает то, что лежит в волте (карточки `kb/commitments`,
`kb/conversations`), с тем, что реестр о них помнит (`projections`,
`commitments`/`conversations`, `evidence_refs`), и называет каждое
расхождение строкой. Пересборка волта из реестра — следующий слой Т2.6 и
гейт Г4/Т2.8: пока авторитет у волта, пересборка стирала бы правки
владельца, а детектор — нет.

Что считается:

- «изменены после переноса» — файл не тот, что реестр видел последним
  (`projections.content_sha256`): правка рукой, которую ночной перенос ещё
  не забрал. Сегодня это норма между проекцией и переносом; после Т2.8 —
  дрейф. Расхождением считается только с `--strict`.
- «карточек без проекции» — файл есть, строки `projections` нет: никогда
  не переносилась. Тоже норма до ночного переноса, расхождение с `--strict`.
- «проекций без файла» — реестр помнит карточку, файла нет: удалена руками,
  объект остался. Расхождение.
- «проекций без объекта» — строка проекции указывает на объект, которого
  нет. Расхождение.
- «шапка разошлась» — `title`/`status`/`due` в шапке не те, что в строке
  объекта. Расхождение (ту же проверку статуса делает `ledger_import.сверка`,
  здесь — все три поля и в одной сводке с остальным).
- «evidence разошлось» — список `evidence` во фронтматтере обязательства не
  совпадает со строками `evidence_refs` объекта. Расхождение.

    python3 scripts/vault_drift.py --check [--strict] [--vault V --root R]
"""
import os, sys, argparse
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import ledger_import as li

ПОЛЯ = {"commitment": ("title", "status", "due"), "conversation": ("title",)}
УМОЛЧАНИЯ = {"status": "proposed"}


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
    """→ (счётчики, замечания). Ничего не пишет."""
    итог, замечания = Counter(), []
    на_диске = {}
    for подкаталог, вид, таблица, _ in li.ВИДЫ:
        for rel, fm, sha, текст in li.карточки(vault, подкаталог):
            на_диске[rel] = (вид, таблица, fm, sha)
    итог["карточек"] = len(на_диске)
    проекции = {r["path"]: dict(r) for r in con.execute(
        "select path, object_kind, object_id, content_sha256 from projections")}
    итог["проекций"] = len(проекции)
    for rel, (вид, таблица, fm, sha) in на_диске.items():
        p = проекции.get(rel)
        if p is None:
            итог["карточек без проекции"] += 1
            замечания.append("%s: проекции в реестре нет — ещё не переносилась" % rel)
            continue
        if p["content_sha256"] != sha:
            итог["изменены после переноса"] += 1
            замечания.append("%s: файл изменён после последнего переноса — правка "
                             "рукой, ещё не перенесена" % rel)
        row = con.execute("select * from %s where id=?" % таблица,
                          (p["object_id"],)).fetchone()
        if row is None:
            итог["проекций без объекта"] += 1
            замечания.append("%s: проекция указывает на объект %s, которого нет"
                             % (rel, p["object_id"]))
            continue
        for поле in ПОЛЯ[вид]:
            в_шапке, в_базе = _норм(поле, fm.get(поле)), _норм(поле, row[поле])
            if в_шапке != в_базе:
                итог["шапка разошлась"] += 1
                замечания.append("%s: %s в шапке %r, в реестре %r"
                                 % (rel, поле, в_шапке, в_базе))
        if вид == "commitment":
            в_реестре = {"%s %d-%d" % (r["segment_id"], r["start_ms"], r["end_ms"])
                         for r in con.execute(
                             "select segment_id, start_ms, end_ms from evidence_refs "
                             "where object_kind='commitment' and object_id=? "
                             "and segment_id is not null", (row["id"],))}
            в_шапке = _evidence_шапки(fm)
            if в_шапке != в_реестре:
                итог["evidence разошлось"] += 1
                замечания.append("%s: evidence в шапке %d, в реестре %d, общих %d"
                                 % (rel, len(в_шапке), len(в_реестре),
                                    len(в_шапке & в_реестре)))
    for rel, p in проекции.items():
        if rel not in на_диске:
            итог["проекций без файла"] += 1
            замечания.append("%s: реестр помнит карточку, файла нет — удалена "
                             "руками, объект %s остался" % (rel, p["object_id"]))
    return итог, замечания


РАСХОЖДЕНИЯ = ("проекций без файла", "проекций без объекта", "шапка разошлась",
               "evidence разошлось")
СТРОГО = ("изменены после переноса", "карточек без проекции")


def расхождение(итог, strict=False):
    return any(итог[k] for k in РАСХОЖДЕНИЯ + (СТРОГО if strict else ()))


def строка(итог):
    return "дрейф проекции: " + ", ".join(
        "%s %d" % (k, итог[k]) for k in ("карточек", "проекций") + СТРОГО + РАСХОЖДЕНИЯ)


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
    con = mi.connect(a.root)
    итог, замечания = проверить(con, a.vault)
    print(строка(итог))
    for з in замечания:
        print("  " + з)
    return 1 if расхождение(итог, a.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
