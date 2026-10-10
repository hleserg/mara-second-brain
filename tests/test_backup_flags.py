"""Флаги `core-backup.py` доезжают до `прогон` (issue #55).

`main()` — единственное место, где флаги превращаются в аргументы, и до сих
пор туда доходили два теста, оба отрицательные: несмонтированная цель
отвергнута (`tests/test_backup_mount.py`) и пробел в пути валит прогон ещё в
`mi.носители` (`tests/test_backup_shell.py:33`). Удачного входа не было ни
одного, поэтому четыре мутанта в разборе аргументов проходили полный гейт —
`--drill-only` с заглушкой вместо `проверка(...)`, `аудио=True` и `drill=True`
вместо `not a.no_*`, `--keep`, подменённый константой.

Гоняем через `subprocess`, а не вызовом `прогон` напрямую: проверяется ровно
дорога от флага до поведения, и вызов `прогон` мимо `main()` эту дорогу как
раз и перепрыгивает. `MARA_STATE` уводим во временный каталог — иначе
подпроцесс пишет отметку носителей в боевой `~/.local/state/mara` хозяина
машины.

По кругу 1 ревью добавлены заставы ещё на два флага: `--work` (мутант с путём
по умолчанию тесты переживал и вдобавок писал в общий `/var/tmp`) и
`--pass-file` (подмена на боевой путь самосогласованна — одним файлом и
шифруют, и расшифровывают, — поэтому её ловит только попытка открыть архив
**нашей** фразой снаружи).

Круг 2 нашёл, что застава главного теста всё равно держалась на достижимом
оракуле: заглушка, считающая счётчики по **живой** базе, отдаёт ровно то же,
что записано в архив, и проходит весь гейт. Поэтому живая база теперь
разведена с архивом лишней строкой, а `--root` спрашивается не счётом блобов —
это свойство машины, а не кода, — а наличием **своего** блоба в базе изнутри
архива.
"""
import glob, hashlib, json, os, random, shutil, sqlite3, subprocess, sys
import tarfile, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import mara_ingest as mi

СКРИПТ = os.path.join(ROOT, "scripts", "core-backup.py")


@unittest.skipUnless(shutil.which("gpg"), "нет gpg")
class Флаги(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "blobs")
        self.цель = os.path.join(self.tmp, "target")
        self.пароль = os.path.join(self.tmp, "pass")
        open(self.пароль, "w").write("проверочная фраза\n")
        os.chmod(self.пароль, 0o600)
        con = mi.connect(self.root)
        тело = b"audio-for-flags"
        s = self.sha = hashlib.sha256(тело).hexdigest()
        p = mi.blob_path(self.root, s, "wav")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(тело)
        con.execute("insert into blobs(sha256,path,bytes,mime,created) "
                    "values(?,?,?,?,?)",
                    (s, p, len(тело), "audio", mi.now_iso()))
        # Круг 5: разводка обязана быть неотличима от боевого звонка не только
        # по форме столбцов, но и по ссылкам. Её `device_id` не был заведён в
        # `devices`, а в бою событие попадает в `put_event` только после
        # `scope_ok` (`contextd.py:603`) — то есть отсев
        # `device_id in (select id from devices)` уносил ровно разводку и ни
        # одной боевой строки. Заглушка с этим отсевом проходила весь гейт, не
        # открыв архива вовсе (перегнано). Устройство теперь есть, и звонков в
        # архиве двое: единственная строка в таблице оракулом не бывает —
        # любой предикат, ложный на ней одной, отдаёт правильный ответ.
        con.execute("insert into devices(id,token_sha256,created) "
                    "values(?,?,?)", ("dev_тест", "0" * 64, mi.now_iso()))
        # Число засевов случайное: контрольная заглушка круга 5 просто
        # зашивала правдоподобные числа фикстуры («events: 2, devices: 1») и
        # проходила все четыре теста, не открыв архива. Зашить то, что при
        # каждом прогоне другое, нельзя. Любое n >= 2 ведёт себя одинаково:
        # `проверить_аудио` берёт `ПРОБА` = 3 свежайших блоба, а их тут n + 1.
        for i in range(random.randrange(2, 6)):
            т = b"seed-%d" % i
            ш = hashlib.sha256(т).hexdigest()
            пс = mi.blob_path(self.root, ш, "wav")
            os.makedirs(os.path.dirname(пс), exist_ok=True)
            open(пс, "wb").write(т)
            con.execute("insert into blobs(sha256,path,bytes,mime,created) "
                        "values(?,?,?,?,?)",
                        (ш, пс, len(т), "audio", mi.now_iso()))
            # каждому засеву свой блоб: ключ идемпотентности звонка —
            # `blob:<sha>` (`mara_ingest.py:152-161`), на общем блобе оба
            # засева схлопнутся в одну строку, а разводка вернётся дублем и
            # строки не заведёт вовсе — тест останется зелёным, а заглушка
            # выживет молча
            mi.put_event(con, {"kind": "call", "source": "phone",
                               "source_id": "фон-%d" % i,
                               "device_id": "dev_тест",
                               "blob": {"sha256": ш, "ext": "wav"},
                               "payload": {"направление": "in"}})
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def запуск(self, *флаги):
        r = subprocess.run(
            [sys.executable, СКРИПТ, "--root", self.root,
             "--targets", self.цель, "--pass-file", self.пароль,
             "--work", os.path.join(self.tmp, "work")]
            + list(флаги),
            capture_output=True, text=True,
            env={**{k: v for k, v in os.environ.items()
                    if k != "MARA_CORE_SNAPSHOTS"},
                 # без переменной каталога снимков: иначе на машине, где она
                 # выставлена, `--snapshot` писал бы в боевой каталог и
                 # оставался зелёным — ожидание берётся из того же `mi.снимки`
                 "MARA_BACKUP_ALLOW_SAME_DEV": "1",
                 "MARA_STATE": os.path.join(self.tmp, "state")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout), r

    def зеркало(self):
        return glob.glob(os.path.join(self.цель, "calls", "*", "*", "*.gpg"))

    def test_drill_only_разворачивает_боевой_архив(self):
        """Отрицательный вход про обманку уже есть; удачный не гонялся нигде,
        так что заглушка вместо `проверка(...)` проходила гейт целиком."""
        r0, _ = self.запуск()
        лежит = [os.path.basename(f) for f in
                 glob.glob(os.path.join(self.цель, "core-*.tar.gz.gpg"))]
        # Оракул обязан быть недостижим для кода под тестом. Правда о том,
        # что лежит в архиве, снята в момент записи, и копий этой правды на
        # машине три: сайдкар рядом с архивом (круг 1), живая база (круг 2) и
        # зеркало аудио на носителе (круг 3). Каждая из трёх в свой круг
        # уносила весь гейт, поэтому обезврежены все три: сайдкар портим
        # целиком, живую базу разводим лишней строкой в `events`, а правду
        # держим снимком в памяти теста — единственной копией, до которой
        # коду не дотянуться.
        #
        # Круги 1 и 2 разводили `blobs`, и это была ровно та ошибка: столбец,
        # который заглушка умеет пересчитать из другого источника, оракулом
        # не становится. Круг 4 нашёл, что и `events` пересчитывается —
        # прежняя редакция этого комментария утверждала, что «ни счётом
        # файлов зеркала, ни отсевом мёртвых строк её не восстановить», и
        # была неправа дважды: `events` целой отдавал сайдкар (портили-то
        # одно поле из девяти), а сырой `insert` был отличим от боевого по
        # форме. Обе дыры закрыты ниже, обе перегнаны заглушкой.
        #
        # Строку заводит `put_event`, а не сырой `insert`. Сырая отличалась
        # от боевой каждым столбцом — `dedupe_key IS NULL`, `kind` не из
        # четырёх видов (`contextd.py:45-46`), `id` не того формата, — и
        # заглушка со счётом `where dedupe_key is not null` весь гейт
        # проходила (перегнал на модели: выживает). Через `put_event` строка
        # неотличима от настоящего звонка ни одним столбцом — а с круга 5 и по
        # ссылке на `devices`, которой ей не хватало (см. `setUp`): отсев по
        # незарегистрированному устройству сносил ровно её.
        #
        # Настоящая `проверка` порчи не видит, и механизм тут называется
        # точно: живую базу она **не открывает вовсе** — и счётчики, и сверку
        # аудио считает по базе внутри архива (`core-backup.py:273-301`).
        # Прежняя редакция этого комментария ссылалась на `continue` в
        # `проверить_аудио`; ветка не исполняется ни разу (проверено пробой:
        # `continue` → `raise`, все четыре теста зелены).
        путь = os.path.join(
            self.цель,
            r0["архив"].replace(".tar.gz.gpg", ".manifest.json"))
        сайдкар = json.load(open(путь, encoding="utf-8"))
        было = сайдкар["counts"]
        # Портим сайдкар ЦЕЛИКОМ, и это механика, а не обещание: круг 4
        # портил один `counts` из пяти полей и писал «портим целиком», а круг 5
        # показал, что целого `created` хватает — заглушка считала события
        # живой базы с отсечкой `received <= created` и проходила весь гейт.
        # Каждое значение подменяется заведомо ложным того же типа,
        # рекурсивно: достать из сайдкара нечего вообще.
        def испортить(v):
            if isinstance(v, dict):
                return {k: испортить(x) for k, x in v.items()}
            if isinstance(v, list):
                return []
            if isinstance(v, bool):
                return not v
            if isinstance(v, (int, float)):
                return 999
            if isinstance(v, str):
                return "испорчено"
            return None

        json.dump(испортить(сайдкар), open(путь, "w", encoding="utf-8"))
        # Разводка живой базы держится на том, что `events` вообще считают.
        # Уберут таблицу из `ТАБЛИЦЫ` (`core-backup.py:42`) — оракул исчезнет
        # молча, а заглушка круга 2 оживёт (перегнано: оживает).
        self.assertIn("events", было, "разводку живой базы больше не считают")
        живая = mi.connect(self.root)
        mi.put_event(живая, {"kind": "call", "source": "phone",
                             "source_id": "разводка", "device_id": "dev_тест",
                             "blob": {"sha256": self.sha, "ext": "wav"},
                             "payload": {"направление": "in"}})
        живая.commit()
        живая.close()
        r, _ = self.запуск("--drill-only")
        self.assertEqual(r["архив"], r0["архив"], "развернули не тот архив")
        self.assertIn(r["архив"], лежит,
                      "архива с таким именем на носителе нет")
        self.assertEqual(r["счётчики"], было,
                         "счётчики не из базы внутри архива")
        # Расшифровка стоит **после** прогона, и место здесь не косметика.
        # Открытая копия архива — четвёртый источник правды, и достижимый:
        # она ложится в `self.tmp`, а это ровно `dirname(--work)`. Заглушка,
        # которая пароль не трогает вовсе, а `counts`, `files` и базу берёт
        # из этой копии, весь гейт проходила (перегнано: выживает). Пока
        # копии рядом нет, взять её коду неоткуда.
        #
        # `--pass-file` доехал ровно тогда, когда архив открывается **нашей**
        # фразой. Подмена пути на боевой по умолчанию самосогласованна — одним
        # и тем же файлом и шифруют, и расшифровывают, — поэтому на машине, где
        # тот файл есть, она переживает всё остальное в этом тесте.
        открыт = subprocess.run(
            ["gpg", "--batch", "--yes", "--quiet",
             "--pinentry-mode", "loopback",
             "--passphrase-file", self.пароль, "-o",
             os.path.join(self.tmp, "открыт.tar.gz"), "-d",
             os.path.join(self.цель, r0["архив"])],
            capture_output=True, text=True)
        self.assertEqual(открыт.returncode, 0,
                         "--pass-file не доехал: архив нашей фразой не "
                         "открылся\n"
                         + открыт.stderr)
        # И `--root`. Считать блобы для этого нельзя: «на стенде один, а у
        # боевого корня не один» — свойство машины, а не кода. Как только в
        # чужом корне окажется ровно один блоб, мутант такую заставу переживёт
        # (перегнал на модели: выживает). Спрашиваем про **свой** блоб по
        # хешу — он в базе внутри архива тогда и только тогда, когда архив
        # собран с нашего корня, при любом содержимом чужого.
        with tarfile.open(os.path.join(self.tmp, "открыт.tar.gz")) as tf:
            изнутри = os.path.join(self.tmp, "из-архива.db")
            open(изнутри, "wb").write(tf.extractfile("contextd.db").read())
        внутри = sqlite3.connect(изнутри)
        свой = внутри.execute("select count(*) from blobs where sha256=?",
                              (self.sha,)).fetchone()[0]
        внутри.close()
        self.assertEqual(свой, 1, "--root не доехал: в архиве не наша база")
        self.assertGreater(r["файлов"], 0,
                           "манифест пуст — архив не разворачивали")
        # Аудио сверяется отдельно от файлов манифеста: расшифрованная копия
        # из зеркала обязана сойтись с живым блобом по хешу.
        # Свежайших перечитано `ПРОБА` = 3; остальные засевы (их n − 2) идут
        # в круг старых, и копия обязана быть у каждой строки (Т3.1, #39).
        self.assertEqual(r["аудио_сверено"], 3, r)
        всего = r["аудио_сверено"] + r["аудио_перечитано"]
        self.assertEqual(r["зеркало_проверено"], len(self.зеркало()), r)
        self.assertLessEqual(всего, r["зеркало_проверено"], r)

    def test_пропажа_старой_копии_из_зеркала_роняет_учение(self):
        """Полнота зеркала — по всем строкам, не по трём свежайшим (Т3.1,
        #39): копия, стёртая с носителя месяц назад, видна учению."""
        self.запуск("--no-drill")
        копии = sorted(self.зеркало(), key=os.path.getmtime)
        self.assertGreaterEqual(len(копии), 3)
        # старейшая по имени — не из трёх свежайших по `created`, если их
        # больше трёх; при ровно трёх стирается любая — всё равно должна быть видна
        os.unlink(копии[0])
        r = subprocess.run(
            [sys.executable, СКРИПТ, "--root", self.root, "--targets", self.цель,
             "--pass-file", self.пароль, "--work", os.path.join(self.tmp, "work"),
             "--drill-only"],
            capture_output=True, text=True,
            env={**os.environ, "MARA_BACKUP_ALLOW_SAME_DEV": "1",
                 "MARA_STATE": os.path.join(self.tmp, "state")})
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("нет в зеркале", r.stderr)
        self.assertNotIn("и ещё", r.stderr, "пропала ровно одна копия")

    def test_no_audio_и_no_drill_доезжают_до_прогона(self):
        """Оба флага проверяются одним прогоном: пара `--no-audio --no-drill`
        осталась в рунбуке как «только архив». Что `--no-audio` ходит и в
        одиночку — соседний тест ниже; здесь проверяется, что второй флаг
        по-прежнему убирает учение целиком."""
        r, _ = self.запуск("--no-audio", "--no-drill")
        self.assertNotIn("проверка", r, "учение прошло вопреки --no-drill")
        self.assertEqual(self.зеркало(), [],
                         "аудио зеркалилось вопреки --no-audio")
        # Носитель при этом записан: флаги убирают работу, а не прогон. Эта же
        # строка держит `--targets`: подмена на путь по умолчанию даст другой.
        self.assertEqual(r["носители"], [self.цель], r)
        # И `--work`: `прогон` создаёт рабочий каталог первым делом
        # (`core-backup.py:392`), так что его отсутствие означает, что работа
        # шла мимо каталога теста — в общий `/var/tmp`, куда пишет кто угодно.
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "work")),
                        "--work не доехал: работа шла мимо каталога теста")

    def test_no_audio_в_одиночку_ночь_со_звонком_не_роняет(self):
        """Прежде это была ловушка: `--no-audio` в одиночку ронял любую ночь
        со звонком, потому что учение требовало аудио из зеркала, которого
        при этом флаге нет. По #63 учение сверяет то, что зеркалили.

        Зелёного молчания при этом быть не должно: ноль сверенных записей и
        «не сверяли вовсе» — разные вещи, и в отчёте они разными и остаются.
        Поэтому проверяется не только код возврата, но и то, что причина
        названа и в итоге, и в `stderr`."""
        r, п = self.запуск("--no-audio")
        self.assertIn("проверка", r, "учение не прошло вопреки #63")
        self.assertNotIn("аудио_сверено", r["проверка"],
                         "ноль сверенных выдан за проверку, которой не было")
        self.assertEqual(r["проверка"]["аудио_не_сверялось"],
                         "зеркала не делали (--no-audio)", r)
        self.assertEqual(self.зеркало(), [],
                         "аудио зеркалилось вопреки --no-audio")
        self.assertIn("аудио не сверялось", п.stderr)
        self.assertIn("--no-audio", п.stderr)

    def test_drill_only_после_ночи_без_зеркала(self):
        """Третий вход в ту же ловушку (#63): ночь прошла с `--no-audio`,
        а учение владелец гоняет руками потом. Зеркала на носителе нет, и
        `--drill-only` в одиночку падает — это правильно, он не знает, что
        зеркала не делали намеренно. Сказать ему об этом можно тем же
        флагом."""
        self.запуск("--no-audio", "--no-drill")
        self.assertEqual(self.зеркало(), [], "зеркало появилось вопреки флагу")

        падение = subprocess.run(
            [sys.executable, СКРИПТ, "--root", self.root,
             "--targets", self.цель, "--pass-file", self.пароль,
             "--drill-only"],
            capture_output=True, text=True,
            env={**os.environ,
                 "MARA_BACKUP_ALLOW_SAME_DEV": "1",
                 "MARA_STATE": os.path.join(self.tmp, "state")})
        self.assertNotEqual(падение.returncode, 0, падение.stdout)
        self.assertIn("нет в зеркале", падение.stderr)

        r, п = self.запуск("--drill-only", "--no-audio")
        self.assertNotIn("аудио_сверено", r,
                         "ноль сверенных выдан за проверку, которой не было")
        # Причина называется вслух и здесь, а не только в ночи: `--drill-only`
        # печатает JSON в stdout, и пропуск, видный лишь там, ушёл бы в файл
        # мимо глаз того, кто гонял учение руками.
        self.assertIn("аудио не сверялось", п.stderr)
        self.assertIn("--no-audio", п.stderr)
        self.assertEqual(r["аудио_не_сверялось"],
                         "зеркала не делали (--no-audio)", r)
        self.assertGreater(r["файлов"], 0, "архив не разворачивали")

    def test_keep_доезжает_до_ротации(self):
        """`--keep` меняет только то, сколько архивов остаётся на носителе, —
        значит и проверять его можно только счётом файлов после прогона."""
        os.makedirs(self.цель)
        for дата in ("2000-01-01", "2000-01-02", "2000-01-03"):
            open(os.path.join(self.цель,
                              "core-%s.tar.gz.gpg" % дата), "w").close()
        # Поколения выключены явно: с Т3б.5 по умолчанию остаётся ещё
        # свежайший архив каждой из пяти недель, и три даты января 2000-го
        # пережили бы `--keep 2` законно — тест про суточный счёт, не про них.
        self.запуск("--no-audio", "--no-drill", "--keep", "2",
                    "--keep-weekly", "0", "--keep-monthly", "0")
        осталось = self.архивы()
        self.assertEqual(len(осталось), 2, осталось)
        # Именно два свежайших по имени, а не два случайных: `2000-01-01` и
        # `2000-01-02` обязаны уйти, сегодняшний — остаться.
        self.assertNotIn("core-2000-01-01.tar.gz.gpg", осталось)
        self.assertIn("core-2000-01-03.tar.gz.gpg", осталось)

    def архивы(self):
        return sorted(os.path.basename(f) for f in
                      glob.glob(os.path.join(self.цель, "core-*.tar.gz.gpg")))

    def test_keep_0_не_ротирует(self):
        """Три нуля — «не ротировать», как `--keep 0` до поколений. Пустое
        множество оставшихся стёрло бы с носителя всё, включая архив этой
        ночи (ревью, P2)."""
        os.makedirs(self.цель)
        for дата in ("2000-01-01", "2000-01-02"):
            open(os.path.join(self.цель,
                              "core-%s.tar.gz.gpg" % дата), "w").close()
        self.запуск("--no-audio", "--no-drill", "--keep", "0",
                    "--keep-weekly", "0", "--keep-monthly", "0")
        self.assertEqual(len(self.архивы()), 3, self.архивы())

    def test_keep_weekly_доезжает_до_ротации(self):
        """`--keep-weekly` оставляет свежайший архив каждой из N недель ISO
        поверх суточного счёта (Т3б.5). Недели здесь: 1999-W52 (1 и 2 января
        2000-го), 2000-W01 (3-е и 5-е) и текущая (сегодняшний архив)."""
        os.makedirs(self.цель)
        for дата in ("2000-01-01", "2000-01-02", "2000-01-03", "2000-01-05"):
            open(os.path.join(self.цель,
                              "core-%s.tar.gz.gpg" % дата), "w").close()
        # сайдкар уходит со своим архивом, а не по собственному счёту
        open(os.path.join(self.цель, "core-2000-01-02.manifest.json"), "w").close()
        self.запуск("--no-audio", "--no-drill", "--keep", "1",
                    "--keep-weekly", "2", "--keep-monthly", "0")
        осталось = self.архивы()
        # Две недели — текущая и 2000-W01; из 2000-W01 свежайший — 5-е.
        # Дефолтные пять недель (мутант, не донёсший флаг) оставили бы и
        # 1999-W52 — то есть 2 января.
        self.assertEqual(len(осталось), 2, осталось)
        self.assertIn("core-2000-01-05.tar.gz.gpg", осталось)
        self.assertNotIn("core-2000-01-03.tar.gz.gpg", осталось)
        self.assertNotIn("core-2000-01-02.tar.gz.gpg", осталось)
        self.assertFalse(glob.glob(os.path.join(self.цель, "core-2000-*.manifest.json")),
                         "сайдкар пережил свой архив")

    def test_keep_monthly_доезжает_до_ротации(self):
        """`--keep-monthly` — свежайший архив каждого из N месяцев. Дефолтные
        шесть месяцев (флаг не доехал) оставили бы и январь."""
        os.makedirs(self.цель)
        for дата in ("2000-01-05", "2000-02-03", "2000-02-04"):
            open(os.path.join(self.цель,
                              "core-%s.tar.gz.gpg" % дата), "w").close()
        self.запуск("--no-audio", "--no-drill", "--keep", "1",
                    "--keep-weekly", "0", "--keep-monthly", "2")
        осталось = self.архивы()
        self.assertEqual(len(осталось), 2, осталось)
        self.assertIn("core-2000-02-04.tar.gz.gpg", осталось)
        self.assertNotIn("core-2000-02-03.tar.gz.gpg", осталось)
        self.assertNotIn("core-2000-01-05.tar.gz.gpg", осталось)

    def test_snapshot_пишет_копию_и_выходит(self):
        """`--snapshot` (Т3б.5): копия базы в каталог, ротация по
        `--snapshot-keep`, и ни носителей, ни архива — команда выходит до них.
        Копия сверяется с живой базой счётом событий изнутри: заглушка,
        создающая пустой файл с правильным именем, прошла бы по `exists`."""
        куда = os.path.join(self.tmp, "snap")
        os.makedirs(куда, mode=0o755)          # чужой каталог с широкими правами
        for n in (1, 2):
            open(os.path.join(куда, "contextd-2000-01-0%dT0000.db" % n), "w").close()
        r, _ = self.запуск("--snapshot", куда, "--snapshot-keep", "2")
        self.assertTrue(os.path.exists(r["снимок"]), r)
        # Незашифрованная база с разговорами: 0600 на файле, 0700 на каталоге,
        # каким бы каталог ни был до прогона (Codex, круг 2, P1). SQLite
        # заводит файл по umask — без явного chmod это 0644.
        self.assertEqual(oct(os.stat(r["снимок"]).st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(os.stat(куда).st_mode & 0o777), oct(0o700))
        self.assertEqual(os.path.dirname(r["снимок"]), куда, r)
        self.assertEqual(r["осталось"], 2, r)
        остались = sorted(os.listdir(куда))
        self.assertEqual(len(остались), 2, остались)
        self.assertNotIn("contextd-2000-01-01T0000.db", остались)
        self.assertFalse([f for f in остались if f.endswith(".tmp")], остались)
        живая = sqlite3.connect("file:%s?mode=ro" % os.path.join(self.root, "contextd.db"), uri=True)
        копия = sqlite3.connect("file:%s?mode=ro" % r["снимок"], uri=True)
        try:
            for т in ("events", "blobs", "devices"):
                self.assertEqual(копия.execute("select count(*) from %s" % т).fetchone()[0],
                                 живая.execute("select count(*) from %s" % т).fetchone()[0], т)
            self.assertEqual(копия.execute("pragma quick_check").fetchone()[0], "ok")
            # Не WAL. Мутант без `journal_mode=delete` хвостов после close()
            # не оставляет — SQLite убирает их за последним соединением, — а
            # заводит при следующем ro-открытии (ревью, P3).
            self.assertEqual(копия.execute("pragma journal_mode").fetchone()[0], "delete")
        finally:
            живая.close(); копия.close()
        # до носителей дело не дошло: ни каталога цели, ни отметки
        self.assertFalse(os.path.exists(self.цель), "снимок полез на носитель")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state")), "снимок оставил отметку носителей")

    def test_snapshot_не_идёт_поверх_другого(self):
        """Второй прогон поверх затянувшегося не снимает его живой `.tmp` и
        не пишет сам (Codex, P1): замок — flock на каталоге снимков."""
        import fcntl
        куда = os.path.join(self.tmp, "snap")
        os.makedirs(куда)
        чужой = os.path.join(куда, ".contextd-2000-01-01T0000.db.tmp")
        open(чужой, "w").close()
        держу = os.open(куда, os.O_RDONLY)
        fcntl.flock(держу, fcntl.LOCK_EX)
        try:
            r = subprocess.run(
                [sys.executable, СКРИПТ, "--root", self.root, "--snapshot", куда],
                capture_output=True, text=True,
                env={**os.environ, "MARA_STATE": os.path.join(self.tmp, "state")})
        finally:
            os.close(держу)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("уже идёт", r.stderr)
        self.assertEqual(sorted(os.listdir(куда)), [os.path.basename(чужой)],
                         "второй прогон тронул каталог")

    def test_snapshot_без_каталога_пишет_под_корень(self):
        """Без аргумента — `snapshots/` под корнем блобов (`mi.снимки`): это
        дорога крона, и дефолт должен быть тем же, что читает сверка."""
        r, _ = self.запуск("--snapshot")
        self.assertEqual(os.path.dirname(r["снимок"]), mi.снимки(self.root), r)
        self.assertTrue(os.path.exists(r["снимок"]), r)
        self.assertEqual(oct(os.stat(mi.снимки(self.root)).st_mode & 0o777), oct(0o700))


if __name__ == "__main__":
    unittest.main()
