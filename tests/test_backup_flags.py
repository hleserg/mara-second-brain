"""Флаги `core-backup.py` доезжают до `прогон` (issue #55).

`main()` — единственное место, где флаги превращаются в аргументы, и до сих
пор его не проверял ни один тест: единственный вход про `--drill-only` был
отрицательным (отвергнуть каталог-обманку). Четыре мутанта в разборе
аргументов проходили полный гейт — `--drill-only` с заглушкой вместо
`проверка(...)`, `аудио=True` и `drill=True` вместо `not a.no_*`, `--keep`,
подменённый константой.

Гоняем через `subprocess`, а не вызовом `прогон` напрямую: проверяется ровно
дорога от флага до поведения, и вызов `прогон` мимо `main()` эту дорогу как
раз и перепрыгивает. `MARA_STATE` уводим во временный каталог — иначе
подпроцесс пишет отметку носителей в боевой `~/.local/state/mara` хозяина
машины.
"""
import glob, hashlib, json, os, shutil, subprocess, sys, tempfile, unittest

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
        s = hashlib.sha256(тело).hexdigest()
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
        r, _ = self.запуск("--drill-only")
        # Сверяем не с «похоже на правду», а с тем, что на носителе и в базе:
        # заглушка с правдоподобными числами первую редакцию этого теста
        # проходила целиком.
        лежит = [os.path.basename(f) for f in
                 glob.glob(os.path.join(self.цель, "core-*.tar.gz.gpg"))]
        self.assertEqual(r["архив"], r0["архив"], "развернули не тот архив")
        self.assertIn(r["архив"], лежит, "архива с таким именем на носителе нет")
        # Оракул для счётчиков — сайдкар рядом с архивом: он написан в момент
        # записи и от `проверка` не зависит вовсе. На стенде ровно один блоб,
        # и заглушка с правдоподобными числами тут и умирает.
        сайдкар = json.load(open(os.path.join(self.цель, r["архив"].replace(
            ".tar.gz.gpg", ".manifest.json")), encoding="utf-8"))
        self.assertEqual(r["счётчики"], сайдкар["counts"], "счётчики не из базы")
        self.assertEqual(r["счётчики"]["blobs"], 1, r["счётчики"])
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
        # Носитель при этом записан: флаги убирают работу, а не прогон.
        self.assertEqual(r["носители"], [self.цель], r)

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
