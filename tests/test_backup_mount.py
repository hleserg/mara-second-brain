"""Бэкап отказывается писать на несмонтированный носитель (ТЗ §12).

Оба бэкапных скрипта делают `mkdir -p` перед записью. Отвалился внешний диск
или сетевая шара — каталог создаётся заново на корневой ФС, запись проходит,
скрипт печатает успех. Три копии по §12 оказываются на одном физическом диске,
и выглядит это ровно как норма: каталог на месте, архив свежий, сверка молчит.
Проверяем, что теперь не молчит — во всех трёх местах, где вопрос задавался.
"""
import os, sys, shutil, subprocess, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import mara_ingest as mi
import contextd_reconcile as rc


def load(name):
    import importlib.util
    p = os.path.join(ROOT, "scripts", name)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_")[:-3], p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Хелпер(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)

    def test_тот_же_диск_это_не_носитель(self):
        цель = os.path.join(self.tmp, "backup")
        os.makedirs(цель)
        self.assertFalse(mi.смонтирован(цель, self.tmp))

    def test_другое_устройство_годится(self):
        # /proc не бэкапный носитель, но устройство у него честно другое —
        # берём его, потому что второй диск в тестах взять неоткуда.
        self.assertNotEqual(os.stat("/proc").st_dev, os.stat(self.tmp).st_dev,
                            "тест опирается на то, что /proc — отдельная ФС")
        self.assertTrue(mi.смонтирован("/proc", self.tmp))

    def test_несуществующий_каталог_смотрит_на_предка(self):
        # На свежем носителе mara/ ещё нет — отказывать из-за этого нельзя.
        self.assertTrue(mi.смонтирован("/proc/нет-такого/и-такого", self.tmp))

    def test_разрешение_снимает_требование(self):
        цель = os.path.join(self.tmp, "backup")
        os.makedirs(цель)
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"
        self.assertTrue(mi.смонтирован(цель, self.tmp))


class Сверка(unittest.TestCase):
    """Реконсилятор обязан отличать смонтированный носитель от каталога-обманки:
    без этого его warn «ни один носитель не смонтирован» недостижим — бэкап сам
    себе создал каталог, и isdir всегда истинен."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.цель = os.path.join(self.tmp, "backup")
        os.makedirs(self.цель)
        open(os.path.join(self.цель, "core-2026-09-04.tar.gz.gpg"), "wb").write(b"gpg")
        self.старый_root = mi.ROOT
        mi.ROOT = self.tmp
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)

    def tearDown(self):
        mi.ROOT = self.старый_root
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)

    def test_корень_берётся_из_аргумента_а_не_из_глобала(self):
        """`run(con, root)` раздаёт свой корень всем проверкам. Если бэкапная
        лезет за ним в `mi.ROOT`, то на машине, где `/srv/mara-blobs` нет, а
        каталог носителя есть, `os.stat` роняет всю сверку — вместе со всеми
        остальными находками, а не только с этой."""
        mi.ROOT = os.path.join(self.tmp, "нет-такого-корня")
        con = mi.connect(self.tmp)
        try:
            f = rc.run(con, self.tmp, vault=None, targets=[self.цель])
        finally:
            con.close()
        self.assertEqual([x["check"] for x in f], ["бэкап-ядра-носители"])

    def test_свежий_архив_на_корневой_фс_это_находка(self):
        f = rc.бэкап_ядра([self.цель])
        self.assertEqual([(x["check"], x["level"]) for x in f],
                         [("бэкап-ядра-носители", "warn")],
                         "свежий архив на том же диске зачтён за бэкап")


@unittest.skipUnless(shutil.which("gpg"), "нет gpg")
class Прогон(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cb = load("core-backup.py")
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)

    def test_единственный_носитель_на_корневой_фс_валит_прогон(self):
        root = os.path.join(self.tmp, "blobs")
        mi.connect(root).close()
        пароль = os.path.join(self.tmp, "pass")
        open(пароль, "w").write("проверочная фраза\n")
        os.chmod(пароль, 0o600)
        with self.assertRaises(RuntimeError) as e:
            self.cb.прогон(root, [os.path.join(self.tmp, "target")], пароль,
                           keep=2, work=os.path.join(self.tmp, "work"))
        self.assertIn("ни один носитель не записан", str(e.exception))

    def test_drill_only_не_разворачивает_с_обманки(self):
        """Развернуть архив с каталога на корневой ФС и доложить «восстановление
        проверено» — ровно та ложная уверенность, ради которой всё затевалось."""
        цель = os.path.join(self.tmp, "target")
        os.makedirs(цель)
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "core-backup.py"),
             "--drill-only", "--root", self.tmp, "--targets", цель],
            capture_output=True, text=True,
            env={**os.environ, "MARA_BACKUP_ALLOW_SAME_DEV": ""})
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("носитель не смонтирован", r.stderr)


if __name__ == "__main__":
    unittest.main()
