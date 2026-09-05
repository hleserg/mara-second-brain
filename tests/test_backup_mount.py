"""Бэкап отказывается писать на несмонтированный носитель (ТЗ §12).

Оба бэкапных скрипта делают `mkdir -p` перед записью. Отвалился внешний диск
или сетевая шара — каталог создаётся заново на корневой ФС, запись проходит,
скрипт печатает успех. Три копии по §12 оказываются на одном физическом диске,
и выглядит это ровно как норма: каталог на месте, архив свежий, сверка молчит.
Проверяем, что теперь не молчит — во всех трёх местах, где вопрос задавался.
"""
import glob, json, os, sys, shutil, subprocess, tempfile, unittest
import unittest.mock

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

    def test_несуществующий_корень_смотрит_на_предка(self):
        """Шаг 1 рунбука восстановления — `--drill-only` — идёт до того, как
        создан `/srv/mara-blobs`. `os.stat` в лоб убил бы команду «убедиться,
        что архив читается» ровно в день замены железа."""
        нет_корня = os.path.join(self.tmp, "нет-такого", "и-такого")
        self.assertTrue(mi.смонтирован("/proc", нет_корня))
        self.assertFalse(mi.смонтирован(os.path.join(self.tmp, "target"), нет_корня))

    def test_разрешение_снимает_требование(self):
        цель = os.path.join(self.tmp, "backup")
        os.makedirs(цель)
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"
        self.assertTrue(mi.смонтирован(цель, self.tmp))


class Сверка(unittest.TestCase):
    """Реконсилятор обязан отличать смонтированный носитель от каталога-обманки:
    без этого каталог, который бэкап создал себе сам на корневой ФС, читается
    за живой носитель — isdir всегда истинен, и находка по нему недостижима."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.цель = os.path.join(self.tmp, "backup")
        os.makedirs(self.цель)
        open(os.path.join(self.цель, "core-2026-09-04.tar.gz.gpg"), "wb").write(b"gpg")
        self.старый_root = mi.ROOT
        mi.ROOT = self.tmp
        # Отметку уводим во временный каталог: иначе сверка читает боевую
        # отметку хозяина машины, и тест зависит от того, что там лежит.
        self.боевая_отметка = mi.ОТМЕТКА_НОСИТЕЛЕЙ
        mi.ОТМЕТКА_НОСИТЕЛЕЙ = os.path.join(self.tmp, "core-targets.json")
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)

    def tearDown(self):
        mi.ROOT = self.старый_root
        mi.ОТМЕТКА_НОСИТЕЛЕЙ = self.боевая_отметка
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)

    def test_корень_берётся_из_аргумента_а_не_из_глобала(self):
        """`run(con, root)` раздаёт свой корень всем проверкам, и бэкапная не
        исключение: спросив вместо него модульный `mi.ROOT`, она отвечает про
        чужую машину. Здесь носитель лежит на одном устройстве с корнем — то
        есть носителем не является, — а с глобалом выглядел бы законным."""
        # Глобал уводим на другое устройство: иначе оба корня дают один и тот
        # же ответ, и тест зелен независимо от того, какой из них спросили.
        mi.ROOT = "/proc"
        con = mi.connect(self.tmp)
        try:
            f = rc.run(con, self.tmp, vault=None, targets=[self.цель])
        finally:
            con.close()
        self.assertIn("бэкап-ядра-носители", [x["check"] for x in f])

    def test_свежий_архив_на_корневой_фс_это_находка(self):
        f = rc.бэкап_ядра([self.цель])
        self.assertEqual([(x["check"], x["level"]) for x in f],
                         [("бэкап-ядра-носители", "warn"),
                          ("бэкап-ядра-копий-ноль", "warn")],
                         "свежий архив на том же диске зачтён за бэкап")


@unittest.skipUnless(shutil.which("gpg"), "нет gpg")
class Прогон(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cb = load("core-backup.py")
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)
        # Удавшийся прогон пишет отметку носителей, и без подмены она уляжется
        # в боевой `~/.local/state/mara` хозяина машины. До сих пор тут все
        # прогоны падали раньше отметки, потому и не мешало.
        self.боевая_отметка = mi.ОТМЕТКА_НОСИТЕЛЕЙ
        self.отметка = os.path.join(self.tmp, "state", "core-targets.json")
        mi.ОТМЕТКА_НОСИТЕЛЕЙ = self.отметка

    def tearDown(self):
        mi.ОТМЕТКА_НОСИТЕЛЕЙ = self.боевая_отметка
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)

    def стенд(self):
        """Корень с базой и парольная фраза — общее начало любого прогона."""
        root = os.path.join(self.tmp, "blobs")
        mi.connect(root).close()
        пароль = os.path.join(self.tmp, "pass")
        open(пароль, "w").write("проверочная фраза\n")
        os.chmod(пароль, 0o600)
        return root, пароль

    def test_битая_копия_не_идёт_в_зачёт(self):
        """Носитель принял запись и отдал не то, что писали.

        Учение восстановления разворачивает только первый носитель, так что
        второй мог лечь битым и молчать до дня, когда он единственный уцелел.
        Порчу вносим на уровне записи в файл, а не подменой сверки: оракул
        тут — сам испорченный байт, а не мнение проверяемого кода."""
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"
        root, пароль = self.стенд()
        цель = os.path.join(self.tmp, "target")
        второй = os.path.join(self.tmp, "target2")
        # Старые архивы на носителе, который сегодня отказал, оставшись при
        # этом писабельным (сверка не сошлась, каталог жив — обычный случай).
        # Ротация не имеет права их трогать: такой носитель живёт последней
        # удачной копией, и вычистить её по `keep` значило бы оставить его ни
        # с чем. Это же единственный вход, отличающий ротацию по `записано`
        # от ротации по `targets`.
        os.makedirs(второй)
        for дата in ("2000-01-01", "2000-01-02", "2000-01-03"):
            open(os.path.join(второй, "core-%s.tar.gz.gpg" % дата), "w").close()
        настоящий = shutil.copy2

        def подмена(src, dst, **kw):
            r = настоящий(src, dst, **kw)
            if os.path.dirname(dst) == второй:
                with open(dst, "ab") as fh:
                    fh.write(b"\0")
            return r

        with unittest.mock.patch.object(self.cb.shutil, "copy2", подмена):
            r = self.cb.прогон(root, [цель, второй], пароль, keep=2,
                               work=os.path.join(self.tmp, "work"))
        self.assertEqual(r["носители"], [цель], "битая копия зачтена")
        # Ровно три — те самые старые: сегодняшний архив сюда не лёг, а
        # ротация по `keep=2` сюда не ходила.
        self.assertEqual(len(glob.glob(os.path.join(второй, "core-*"))), 3,
                         "битый архив лёг под боевым именем либо ротация "
                         "выкосила архивы отказавшего носителя")
        self.assertEqual(glob.glob(os.path.join(второй, ".*")), [],
                         "временный огрызок остался на носителе")
        # Отметка — то, по чему сверка §12 отличает отвал от пропажи; носитель,
        # на который не легло ничего, обязан в ней отсутствовать.
        self.assertEqual(list(json.load(open(self.отметка, encoding="utf-8"))),
                         [цель])
        # Права ставятся временному файлу, до переименования: если сверка
        # переедет, легко потерять и chmod.
        лежит = os.path.join(цель, r["архив"])
        self.assertEqual(os.stat(лежит).st_mode & 0o777, 0o600)

    def test_отказ_уборки_не_валит_прогон(self):
        """Носитель ушёл в read-only той же бедой, что испортила копию.

        Тогда `unlink` огрызка бросает EACCES — и, не будь он прикрыт, унёс бы
        с собой запись на здоровый носитель, отметку, учение и ротацию: отказ
        одного носителя стоил бы всей ночной работы."""
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"
        root, пароль = self.стенд()
        цель = os.path.join(self.tmp, "target")
        второй = os.path.join(self.tmp, "target2")
        настоящий = shutil.copy2

        def подмена(src, dst, **kw):
            r = настоящий(src, dst, **kw)
            if os.path.dirname(dst) == второй:
                with open(dst, "ab") as fh:
                    fh.write(b"\0")
                os.chmod(второй, 0o500)     # писать сюда больше нельзя вовсе
            return r

        try:
            # Больной носитель первым: с ним падение в обработчике не даёт
            # здоровому получить архив вообще, и ночь пропадает целиком.
            with unittest.mock.patch.object(self.cb.shutil, "copy2", подмена):
                r = self.cb.прогон(root, [второй, цель], пароль, keep=2,
                                   work=os.path.join(self.tmp, "work"))
        finally:
            # Иначе tearDown не уберёт каталог; `isdir` — чтобы падение до
            # `makedirs` не пряталось за `FileNotFoundError` отсюда.
            if os.path.isdir(второй):
                os.chmod(второй, 0o700)
        self.assertEqual(r["носители"], [цель])
        self.assertIn("проверка", r, "учение не дошло из-за уборки огрызка")

    def test_огрызок_прошлой_ночи_убирается(self):
        """Носитель, ушедший в read-only, оставил временный файл: убрать его
        тогда было нечем. Имя с датой — следующая ночь его не перезапишет, а
        ротация ходит по `core-*` и точечных имён не видит, так что без
        отдельной уборки он остался бы на носителе навсегда."""
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"
        root, пароль = self.стенд()
        цель = os.path.join(self.tmp, "target")
        os.makedirs(цель)
        огрызок = os.path.join(цель, ".core-2000-01-01.tar.gz.gpg.tmp")
        open(огрызок, "w").close()
        # На тех же носителях лежит сосед — `vault-backup.sh` пишет туда свой
        # бандл и в понедельник расходится с нами всего на десять минут.
        # Шаблон уборки обязан различать их по префиксу, а не по хвосту.
        сосед = os.path.join(цель, ".vault-2000-01-01.bundle.gpg.tmp")
        open(сосед, "w").close()
        # Огрызок, который не снять: на шаре его держит открытым чужой
        # процесс, а на стенде проще всего каталогом. Уборка не обязана
        # удаться — но и уронить удачную ночь задним числом не имеет права.
        неснимаемый = os.path.join(цель, ".core-беда.tmp")
        os.makedirs(неснимаемый)
        r = self.cb.прогон(root, [цель], пароль, keep=2,
                           work=os.path.join(self.tmp, "work"))
        self.assertEqual(r["носители"], [цель])
        self.assertFalse(os.path.exists(огрызок), "огрызок остался навсегда")
        self.assertTrue(os.path.exists(сосед), "убрали временный файл соседа")

    def test_носитель_без_каталога_не_роняет_прогон(self):
        """`makedirs` падает до того, как появился временный файл.

        Имя `врем` связано до `try` именно поэтому: уборка в обработчике иначе
        даёт `UnboundLocalError` и превращает отвал одного носителя в падение
        всего прогона."""
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"
        root, пароль = self.стенд()
        цель = os.path.join(self.tmp, "target")
        не_каталог = os.path.join(self.tmp, "файл")
        open(не_каталог, "w").close()
        r = self.cb.прогон(root, [os.path.join(не_каталог, "target2"), цель],
                           пароль, keep=2, work=os.path.join(self.tmp, "work"))
        self.assertEqual(r["носители"], [цель])

    def test_единственный_носитель_на_корневой_фс_валит_прогон(self):
        root, пароль = self.стенд()
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
