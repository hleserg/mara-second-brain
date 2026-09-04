#!/usr/bin/env python3
"""Бэкап ядра приёма: база, метаданные, аудио (ТЗ §5.3, P0-2 аудита).

Волт возит `vault-backup.sh`, а ядро не возил никто: `contextd.db` и дерево
`/srv/mara-blobs` не попадали ни в одну копию. Диск doctor умрёт — исчезнут
события, работы, дайджесты и все записи разговоров, и узнаем мы об этом в
момент, когда они понадобятся.

Что уезжает:

* `contextd.db` — снимок через SQLite backup API. Копировать файл базы под
  работающим демоном нельзя: WAL живёт отдельно, и `cp` даёт обрубок.
* `manifests/`, `transcripts/`, `extractions/` — маленькие, вместе с базой
  одним зашифрованным архивом, ротация по счёту.
* `calls/` — аудио, пофайловым зашифрованным зеркалом. Имя файла это хеш
  содержимого, содержимое неизменно, поэтому зеркало инкрементально само по
  себе: что уже лежит — не перешифровываем.

Что НЕ уезжает: `tdlib/` и `gmail/`. ТЗ §11: credentials и session-файлы
denylisted из Git, R2 и бэкапов. Восстановление ядра означает повторный вход
в Telegram и повторный OAuth Gmail — это дешевле, чем возить ключи по
сетевым шарам. Раздел «Восстановление» в docs/backup-core.md.

Бэкап, который ни разу не разворачивали, — не бэкап: после каждой удачной
записи прогон сам расшифровывает архив с носителя, проверяет целостность
базы, сверяет счётчики с манифестом и сверяет хеш случайной записи из
зеркала. Без этого мы бы узнали о порче в единственный неподходящий день.
"""
import os, sys, json, glob, time, shutil, hashlib, sqlite3, tarfile
import argparse, tempfile, subprocess
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import mara_ingest as mi

МЕЛОЧЬ = ("manifests", "transcripts", "extractions")   # allowlist, не exclude
СЕКРЕТЫ = ("tdlib", "gmail")                           # ТЗ §11 — не бэкапим
# Список жёсткий, и он же кормит сверку восстановления: таблица, которой тут
# нет, в проверку не попадёт молча. Добавлять сюда каждую новую (ADR-0001).
ТАБЛИЦЫ = ("devices", "events", "jobs", "blobs", "digests",
           "commitments", "conversations", "projections")
ПРОБА = 3                                              # столько блобов сверяем


def гпг(args, пароль):
    """gpg без pinentry: под кроном терминала нет, а спросить фразу некого."""
    subprocess.run(["gpg", "--batch", "--yes", "--quiet", "--pinentry-mode",
                    "loopback", "--passphrase-file", пароль] + args, check=True)


def шифр(src, dst, пароль):
    гпг(["--symmetric", "--cipher-algo", "AES256", "-o", dst, src], пароль)
    # gpg отдаёт файл по umask: на носителе это 664, а внутри разговоры
    os.chmod(dst, 0o600)


def дешифр(src, dst, пароль):
    гпг(["-o", dst, "-d", src], пароль)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for кусок in iter(lambda: fh.read(1 << 20), b""):
            h.update(кусок)
    return h.hexdigest()


def снимок(db, dst):
    """Согласованная копия базы под живым демоном (SQLite backup API)."""
    # mode=ro, а не mi.connect и не обычный connect: mi.connect ставит pragma
    # и прогоняет SCHEMA, а rw-соединение на закрытии может чекпойнтить WAL —
    # то есть писать в боевой файл. Бэкап не имеет права ни на то, ни на другое
    try:
        src = sqlite3.connect("file:%s?mode=ro" % db, timeout=30, uri=True)
        src.execute("select count(*) from sqlite_master").fetchone()
    except sqlite3.OperationalError as e:
        # WAL после падения демона требует восстановления, а его read-only
        # соединение сделать не может. Отказаться от бэкапа ровно в тот день,
        # когда он нужнее всего, хуже, чем один чекпойнт — но сказать вслух
        print("core-backup: read-only не открылось (%s), беру на запись" % e,
              file=sys.stderr)
        src = sqlite3.connect(db, timeout=30)
    try:
        цель = sqlite3.connect(dst)
        try:
            src.backup(цель)
        finally:
            цель.close()
    finally:
        src.close()


def счётчики(db):
    con = sqlite3.connect(db)
    try:
        out = {t: con.execute("select count(*) from %s" % t).fetchone()[0]
               for t in ТАБЛИЦЫ}
        out["user_version"] = con.execute("pragma user_version").fetchone()[0]
        return out
    finally:
        con.close()


def мелочь(root):
    """Пути метаданных, отсортированные — манифест должен быть воспроизводимым."""
    out = []
    for d in МЕЛОЧЬ:
        для_обхода = os.path.join(root, d)
        for каталог, _, файлы in os.walk(для_обхода):
            for f in sorted(файлы):
                p = os.path.join(каталог, f)
                out.append((os.path.relpath(p, root), p))
    return sorted(out)


def архив(root, снимок_db, манифест, dst):
    """tar.gz: база, манифест и метаданные. Список файлов — allowlist."""
    with tarfile.open(dst, "w:gz") as tf:
        tf.add(снимок_db, arcname="contextd.db")
        tf.add(манифест, arcname="manifest.json")
        for rel, p in мелочь(root):
            tf.add(p, arcname=rel)


def зеркало_аудио(db, root, target, пароль):
    """Аудио пофайлово: чего нет — шифруем, что вычищено ретеншеном — убираем.

    Имя файла это хеш содержимого, поэтому «уже есть» значит «то же самое», и
    инкрементальность получается бесплатно."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    новых, байт, убрано, без_файла = 0, 0, 0, 0
    try:
        for r in con.execute("select sha256, path, purged_at from blobs"):
            рядом = os.path.join(target, os.path.relpath(r["path"], root) + ".gpg")
            if r["purged_at"]:
                if os.path.exists(рядом):
                    os.unlink(рядом)
                    убрано += 1
                continue
            if not os.path.exists(r["path"]):
                # строка есть, файла нет — молчать об этом нельзя: ровно эту
                # тихую потерю бэкап и должен показывать
                без_файла += 1
                continue
            if os.path.exists(рядом):
                continue
            os.makedirs(os.path.dirname(рядом), mode=0o700, exist_ok=True)
            # ponytail: один gpg на файл — при десятках записей в сутки это
            # копейки; вырастет на порядки — шифровать помесячным таром
            шифр(r["path"], рядом + ".tmp", пароль)
            os.replace(рядом + ".tmp", рядом)
            новых += 1
            байт += os.path.getsize(рядом)
    finally:
        con.close()
    return {"новых": новых, "байт": байт, "убрано_вычищенных": убрано,
            "без_файла": без_файла}


def проверка(target, пароль, root, имя=None):
    """Развернуть свежий архив с носителя и убедиться, что это база, а не мусор.

    Проверяем то, что нужно для восстановления: расшифровка, целостность
    SQLite, совпадение счётчиков с манифестом и хеш аудио из зеркала."""
    архивы = sorted(glob.glob(os.path.join(target, "core-*.tar.gz.gpg")))
    src = os.path.join(target, имя) if имя else (архивы[-1] if архивы else None)
    if not src or not os.path.exists(src):
        raise RuntimeError("проверка: в %s нет архивов ядра" % target)
    tmp = tempfile.mkdtemp(prefix="mara-core-restore.")
    try:
        дешифр(src, os.path.join(tmp, "core.tar.gz"), пароль)
        with tarfile.open(os.path.join(tmp, "core.tar.gz")) as tf:
            члены = tf.getnames()
            # filter="data": свой же архив, но распаковка тара — это место, где
            # чужое имя вида ../../ уводит запись мимо каталога
            tf.extractall(tmp, filter="data")
        for d in СЕКРЕТЫ:
            утечка = [m for m in члены if m == d or m.startswith(d + "/")]
            if утечка:
                raise RuntimeError("проверка: в архиве секреты %s" % утечка[:3])
        db = os.path.join(tmp, "contextd.db")
        con = sqlite3.connect(db)
        try:
            ц = con.execute("pragma integrity_check").fetchone()[0]
        finally:
            con.close()
        if ц != "ok":
            raise RuntimeError("проверка: база битая — %s" % ц)
        м = json.load(open(os.path.join(tmp, "manifest.json"), encoding="utf-8"))
        было = счётчики(db)
        if было != м["counts"]:
            raise RuntimeError("проверка: счётчики разошлись %s != %s"
                               % (было, м["counts"]))
        for rel, ожидаемый in list(м["files"].items()):
            факт = os.path.join(tmp, rel)
            if not os.path.exists(факт) or sha(факт) != ожидаемый:
                raise RuntimeError("проверка: %s не совпал с манифестом" % rel)
        сверено = проверить_аудио(db, root, target, пароль, tmp)
        return {"архив": os.path.basename(src), "файлов": len(м["files"]),
                "счётчики": было, "аудио_сверено": сверено}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def проверить_аудио(db, root, target, пароль, tmp):
    """Хеш живого аудио обязан сойтись с расшифрованной копией из зеркала."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        строки = con.execute("select sha256, path from blobs where purged_at is null "
                             "order by created desc limit ?", (ПРОБА,)).fetchall()
    finally:
        con.close()
    n = 0
    for r in строки:
        if not os.path.exists(r["path"]):
            # Строка в blobs без живого файла — разрыв приёма, а не бэкапа:
            # зеркалить нечего, и `зеркало_аудио` такую строку пропускает.
            # Падать тут значило бы красить прогон в чужую поломку — и первый
            # прогон на новом носителе после замены диска был бы красным
            # всегда. Отчёт про такие строки даёт поле `без_файла`.
            continue
        рядом = os.path.join(target, os.path.relpath(r["path"], root) + ".gpg")
        if not os.path.exists(рядом):
            raise RuntimeError("проверка: аудио %s нет в зеркале" % r["sha256"][:12])
        назад = os.path.join(tmp, "blob.bin")
        дешифр(рядом, назад, пароль)
        if sha(назад) != r["sha256"]:
            raise RuntimeError("проверка: аудио %s расшифровалось не тем"
                               % r["sha256"][:12])
        os.unlink(назад)
        n += 1
    return n


def прогон(root, targets, пароль, keep, work, аудио=True, drill=True):
    начало = time.time()
    if not os.path.exists(пароль) or os.path.getsize(пароль) == 0:
        raise RuntimeError("нет парольной фразы %s" % пароль)
    if oct(os.stat(пароль).st_mode & 0o777) != oct(0o600):
        raise RuntimeError("%s должен быть 600" % пароль)
    db = os.path.join(root, "contextd.db")
    if not os.path.exists(db):
        raise RuntimeError("нет базы %s" % db)

    os.makedirs(work, mode=0o700, exist_ok=True)
    stage = tempfile.mkdtemp(prefix="core.", dir=work)
    имя = "core-%s.tar.gz.gpg" % datetime.now(mi.TZ).strftime("%F")
    try:
        копия = os.path.join(stage, "contextd.db")
        снимок(db, копия)
        сч = счётчики(копия)
        файлы = {"contextd.db": sha(копия)}
        for rel, p in мелочь(root):
            файлы[rel] = sha(p)
        м = {"created": mi.now_iso(), "root": root, "counts": сч, "files": файлы,
             "excluded": list(СЕКРЕТЫ), "db_bytes": os.path.getsize(копия)}
        путь_м = os.path.join(stage, "manifest.json")
        with open(путь_м, "w", encoding="utf-8") as fh:
            json.dump(м, fh, ensure_ascii=False, indent=1, sort_keys=True)
        tar = os.path.join(stage, "core.tar.gz")
        архив(root, копия, путь_м, tar)
        enc = os.path.join(stage, имя)
        шифр(tar, enc, пароль)
        сводка = {"архив": имя, "байт": os.path.getsize(enc),
                  "sha256": sha(enc), "created": м["created"], "counts": сч}

        записано = []
        for t in targets:
            try:
                os.makedirs(t, mode=0o700, exist_ok=True)
                # временный файл на том же носителе: переименование между
                # монтированиями не атомарно, а на CIFS ещё и не всегда работает
                shutil.copy2(enc, os.path.join(t, "." + имя + ".tmp"))
                os.chmod(os.path.join(t, "." + имя + ".tmp"), 0o600)
                os.replace(os.path.join(t, "." + имя + ".tmp"), os.path.join(t, имя))
                with open(os.path.join(t, имя.replace(".tar.gz.gpg",
                                                      ".manifest.json")),
                          "w", encoding="utf-8") as fh:
                    # рядом с архивом только сводка: имена файлов это id событий,
                    # им на сетевой шаре в открытом виде делать нечего
                    json.dump(сводка, fh, ensure_ascii=False, indent=1,
                              sort_keys=True)
                os.chmod(os.path.join(t, имя.replace(".tar.gz.gpg",
                                                     ".manifest.json")), 0o600)
                записано.append(t)
            except OSError as e:
                print("core-backup: %s недоступен (%s)" % (t, e), file=sys.stderr)
        if not записано:
            raise RuntimeError("ни один носитель не записан")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    зв = {"новых": 0, "байт": 0, "убрано_вычищенных": 0}
    if аудио:
        for t in записано:
            зв = зеркало_аудио(db, root, t, пароль)
    бэкап_сек = round(time.time() - начало, 1)
    итог = {"архив": имя, "байт": сводка["байт"], "носители": записано,
            "счётчики": сч, "аудио": зв, "секунд": бэкап_сек}
    if drill:
        t0 = time.time()
        итог["проверка"] = проверка(записано[0], пароль, root, имя=имя)
        итог["проверка"]["секунд"] = round(time.time() - t0, 1)
    # ротация последней: свежий архив, не прошедший проверку, не имеет права
    # вытеснить старый, который разворачивался
    for t in записано:
        ротация(t, keep)
    return итог


def ротация(target, keep):
    for шаблон in ("core-*.tar.gz.gpg", "core-*.manifest.json"):
        файлы = sorted(glob.glob(os.path.join(target, шаблон)))
        for старый in файлы[:-keep] if keep > 0 else []:
            os.unlink(старый)


def самопроверка():
    """Полный круг на игрушечном корне: бэкап, зеркало, разворачивание.

    Отдельно проверяем, что tdlib и gmail в архив не попали: это не
    придирка, а ТЗ §11 — ключи не должны уезжать на носители."""
    if not shutil.which("gpg"):
        print("core-backup: нет gpg, самопроверка пропущена")
        return
    tmp = tempfile.mkdtemp(prefix="core-selfcheck.")
    try:
        root = os.path.join(tmp, "blobs")
        con = mi.connect(root)
        тело = b"audio-for-backup"
        s = hashlib.sha256(тело).hexdigest()
        p = mi.blob_path(root, s, "wav")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(тело)
        con.execute("insert into blobs(sha256,path,bytes,mime,created) values(?,?,?,?,?)",
                    (s, p, len(тело), "audio", mi.now_iso()))
        con.execute("insert into events(id,kind,dedupe_key,state) values('e1','call','k1','done')")
        for каталог, имя_ф, текст in (("manifests", "e1.json", "{}"),
                                      ("transcripts", "e1.jsonl", "{}\n"),
                                      ("extractions", "e1.json", "{}")):
            os.makedirs(os.path.join(root, каталог), exist_ok=True)
            open(os.path.join(root, каталог, имя_ф), "w").write(текст)
        for d in СЕКРЕТЫ:                       # то, что уехать не должно
            os.makedirs(os.path.join(root, d), exist_ok=True)
            open(os.path.join(root, d, "token.json"), "w").write("секрет")
        con.close()

        пароль = os.path.join(tmp, "pass")
        open(пароль, "w").write("проверочная фраза\n")
        os.chmod(пароль, 0o600)
        target = os.path.join(tmp, "target")
        общее = dict(root=root, targets=[target], пароль=пароль, keep=2,
                     work=os.path.join(tmp, "work"))

        r = прогон(**общее)
        assert r["носители"] == [target], r
        assert r["аудио"]["новых"] == 1, r
        assert r["проверка"]["счётчики"]["blobs"] == 1, r
        assert r["проверка"]["аудио_сверено"] == 1, r

        r2 = прогон(**общее)
        assert r2["аудио"]["новых"] == 0, "уже зашифрованное аудио перешифровано"

        # ретеншен вычистил запись — из зеркала она обязана уйти
        con = mi.connect(root)
        con.execute("update blobs set purged_at=?", (mi.now_iso(),))
        con.close()
        os.unlink(p)
        r3 = прогон(**общее)
        assert r3["аудио"]["убрано_вычищенных"] == 1, r3
        assert r3["проверка"]["аудио_сверено"] == 0, r3

        for f in glob.glob(os.path.join(target, "core-*")):
            assert oct(os.stat(f).st_mode & 0o777) == oct(0o600), f

        # ротация: на носителе не больше keep архивов
        assert len(glob.glob(os.path.join(target, "core-*.tar.gz.gpg"))) <= 2

        # порченый архив обязан валить проверку, а не молчать
        свежий = sorted(glob.glob(os.path.join(target, "core-*.tar.gz.gpg")))[-1]
        with open(свежий, "r+b") as fh:
            fh.seek(os.path.getsize(свежий) // 2)
            fh.write(b"\x00\x00\x00\x00")
        try:
            проверка(target, пароль, root)
            raise AssertionError("битый архив прошёл проверку")
        except (RuntimeError, subprocess.CalledProcessError, tarfile.TarError,
                OSError):
            pass
        print("core-backup: самопроверка ок")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="бэкап и проверка восстановления ядра")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--targets", default=os.environ.get(
        "MARA_CORE_TARGETS", "/mnt/backup/mara /mnt/win-backups/mara"))
    ap.add_argument("--pass-file", default=os.path.expanduser(
        "~/.config/mara/backup-pass"))
    ap.add_argument("--keep", type=int, default=8)
    ap.add_argument("--work", default="/var/tmp/mara-backup")
    ap.add_argument("--no-audio", action="store_true", help="без зеркала аудио")
    ap.add_argument("--no-drill", action="store_true",
                    help="без проверки восстановления (не рекомендуется)")
    ap.add_argument("--drill-only", action="store_true",
                    help="только развернуть свежий архив с первого носителя")
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        return самопроверка()
    targets = a.targets.split()
    if a.drill_only:
        r = проверка(targets[0], a.pass_file, a.root)
    else:
        r = прогон(a.root, targets, a.pass_file, a.keep, a.work,
                   аудио=not a.no_audio, drill=not a.no_drill)
    print(json.dumps(r, ensure_ascii=False, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
