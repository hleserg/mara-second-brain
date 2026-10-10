#!/usr/bin/env python3
"""Проверка после восстановления (ТЗ §5.3 шаги 5, 7, 8; §17.3 п.6; план
Т3б.2): «после restore проекции rebuild, blobs reconcile, sample evidence
открывается».

Запускается на восстановленном корне блобов (`--root`) и волте из git
(`--vault`), до возврата сервиса (шаг 9 ранбука `docs/backup-core.md`).
Корень может быть и чистым каталогом учения: пути в `blobs.path`
абсолютные, с живого корня, и файл ищется под `--root` по хвосту
`calls/…` — иначе проверка на чистом каталоге смотрела бы в живой
(ревью). Ничего не пишет. Проверки, каждая — строкой отчёта с ключом:

- «целостность» — `integrity_check` (полный, не `quick_check`: раз в
  восстановление можно) и `foreign_key_check`; версия схемы против кода:
  старее — сначала `mara_ingest.py --migrate` (шаг 4), новее — код откачен.
  Схема не той версии — дальше не идём: остальным разделам нужны таблицы
  этой версии, и вместо подсказки они падали бы (ревью).
- «блобы» — строки `blobs` без `purged_at` против диска: файла нет, размер
  не тот, хеш не тот (хеш — у выборки `--sample`, или у всех с `--full`:
  аудио на годы — часы чтения); файлы в `calls/` без строки — осиротевшие,
  их не трогаем (единственная копия разговора стирается только по ретеншену
  или команде, как в сверке).
- «проекции» — `vault_rebuild.пересобрать`: сколько карточек реестр рисует
  байт в байт с волтом из git, сколько разошлось (правки рукой, не
  перенесённые до копии — ожидаемо после восстановления, смотреть
  `vault_rebuild.py --check --diff`), без файла, без источника. «Без
  источника» у карточки звонка или правки (ключ `call/…`, `commitment/…`,
  `correction/…`) — поломка: извлечение или событие были в копии и обязаны
  быть на месте; у перенесённой из волта (`vault:…`) источника в реестре
  нет по построению (Codex по #126).
- «id» — у каждой проекции с файлом `id:` в шапке равен ключу строки
  объекта, `source_id` — её `source_native_id` (ADR-0002: стабильный id
  пережил восстановление); пустой `source_id` допустим только у
  перенесённой из волта (`vault:…` — её ключ и есть путь), у карточки
  звонка или правки без него следующий перенос завёл бы объект заново.
- «evidence» — выборка обязательств со ссылками `evidence_refs`: сегмент и
  расшифровка на месте, интервал в границах сегмента, аудио — по хешу
  **расшифровки** (`transcripts.blob_sha256`: цепочка происхождения —
  сегмент → расшифровка → блоб; хеш, не равный хешу события, — поломка, а
  не чужое аудио «открылось»; Codex по #126) на диске, или стёрто по
  ретеншену — тогда ссылка ведёт в сегмент, не в файл, и это не поломка.
  «Открывается» значит: по ссылке находится файл и миллисекунды в нём,
  больше проверка ничего не слушает.

Код выхода: 0 — всё сошлось, 1 — есть расхождения, 2 — проверить нельзя
(база не открылась, волт не прочитан).

    python3 scripts/restore_check.py --root /srv/mara-blobs --vault /srv/vault
    python3 scripts/restore_check.py --root … --vault … --full --sample 10
"""
import os, sys, glob, json, hashlib, argparse, random, sqlite3
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import ledger_import as li
import vault_drift as vd
import vault_rebuild as vr
import context_pack

ВЫБОРКА = 3
ИЗ_РЕЕСТРА = ("call/", "commitment/", "correction/")   # ключи карточек проектора и правок


def _ключ_объекта(con, вид, oid):
    таблица = {"commitment": "commitments", "conversation": "conversations"}.get(вид)
    row = таблица and con.execute("select source_native_id from %s where id=?" % таблица,
                                  (oid,)).fetchone()
    return row["source_native_id"] if row else None


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for кусок in iter(lambda: fh.read(1 << 20), b""):
            h.update(кусок)
    return h.hexdigest()


def _на_корне(path, root):
    """Путь блоба на этом корне: строка несёт абсолютный путь живого корня,
    у копии на чистом каталоге файл лежит под `root` по тому же хвосту
    `calls/ГГГГ/ММ/имя` (`mi.blob_path`)."""
    if not path:
        return path
    корень = os.path.realpath(root)
    if os.path.realpath(path).startswith(корень + os.sep):
        return path
    голова, разд, хвост = path.replace(os.sep, "/").rpartition("/calls/")
    return os.path.join(root, "calls", *хвост.split("/")) if разд else path


def целостность(con):
    out = []
    try:
        итог = [r[0] for r in con.execute("pragma integrity_check").fetchall()]
    except sqlite3.DatabaseError as e:
        итог = ["%s: %s" % (type(e).__name__, e)]
    if итог != ["ok"]:
        out.append(("целостность", "integrity_check: %s" % "; ".join(итог[:3])))
        return out                    # битая база: дальше спрашивать нечего
    fk = con.execute("pragma foreign_key_check").fetchall()
    if fk:
        out.append(("целостность", "foreign_key_check: %d нарушений, первое в %s"
                    % (len(fk), fk[0][0])))
    v = con.execute("pragma user_version").fetchone()[0]
    if v < mi.ВЕРСИЯ:
        out.append(("целостность", "схема версии %d, код ждёт %d — сначала `%s` (шаг 4)"
                    % (v, mi.ВЕРСИЯ, mi.КОМАНДА)))
    elif v > mi.ВЕРСИЯ:
        out.append(("целостность", "схема версии %d новее кода (%d) — код откачен без базы"
                    % (v, mi.ВЕРСИЯ)))
    return out


def блобы(con, root, выборка, полностью, rnd):
    """→ (счётчики, замечания). Хеш — у выборки или у всех."""
    итог, out = Counter(), []
    строки = [dict(r) for r in con.execute(
        "select sha256, path, bytes from blobs where purged_at is null order by sha256")]
    итог["строк"] = len(строки)
    на_месте = []
    for r in строки:
        p = r["path"] = _на_корне(r["path"], root)
        if not p or not os.path.isfile(p):
            итог["без файла"] += 1
            out.append(("блобы", "%s: файла нет — %s" % (r["sha256"][:12], p)))
            continue
        if r["bytes"] is not None and os.path.getsize(p) != r["bytes"]:
            итог["размер не тот"] += 1
            out.append(("блобы", "%s: размер %d, в реестре %d"
                        % (r["sha256"][:12], os.path.getsize(p), r["bytes"])))
            continue
        на_месте.append(r)
    проверить = на_месте if полностью else rnd.sample(на_месте, min(выборка, len(на_месте)))
    for r in проверить:
        if _sha(r["path"]) != r["sha256"]:
            итог["хеш не тот"] += 1
            out.append(("блобы", "%s: хеш файла не тот — %s" % (r["sha256"][:12], r["path"])))
    итог["хеш сверен"] = len(проверить)
    известные = {os.path.realpath(r["path"]) for r in строки if r["path"]}
    известные |= {os.path.realpath(_на_корне(r[0], root)) for r in con.execute(
        "select path from blobs where purged_at is not null and path is not null")}
    for p in glob.glob(os.path.join(root, "calls", "**", "*"), recursive=True):
        if os.path.isfile(p) and os.path.realpath(p) not in известные:
            итог["без строки"] += 1
    if итог["без строки"]:
        out.append(("блобы", "файлов в calls/ без строки blobs: %d — не трогаем, см. сверку"
                    % итог["без строки"]))
    return итог, out


def стабильные_id(con, vault):
    итог, out = Counter(), []
    for p in con.execute("select path, object_kind, object_id from projections order by path"):
        путь = os.path.join(vault, p["path"])
        if not os.path.isfile(путь):
            continue
        with open(путь, "rb") as fh:
            fm, _ = context_pack.mb.frontmatter(
                fh.read().decode("utf-8", "replace").lstrip("﻿").replace("\r\n", "\n"))
        итог["проверено"] += 1
        ключ = _ключ_объекта(con, p["object_kind"], p["object_id"])
        if ключ is None:
            # у `projections.object_id` внешнего ключа нет (`mara_ingest`):
            # висящую проекцию `foreign_key_check` не увидит (Codex по #126)
            итог["объекта нет"] += 1
            out.append(("id", "%s: объекта %s нет в реестре" % (p["path"], p["object_id"])))
            continue
        # у перенесённой из волта (`vault:…`) `id:` появляется только после
        # `ledger_import --write-ids`: пусто — не расхождение, объект находится
        # по ключу-пути; чужое значение — расхождение у любой
        в_шапке_id = (li._строка(fm.get("id")) or "").strip()
        if в_шапке_id != p["object_id"] and not (в_шапке_id == "" and ключ.startswith("vault:")):
            итог["id не тот"] += 1
            out.append(("id", "%s: в шапке id %r, в реестре %s"
                        % (p["path"], fm.get("id"), p["object_id"])))
            continue
        в_шапке = (li._строка(fm.get("source_id")) or "").strip()
        # пусто допустимо только у перенесённой из волта: её ключ — путь
        if ключ and в_шапке != ключ and not (в_шапке == "" and ключ.startswith("vault:")):
            итог["source_id не тот"] += 1
            out.append(("id", "%s: source_id в шапке %r, ключ строки %s"
                        % (p["path"], fm.get("source_id"), ключ)))
    return итог, out


def образец_evidence(con, root, выборка, rnd):
    """Выборка обязательств со ссылками производителя `model`: ссылка
    разрешается в сегмент, расшифровку и файл аудио события."""
    итог, out = Counter(), []
    # Сплошные проверки до выборки: внешних ключей у `evidence_refs.object_id`
    # нет, а `segment_id` у аудио-ссылки обязателен (ADR-0004 п.1), но
    # схемой не вынужден — такие строки ни выборка, ни `foreign_key_check`
    # не увидели бы (Codex по #126, круг 5)
    без_объекта = con.execute(
        "select count(*) from evidence_refs e left join commitments c on c.id=e.object_id "
        "where e.object_kind='commitment' and c.id is null").fetchone()[0]
    if без_объекта:
        итог["не открывается"] += без_объекта
        out.append(("evidence", "ссылок без объекта в реестре: %d" % без_объекта))
    без_сегмента = con.execute(
        "select count(*) from evidence_refs where kind='audio' and segment_id is null"
    ).fetchone()[0]
    if без_сегмента:
        итог["не открывается"] += без_сегмента
        out.append(("evidence", "аудио-ссылок без сегмента: %d" % без_сегмента))
    объекты = [r[0] for r in con.execute(
        "select distinct e.object_id from evidence_refs e join commitments c on c.id=e.object_id "
        "where e.object_kind='commitment' and e.producer='model' and e.segment_id is not null "
        "order by e.object_id")]
    итог["со ссылками"] = len(объекты)
    for oid in rnd.sample(объекты, min(выборка, len(объекты))):
        for e in con.execute(
                "select e.id, e.segment_id, e.start_ms, e.end_ms, s.start_ms as a, s.end_ms as b, "
                "s.text, t.event_id, t.blob_sha256, ev.blob_sha256 as у_события, "
                "b.path, b.purged_at "
                "from evidence_refs e left join transcript_segments s on s.id=e.segment_id "
                "left join transcripts t on t.id=s.transcript_id "
                "left join events ev on ev.id=t.event_id "
                "left join blobs b on b.sha256=t.blob_sha256 "
                "where e.object_kind='commitment' and e.object_id=? and e.producer='model' "
                "and e.segment_id is not null", (oid,)):
            итог["ссылок"] += 1
            if e["a"] is None:
                итог["не открывается"] += 1
                out.append(("evidence", "%s: сегмента %s нет в реестре" % (oid[-8:], e["segment_id"])))
            elif not (e["a"] <= (e["start_ms"] if e["start_ms"] is not None else e["a"])
                      <= (e["end_ms"] if e["end_ms"] is not None else e["b"]) <= e["b"]):
                итог["не открывается"] += 1
                out.append(("evidence", "%s: интервал %s–%s вне сегмента %s–%s"
                            % (oid[-8:], e["start_ms"], e["end_ms"], e["a"], e["b"])))
            elif e["event_id"] is None or e["blob_sha256"] is None:
                итог["не открывается"] += 1
                out.append(("evidence", "%s: у расшифровки нет события или хеша аудио"
                            % oid[-8:]))
            elif e["blob_sha256"] != e["у_события"]:
                итог["не открывается"] += 1
                out.append(("evidence", "%s: хеш аудио расшифровки %s не тот, что у события %s"
                            % (oid[-8:], e["blob_sha256"][:12], (e["у_события"] or "")[:12])))
            elif e["purged_at"]:
                итог["аудио стёрто по ретеншену"] += 1
            elif not (e["path"] and os.path.isfile(_на_корне(e["path"], root))):
                итог["не открывается"] += 1
                out.append(("evidence", "%s: аудио %s не на диске — %s"
                            % (oid[-8:], (e["blob_sha256"] or "")[:12],
                               _на_корне(e["path"], root))))
            else:
                итог["открывается"] += 1
    return итог, out


def проверить(con, root, vault, выборка=ВЫБОРКА, полностью=False, seed=None):
    """→ (сводка по разделам, замечания `(раздел, текст)`)."""
    rnd = random.Random(seed)
    сводка = {"блобы": Counter(), "проекции": Counter(), "id": Counter(),
              "evidence": Counter(), "прервано": False}
    замечания = целостность(con)
    if замечания:
        # битая база или схема не той версии: таблиц этой версии может не
        # быть, и разделы ниже падали бы вместо подсказки
        сводка["прервано"] = True
        return сводка, замечания
    сводка["блобы"], з = блобы(con, root, выборка, полностью, rnd)
    замечания += з
    итог, карточки = vr.пересобрать(con, root, vault)
    сводка["проекции"] = итог
    объекты = {r["path"]: (r["object_kind"], r["object_id"]) for r in con.execute(
        "select path, object_kind, object_id from projections")}
    for rel, (состояние, _, причина) in sorted(карточки.items()):
        if состояние in ("разошлось", "без файла"):
            замечания.append(("проекции", "%s: %s" % (состояние, rel)))
        elif состояние == "без источника":
            ключ = _ключ_объекта(con, *объекты.get(rel, (None, None)))
            if ключ is None or ключ.startswith(ИЗ_РЕЕСТРА):
                # объекта нет вовсе — тоже поломка, не «перенесённая из волта»
                # карточку звонка или правки реестр обязан уметь нарисовать:
                # извлечение и событие были в копии (Codex по #126)
                итог["без источника реестра"] += 1
                замечания.append(("проекции", "без источника у карточки из реестра: %s — %s"
                                  % (rel, причина)))
    сводка["id"], з = стабильные_id(con, vault)
    замечания += з
    сводка["evidence"], з = образец_evidence(con, root, выборка, rnd)
    замечания += з
    return сводка, замечания


def расхождение(сводка, замечания):
    return any(з[0] in ("целостность", "блобы", "id", "evidence") for з in замечания) \
        or bool(сводка["проекции"]["без файла"] or сводка["проекции"]["без источника реестра"])


def строки_сводки(сводка):
    if сводка["прервано"]:
        return ["проверка прервана на целостности: сначала починить базу или схему"]
    б, п, и, e = сводка["блобы"], сводка["проекции"], сводка["id"], сводка["evidence"]
    return [
        "блобы: строк %d, без файла %d, размер не тот %d, хеш сверен %d, хеш не тот %d, "
        "файлов без строки %d" % (б["строк"], б["без файла"], б["размер не тот"],
                                  б["хеш сверен"], б["хеш не тот"], б["без строки"]),
        vr.строка(п).replace("пересборка", "проекции")
        + ", без источника у карточек из реестра %d" % п["без источника реестра"],
        "id: проверено %d, id не тот %d, source_id не тот %d, объекта нет %d"
        % (и["проверено"], и["id не тот"], и["source_id не тот"], и["объекта нет"]),
        "evidence: обязательств со ссылками %d, в выборке ссылок %d, открывается %d, "
        "аудио стёрто по ретеншену %d, не открывается %d"
        % (e["со ссылками"], e["ссылок"], e["открывается"], e["аудио стёрто по ретеншену"],
           e["не открывается"])]


def self_check():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root, vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(root)
        for под in ("kb/commitments", "kb/conversations", ".git"):
            os.makedirs(os.path.join(vault, под))
        con = mi.connect(root)
        тело = b"audio"
        s = hashlib.sha256(тело).hexdigest()
        p = mi.blob_path(root, s, "wav")
        os.makedirs(os.path.dirname(p))
        with open(p, "wb") as fh:
            fh.write(тело)
        con.execute("insert into blobs(sha256,path,bytes,mime,created) values(?,?,?,?,?)",
                    (s, p, len(тело), "audio/wav", mi.now_iso()))
        сводка, з = проверить(con, root, vault)
        assert not з and not расхождение(сводка, з), (сводка, з)
        with open(p, "ab") as fh:
            fh.write(b"x")
        сводка, з = проверить(con, root, vault)
        assert сводка["блобы"]["размер не тот"] == 1 and расхождение(сводка, з), сводка
        con.close()
        mi.migrate(root, mi.ВЕРСИЯ - 1).close()       # настоящая старая копия
        сводка, з = проверить(vd.только_чтение(root), root, vault)
        assert сводка["прервано"] and any("схема версии" in т for _, т in з), з
    print("restore_check self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="проверка после восстановления (ТЗ §5.3)")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--vault", default=li.VAULT)
    ap.add_argument("--sample", type=int, default=ВЫБОРКА,
                    help="сколько блобов и обязательств сверять выборочно")
    ap.add_argument("--full", action="store_true", help="хеш у всех блобов, не у выборки")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    try:
        con = vd.только_чтение(a.root)
        сводка, замечания = проверить(con, a.root, a.vault, a.sample, a.full)
    except (vd.ВолтНеПрочитан, sqlite3.DatabaseError, RuntimeError, OSError,
            ValueError) as e:
        print("restore_check: %s" % e, file=sys.stderr)
        return 2
    for s in строки_сводки(сводка):
        print(s)
    for раздел, текст in замечания:
        print("  %s: %s" % (раздел, текст))
    плохо = расхождение(сводка, замечания)
    print("итог: %s" % ("расхождения есть" if плохо else "сошлось"))
    return 1 if плохо else 0


if __name__ == "__main__":
    raise SystemExit(main())
