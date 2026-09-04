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
        if not os.path.exists(db):
            # именно до connect: после него файл уже создан, и проверять нечего
            raise RuntimeError("проверка: в архиве нет contextd.db")
        con = sqlite3.connect(db)
        try:
            ц = con.execute("pragma integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as e:
            # На одной сборке sqlite порченая страница возвращается строкой, на
            # другой — бросается. Проверено: 3.46.1 отдаёт «Tree N page N:
            # btreeInitPage() returns error code 11», сборка на CI бросает
            # «database disk image is malformed». Разница версии, не бэкапа, и
            # сторож обязан сработать одинаково на обеих.
            ц = str(e)
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


def отметка(путь, записанные, когда=None):
    """Когда на каждый носитель в последний раз лёг архив.

    Сверка сама этого не узнает: отвалившийся носитель о себе не
    рассказывает, а по архивам на соседнем носителе его простой не измерить.
    Знает только бэкап — он один видел, что запись удалась. Файл машинный,
    поэтому epoch, а не ISO: его сравнивают с `time.time()`, а не читают.
    """
    когда = time.time() if когда is None else когда
    try:
        with open(путь, encoding="utf-8") as fh:
            было = json.load(fh)
        if not isinstance(было, dict):
            было = {}
    except (OSError, ValueError):
        # Потерянный файл — не повод валить бэкап: сверка на пустом состоянии
        # скажет «ни разу не принимал архив», и это ровно та правда, что есть.
        было = {}
    было.update(dict.fromkeys(записанные, когда))
    os.makedirs(os.path.dirname(путь), mode=0o700, exist_ok=True)
    mi.write_json(путь, было)
    return было


def сверить_копию(путь, ожидаемый):
    """Прочитать записанное с носителя и сравнить с хешем оригинала.

    `fsync` тут не про долговечность: на сетевой шаре отложенная ошибка записи
    всплывает только на нём, а до него `copy2` рапортует успех. `DONTNEED`
    роняет страницы файла — иначе хеш считался бы по тому же кешу, из которого
    мы и писали, и копия сверялась бы сама с собой."""
    with open(путь, "rb+") as fh:
        # `rb+`, а не `rb`: на сетевых ФС сброс на сервер просят по пишущему
        # хендлу, с читающего клиент может и промолчать.
        os.fsync(fh.fileno())
        if hasattr(os, "posix_fadvise"):        # Linux; на другом живём вслепую
            os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    if sha(путь) != ожидаемый:
        raise OSError("копия прочиталась не тем, чем писалась")


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
            if not mi.смонтирован(t, root):
                # Не «недоступен»: каталог как раз доступен, в том и беда.
                print("core-backup: %s на одном устройстве с %s — носитель не "
                      "смонтирован, пропускаю" % (t, root), file=sys.stderr)
                continue
            # Имя связываем до `try`: чистка временного файла живёт в
            # `except`, а до неё долетает и падение `makedirs`.
            врем = os.path.join(t, "." + имя + ".tmp")
            try:
                os.makedirs(t, mode=0o700, exist_ok=True)
                # временный файл на том же носителе: переименование между
                # монтированиями не атомарно, а на CIFS ещё и не всегда работает
                shutil.copy2(enc, врем)
                os.chmod(врем, 0o600)
                # Сверяем до переименования: под боевым именем битая копия не
                # появляется ни на секунду, и §12 не зачтёт её за третью.
                сверить_копию(врем, сводка["sha256"])
                os.replace(врем, os.path.join(t, имя))
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
                print("core-backup: %s не записан (%s)" % (t, e),
                      file=sys.stderr)
                if os.path.exists(врем):
                    # Иначе на носителе, куда перестало писаться, за год
                    # накопится триста скрытых огрызков: ротация ходит по
                    # `core-*` и этих имён не видит.
                    os.unlink(врем)
        if not записано:
            raise RuntimeError("ни один носитель не записан")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    отметка(mi.ОТМЕТКА_НОСИТЕЛЕЙ, записано)

    # По носителям, а не одним числом: с общим счётчиком статистика последнего
    # носителя затирала предыдущие, и отчёт врал тем сильнее, чем больше
    # носителей. Сумма врала бы иначе — «новых 2» про один файл на два диска.
    зв = {}
    if аудио:
        for t in записано:
            зв[t] = зеркало_аудио(db, root, t, пароль)
    бэкап_сек = round(time.time() - начало, 1)
    итог = {"архив": имя, "байт": сводка["байт"], "носители": записано,
            "счётчики": сч, "аудио": зв, "секунд": бэкап_сек}
    if drill:
        t0 = time.time()
        # Разворачиваем один носитель, а не все: остальные копии побайтно
        # сверены с тем же `enc` при записи, значит развернутся так же, а
        # gpg на каждый носитель каждую ночь — минуты на ровном месте.
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


def подделка(root, work, пароль, dst, порча):
    """Архив с намеренным дефектом: `порча(копия_бд, дерево, манифест)` правит
    распакованное содержимое перед упаковкой. Нужен, чтобы сторожа внутри
    `проверка` срабатывали на стенде, а не впервые на живом носителе."""
    stage = tempfile.mkdtemp(prefix="core-bad.", dir=work)
    try:
        копия = os.path.join(stage, "contextd.db")
        снимок(os.path.join(root, "contextd.db"), копия)
        дерево = os.path.join(stage, "root")
        файлы = {"contextd.db": sha(копия)}
        for rel, p in мелочь(root):
            куда = os.path.join(дерево, rel)
            os.makedirs(os.path.dirname(куда), exist_ok=True)
            shutil.copy2(p, куда)
            файлы[rel] = sha(p)
        м = {"counts": счётчики(копия), "files": файлы}
        порча(копия, дерево, м)
        путь_м = os.path.join(stage, "manifest.json")
        with open(путь_м, "w", encoding="utf-8") as fh:
            json.dump(м, fh, ensure_ascii=False)
        tar = os.path.join(stage, "core.tar.gz")
        with tarfile.open(tar, "w:gz") as tf:
            tf.add(копия, arcname="contextd.db")
            tf.add(путь_м, arcname="manifest.json")
            for каталог, _, имена in os.walk(дерево):
                for f in sorted(имена):
                    полный = os.path.join(каталог, f)
                    tf.add(полный, arcname=os.path.relpath(полный, дерево))
        шифр(tar, dst, пароль)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def самопроверка():
    """Полный круг на игрушечном корне: бэкап, зеркало, разворачивание.

    Отдельно проверяем, что tdlib и gmail в архив не попали: это не
    придирка, а ТЗ §11 — ключи не должны уезжать на носители."""
    if not shutil.which("gpg"):
        print("core-backup: нет gpg, самопроверка пропущена")
        return
    tmp = tempfile.mkdtemp(prefix="core-selfcheck.")
    боевая_отметка = mi.ОТМЕТКА_НОСИТЕЛЕЙ
    os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"   # см. mi.смонтирован
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
        # Два носителя, а не один: боевой прогон пишет на диск и на шару, а
        # проверялся до сих пор только путь с одним. Затирание статистики
        # зеркала и отметку по каждому носителю на одном не увидеть.
        второй = os.path.join(tmp, "target2")
        # Путь отметки подменяем в самой константе, а не параметром прогона:
        # параметр означал бы, что боевое выражение с дефолтом не исполняет
        # ни один вход, кроме крона, — а на этом уже спотыкались.
        отм = os.path.join(tmp, "state", "core-targets.json")
        mi.ОТМЕТКА_НОСИТЕЛЕЙ = отм
        общее = dict(root=root, targets=[target, второй], пароль=пароль, keep=2,
                     work=os.path.join(tmp, "work"))

        # Главная гарантия скрипта — согласованная копия под живым демоном —
        # до сих пор не проверялась ничем. Плоское копирование WAL-базы даёт
        # файл самосогласованный, но пустой, и ни один сторож этого не увидит:
        # счётчики манифеста считаются с той же копии, то есть ноль сверяется с
        # нулём. Пишем строку и не закрываем писателя — она остаётся в WAL,
        # куда простое копирование не заглядывает.
        писатель = mi.connect(root)
        писатель.execute("insert into events(id,kind,dedupe_key,state)"
                         " values('e2','call','k2','done')")   # mi автокоммит

        r = прогон(**общее)
        писатель.close()
        assert r["проверка"]["счётчики"]["events"] == 2, r
        # Число жёсткое, а не `len(мелочь(root)) + 1`: `мелочь()` строит и
        # содержимое архива, и манифест, так что сверка их друг с другом
        # усечения списка каталогов не видит — манифест тут не оракул.
        assert r["проверка"]["файлов"] == 4, r    # база, манифест, мелочь ×3
        assert r["носители"] == [target, второй], r
        assert r["аудио"][target]["новых"] == 1, r
        assert r["аудио"][второй]["новых"] == 1, r
        # Отметка — единственный источник, по которому сверка отличает
        # отвалившийся на час носитель от пропавшего на неделю.
        отмечено = json.load(open(отм, encoding="utf-8"))
        assert sorted(отмечено) == sorted([target, второй]), отмечено
        assert time.time() - min(отмечено.values()) < 300, отмечено
        assert r["проверка"]["счётчики"]["blobs"] == 1, r
        assert r["проверка"]["аудио_сверено"] == 1, r

        # Сайдкар лежит на сетевой шаре открытым текстом. Имена файлов мелочи —
        # это id событий, и полный манифест туда выкладывать нельзя (ТЗ §18).
        рядом = json.load(open(os.path.join(target, r["архив"].replace(
            ".tar.gz.gpg", ".manifest.json")), encoding="utf-8"))
        assert set(рядом) == {"архив", "байт", "sha256", "created",
                              "counts"}, sorted(рядом)
        # сайдкар — единственный способ проверить архив на шаре, не расшифровав
        # его; значит и хеш в нём должен быть от шифротекста, а не от
        # плейнтекста
        лежит = os.path.join(target, r["архив"])
        assert рядом["sha256"] == sha(лежит), рядом["sha256"]
        assert рядом["байт"] == os.path.getsize(лежит), рядом["байт"]

        r2 = прогон(**общее)
        assert r2["аудио"][второй]["новых"] == 0, "аудио перешифровано заново"

        # Два сторожа `проверить_аудио` доступны только пока зеркало живое, то
        # есть до ретеншена ниже: после него сверять нечего и оба молчат.
        зерк = os.path.join(target, os.path.relpath(p, root) + ".gpg")
        assert oct(os.stat(зерк).st_mode & 0o777) == oct(0o600), зерк
        os.unlink(зерк)
        try:
            проверка(target, пароль, root)
            raise AssertionError("зеркало без файла прошло проверку")
        except RuntimeError as e:
            assert "нет в зеркале" in str(e), str(e)
        не_то = os.path.join(root, "manifests", "e1.json")
        шифр(не_то, зерк, пароль)       # файл на месте, но внутри не то аудио
        try:
            проверка(target, пароль, root)
            raise AssertionError("чужое аудио в зеркале прошло проверку")
        except RuntimeError as e:
            assert "расшифровалось не тем" in str(e), str(e)
        os.unlink(зерк)
        вернули = зеркало_аудио(os.path.join(root, "contextd.db"), root,
                                target, пароль)
        assert вернули["новых"] == 1, вернули

        # ретеншен вычистил запись — из зеркала она обязана уйти
        con = mi.connect(root)
        con.execute("update blobs set purged_at=?", (mi.now_iso(),))
        con.close()
        # Файл намеренно оставляем на месте: если убрать и его, снятый фильтр
        # `purged_at is null` в `проверить_аудио` станет неотличим от целого —
        # сверять будет нечего в обоих случаях.

        # Отметку до сих пор сверяли со списком носителей, на которые запись
        # прошла, — а в самопроверке проходила она всегда, так что оракул
        # брался из того же места, что и проверяемое. Прогон, где один
        # носитель отвалился, — единственный вход, который отличает «отмечаем
        # записанных» от «отмечаем всех подряд» и «пишем отметку с нуля» от
        # «дописываем в неё». Оба мутанта отменяют весь смысл механизма:
        # простой отвалившегося носителя не дотикает до порога никогда.
        было_отмечено = json.load(open(отм, encoding="utf-8"))
        # Права на отметку обещаны в §6 тела PR; кроме этой строки
        # их не проверяет ничто.
        assert os.stat(отм).st_mode & 0o777 == 0o600, \
            oct(os.stat(отм).st_mode)
        shutil.rmtree(второй)
        open(второй, "w").close()   # файл вместо каталога: makedirs упадёт
        r3 = прогон(**общее)
        assert r3["носители"] == [target], r3
        отмечено = json.load(open(отм, encoding="utf-8"))
        assert отмечено[второй] == было_отмечено[второй], \
            "отмечен носитель, на который не писали"
        assert отмечено[target] > было_отмечено[target], "отметка не обновилась"
        # Значение, а не только порядок: отметка из будущего делает простой
        # отрицательным навсегда, и отвал не выстрелит ни разу.
        assert отмечено[target] <= time.time(), отмечено[target]
        assert r3["аудио"][target]["убрано_вычищенных"] == 1, r3
        assert r3["проверка"]["аудио_сверено"] == 0, r3

        for f in glob.glob(os.path.join(target, "core-*")):
            assert oct(os.stat(f).st_mode & 0o777) == oct(0o600), f

        # Ротация: три прогона выше пишут один и тот же файл — дата в имени
        # у них общая, — так что вытеснять ротации было нечего ни разу.
        # Проверяем её отдельно, на подставных именах.
        катр = os.path.join(tmp, "rot")
        os.makedirs(катр)
        for дата in ("2020-01-01", "2020-01-02", "2020-01-03"):
            for хвост in (".tar.gz.gpg", ".manifest.json"):
                open(os.path.join(катр, "core-" + дата + хвост), "w").close()
        ротация(катр, 2)
        осталось = sorted(os.path.basename(f)
                          for f in glob.glob(os.path.join(катр, "core-*")))
        assert осталось == ["core-2020-01-02.manifest.json",
                            "core-2020-01-02.tar.gz.gpg",
                            "core-2020-01-03.manifest.json",
                            "core-2020-01-03.tar.gz.gpg"], осталось

        # Сторожа внутри `проверка` — утечка секретов, целостность базы,
        # счётчики, хеши файлов — до сих пор не срабатывали ни разу: порченый
        # архив ниже валится ещё на gpg, и дальше расшифровки дело не идёт.
        # Собираем архив с ровно одним дефектом на сторожа и ждём именно свой:
        # вариант, упавший не на том стороже, иначе зачёлся бы как успех.
        def утечка(копия, дерево, м):
            os.makedirs(os.path.join(дерево, "tdlib"))
            open(os.path.join(дерево, "tdlib", "token.json"), "w").write("сек")

        def гниль(копия, дерево, м):
            k = sqlite3.connect(копия)
            страница = k.execute("pragma page_size").fetchone()[0]
            k.close()
            # Байт типа страницы в мусор — так порча попадает в живую страницу
            # с blobs, а не в пустое место, где integrity_check её не заметит.
            номер = open(копия, "rb").read().find(s.encode()) // страница
            assert номер > 0, "sha не нашлась в базе"
            with open(копия, "r+b") as fh:
                fh.seek(номер * страница)
                fh.write(b"\x7f")
            # иначе порча ломает два сторожа сразу — и целостность, и хеш базы,
            # а сверка хеша самой базы остаётся без отрицательного входа
            м["files"]["contextd.db"] = sha(копия)

        def разошлись(копия, дерево, м):
            м["counts"]["blobs"] += 1

        def подменён(копия, дерево, м):
            open(os.path.join(дерево, "manifests", "e1.json"), "w").write("{ }")

        def база_не_та(копия, дерево, м):
            м["files"]["contextd.db"] = "0" * 64

        плохие = os.path.join(tmp, "плохие")
        os.makedirs(плохие)
        имя_п = "core-1999-01-01.tar.gz.gpg"
        # контроль: без порчи подделка обязана пройти, иначе падения ниже
        # доказывают не порчу, а расхождение сборки архива с боевой
        подделка(root, tmp, пароль, os.path.join(плохие, имя_п),
                 lambda *a: None)
        проверка(плохие, пароль, root, имя=имя_п)
        for порча, слово in ((утечка, "секреты"), (гниль, "битая"),
                             (разошлись, "счётчики"),
                             (подменён, "manifests/e1.json не совпал"),
                             (база_не_та, "contextd.db не совпал")):
            подделка(root, tmp, пароль, os.path.join(плохие, имя_п), порча)
            try:
                проверка(плохие, пароль, root, имя=имя_п)
                raise AssertionError("%s: проверка промолчала" % слово)
            except RuntimeError as e:
                assert слово in str(e), (слово, str(e))

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
        # `mode=ro` в `снимок` держит бэкап подальше от боевого файла: rw-
        # соединение на закрытии чекпойнтит WAL, то есть пишет в базу.
        # Состояние, в котором это видно, ровно одно: соединений к базе нет,
        # а WAL непустой — то есть демон упал не закрывшись. Как раз тот
        # случай, ради которого в `снимок` написана ветка отката на запись.
        дитя = ("import sys, os; sys.path.insert(0, %r);"
                " import mara_ingest as mi; c = mi.connect(sys.argv[1]);"
                " c.execute(\"insert into events(id,kind,dedupe_key,state)"
                " values('e3','call','k3','done')\"); os._exit(0)"
                % os.path.dirname(os.path.abspath(__file__)))
        subprocess.run([sys.executable, "-c", дитя, root], check=True)
        живая = os.path.join(root, "contextd.db")

        def на_диске():
            # Хеш, а не размер с mtime: PASSIVE-чекпойнт переписывает базу
            # тем же размером и WAL не трогает, так что от записи остаётся один
            # только mtime — а он зависит от гранулярности ФС, не от бэкапа.
            # None за пропавший файл, а не исключение: снёсший WAL мутант обязан
            # падать на assert ниже, а не чужим FileNotFoundError отсюда.
            return [sha(f) if os.path.exists(f) else None
                    for f in (живая, живая + "-wal")]

        было_на_диске = на_диске()
        снимок(живая, os.path.join(tmp, "ro.db"))
        assert на_диске() == было_на_диске, "снимок написал в боевую базу"

        print("core-backup: самопроверка ок")
    finally:
        mi.ОТМЕТКА_НОСИТЕЛЕЙ = боевая_отметка
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)
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
    try:
        targets = mi.носители(a.targets)
    except ValueError as e:
        # Бэкапу, в отличие от сверки, продолжать нечем: писать некуда.
        raise SystemExit(str(e))
    if a.drill_only:
        # Развернуть архив с каталога-обманки значит доложить «восстановление
        # проверено» ровно про ту копию, которой на самом деле нет.
        if not mi.смонтирован(targets[0], a.root):
            raise SystemExit("core-backup: %s на одном устройстве с %s — "
                             "носитель не смонтирован" % (targets[0], a.root))
        r = проверка(targets[0], a.pass_file, a.root)
    else:
        r = прогон(a.root, targets, a.pass_file, a.keep, a.work,
                   аудио=not a.no_audio, drill=not a.no_drill)
    print(json.dumps(r, ensure_ascii=False, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
