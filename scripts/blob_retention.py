#!/usr/bin/env python3
"""Уборка просроченного аудио (ТЗ §7) и сырых логов источников.

Запись живёт девяносто дней и исчезает сама. Манифест не исчезает никогда: по
нему потом видно, что звонок был, сколько длился и куда делась запись. Поэтому
файл удаляется, а в манифест дописывается `purged` — это единственная правка
неизменяемого документа, и она добавляет, а не переписывает.

`pin: 1` отменяет удаление навсегда: владелец сам решил, что эта запись нужна.

Сырые обновления TDLib и письма Gmail (`<источник>/raw/ДАТА.jsonl`) — отладка,
через месяц они ничего не объяснят, а держать переписку в двух местах незачем.
Срок — `MARA_RAW_DAYS`, по умолчанию тридцать дней; в ТЗ этого числа
нет, месяц выбран здесь и по причине выше.

    python3 scripts/blob_retention.py            # прогон
    python3 scripts/blob_retention.py --dry-run  # только показать
    python3 scripts/blob_retention.py --self-check
"""
import os, sys, json, glob, math, argparse
from datetime import datetime, date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi


ОШИБКА_КОНФИГА = None
# Сто лет. Выше по-настоящему нельзя: около 740 тысяч суток назад лежит
# `date.min`, и вычитание за него бросает `OverflowError`. Точное число
# здесь не пишем: оно растёт на единицу каждые сутки. Ниже незачем — всё, что
# больше этого, значит одно и то же: «не убирать».
ПОТОЛОК = 36500
try:
    СРОК = float(os.environ.get("MARA_RAW_DAYS", 30))
except ValueError:
    # `float`, а не `int`, и это не косметика. `int` не берёт ни `45.0`, ни
    # `1e6`, ни `inf` — все три уезжали сюда, в «беру 30». Первая запись
    # подменяла полтора месяца месяцем, две другие означают «не убирать» и
    # получали месяц, то есть теряли ровно то, что владелец просил хранить.
    # `float` их различает и раздаёт по веткам ниже; здесь остаётся только
    # то, что и вправду не число. `OverflowError` ловить не нужно: `float`
    # от строки его не бросает (`1e400` даёт `inf`), а `int` от непроверенной
    # дроби мы больше не зовём. `nan` бросить тоже нечему — заставу на него
    # ставим отдельно, ниже.
    СРОК = float("nan")
if math.isnan(СРОК):
    # Уборка идёт своим кроном в 4:40, и падать на импорте ей нельзя: опечатка
    # в задокументированной переменной давала `EXIT=1` и ноль убранного, а
    # сырьё копилось дальше. Берём дефолт, чтобы прогон состоялся, и оставляем
    # причину двум читателям — своему логу (`main`) и сверке, которая этот
    # модуль и так импортирует. Тихо подставить тридцать было бы хуже отказа:
    # владелец, который правил срок, увидел бы обычный прогон.
    ОШИБКА_КОНФИГА = ("MARA_RAW_DAYS=%r — не число, беру 30"
                      % os.environ["MARA_RAW_DAYS"])
    RAW_DAYS = 30
elif СРОК < 0:
    # Отрицательный срок — та же опечатка владельца, но тише и дороже:
    # `int` её пропускает, а `raw_sweep` уносит порог в будущее и удаляет
    # всё сырьё, включая сегодняшнее. Чинить нечисловую опечатку и оставить
    # эту значило бы закрыть отказ и оставить рядом потерю данных.
    # Ноль остаётся валидным: «держать только сегодняшний день» — решение
    # странное, но осмысленное; у минуса смысла нет.
    ОШИБКА_КОНФИГА = ("MARA_RAW_DAYS=%r — отрицательный срок, беру 30"
                      % os.environ["MARA_RAW_DAYS"])
    RAW_DAYS = 30
elif СРОК > ПОТОЛОК:
    # Третий класс той же опечатки, и он ронял крон ровно так же, как ронял
    # нечисловой: `timedelta` за 740 тысяч суток бросает `OverflowError` уже
    # внутри `raw_sweep`, то есть после чистого импорта — сверка про это не
    # узнаёт вовсе и в 8:00 говорит «всё сходится». А шесть знаков набираются
    # сами: так пишут «фактически не убирать».
    # Здесь, в отличие от двух веток выше, откат не на тридцать, а на потолок.
    # Различает ветки цена подмены, а не намерение владельца. Выше потолка
    # все значения означают одно и то же — «не убирать», — и кламп ничего не
    # отнимает: имя файлу дают часы писателя (`time.strftime` в
    # `tdlib_ingest`, `date.today` в `gmail_ingest`), и худший отказ часов —
    # эпоха, 1970 год, — лежит внутри потолка и при клампе выживает. Всё, что
    # сортируется ниже 1926 года, — не дата, а мусор, и его снёс бы и дефолт.
    # Значит прогон с потолком удаляет ровно то же, что удалил бы миллион
    # суток, если бы досчитал. Свойство писателей, а не диска: проверяемо.
    # Ниже так не выходит: у минуса ближайшее допустимое — ноль, то есть
    # «держать только сегодняшний день», и это уже другой смысл, а не тот
    # же. Там остаётся дефолт модуля — тридцать из докстринга; в ТЗ этого
    # числа нет.
    # Сюда доезжают и `1e6`, и `inf`: разбор начинается с `float`, и всё,
    # что больше потолка, попадает в эту ветку записью любого вида. Застава
    # стоит на `СРОК`, то есть до округления, — иначе `ceil(inf)` бросил бы
    # раньше, чем ветка успела бы сработать.
    # Ценой ошибки ветки не различаются: снести сырьё, которое владелец просил
    # сохранить, в ближайшие 4:40 способна любая из четырёх — ревьюер показал
    # это прогоном в круге 2.
    ОШИБКА_КОНФИГА = ("MARA_RAW_DAYS=%r — больше ста лет, беру %d"
                      % (os.environ["MARA_RAW_DAYS"], ПОТОЛОК))
    RAW_DAYS = ПОТОЛОК
else:
    # Вверх, а не усечением. `44.5 → 44` удаляет сутки сырья, которые
    # владелец просил держать; `44.5 → 45` держит лишние сутки диска.
    # Различает ветки цена подмены — тем же правилом выбран и кламп на
    # потолок, — а обратимая ошибка дешевле невозвратной.
    RAW_DAYS = math.ceil(СРОК)
    if RAW_DAYS != СРОК:
        # Только на настоящей дроби. `45.0` даёт тот же порог, что `45`, и
        # жаловаться на него значило бы слать владельцу ежеутренний `error`
        # на здоровом конфиге — навсегда, потому что чинить нечего. Тот же
        # довод, что у границы потолка (`>`, а не `>=`), и здесь он весомее:
        # дробную запись владельцу подсказывает соседняя строка крона,
        # `MARA_CORE_BACKUP_MAX_DAYS=2.2`.
        ОШИБКА_КОНФИГА = ("MARA_RAW_DAYS=%r — дробный срок, беру %d"
                          % (os.environ["MARA_RAW_DAYS"], RAW_DAYS))
СЫРЬЁ = ("tdlib", "gmail")


def сегодня():
    return datetime.now(mi.TZ).date().isoformat()


def raw_sweep(root=None, days=RAW_DAYS, dry=False, day=None):
    """Файл по дате в имени, без базы: повтор просто ничего не находит."""
    root = root or mi.ROOT
    порог = (date.fromisoformat(day or сегодня()) - timedelta(days=days)).isoformat()
    отчёт = {"files": 0, "bytes": 0}
    for name in СЫРЬЁ:
        for p in sorted(glob.glob(os.path.join(root, name, "raw", "????-??-??.jsonl"))):
            if os.path.basename(p)[:10] >= порог:
                continue
            отчёт["files"] += 1
            отчёт["bytes"] += os.path.getsize(p)
            if not dry:
                os.unlink(p)
    return отчёт


def просроченные(con, day=None):
    """Блобы, которым пора: срок вышел, не закреплены, ещё не убраны."""
    return con.execute(
        "select sha256, path from blobs where purged_at is null and pin=0 "
        "and audio_until is not null and audio_until <= ? order by audio_until",
        (day or сегодня(),)).fetchall()


def пометить_манифесты(con, root, sha, when):
    """`purged` во все манифесты, которые ссылались на эту запись."""
    for row in con.execute("select id from events where blob_sha256=?", (sha,)):
        path = mi.manifest_path(root, row["id"])
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            man = json.load(fh)
        man["purged"] = {"at": when, "reason": "retention"}
        mi.write_json(path, man)


def sweep(con, root=None, dry=False, day=None):
    """Один проход уборки. Возвращает отчёт, не печатает ничего сам."""
    root = root or mi.ROOT
    отчёт = {"purged": 0, "bytes": 0, "errors": []}
    for b in просроченные(con, day):
        path, sha = b["path"], b["sha256"]
        try:
            size = os.path.getsize(path) if path and os.path.exists(path) else 0
            if dry:
                отчёт["purged"] += 1
                отчёт["bytes"] += size
                continue
            if path and os.path.exists(path):
                os.unlink(path)
            when = mi.now_iso()
            пометить_манифесты(con, root, sha, when)
            con.execute("update blobs set purged_at=? where sha256=?", (when, sha))
            отчёт["purged"] += 1
            отчёт["bytes"] += size
        except OSError as e:
            # ponytail: одна плохая запись не должна останавливать уборку —
            # завтрашний прогон попробует её снова.
            отчёт["errors"].append("%s: %s" % (sha[:12], e))
    return отчёт


def main():
    ap = argparse.ArgumentParser(description="уборка просроченного аудио")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--dry-run", action="store_true", dest="dry")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    # Сверка увидит эту же жалобу через `ОШИБКА_КОНФИГА`, но у неё свой цикл в
    # час; тот, кто открыл `retention.log` с вопросом «почему тридцать, я же
    # ставил шестьдесят», читает здесь.
    if ОШИБКА_КОНФИГА:
        print("%s ретеншен: %s" % (mi.now_iso(), ОШИБКА_КОНФИГА))
    mi.ROOT = a.root
    r = sweep(mi.connect(a.root), a.root, a.dry)
    rr = raw_sweep(a.root, dry=a.dry)
    print("%s ретеншен: убрано %d записей, %.1f МБ; сырых логов %d, %.1f МБ%s" % (
        mi.now_iso(), r["purged"], r["bytes"] / 1e6, rr["files"], rr["bytes"] / 1e6,
        ", вхолостую" if a.dry else ""))
    for e in r["errors"]:
        print("  сбой: " + e)
    return 1 if r["errors"] else 0


def self_check():
    import tempfile
    from datetime import timedelta
    root = tempfile.mkdtemp()
    mi.ROOT = root
    con = mi.connect(root)
    sha = "c" * 64
    eid, _ = mi.put_event(con, {"kind": "call", "source": "sc", "source_id": "1",
                                "payload": {"ext": "m4a"},
                                "blob": {"sha256": sha, "ext": "m4a"}})
    path = mi.blob_path(root, sha, "m4a")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    open(path, "wb").write(b"x" * 10)
    вчера = (datetime.now(mi.TZ) - timedelta(days=1)).date().isoformat()
    con.execute("insert into blobs(sha256,path,bytes,created,audio_until) "
                "values(?,?,?,?,?)", (sha, path, 10, mi.now_iso(), вчера))
    mi.write_json(mi.manifest_path(root, eid), {"id": eid, "purged": None})
    assert sweep(con, root, dry=True)["purged"] == 1, "холостой прогон не увидел срок"
    assert os.path.exists(path), "холостой прогон удалил файл"
    assert sweep(con, root)["purged"] == 1
    assert not os.path.exists(path), "файл не удалён"
    with open(mi.manifest_path(root, eid), encoding="utf-8") as fh:
        assert json.load(fh)["purged"]["reason"] == "retention", "манифест не помечен"
    assert sweep(con, root) == {"purged": 0, "bytes": 0, "errors": []}, "повтор убрал ещё раз"
    for name, d in (("tdlib", 40), ("gmail", 40), ("gmail", 3)):
        p = os.path.join(root, name, "raw", (date.today() - timedelta(days=d)).isoformat() + ".jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("x")
    assert raw_sweep(root, dry=True)["files"] == 2, "холостой прогон не увидел старое сырьё"
    assert len(glob.glob(os.path.join(root, "*", "raw", "*.jsonl"))) == 3, "холостой прогон удалил"
    assert raw_sweep(root) == {"files": 2, "bytes": 2}
    assert len(glob.glob(os.path.join(root, "*", "raw", "*.jsonl"))) == 1, "свежее сырьё убрано"
    assert raw_sweep(root)["files"] == 0, "повтор нашёл что убирать"
    print("blob_retention self-check: ок")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
