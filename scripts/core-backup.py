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
Каждый новый файл зеркала читается с носителя обратно сразу после записи и на
каждом носителе: разворачиваем мы один, а пишем на все.
"""
import os, sys, json, glob, time, shutil, hashlib, sqlite3, tarfile
import argparse, tempfile, subprocess, io, contextlib
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


def гпг_вызов(args, пароль):
    """gpg без pinentry: под кроном терминала нет, а спросить фразу некого."""
    return ["gpg", "--batch", "--yes", "--quiet", "--pinentry-mode",
            "loopback", "--passphrase-file", пароль] + args


def гпг(args, пароль):
    subprocess.run(гпг_вызов(args, пароль), check=True)


def шифр(src, dst, пароль):
    гпг(["--symmetric", "--cipher-algo", "AES256", "-o", dst, src], пароль)
    # gpg отдаёт файл по umask: на носителе это 664, а внутри разговоры
    os.chmod(dst, 0o600)


def дешифр(src, dst, пароль):
    гпг(["-o", dst, "-d", src], пароль)


def sha_шифра(src, пароль):
    """sha256 того, что лежит внутри шифротекста, не выкладывая его на диск.

    Через трубу, а не во временный файл: расшифрованному разговору незачем
    появляться на диске ради одной сверки — ни на носителе, где он оказался бы
    рядом с зеркалом в открытом виде, ни в рабочем каталоге."""
    p = subprocess.Popen(гпг_вызов(["-d", src], пароль),
                         stdout=subprocess.PIPE)
    h = hashlib.sha256()
    try:
        for кусок in iter(lambda: p.stdout.read(1 << 20), b""):
            h.update(кусок)
    finally:
        p.stdout.close()
        код = p.wait()
    if код:
        # Свой отказ, а не хеш прочитанного: на порченом шифротексте gpg
        # отдаёт ненулевой код и обрубок потока, и молчаливый хеш обрубка
        # означал бы «сверили» там, где не расшифровалось вовсе.
        raise subprocess.CalledProcessError(код, "gpg -d %s" % src)
    return h.hexdigest()


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
    инкрементальность получается бесплатно.

    Каждый новый файл читается с носителя обратно и расшифровывается: хеш
    обязан сойтись с записанным в `blobs`. Побайтной сверки, как у архива, тут
    быть не может — на каждый носитель идёт свой `gpg`, и два шифротекста
    одного файла разные, — так что оракул остаётся один: содержимое."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    новых, байт, убрано, без_файла, битых = 0, 0, 0, 0, 0
    try:
        for r in con.execute("select sha256, path, purged_at from blobs"):
            рядом = os.path.join(target, os.path.relpath(r["path"], root) + ".gpg")
            if r["purged_at"]:
                if os.path.exists(рядом):
                    try:
                        os.unlink(рядом)
                        убрано += 1
                    except OSError as e:
                        # Тот же счёт, что у неудавшейся записи: носитель, с
                        # которого не убрать вычищенное, ушёл в read-only или
                        # отвалился — и ронять из-за него соседний носитель,
                        # ротацию и учение незачем.
                        print("core-backup: аудио %s не убрано из %s (%s)"
                              % (r["sha256"][:12], target, e), file=sys.stderr)
                        битых += 1
                continue
            if not os.path.exists(r["path"]):
                # строка есть, файла нет — молчать об этом нельзя: ровно эту
                # тихую потерю бэкап и должен показывать
                без_файла += 1
                continue
            if os.path.exists(рядом):
                continue
            try:
                # Каталог создаём внутри `try`: на забитом или ушедшем в
                # read-only носителе падает именно `makedirs`, и это ровно тот
                # отказ, ради которого счётчик и заведён.
                os.makedirs(os.path.dirname(рядом), mode=0o700, exist_ok=True)
                # ponytail: два gpg на файл — при десятках записей в сутки это
                # копейки; вырастет на порядки — шифровать помесячным таром
                шифр(r["path"], рядом + ".tmp", пароль)
                # Порядок тот же, что у архива: сверяем под временным именем,
                # чтобы копия, прочитавшаяся не тем, под боевым не появилась
                # ни на секунду.
                сбросить_кеш(рядом + ".tmp")
                if sha_шифра(рядом + ".tmp", пароль) != r["sha256"]:
                    raise OSError("расшифровалось не тем, чем шифровали")
                # Размер снимаем до переименования и с временного имени, а
                # `os.replace` идёт последним: отказ после него означал бы
                # «сбойный» про файл, который уже лежит в зеркале под боевым
                # именем, и назавтра его никто не переписал бы.
                размер = os.path.getsize(рядом + ".tmp")
                os.replace(рядом + ".tmp", рядом)
            except (OSError, subprocess.CalledProcessError) as e:
                # Не падаем здесь: у зеркала много файлов и носителей больше
                # одного, и один сбойный файл не повод бросить остальные и
                # соседний носитель. Считаем и валимся в конце прогона.
                print("core-backup: аудио %s не легло в %s (%s)"
                      % (r["sha256"][:12], target, e), file=sys.stderr)
                try:
                    os.unlink(рядом + ".tmp")
                except OSError:
                    # Огрызка может не быть вовсе — упасть мог сам `шифр`.
                    pass
                битых += 1
                continue
            новых += 1
            байт += размер
    finally:
        con.close()
    return {"новых": новых, "байт": байт, "убрано_вычищенных": убрано,
            "без_файла": без_файла, "битых": битых}


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
        сверено = проверить_аудио(db, root, target, пароль)
        return {"архив": os.path.basename(src), "файлов": len(м["files"]),
                "счётчики": было, "аудио_сверено": сверено}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def проверить_аудио(db, root, target, пароль):
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
        if sha_шифра(рядом, пароль) != r["sha256"]:
            raise RuntimeError("проверка: аудио %s расшифровалось не тем"
                               % r["sha256"][:12])
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


def сбросить_кеш(путь):
    """Дописанное — на носитель, прочитанное потом — мимо кеша страниц.

    `fsync` тут не про долговечность: на сетевой шаре отложенная ошибка записи
    всплывает только на нём, а до него пишущий — `copy2` у архива, `gpg` у
    зеркала аудио — рапортует успех. `DONTNEED` роняет страницы файла: иначе
    хеш считался бы по тому же кешу, из которого мы и писали, и копия
    сверялась бы сама с собой."""
    with open(путь, "rb+") as fh:
        # `rb+`, а не `rb`: на сетевых ФС сброс на сервер просят по пишущему
        # хендлу, с читающего клиент может и промолчать.
        os.fsync(fh.fileno())
        if hasattr(os, "posix_fadvise"):        # Linux; на другом живём вслепую
            os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)


def сверить_копию(путь, ожидаемый):
    """Прочитать записанное с носителя и сравнить с хешем оригинала."""
    сбросить_кеш(путь)
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
                try:
                    # Огрызок убираем, иначе на носителе, куда перестало
                    # писаться, за год накопится триста скрытых файлов:
                    # ротация ходит по `core-*` и этих имён не видит.
                    os.unlink(врем)
                except OSError:
                    # Убраться не обязано: огрызка может не быть вовсе (упал
                    # `makedirs`), а носитель мог уйти в read-only ровно той
                    # же бедой. Падение здесь унесло бы прогон мимо остальных
                    # носителей, отметки, учения и ротации — то есть отказ
                    # одного носителя стоил бы всей ночной работы.
                    pass
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
        # Разворачиваем один носитель, а не все: остальные копии архива
        # побайтно сверены с тем же `enc` при записи, значит развернутся так
        # же, а gpg на каждый носитель каждую ночь — минуты на ровном месте.
        # На зеркало аудио вывод не переносится: `зеркало_аудио` шифрует на
        # каждый носитель отдельно, и копии не равны побайтно. Поэтому там
        # своя сверка — при записи каждого нового файла и на каждом носителе.
        итог["проверка"] = проверка(записано[0], пароль, root, имя=имя)
        итог["проверка"]["секунд"] = round(time.time() - t0, 1)
    # ротация последней: свежий архив, не прошедший проверку, не имеет права
    # вытеснить старый, который разворачивался
    for t in записано:
        ротация(t, keep)
    битые = {t: з["битых"] for t, з in зв.items() if з["битых"]}
    if битые:
        # Полем в итоге отделаться нельзя: `core-backup.log` не читает ни одна
        # программа, и тихое число было бы ровно той тишиной, из-за которой
        # заведён #45, только этажом ниже. Роняем прогон — так же громко, как
        # его роняет несошедшееся учение. Итог печатаем перед этим: ночные
        # числа и результат учения из-за одного файла терять незачем.
        # После ротации, а не до: архив свою сверку на носителе прошёл, и
        # держать ротацию заложником аудио значило бы копить архивы сверх
        # `keep` из-за беды, к архивам не относящейся.
        print(json.dumps(итог, ensure_ascii=False, indent=1, sort_keys=True),
              file=sys.stderr)
        raise RuntimeError("зеркало аудио: сбойных %s"
                           % "; ".join("%s — %d" % (t, n)
                                       for t, n in sorted(битые.items())))
    return итог


def ротация(target, keep):
    for огрызок in glob.glob(os.path.join(target, ".core-*.tmp")):
        # Огрызок неудачной ночи, который не удалось убрать тогда же (носитель
        # ушёл в read-only). Имя с датой — следующая ночь его не перезапишет, а
        # `core-*` ниже точечных имён не видит: без этой уборки он лежал бы на
        # носителе вечно. Сюда доходят только записанные носители, то есть те,
        # на которых писать снова получается.
        try:
            os.unlink(огрызок)
        except OSError:
            # По той же причине, что и уборка при записи, только цена выше:
            # архив, отметка, зеркало и учение к этому месту уже прошли, и
            # падение здесь стоило бы удачной ночи задним числом — прогон
            # не напечатал бы итог, а ротация не отработала бы на остальных
            # носителях. Не убрался — полежит ещё сутки.
            pass
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

        # #45: копию аудио на носителе не проверяло ничто. Архив с #44
        # читается обратно на каждом носителе, а аудио — только на том одном,
        # который разворачивали, и только три свежайших записи. Ломаем запись
        # на втором носителе двумя способами: прогон обязан положить файл на
        # первый, не положить на второй, отротировать оба и упасть.
        os.unlink(второй)
        os.makedirs(второй, mode=0o700)
        тело2 = b"audio-number-two"
        s2 = hashlib.sha256(тело2).hexdigest()
        p2 = mi.blob_path(root, s2, "wav")
        os.makedirs(os.path.dirname(p2), exist_ok=True)
        open(p2, "wb").write(тело2)
        con = mi.connect(root)
        con.execute("insert into blobs(sha256,path,bytes,mime,created)"
                    " values(?,?,?,?,?)",
                    (s2, p2, len(тело2), "audio", mi.now_iso()))
        con.close()
        зерк2 = os.path.join(второй, os.path.relpath(p2, root) + ".gpg")
        цел2 = os.path.join(target, os.path.relpath(p2, root) + ".gpg")
        мод = sys.modules[__name__]
        целый_шифр = мод.шифр

        def не_gpg(src, dst, пароль):
            # Только зеркало второго носителя: архив шифруется в рабочем
            # каталоге, и подмена по имени носителя его не задевает.
            if dst.startswith(второй):
                open(dst, "wb").write(b"not a gpg packet")
                return
            целый_шифр(src, dst, пароль)

        def не_тот_файл(src, dst, пароль):
            # Шифротекст валиден, внутри не то аудио: так выглядит носитель,
            # подменивший байты не насмерть. Первую порчу ловит код возврата
            # gpg, эту — только хеш, и без него она прошла бы молча.
            целый_шифр(не_то if dst.startswith(второй) else src, dst, пароль)

        for кривой, слово in ((не_gpg, "не gpg"), (не_тот_файл, "не то аудио")):
            for дата in ("1999-01-01", "1999-01-02", "1999-01-03"):
                стар = os.path.join(target, "core-%s.tar.gz.gpg" % дата)
                open(стар, "w").close()
                os.chmod(стар, 0o600)
            мод.шифр = кривой
            перехват = io.StringIO()
            try:
                with contextlib.redirect_stderr(перехват):
                    прогон(**общее)
                raise AssertionError("битое зеркало прошло молча (%s)" % слово)
            except RuntimeError as e:
                assert "зеркало аудио" in str(e), (слово, str(e))
                assert "%s — 1" % второй in str(e), (слово, str(e))
            finally:
                мод.шифр = целый_шифр
            шум = перехват.getvalue()
            # Сбойный файл обязан быть назван в `stderr` поимённо: в отказе
            # стоит только счёт по носителям, и без этой строки с чего
            # начинать разбирательство — неизвестно.
            assert "не легло" in шум and s2[:12] in шум, (слово, шум[-400:])
            # И итог ночи обязан быть напечатан до падения: числа и результат
            # учения из-за одного файла не теряются.
            assert '"битых"' in шум and '"проверка"' in шум, (слово, шум[-400:])
            assert not os.path.exists(зерк2), слово
            # Огрызок в зеркале не подметает никто: ротация ходит по `core-*`
            # и этих имён не видит, а имя у него всегда одно — назавтра под
            # ним лежала бы позавчерашняя порча, и `os.path.exists(рядом)`
            # её бы не заметил.
            assert not os.path.exists(зерк2 + ".tmp"), слово
            # Носитель, где запись цела, обязан получить свой файл, а ротация
            # — отработать: падение идёт после них обоих, иначе одна битая
            # запись на одном носителе стоила бы всей ночной работы.
            assert os.path.exists(цел2), слово
            assert not glob.glob(os.path.join(target, "core-1999-01-0[12]*")), \
                слово
            # Файла в зеркале нет — значит следующая ночь попробует снова.
            os.unlink(цел2)

        # Заставу «не расшифровалось» саму по себе прогон не покажет: без
        # своего отказа gpg-обрубок вернул бы хеш пустоты, тот ни с чем не
        # сойдётся, и беда «не читается» доложилась бы как «прочиталось не
        # то». Беды разные, и первый шаг по ним разный.
        мусор = os.path.join(tmp, "not-gpg.bin")
        open(мусор, "wb").write(b"not a gpg packet")
        try:
            sha_шифра(мусор, пароль)
            raise AssertionError("нерасшифруемое отдало хеш")
        except subprocess.CalledProcessError:
            pass

        # Сброс кеша на этой ФС не наблюдаем: пишем и читаем один и тот же
        # кеш, и хеш сойдётся с ним и без сброса — а на сетевой шаре без него
        # копия сверялась бы сама с собой, ради чего всё и заведено.
        # Наблюдаемы только сами просьбы и их порядок, их и пишем: сброс
        # обязан идти перед хешем, иначе на шаре хеш снова считался бы по
        # тому же кешу, из которого писали. Заодно третья ночь с целым
        # `шифр` обязана положить файл, который две прошлые не клали.
        целый_сброс, целый_sha = мод.сбросить_кеш, мод.sha_шифра
        просили = []
        мод.сбросить_кеш = lambda п: (просили.append(("сброс", п)),
                                      целый_сброс(п))[1]
        мод.sha_шифра = lambda п, пар: (просили.append(("хеш", п)),
                                        целый_sha(п, пар))[1]
        try:
            вернули = зеркало_аудио(os.path.join(root, "contextd.db"), root,
                                    второй, пароль)
        finally:
            мод.сбросить_кеш, мод.sha_шифра = целый_сброс, целый_sha
        assert вернули == {"новых": 1, "байт": вернули["байт"], "битых": 0,
                           "убрано_вычищенных": 0, "без_файла": 0}, вернули
        assert просили == [("сброс", зерк2 + ".tmp"),
                           ("хеш", зерк2 + ".tmp")], просили
        assert os.path.exists(зерк2), зерк2

        # Три заставы, до которых прогон выше не дотягивается: он ломает
        # ровно один файл, и тот последний в обходе. Нужен носитель, где
        # сбойная запись идёт перед целой, и живой файл, не отвечающий своему
        # хешу.
        третий = os.path.join(tmp, "target3")
        os.makedirs(третий, mode=0o700)
        тело3 = b"audio-number-three"
        # Хеш чужой: так выглядит живой файл, переставший отвечать имени, под
        # которым он числится в базе. Оракул зеркала — `blobs.sha256`; на хеше
        # самого файла, снятом тут же (очевидная замена), такая запись уехала
        # бы в зеркало молча.
        s3 = hashlib.sha256(тело3 + b"-not").hexdigest()
        p3 = mi.blob_path(root, s3, "wav")
        тело4 = b"audio-number-four"
        s4 = hashlib.sha256(тело4).hexdigest()
        p4 = mi.blob_path(root, s4, "wav")
        con = mi.connect(root)
        # Порядок вставки и есть порядок обхода: `зеркало_аудио` читает blobs
        # без `order by`, то есть по rowid. Сбойная запись обязана идти
        # первой — иначе `break` вместо `continue` в цикле неотличим.
        for s, p, тело in ((s3, p3, тело3), (s4, p4, тело4)):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(тело)
            con.execute("insert into blobs(sha256,path,bytes,mime,created)"
                        " values(?,?,?,?,?)",
                        (s, p, len(тело), "audio", mi.now_iso()))
        con.close()
        зерк3 = os.path.join(третий, os.path.relpath(p3, root) + ".gpg")
        зерк4 = os.path.join(третий, os.path.relpath(p4, root) + ".gpg")
        вернули = зеркало_аудио(os.path.join(root, "contextd.db"), root,
                                третий, пароль)
        assert вернули["битых"] == 1, вернули
        assert not os.path.exists(зерк3), зерк3
        # Запись после сбойной обязана лечь: один битый файл не отменяет ночь
        # на этом носителе.
        assert os.path.exists(зерк4), зерк4

        def шифр_упал(src, dst, пароль):
            # gpg упал, не записав ничего: огрызка нет вовсе, и уборка после
            # отказа обязана это пережить. Живой без зеркала тут ровно один.
            raise subprocess.CalledProcessError(2, "gpg")

        мод.шифр = шифр_упал
        try:
            вернули = зеркало_аудио(os.path.join(root, "contextd.db"), root,
                                    третий, пароль)
        finally:
            мод.шифр = целый_шифр
        assert вернули["битых"] == 1 and вернули["новых"] == 0, вернули

        # Носитель, с которого не убрать вычищенное, тоже сбойный, а не повод
        # уронить прогон: read-only USB — та же беда, что и не принявший
        # запись. Каталогом вместо файла, а не правами: под root права
        # ничего не значат, а `unlink` каталога не проходит ни у кого.
        con = mi.connect(root)
        con.execute("update blobs set purged_at=? where sha256=?",
                    (mi.now_iso(), s4))
        con.close()
        os.unlink(зерк4)
        os.makedirs(зерк4)
        вернули = зеркало_аудио(os.path.join(root, "contextd.db"), root,
                                третий, пароль)
        assert вернули["битых"] == 2, вернули
        assert вернули["убрано_вычищенных"] == 0, вернули
        os.rmdir(зерк4)

        # Носитель, на котором не создаётся даже каталог: забитый диск,
        # read-only, отвалившийся USB. Падает `makedirs`, до `gpg` дело не
        # доходит — и это ровно тот отказ, ради которого счётчик заведён.
        # Файлом вместо каталога, а не правами: под root права молчат.
        четвёртый = os.path.join(tmp, "target4")
        os.makedirs(четвёртый, mode=0o700)
        open(os.path.join(четвёртый, "calls"), "w").close()
        вернули = зеркало_аудио(os.path.join(root, "contextd.db"), root,
                                четвёртый, пароль)
        # Живых записей здесь две: всё, что было до `s2`, самопроверка
        # погасила выше, `s4` погашен только что.
        assert вернули["новых"] == 0 and вернули["битых"] == 2, вернули

        # Носитель, ушедший в read-only на самом переименовании: каталог
        # создался, `gpg` отработал, сверка сошлась — и всё равно отказ.
        # Проверяем, что и он считается, а не уносит прогон, и что огрызок
        # под временным именем в зеркале не остаётся.
        пятый = os.path.join(tmp, "target5")
        os.makedirs(пятый, mode=0o700)
        зерк5 = os.path.join(пятый, os.path.relpath(p2, root) + ".gpg")

        def replace_упал(src, dst):
            raise OSError("носитель ушёл в read-only")

        целый_replace, os.replace = os.replace, replace_упал
        try:
            вернули = зеркало_аудио(os.path.join(root, "contextd.db"), root,
                                    пятый, пароль)
        finally:
            os.replace = целый_replace
        assert вернули["новых"] == 0 and вернули["битых"] == 2, вернули
        assert not os.path.exists(зерк5), зерк5
        assert not os.path.exists(зерк5 + ".tmp"), зерк5

        # Возвращаем базу в состояние, которого ждут проверки ниже: живая
        # запись, не покрытая зеркалом, красит контрольный прогон `подделка`
        # — `проверить_аудио` пойдёт искать её на носителе-обманке и не
        # найдёт. Файлы на диске оставляем, как и у первой записи.
        con = mi.connect(root)
        con.execute("update blobs set purged_at=? where sha256 in (?,?)",
                    (mi.now_iso(), s2, s3))
        con.close()

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

        # `подделка` всегда кладёт и базу, и манифест, поэтому два сторожа
        # ею не проверить: тот, что стоит на отсутствии базы, и тот, что не
        # пускает наружу имена из тара. Их обоих до сих пор не проверяло
        # ничто — оба переживали мутацию при зелёном гейте.
        def кривой_архив(члены):
            сырой = os.path.join(tmp, "кривой.tar.gz")
            with tarfile.open(сырой, "w:gz") as tf:
                for arcname, откуда in члены:
                    tf.add(откуда, arcname=arcname)
            шифр(сырой, os.path.join(плохие, имя_п), пароль)
            os.unlink(сырой)

        пустой_м = os.path.join(tmp, "чужой-manifest.json")
        with open(пустой_м, "w", encoding="utf-8") as fh:
            json.dump({"counts": {}, "files": {}}, fh)
        # Сторож на отсутствии базы стоит до `sqlite3.connect` не случайно:
        # connect создал бы пустой файл сам, и проверять после него было бы
        # нечего. Мутация «сторожа нет» отсюда и видна.
        кривой_архив([("manifest.json", пустой_м)])
        try:
            проверка(плохие, пароль, root, имя=имя_п)
            raise AssertionError("архив без базы прошёл проверку")
        except RuntimeError as e:
            assert "нет contextd.db" in str(e), str(e)
        # `filter="data"` — единственное, что стоит между архивом и записью
        # мимо каталога распаковки. Свой архив таких имён не содержит, но
        # разворачиваем мы то, что лежит на носителе, а не то, что писали.
        кривой_архив([("contextd.db", os.path.join(root, "contextd.db")),
                      ("manifest.json", пустой_м), ("../беда", пустой_м)])
        try:
            проверка(плохие, пароль, root, имя=имя_п)
            raise AssertionError("путь наружу прошёл распаковку")
        except tarfile.TarError:
            pass

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
