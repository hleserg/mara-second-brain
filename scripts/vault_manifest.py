#!/usr/bin/env python3
"""Т2.6, ТЗ §4.8 «manifest с hash проекций» и §5.2 «обновление projection
checkpoint только после успешной записи проекций».

Манифест — `_system/projections.json` в волте: каждая проекция реестра
(`projections`) с хешем содержимого, объектом и версиями (`ledger_version`,
`projector_version`). Его пишет тот же прогон, что пишет карточки, и
только после них: сперва файлы, потом манифест, потом контрольная точка в
реестре — `projections.manifest_hash` у всех строк становится хешем
манифеста. Прерванный между шагами прогон виден по строкам, у которых
`manifest_hash` не равен хешу манифеста из реестра.

Хеш манифеста считается от его содержимого (`projections`), а не от
времени записи: пока проекции те же, хеш тот же, и повторная запись
ничего не меняет. Поэтому `projections.written` в манифест не входит —
перенос обновляет его каждым прогоном, и холостой перенос менял бы хеш,
точку и коммит волта без единой изменённой карточки (ревью).

Пишется под флоком волта (`vault_common.locked`) — тем же, что держат
проектор и правка словами: два писателя без флока могли бы положить
старый манифест поверх нового и поставить точку не туда. Флок берёт
зовущий, не `записать`: правка словами уже держит его, а второй
дескриптор того же процесса ждал бы первого.

Зачем он нужен, когда есть реестр: волт переносим и читается без Мары
(§4.8), и по манифесту его можно сверить без базы — после восстановления,
на зеркале, в git. `--check` ничего не пишет.

Политика находок та же, что у `vault_drift`: правка карточки рукой —
норма дня до Т2.8 и расхождение только с `--strict`; повреждённый или
устаревший манифест, пропавший файл и строки без контрольной точки —
расхождение всегда.
"""
import os, sys, json, hashlib, argparse, sqlite3
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mara_ingest as mi
from vault_common import locked

ПУТЬ = "_system/projections.json"
# расхождение всегда / только с --strict (правка рукой и волт до первой
# проекции). Нечитаемый манифест — порча, не отсутствие: обрезанный или
# битый файл — ровно то, что манифест и должен ловить (Codex по #138)
РАСХОЖДЕНИЯ = ("манифест не читается", "манифест повреждён", "манифест устарел",
               "строк без контрольной точки", "файлов нет")
СТРОГО = ("манифеста нет", "файлов не как в манифесте")
# что в находку сверки: только то, что умеет один манифест. Пропавший файл
# сверка уже называет через `vault_drift` («проекций без файла»), и второй
# раз об одном файле — не находка (ревью)
СВЕРКА = ("манифест не читается", "манифест повреждён", "манифест устарел",
          "строк без контрольной точки")


def собрать(con):
    """Проекции реестра → `{rel: {...}}`, порядок по пути: манифест
    детерминирован, как и сами проекции (§4.8)."""
    out = {}
    for r in con.execute(
            "select path, object_kind, object_id, content_sha256, ledger_version, "
            "projector_version from projections order by path"):
        out[r["path"]] = {"sha256": r["content_sha256"], "object_kind": r["object_kind"],
                          "object_id": r["object_id"], "ledger_version": r["ledger_version"],
                          "projector_version": r["projector_version"]}
    return out


def хеш(проекции):
    """От содержимого, в каноническом виде: порядок ключей и без пробелов."""
    return hashlib.sha256(json.dumps(проекции, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def документ(проекции, когда=None):
    return {"hash": хеш(проекции), "generated": когда or mi.now_iso(),
            "pipeline_version": mi.PIPELINE_VERSION, "count": len(проекции),
            "projections": проекции}


def сохранить(vault, проекции, когда=None):
    """Только файл, атомарно: temp, fsync, rename. Не `mi.write_json` —
    тот заводит каталог 0700 и файл 0600 для блобов, а волт читают синк и
    Obsidian, и `_system` в пересобранном каталоге должен быть как остальные
    (ревью). Для пересборки в пустой каталог: реестр она не трогает."""
    путь = os.path.join(vault, ПУТЬ)
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    tmp = путь + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(документ(проекции, когда), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        # fsync до rename (§5.2): контрольная точка ставится после манифеста,
        # и после сбоя питания файл обязан нести байты, а не только имя
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, путь)
    # и каталог после rename: без этого имя могло бы не пережить сбой, а
    # точка в базе — пережить (Codex по #138)
    fsync_каталога(os.path.dirname(путь))
    return путь


def fsync_каталога(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def тот_же(vault, h):
    """Манифест на диске уже этот (хеш и версия конвейера): не переписывать —
    иначе холостой перенос менял бы `generated`, а с ним синк и коммит волта
    (Codex по #138)."""
    try:
        док = прочитать(vault)
    except (OSError, ValueError):
        return False
    # верхний хеш пересчитывается от содержимого: подменённая строка при
    # нетронутом `hash` иначе осталась бы навсегда (Codex по #138, круг 2)
    return (док["hash"] == h and хеш(док["projections"]) == h
            and док.get("pipeline_version") == mi.PIPELINE_VERSION)


def записать(con, vault, когда=None):
    """Карточки уже на диске → манифест → контрольная точка (§5.2).
    Возвращает хеш. Зовётся из проектора звонка, правки словами и переноса
    — из каждого места, которое меняет `projections`."""
    проекции = собрать(con)
    h = хеш(проекции)
    if not тот_же(vault, h):
        сохранить(vault, проекции, когда)
    with mi.транзакция(con):
        con.execute("update projections set manifest_hash=? where manifest_hash is not ?",
                    (h, h))
    return h


def прочитать(vault):
    """Манифест с диска или исключение: `FileNotFoundError`, `OSError`,
    `ValueError` (не JSON или не той формы)."""
    with open(os.path.join(vault, ПУТЬ), encoding="utf-8") as fh:
        док = json.load(fh)
    if not isinstance(док, dict) or not isinstance(док.get("projections"), dict) \
            or not isinstance(док.get("hash"), str):
        raise ValueError("не манифест проекций")
    return док


def проверить(con, vault):
    """→ (счётчики, замечания `(ключ, текст)`). Ничего не пишет.

    Порядок: есть ли манифест и цел ли он (хеш в файле против его же
    содержимого); свеж ли (хеш против реестра); у всех ли строк реестра
    контрольная точка этого манифеста; каждый ли файл из манифеста на диске
    и тот ли (хеш содержимого)."""
    итог, замечания = Counter(), []

    def заметить(ключ, текст):
        итог[ключ] += 1
        замечания.append((ключ, текст))

    проекции = собрать(con)
    h = хеш(проекции)
    итог["проекций"] = len(проекции)
    try:
        док = прочитать(vault)
    except FileNotFoundError:
        заметить("манифеста нет", "%s: проектор его ещё не писал — напишет следующая "
                 "проекция или `vault_manifest.py --write`" % ПУТЬ)
        return итог, замечания
    except (OSError, ValueError) as e:
        заметить("манифест не читается", "%s: %s" % (ПУТЬ, e.__class__.__name__))
        return итог, замечания
    в_файле = док["projections"]
    итог["в манифесте"] = len(в_файле)
    if док["hash"] != хеш(в_файле):
        заметить("манифест повреждён", "%s: хеш в файле не сходится с его содержимым"
                 % ПУТЬ)
        return итог, замечания
    if док["hash"] != h:
        заметить("манифест устарел", "%s: реестр и манифест разошлись — проекций в реестре "
                 "%d, в манифесте %d; после проекции, правки или переноса сойдутся"
                 % (ПУТЬ, len(проекции), len(в_файле)))
    строк = con.execute("select count(*) from projections where manifest_hash is not ?",
                        (h,)).fetchone()[0]
    if строк:
        заметить("строк без контрольной точки",
                 "%d проекций в реестре без контрольной точки текущего манифеста — "
                 "прогон проектора прерван между записью и точкой (§5.2)" % строк)
    for rel, з in sorted(в_файле.items()):
        try:
            with open(os.path.join(vault, rel), "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            заметить("файлов нет", "%s: в манифесте есть, на диске нет" % rel)
            continue
        if sha != (з.get("sha256") if isinstance(з, dict) else None):
            заметить("файлов не как в манифесте",
                     "%s: файл изменён после записи манифеста — правка рукой или "
                     "проекция новее манифеста" % rel)
    return итог, замечания


def расхождение(итог, strict=False):
    return any(итог[k] for k in РАСХОЖДЕНИЯ + (СТРОГО if strict else ()))


def строка(итог):
    return "манифест проекций: в реестре %d, в манифесте %d, " % (
        итог["проекций"], итог["в манифесте"]) + ", ".join(
        "%s %d" % (k, итог[k]) for k in РАСХОЖДЕНИЯ + СТРОГО if итог[k])


def только_чтение(root):
    con = sqlite3.connect("file:%s?mode=ro" % os.path.join(root, "contextd.db"), uri=True)
    con.row_factory = sqlite3.Row
    return con


def self_check():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root, vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(root)
        os.makedirs(os.path.join(vault, "kb/commitments"))
        con = mi.connect(root)
        итог, зам = проверить(con, vault)
        assert итог["манифеста нет"] == 1 and not расхождение(итог) and расхождение(итог, True)
        rel = "kb/commitments/2026-09-03-smeta.md"
        текст = "---\ntitle: прислать смету\n---\n"
        with open(os.path.join(vault, rel), "w", encoding="utf-8") as fh:
            fh.write(текст)
        sha = hashlib.sha256(текст.encode()).hexdigest()
        con.execute("insert into projections(path, object_kind, object_id, content_sha256, "
                    "written) values(?,?,?,?,?)", (rel, "commitment", "c1", sha, mi.now_iso()))
        h = записать(con, vault)
        assert h == записать(con, vault), "повторная запись — тот же хеш"
        итог, зам = проверить(con, vault)
        assert not зам and not расхождение(итог, True), (dict(итог), зам)
        assert con.execute("select manifest_hash from projections").fetchone()[0] == h
        with open(os.path.join(vault, rel), "a", encoding="utf-8") as fh:
            fh.write("\nправка рукой\n")
        итог, _ = проверить(con, vault)
        assert итог["файлов не как в манифесте"] == 1 and not расхождение(итог) \
            and расхождение(итог, True), dict(итог)
        con.execute("update projections set content_sha256='x'")
        итог, _ = проверить(con, vault)
        assert итог["манифест устарел"] == 1 and итог["строк без контрольной точки"] == 1 \
            and расхождение(итог), dict(итог)
        записать(con, vault)
        os.remove(os.path.join(vault, rel))
        итог, _ = проверить(con, vault)
        assert итог["файлов нет"] == 1 and расхождение(итог), dict(итог)
        with open(os.path.join(vault, ПУТЬ), "r+", encoding="utf-8") as fh:
            док = json.load(fh)
            док["projections"][rel]["sha256"] = "подделка"
            fh.seek(0); fh.truncate(); json.dump(док, fh)
        итог, _ = проверить(con, vault)
        assert итог["манифест повреждён"] == 1 and расхождение(итог), dict(итог)
        with open(os.path.join(vault, ПУТЬ), "w") as fh:
            fh.write("{")
        итог, _ = проверить(con, vault)
        assert итог["манифест не читается"] == 1 and расхождение(итог), dict(итог)
    print("vault_manifest self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="манифест проекций волта: хеши, свежесть, "
                                             "контрольная точка")
    ap.add_argument("--check", action="store_true", help="сверить манифест, реестр и файлы")
    ap.add_argument("--strict", action="store_true",
                    help="правка рукой и отсутствие манифеста — тоже расхождение")
    ap.add_argument("--write", action="store_true",
                    help="записать манифест из реестра и обновить контрольную точку")
    ap.add_argument("--vault", default=os.environ.get("MARA_VAULT",
                                                      os.environ.get("VAULT", "/srv/vault")))
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if not a.check and not a.write:
        ap.error("нужен --check или --write")
    # опечатка в --vault или --root — отказ, а не «манифеста нет» и не новая
    # пустая база с пустым манифестом поверх живого (ревью)
    if not os.path.isdir(a.vault):
        print("vault_manifest: волт не прочитан: %s — нет каталога" % a.vault, file=sys.stderr)
        return 2
    if not os.path.exists(os.path.join(a.root, "contextd.db")):
        print("vault_manifest: базы нет: %s" % os.path.join(a.root, "contextd.db"),
              file=sys.stderr)
        return 2
    if a.write:
        with locked(a.vault):
            h = записать(mi.connect(a.root), a.vault)
        print("манифест записан: %s %s" % (os.path.join(a.vault, ПУТЬ), h[:12]))
        return 0
    try:
        итог, замечания = проверить(только_чтение(a.root), a.vault)
    except sqlite3.OperationalError as e:
        print("vault_manifest: %s" % e, file=sys.stderr)
        return 2
    print(строка(итог))
    for _, з in замечания:
        print("  " + з)
    return 1 if расхождение(итог, a.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
