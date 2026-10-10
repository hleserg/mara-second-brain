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
Memory); извлечение, переделанное после проекции; строка журнала,
поправленная рукой после переноса (в `corrections` остаются обе — прежняя
и новая, пересборка рисует две строки); сущность, заведённая после проекции
(индекс `_system/entity-index.json` — не реестр: «Люди:» линкуется иначе, а
`content_sha256` разговора — хеш тела на момент проекции); поднятый
`PIPELINE_VERSION` (строка `pipeline_version:` у всех карточек; какой
версией нарисована — `projections.projector_version`). Это не ошибки
пересборки, а сведения о волте — их и печатает сухой прогон.

Для `--into` живой волт не обязателен: нет его — сравнивать не с чем
(«не сравнивалось»), а имена в «Люди:» остаются без ссылок, пока
`entity-link.py` не догонит их по индексу сущностей восстановленного волта.

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
import call_extract as ce
import vault_drift as vd
from vault_common import canon_map, scrub, yaml_str

def _кавычки(v):
    """Строка шапки обратно в `yaml_str`: разбор (`mara-brief.скаляр`)
    снимает кавычки и экранирования, `yaml_str` ставит их заново — круг
    замкнут и для кавычки внутри, и для кавычки в конце заголовка (Codex
    по #125, круги 1 и 3)."""
    return yaml_str(v)


def _как_есть(v):
    return v


# Поля шапки, которые берутся из строки объекта, а не из извлечения, и в
# каком виде они стоят в шапке: всё, что перенос кладёт в строку (`ВИДЫ`
# `ledger_import`) и что правки (`_поправить`) или рука могли сдвинуть —
# реестр авторитет для них, а не извлечение на диске (Codex по #125: иначе
# заголовок, поправленный рукой и перенесённый, пересборка откатывала бы к
# извлечению; круг 2 — то же про `occurred`). `source_id`/`origin` — ключи
# строки, не поля.
ИЗ_СТРОКИ = {
    "commitment": (("title", _кавычки), ("created", _как_есть), ("occurred", _как_есть),
                   ("status", _как_есть), ("owner", _как_есть), ("promised_to", _как_есть),
                   ("due", _как_есть), ("due_explicit", _как_есть), ("valid_from", _как_есть),
                   ("confidence", lambda v: "%.2f" % float(v)), ("supersedes", _кавычки),
                   ("classification", _как_есть), ("extractor", _как_есть),
                   ("prompt_version", _как_есть), ("extraction_id", _как_есть)),
    "conversation": (("title", _кавычки), ("created", _как_есть), ("occurred", _как_есть),
                     ("valid_from", _как_есть), ("classification", _как_есть),
                     ("extraction_id", _как_есть))}
СОСТОЯНИЯ = ("совпало", "разошлось", "без файла", "без источника", "не сравнивалось")


class НеПересобрать(RuntimeError):
    """У проекции нет источника в реестре и блобах."""


def _шапка_из_строки(text, row, поля):
    """Строки шапки — как в реестре: есть значение — заменить на месте или
    дописать в конец шапки (так же дописывает `_поправить`), нет — убрать."""
    for поле, вид in поля:
        v = row[поле]
        if v is None:
            head, sep, tail = text.partition("\n---\n")
            text = "\n".join(l for l in head.split("\n")
                             if not l.startswith(поле + ":")) + sep + tail
        else:
            text = cp._шапка(text, **{поле: вид(str(v))})
    return text


def _журнал(con, oid):
    """Строки «Правки:» из `corrections` — в том виде, в каком их пишет
    `call_project._поправить`: одна строка журнала на правку, переходы
    статуса и срока, затем заметка. Отметки переноса (`actor_type`
    `import`) — не журнал.

    Строки реестра идут по одной на поле, и в строку журнала они
    собираются по соседству (`rowid` — порядок записи, он же порядок строк
    в файле: журнал только дописывается). Граница строки — не догадка по
    ключу `(время, автор, событие)` (две строки рукой в одну минуту
    сливались бы — ревью PR #125 и Codex, круг 3), а точная проверка: id
    строки реестра перенос считает от номера строки журнала, её текста и
    поля (`ledger_import._id_правки`), и пересборка пересчитывает его для
    строки «как если бы этот переход был в текущей» — совпал, значит он
    оттуда; иначе переход открывает новую строку. Строка, поправленная рукой
    после переноса, в реестре остаётся обеими — пересборка рисует обе."""
    rows = con.execute(
        "select id, field, old_json, new_json, actor_id, origin_event, occurred, reason "
        "from corrections where object_kind='commitment' and object_id=? and "
        "actor_type='human' order by rowid", (oid,)).fetchall()

    def текст(с):
        когда, кто, событие = с["ключ"]
        заметка = с["заметка"] if с["заметка"] is not None else с["хвост"]
        части = с["переходы"] + ([заметка] if заметка else [])
        адрес = "%s, %s" % (когда[:16], кто) + (", correction/%s" % событие if событие else "")
        return "- %s: %s" % (адрес, "; ".join(части))

    def с_переходом(с, r):
        с = {"ключ": с["ключ"], "переходы": list(с["переходы"]), "заметка": с["заметка"],
             "хвост": с["хвост"]}
        если = json.loads(r["new_json"]) if r["new_json"] else None
        было = json.loads(r["old_json"]) if r["old_json"] else None
        if r["field"] == "status":
            с["переходы"].append("статус %s → %s" % (было or "?", если))
        elif r["field"] == "due":
            с["переходы"].append("срок %s → %s" % (было or "не был", если))
        else:
            с["заметка"] = если
        _, _, хвост = (r["reason"] or "").partition(": ")
        с["хвост"] = с["хвост"] or хвост
        return с

    def своя(с, r):
        """Строка реестра `r` — из строки журнала с текстом `текст(с)`? Номер
        строки журнала ищется по id, а не берётся числом уже нарисованных
        строк: после строки, поправленной рукой (в реестре остаются обе,
        старая и новая, с одним номером), счёт нарисованных убегает вперёд
        (Codex по #125, круг 4). Номеров не больше, чем строк реестра."""
        raw = текст(с)
        return any(li._id_правки(oid, "%d\x00%s\x00%s" % (n, raw, r["field"])) == r["id"]
                   for n in range(len(rows) + 1))

    строки = []
    for r in rows:
        ключ = (r["occurred"], r["actor_id"], r["origin_event"])
        # строка `note` — всегда своя строка журнала: перенос заводит её
        # только у строки без переходов, а слияние заменяло бы заметку
        # предыдущей (и id второй строки «подтверждал» бы подмену)
        if строки and строки[-1]["ключ"] == ключ and r["field"] != "note":
            кандидат = с_переходом(строки[-1], r)
            if своя(кандидат, r):
                строки[-1] = кандидат
                continue
        строки.append(с_переходом({"ключ": ключ, "переходы": [], "заметка": None,
                                   "хвост": None}, r))
    return [текст(с) for с in строки]


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
    # Ревизия — та, на которую ссылаются карточки звонка (`extraction_id`
    # у всех одна: у разговора и у обязательств), а не последняя: между
    # переизвлечением и перепроекцией они разные, и по последней пересборка
    # показала бы «разошлось» на волте, который реестру соответствует
    # (ревью PR #128). Разговор — тоже: у звонка без обязательств иначе
    # ссылки нет вовсе (Codex, круг 2). Ссылок нет (до миграции 6) или они
    # разные (перепроекция упала на полпути) — последняя из реестра, файл.
    ссылки = {r[0] for r in con.execute(
        "select extraction_id from commitments where origin_event=? union "
        "select extraction_id from conversations where origin_event=?",
        (event_id, event_id))}
    extraction = None
    if len(ссылки) == 1 and None not in ссылки:
        extraction = ce.прочитать_ревизию(con, next(iter(ссылки)))
    try:
        if extraction is None:
            extraction = ce.прочитать_извлечение(con, root, event_id)
    except (OSError, ValueError) as e:
        raise НеПересобрать("извлечение %s не читается: %s"
                            % (os.path.relpath(epath, root), type(e).__name__))
    if extraction is None:
        raise НеПересобрать("извлечения %s нет" % os.path.relpath(epath, root))
    extraction = cp._сверить_с_реестром(con, event_id, extraction)
    # ссылки — из реестра, где они есть: пересобранная карточка показывает
    # принятое реестром, а не список модели
    объекты = {r["source_native_id"]: r["id"] for r in con.execute(
        "select id, source_native_id from commitments where origin_event=?", (event_id,))}
    for native, it in cp._пункты(dict(extraction, event_id=event_id)).items():
        oid = объекты.get(native)
        ссылки = _evidence_из_реестра(con, oid) if oid else []
        # только у однородного списка: у смешанного (первая ссылка — старая,
        # без `segment_id`) проекция рисовала метку по старой, и подмена
        # сдвинула бы её (ревью PR #125)
        if ссылки and all(isinstance(e.get("segment_id"), str) and e.get("segment_id")
                          for e in it.get("evidence") or [] if isinstance(e, dict)):
            it["evidence"] = ссылки
    cards = cp.all_cards(ev, extraction, canon, cp._из_реестра(con),
                         lambda вид, rel, oid, native: пути.get(oid, rel), cp._создан(con))
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
    # заметка — той же нормализацией, что при записи (Codex по #125)
    return cp.карточка_правки(scrub(str(p.get("item") or "").strip()),
                              p.get("due") or None, cp.заметка(p.get("note")),
                              row["created"], {"id": ev["id"], "occurred_at": ev["occurred"]},
                              row["id"])


def _строка(con, вид, oid):
    таблица = "commitments" if вид == "commitment" else "conversations"
    return con.execute("select * from %s where id=?" % таблица, (oid,)).fetchone()


def пересобрать(con, root, vault, сравнивать=True):
    """→ (счётчики, `{rel: (состояние, текст или None, причина)}`). Ничего
    не пишет; волт только читается — ради сравнения и карты сущностей.
    Без волта (`сравнивать=False`, путь `--into` без живого волта) карточки
    рисуются с пустой картой сущностей и помечаются «не сравнивалось»."""
    волт_есть = bool(vault) and any(os.path.isdir(os.path.join(vault, под))
                                    for под, *_ in li.ВИДЫ)
    if сравнивать and not волт_есть:
        raise vd.ВолтНеПрочитан("волт не прочитан: %s — нет каталогов карточек" % vault)
    canon = canon_map(vault) if волт_есть else {}
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
        if not волт_есть:
            итог["не сравнивалось"] += 1
            карточки[rel] = ("не сравнивалось", text, "волта нет — сравнивать не с чем")
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
    """Пересобранные карточки — в пустой каталог. Живой волт и всё внутри
    него — отказ (Г4): подкаталог волта попал бы в синк и в коммит."""
    if vault and os.path.isdir(vault) and os.path.commonpath(
            [os.path.realpath(into), os.path.realpath(vault)]) == os.path.realpath(vault):
        raise RuntimeError("в живой волт пересборка не пишет (Г4/Т2.8): "
                           "укажите пустой каталог вне волта")
    if os.path.exists(into) and (not os.path.isdir(into) or os.listdir(into)):
        raise RuntimeError("%s — не пустой каталог, пересборка пишет только в пустой" % into)
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
        итог, карточки = пересобрать(con, a.root, a.vault, сравнивать=bool(a.check))
        if a.into:
            print("записано карточек: %d → %s" % (записать(карточки, a.into, a.vault), a.into))
    except (vd.ВолтНеПрочитан, sqlite3.OperationalError, RuntimeError, OSError) as e:
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
