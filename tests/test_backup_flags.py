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
import glob, hashlib, json, os, shutil, sqlite3, subprocess, sys
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
                    "values(?,?,?,?,?)", (s, p, len(тело), "audio", mi.now_iso()))
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def запуск(self, *флаги):
        r = subprocess.run(
            [sys.executable, СКРИПТ, "--root", self.root, "--targets", self.цель,
             "--pass-file", self.пароль, "--work", os.path.join(self.tmp, "work")]
            + list(флаги),
            capture_output=True, text=True,
            env={**os.environ,
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
        # `--pass-file` доехал ровно тогда, когда архив открывается **нашей**
        # фразой. Подмена пути на боевой по умолчанию самосогласованна — одним
        # и тем же файлом и шифруют, и расшифровывают, — поэтому на машине, где
        # тот файл есть, она переживает всё остальное в этом тесте.
        открыт = subprocess.run(
            ["gpg", "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback",
             "--passphrase-file", self.пароль, "-o",
             os.path.join(self.tmp, "открыт.tar.gz"), "-d",
             os.path.join(self.цель, r0["архив"])],
            capture_output=True, text=True)
        self.assertEqual(открыт.returncode, 0,
                         "--pass-file не доехал: архив нашей фразой не открылся\n"
                         + открыт.stderr)
        # Оракул обязан быть недостижим для кода под тестом. Правда о том, что
        # лежит в архиве, снята в момент записи, и дорог к ней две: сайдкар
        # рядом с архивом (круг 1: заглушка читала его и прошла весь гейт) и
        # живая база, счётчики которой совпадают с записанными, пока её никто
        # не трогает (круг 2: заглушка считала по ней — и тоже прошла).
        # Закрываем обе: сайдкар портим, живую базу разводим с архивом лишней
        # строкой, правду держим снимком в памяти теста. Настоящая `проверка`
        # ни той, ни другой порчи не видит — и счётчики, и сверку аудио она
        # считает по базе **внутри** архива (`core-backup.py:273-301`), а
        # строку без живого файла `проверить_аудио` пропускает (`:329-334`).
        путь = os.path.join(self.цель,
                            r0["архив"].replace(".tar.gz.gpg", ".manifest.json"))
        сайдкар = json.load(open(путь, encoding="utf-8"))
        было = сайдкар["counts"]
        json.dump(dict(сайдкар, counts=dict(было, blobs=999)),
                  open(путь, "w", encoding="utf-8"))
        живая = mi.connect(self.root)
        живая.execute("insert into blobs(sha256,path,bytes,mime,created) "
                      "values(?,?,?,?,?)",
                      ("f" * 64, os.path.join(self.root, "нет.wav"),
                       1, "audio", mi.now_iso()))
        живая.commit()
        живая.close()
        r, _ = self.запуск("--drill-only")
        self.assertEqual(r["архив"], r0["архив"], "развернули не тот архив")
        self.assertIn(r["архив"], лежит, "архива с таким именем на носителе нет")
        self.assertEqual(r["счётчики"], было, "счётчики не из базы внутри архива")
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
        self.assertGreater(r["файлов"], 0, "манифест пуст — архив не разворачивали")
        # Аудио сверяется отдельно от файлов манифеста: расшифрованная копия
        # из зеркала обязана сойтись с живым блобом по хешу.
        self.assertEqual(r["аудио_сверено"], 1, r)

    def test_no_audio_и_no_drill_доезжают_до_прогона(self):
        """Оба флага проверяются одним прогоном: они и в бою ходят парой —
        `--no-audio` в одиночку роняет любую ночь со звонком, потому что
        `проверить_аудио` по-прежнему требует три свежайшие живые записи."""
        r, _ = self.запуск("--no-audio", "--no-drill")
        self.assertNotIn("проверка", r, "учение прошло вопреки --no-drill")
        self.assertEqual(self.зеркало(), [], "аудио зеркалилось вопреки --no-audio")
        # Носитель при этом записан: флаги убирают работу, а не прогон. Эта же
        # строка держит `--targets`: подмена на путь по умолчанию даст другой.
        self.assertEqual(r["носители"], [self.цель], r)
        # И `--work`: `прогон` создаёт рабочий каталог первым делом
        # (`core-backup.py:392`), так что его отсутствие означает, что работа
        # шла мимо каталога теста — в общий `/var/tmp`, куда пишет кто угодно.
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "work")),
                        "--work не доехал: работа шла мимо каталога теста")

    def test_no_audio_в_одиночку_роняет_ночь_со_звонком(self):
        """Не поведение, которое хочется, а поведение, которое есть, — и
        которое поэтому названо в «Запуске» `docs/backup-core.md`. Учение
        сверяет аудио по зеркалу, а зеркала при `--no-audio` нет."""
        r = subprocess.run(
            [sys.executable, СКРИПТ, "--root", self.root, "--targets", self.цель,
             "--pass-file", self.пароль, "--work", os.path.join(self.tmp, "work"),
             "--no-audio"],
            capture_output=True, text=True,
            env={**os.environ,
                 "MARA_BACKUP_ALLOW_SAME_DEV": "1",
                 "MARA_STATE": os.path.join(self.tmp, "state")})
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("нет в зеркале", r.stderr)

    def test_keep_доезжает_до_ротации(self):
        """`--keep` меняет только то, сколько архивов остаётся на носителе, —
        значит и проверять его можно только счётом файлов после прогона."""
        os.makedirs(self.цель)
        for дата in ("2000-01-01", "2000-01-02", "2000-01-03"):
            open(os.path.join(self.цель, "core-%s.tar.gz.gpg" % дата), "w").close()
        self.запуск("--no-audio", "--no-drill", "--keep", "2")
        осталось = sorted(os.path.basename(f) for f in
                          glob.glob(os.path.join(self.цель, "core-*.tar.gz.gpg")))
        self.assertEqual(len(осталось), 2, осталось)
        # Именно два свежайших по имени, а не два случайных: `2000-01-01` и
        # `2000-01-02` обязаны уйти, сегодняшний — остаться.
        self.assertNotIn("core-2000-01-01.tar.gz.gpg", осталось)
        self.assertIn("core-2000-01-03.tar.gz.gpg", осталось)


if __name__ == "__main__":
    unittest.main()
