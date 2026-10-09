#!/usr/bin/env python3
"""Пересборка карточек волта из реестра (Т2.6, слой 2; ТЗ §4.8: «проекции
пересобираются из ledger детерминированно»).

Каждая проекция (`projections`) рисуется заново тем же проектором
(`call_project`) из того, что помнит реестр и блобы: событие звонка и его
извлечение `extractions/<event>.json`, строка объекта (`commitments` /
`conversations` — статус, срок, `valid_from`, `created`), ссылки
`evidence_refs` (а не список модели: пересобранная карточка показывает то,
что реестр принял), журнал «Правки:» — из `corrections`. Карточка,
заведённая словами владельца, рисуется из события правки.

Сухой прогон (`--check`) только сравнивает: «совпало», «разошлось»
(с `--diff` — построчно), «без файла» (реестр помнит, файла нет), «без
источника» (карточка перенесена из волта, воспроизвести её не из чего —
`source_id` вида `vault:…` или извлечения нет). `--into КАТАЛОГ` пишет
пересобранные карточки в **пустой** каталог — так проверяется, что пустой
волт восстанавливается из реестра и блобов (Т3б.2, учение Т3б.1). В живой
волт пересборка не пишет: пока авторитет у волта (до Т2.8, гейт Г4), она
стёрла бы правки владельца, о которых реестр ещё не знает — их называет
`vault_drift.py`.

Что в проекции **не** воспроизводится из реестра и потому даёт «разошлось»:
правка рукой, не перенесённая `ledger_import.py`; чужие ключи шапки (Basic
Memory); извлечение, переделанное после проекции. Это не ошибки пересборки,
а сведения о волте — их и печатает сухой прогон.

    python3 scripts/vault_rebuild.py --check [--diff] [--vault V --root R]
    python3 scripts/vault_rebuild.py --into /tmp/vault-rebuilt [--vault V --root R]
"""
import os, sys, json, argparse, difflib, sqlite3
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import ledger_import as li
import call_project as cp
import vault_drift as vd
from vault_common import canon_map, scrub

# Поля шапки, которые берутся из строки объекта, а не из извлечения: их
# двигают правки (`_поправить`) и перенос, извлечение о них не знает.
ИЗ_СТРОКИ = {"commitment": ("created", "status", "owner", "promised_to", "due",
                            "due_explicit", "valid_from", "classification"),
             "conversation": ("created", "valid_from", "classification")}
СОСТОЯНИЯ = ("совпало", "разошлось", "без файла", "без источника")


class НеПересобрать(RuntimeError):
    """У проекции нет источника в реестре и блобах."""


def _шапка_из_строки(text, row, поля):
    """Строки шапки — как в реестре: есть значение — заменить на месте или
    дописать в конец шапки (так же дописывает `_поправить`), нет — убрать."""
    for поле in поля:
        v = row[поле]
        if v is None:
            head, sep, tail = text.partition("\n---\n")
            text = "\n".join(l for l in head.split("\n")
                             if not l.startswith(поле + ":")) + sep + tail
        else:
            text = cp._шапка(text, **{поле: v})
    return text


def _журнал(con, oid):
    """Строки «Правки:» из `corrections` — в том виде, в каком их пишет
    `call_project._поправить`: одна строка журнала на правку, переходы
    статуса и срока, затем заметка. Отметки переноса (`actor_type`
    `import`) — не журнал. Правки одной минуты — в порядке записи."""
    rows = con.execute(
        "select field, old_json, new_json, actor_id, origin_event, occurred, reason "
        "from corrections where object_kind='commitment' and object_id=? and "
        "actor_type='human' order by occurred, rowid", (oid,)).fetchall()
    группы = {}
    for r in rows:
        группы.setdefault((r["occurred"], r["actor_id"], r["origin_event"]), []).append(r)
    строки = []
    for (когда, кто, событие), части in группы.items():
        переходы, заметка = [], None
        for r in части:
            если = json.loads(r["new_json"]) if r["new_json"] else None
            было = json.loads(r["old_json"]) if r["old_json"] else None
            if r["field"] == "status":
                переходы.append("статус %s → %s" % (было or "?", если))
            elif r["field"] == "due":
                переходы.append("срок %s → %s" % (было or "не был", если))
            else:
                заметка = если
            _, _, хвост = (r["reason"] or "").partition(": ")
            if хвост and заметка is None:
                заметка = хвост
        части_строки = переходы + ([заметка] if заметка else [])
        адрес = "%s, %s" % (когда[:16], кто) + (", correction/%s" % событие if событие else "")
        строки.append("- %s: %s" % (адрес, "; ".join(части_строки)))
    return строки


def _с_журналом(text, строки):
    if not строки:
        return text
    return text.rstrip("\n") + "\n\nПравки:\n" + "\n".join(строки) + "\n"


def _evidence_из_реестра(con, oid):
    """Ссылки обязательства из `evidence_refs` (производитель `model`) в
    форме пункта извлечения — метка `sNNNN` по `seq` сегмента."""
    return [{"segment": "s%04d" % r["seq"], "segment_id": r["segment_id"],
             "start_ms": r["start_ms"], "end_ms": r["end_ms"]}
            for r in con.execute(
                "select e.segment_id, e.start_ms, e.end_ms, s.seq from evidence_refs e "
                "join transcript_segments s on s.id=e.segment_id where e.object_kind="
                "'commitment' and e.object_id=? and e.producer='model' order by e.id",
                (oid,))]


def _событие(con, event_id):
    try:
        ev = mi.event_row(con, event_id)
    except KeyError:
        raise НеПересобрать("события %s нет в реестре" % event_id)
    blob = con.execute("select audio_until from blobs where sha256=?",
                       (ev["blob_sha256"],)).fetchone()
    if blob:
        ev["payload"]["audio_until"] = blob["audio_until"]
    return ev


def _из_звонка(con, root, event_id, canon, пути):
    """Все карточки одного звонка, как их нарисовал бы проектор сегодня из
    реестра: `{rel: (вид, oid, текст)}`."""
    ev = _событие(con, event_id)
    epath = mi.extraction_path(root, event_id)
    if not os.path.exists(epath):
        raise НеПересобрать("извлечения %s нет" % os.path.relpath(epath, root))
    with open(epath, encoding="utf-8") as fh:
        extraction = json.load(fh)
    extraction = cp._сверить_с_реестром(con, event_id, extraction)
    # ссылки — из реестра, где они есть: пересобранная карточка показывает
    # принятое реестром, а не список модели
    объекты = {r["source_native_id"]: r["id"] for r in con.execute(
        "select id, source_native_id from commitments where origin_event=?", (event_id,))}
    for native, it in cp._пункты(dict(extraction, event_id=event_id)).items():
        oid = объекты.get(native)
        ссылки = _evidence_из_реестра(con, oid) if oid else []
        if ссылки:
            it["evidence"] = ссылки
    ид = cp._из_реестра(con)
    cards = cp.all_cards(ev, extraction, canon, ид,
                         lambda вид, rel, oid, native: пути.get(oid, rel))
    out = {}
    for rel, text in cards:
        вид = li.вид_по_пути(rel)
        if not вид:
            continue                          # карточка человека — не проекция
        fm, _ = cp.context_pack.mb.frontmatter(text)
        out[rel] = (вид[0], fm.get("id"), text)
    return out


def _из_правки(con, row):
    """Карточка, заведённая словами владельца (`_завести`): из события
    правки и строки объекта."""
    ev = _событие(con, row["origin_event"])
    p = ev["payload"]
    return cp.карточка_правки(scrub(str(p.get("item") or "").strip()),
                              p.get("due") or None,
                              scrub(str(p.get("note") or "").strip()) or None,
                              row["created"], {"id": ev["id"], "occurred_at": ev["occurred"]},
                              row["id"])


def _строка(con, вид, oid):
    таблица = "commitments" if вид == "commitment" else "conversations"
    return con.execute("select * from %s where id=?" % таблица, (oid,)).fetchone()


def пересобрать(con, root, vault):
    """→ (счётчики, `{rel: (состояние, текст или None, причина)}`). Ничего
    не пишет; волт только читается — ради сравнения и карты сущностей."""
    if not vault or not any(os.path.isdir(os.path.join(vault, под)) for под, *_ in li.ВИДЫ):
        raise vd.ВолтНеПрочитан("волт не прочитан: %s — нет каталогов карточек" % vault)
    canon = canon_map(vault)
    проекции = [dict(r) for r in con.execute(
        "select path, object_kind, object_id from projections order by path")]
    пути = {p["object_id"]: p["path"] for p in проекции}
    итог, карточки, звонки = Counter(), {}, {}
    for p in проекции:
        rel, вид, oid = p["path"], p["object_kind"], p["object_id"]
        row = _строка(con, вид, oid) if вид in ("commitment", "conversation") else None
        try:
            if row is None:
                raise НеПересобрать("объекта %s нет в реестре" % oid)
            native = row["source_native_id"] or ""
            if native.startswith("correction/"):
                text = _из_правки(con, row)
            elif native.startswith(("call/", "commitment/")) and row["origin_event"]:
                eid = row["origin_event"]
                if eid not in звонки:
                    звонки[eid] = _из_звонка(con, root, eid, canon, пути)
                if rel not in звонки[eid]:
                    raise НеПересобрать("проектор не рисует %s из звонка %s" % (rel, eid))
                text = звонки[eid][rel][2]
            else:
                raise НеПересобрать("перенесена из волта (%s), источника в реестре нет"
                                    % (native or "без source_id"))
            text = _шапка_из_строки(text, row, ИЗ_СТРОКИ[вид])
            if вид == "commitment":
                text = _с_журналом(text, _журнал(con, oid))
        except НеПересобрать as e:
            итог["без источника"] += 1
            карточки[rel] = ("без источника", None, str(e))
            continue
        путь = os.path.join(vault, rel)
        try:
            with open(путь, "rb") as fh:
                было = fh.read().decode("utf-8", "replace")
        except OSError:
            итог["без файла"] += 1
            карточки[rel] = ("без файла", text, "реестр помнит карточку, файла нет")
            continue
        if было == text:
            итог["совпало"] += 1
            карточки[rel] = ("совпало", text, "")
        else:
            итог["разошлось"] += 1
            карточки[rel] = ("разошлось", text, "".join(difflib.unified_diff(
                было.splitlines(True), text.splitlines(True), "волт/" + rel,
                "реестр/" + rel)))
    итог["проекций"] = len(проекции)
    return итог, карточки


def записать(карточки, into, vault=None):
    """Пересобранные карточки — в пустой каталог. Живой волт — отказ (Г4)."""
    if vault and os.path.realpath(into) == os.path.realpath(vault):
        raise RuntimeError("в живой волт пересборка не пишет (Г4/Т2.8): "
                           "укажите пустой каталог")
    if os.path.isdir(into) and os.listdir(into):
        raise RuntimeError("каталог %s не пуст — пересборка пишет только в пустой" % into)
    n = 0
    for rel, (состояние, text, _) in sorted(карточки.items()):
        if text is None:
            continue
        cp._atomic(os.path.join(into, rel), text)
        n += 1
    return n


def расхождение(итог):
    return bool(итог["разошлось"] or итог["без файла"])


def строка(итог):
    return "пересборка: проекций %d, " % итог["проекций"] + ", ".join(
        "%s %d" % (k, итог[k]) for k in СОСТОЯНИЯ)


def self_check():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root, vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(root)
        for под in ("kb/commitments", "kb/conversations", ".git"):
            os.makedirs(os.path.join(vault, под))
        con = mi.connect(root)

        def правка(n, **payload):
            # как contextd: событие правки сначала в реестр, потом карточка
            ev = {"kind": "correction", "source": "mara", "source_id": "c%d" % n,
                  "occurred_at": "2026-09-02T18:0%d:00+03:00" % n, "payload": payload}
            eid, _ = mi.put_event(con, ev)
            return cp.apply_correction(vault, dict(ev, id=eid), con)
        out = правка(1, item="покрасить забор", status="open")
        правка(2, item="покрасить забор", status="done", note="сделано в субботу")
        итог, карточки = пересобрать(con, root, vault)
        assert итог["совпало"] == 1 and not расхождение(итог), (dict(итог), карточки)
        into = os.path.join(tmp, "новый")
        assert записать(карточки, into, vault) == 1
        with open(os.path.join(into, out["created"]), encoding="utf-8") as fh:
            пересобрано = fh.read()
        with open(os.path.join(vault, out["created"]), encoding="utf-8") as fh:
            assert fh.read() == пересобрано, "пустой каталог — байт в байт"
        try:
            записать(карточки, vault, vault)
            raise AssertionError("в живой волт писать нельзя")
        except RuntimeError:
            pass
        with open(os.path.join(vault, out["created"]), "a", encoding="utf-8") as fh:
            fh.write("заметка рукой\n")
        итог, карточки = пересобрать(con, root, vault)
        assert итог["разошлось"] == 1 and расхождение(итог), dict(итог)
    print("vault_rebuild self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="пересборка карточек волта из реестра")
    ap.add_argument("--check", action="store_true", help="сухой прогон: сравнить с волтом")
    ap.add_argument("--diff", action="store_true", help="печатать построчный дифф разошедшихся")
    ap.add_argument("--into", help="записать пересобранные карточки в пустой каталог")
    ap.add_argument("--vault", default=li.VAULT)
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if not a.check and not a.into:
        ap.error("нужен --check или --into КАТАЛОГ")
    try:
        con = vd.только_чтение(a.root)
        итог, карточки = пересобрать(con, a.root, a.vault)
        if a.into:
            print("записано карточек: %d → %s" % (записать(карточки, a.into, a.vault), a.into))
    except (vd.ВолтНеПрочитан, sqlite3.OperationalError, RuntimeError) as e:
        print("vault_rebuild: %s" % e, file=sys.stderr)
        return 2
    print(строка(итог))
    for rel, (состояние, _, причина) in sorted(карточки.items()):
        if состояние == "совпало":
            continue
        print("  %s: %s%s" % (состояние, rel, "" if состояние == "разошлось" else " — " + причина))
        if a.diff and состояние == "разошлось":
            sys.stdout.write("".join("    " + l for l in причина.splitlines(True)))
    return 1 if расхождение(итог) else 0


if __name__ == "__main__":
    raise SystemExit(main())
