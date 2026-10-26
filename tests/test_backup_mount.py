"""Бэкап отказывается писать на несмонтированный носитель (§10 `TZ.md`).

Оба бэкапных скрипта делают `mkdir -p` перед записью. Отвалился внешний диск
или сетевая шара — каталог создаётся заново на корневой ФС, запись проходит,
скрипт печатает успех. Три копии по §10 `TZ.md` оказываются на одном физическом
диске, и выглядит это ровно как норма: каталог на месте, архив свежий, сверка
молчит. Проверяем, что теперь не молчит — во всех трёх местах, где вопрос
задавался.
"""
import glob, io, json, os, sys, shutil, subprocess, tempfile, unittest
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
        # и отметка учения (Т9.1): удавшееся учение пишет её туда же
        self.боевое_учение = mi.ОТМЕТКА_УЧЕНИЯ
        self.учение = os.path.join(self.tmp, "state", "core-drill.json")
        mi.ОТМЕТКА_УЧЕНИЯ = self.учение
        # и бандл секретов (Т3б.5 п.5): отметка — сюда же, состав — пустой,
        # чтобы прогон не унёс настоящие `/etc/mara` и `~/.config/mara`
        self.боевые_секреты = mi.ОТМЕТКА_СЕКРЕТОВ
        mi.ОТМЕТКА_СЕКРЕТОВ = os.path.join(self.tmp, "state", "core-secrets.json")
        self.cb.ЭТЦ_СЕКРЕТОВ = os.path.join(self.tmp, "нет-etc")
        self.cb.КОНФИГ_СЕКРЕТОВ = os.path.join(self.tmp, "нет-config")

    def tearDown(self):
        mi.ОТМЕТКА_НОСИТЕЛЕЙ = self.боевая_отметка
        mi.ОТМЕТКА_УЧЕНИЯ = self.боевое_учение
        mi.ОТМЕТКА_СЕКРЕТОВ = self.боевые_секреты
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
        # Отметка — то, по чему сверка §10 `TZ.md` отличает отвал от пропажи;
        # носитель, на который не легло ничего, обязан в ней отсутствовать.
        self.assertEqual(list(json.load(open(self.отметка, encoding="utf-8"))),
                         [цель])
        # учение прошло — отметка учения на носителе учения (Т9.1, §15.1)
        self.assertEqual(json.load(open(self.учение, encoding="utf-8"))["target"], цель)
        # Права ставятся временному файлу, до переименования: если сверка
        # переедет, легко потерять и chmod.
        лежит = os.path.join(цель, r["архив"])
        self.assertEqual(os.stat(лежит).st_mode & 0o777, 0o600)

    def test_sftp_цель_в_прогоне_последняя_и_без_учения(self):
        """Т3б.5 п.8: цель `sftp://` рядом с носителем — архив уезжает,
        отметка носителей её знает, аудио на неё не зеркалится, учение
        идёт по носителю. Одна лишь удалённая цель — ночь красная: с неё
        разворачивать нечем."""
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"
        root, пароль = self.стенд()
        bin_, удалённо = поддельный_sftp(self.tmp)
        with unittest.mock.patch.dict(os.environ, {
                "PATH": bin_ + os.pathsep + os.environ["PATH"],
                "FAKE_SFTP_ROOT": удалённо}):
            цель = os.path.join(self.tmp, "target")
            url = "sftp://user@host/mara"
            r = self.cb.прогон(root, [цель, url], пароль, keep=2,
                               work=os.path.join(self.tmp, "work"))
            self.assertEqual(r["носители"], [цель, url])
            self.assertEqual(r["аудио"][url], {"пропущен": "sftp: зеркало аудио не поддерживается"})
            self.assertEqual(sorted(json.load(open(self.отметка, encoding="utf-8"))),
                             sorted([цель, url]))
            self.assertEqual(json.load(open(self.учение, encoding="utf-8"))["target"], цель)
            # состав секретов пуст (setUp) — бандла на цели нет
            self.assertEqual(r["секреты"], {"пропущен": "состав пуст"})
            self.assertEqual(sorted(os.listdir(os.path.join(удалённо, "mara"))),
                             [r["архив"].replace(".tar.gz.gpg", ".manifest.json"), r["архив"]])
            with self.assertRaisesRegex(RuntimeError, "учение"):
                self.cb.прогон(root, [url], пароль, keep=2,
                               work=os.path.join(self.tmp, "work"))

    def test_отметка_учения_только_за_удачным_учением(self):
        """Т9.1, ревью P2-1: отметка учения — след удачного учения, и ничего
        больше. Без учения и при упавшем учении её нет; сбой её записи ночь
        не роняет — ротация и итог доходят."""
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"
        root, пароль = self.стенд()
        цель = os.path.join(self.tmp, "target")
        work = os.path.join(self.tmp, "work")
        self.cb.прогон(root, [цель], пароль, keep=2, work=work, аудио=False, drill=False)
        self.assertFalse(os.path.exists(self.учение), "отметка без учения")
        with unittest.mock.patch.object(self.cb, "проверка",
                                        side_effect=RuntimeError("не сошлось")):
            with self.assertRaises(RuntimeError):
                self.cb.прогон(root, [цель], пароль, keep=2, work=work, аудио=False,
                               drill=True)
        self.assertFalse(os.path.exists(self.учение), "отметка при упавшем учении")
        self.cb.прогон(root, [цель], пароль, keep=2, work=work, аудио=False, drill=True)
        self.assertEqual(os.stat(self.учение).st_mode & 0o777, 0o600)
        # отметке некуда лечь — прогон всё равно доходит до ротации и итога
        файл = os.path.join(self.tmp, "не-каталог")
        open(файл, "w").close()
        mi.ОТМЕТКА_УЧЕНИЯ = os.path.join(файл, "core-drill.json")
        with unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            r = self.cb.прогон(root, [цель], пароль, keep=1, work=work, аудио=False,
                               drill=True)
        self.assertIn("отметка учения не записана", err.getvalue())
        self.assertEqual(r["носители"], [цель])
        self.assertEqual(len(glob.glob(os.path.join(цель, "core-*.tar.gz.gpg"))), 1,
                         "ротация по keep=1 не дошла")

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

ПОДДЕЛЬНЫЙ_SFTP = r"""import os, shutil, sys
root = os.environ["FAKE_SFTP_ROOT"]
host = sys.argv[-1]
if host.endswith("dead"):
    sys.stderr.write("ssh: connect to host dead: Connection refused\n")
    sys.exit(1)
def loc(p):
    return os.path.join(root, p.lstrip("/"))
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    sys.stdout.write("sftp> " + line + "\n")
    soft = line.startswith("-")
    parts = line.lstrip("-").split()
    cmd, args = parts[0], parts[1:]
    try:
        if cmd == "mkdir":
            os.mkdir(loc(args[0]))
        elif cmd == "put":
            data = open(args[0], "rb").read()
            if host.endswith("corrupt"):
                data += b"\0"
            open(loc(args[1]), "wb").write(data)
        elif cmd == "get":
            shutil.copyfile(loc(args[0]), args[1])
        elif cmd == "rename":
            if os.path.exists(loc(args[1])):
                raise OSError("Failure")
            os.rename(loc(args[0]), loc(args[1]))
        elif cmd == "rm":
            os.remove(loc(args[0]))
        elif cmd == "ls":
            for n in sorted(os.listdir(loc(args[-1]))):
                sys.stdout.write(n + "\n")
        else:
            raise OSError("unknown " + cmd)
    except OSError as e:
        if soft:
            continue
        sys.stderr.write("%s: %s\n" % (cmd, e))
        sys.exit(1)
"""


def поддельный_sftp(tmp):
    """`sftp` на PATH, который кладёт «удалённые» файлы под `FAKE_SFTP_ROOT`
    (Т3б.5 п.8). Хост `…corrupt` портит каждый `put` байтом, `…dead` не
    отвечает, `rename` поверх существующего отказывает — как Windows OpenSSH.
    Эхо `sftp> …` — как у настоящего с `-b`."""
    bin_ = os.path.join(tmp, "bin")
    os.makedirs(bin_, exist_ok=True)
    шим = os.path.join(bin_, "sftp")
    with open(шим, "w") as fh:
        fh.write("#!%s\n%s" % (sys.executable, ПОДДЕЛЬНЫЙ_SFTP))
    os.chmod(шим, 0o755)
    удалённо = os.path.join(tmp, "remote")
    os.makedirs(удалённо)
    return bin_, удалённо


class Sftp(unittest.TestCase):
    """Запись и ротация на цель `sftp://` (Т3б.5 п.8) — без gpg, напрямую."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cb = load("core-backup.py")
        bin_, self.удалённо = поддельный_sftp(self.tmp)
        self.окружение = dict(os.environ)
        os.environ["PATH"] = bin_ + os.pathsep + os.environ["PATH"]
        os.environ["FAKE_SFTP_ROOT"] = self.удалённо
        self.stage = os.path.join(self.tmp, "stage")
        os.makedirs(self.stage)
        self.enc = os.path.join(self.stage, "core-2026-10-26.tar.gz.gpg")
        with open(self.enc, "wb") as fh:
            fh.write(b"gpg-bytes" * 100)
        self.сводка = {"sha256": self.cb.sha(self.enc), "байт": os.path.getsize(self.enc)}

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.окружение)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def на_цели(self):
        return sorted(os.listdir(os.path.join(self.удалённо, "mara")))

    def test_запись_кладёт_архив_и_сайдкар_без_огрызка(self):
        self.cb.sftp_запись("sftp://user@host/mara", self.enc,
                            "core-2026-10-26.tar.gz.gpg", self.сводка, self.stage)
        self.assertEqual(self.на_цели(), ["core-2026-10-26.manifest.json",
                                          "core-2026-10-26.tar.gz.gpg"])
        лёг = os.path.join(self.удалённо, "mara", "core-2026-10-26.tar.gz.gpg")
        self.assertEqual(self.cb.sha(лёг), self.сводка["sha256"])
        # повторная запись тем же именем: `rename` поверх существующего на
        # Windows OpenSSH отказывает, потому перед ним `-rm`
        self.cb.sftp_запись("sftp://user@host/mara", self.enc,
                            "core-2026-10-26.tar.gz.gpg", self.сводка, self.stage)
        self.assertEqual(len(self.на_цели()), 2)

    def test_битая_копия_не_остаётся_на_цели(self):
        with self.assertRaises(OSError):
            self.cb.sftp_запись("sftp://user@corrupt/mara", self.enc,
                                "core-2026-10-26.tar.gz.gpg", self.сводка, self.stage)
        self.assertEqual(self.на_цели(), [], "битый архив или огрызок остались")

    def test_мёртвый_хост_это_oserror_с_хвостом_stderr(self):
        with self.assertRaises(OSError) as cm:
            self.cb.sftp_запись("sftp://user@dead/mara", self.enc,
                                "core-2026-10-26.tar.gz.gpg", self.сводка, self.stage)
        self.assertIn("Connection refused", str(cm.exception))

    def test_ротация_оставляет_поколения_и_убирает_огрызки(self):
        каталог = os.path.join(self.удалённо, "mara")
        os.makedirs(каталог)
        for д in ("2026-10-20", "2026-10-21", "2026-10-22"):
            for суффикс in (".tar.gz.gpg", ".manifest.json"):
                open(os.path.join(каталог, "core-%s%s" % (д, суффикс)), "w").close()
        open(os.path.join(каталог, ".core-2026-10-19.tar.gz.gpg.tmp"), "w").close()
        open(os.path.join(каталог, "чужое.txt"), "w").close()
        self.cb.ротация("sftp://user@host/mara", keep=2)
        self.assertEqual(self.на_цели(), ["core-2026-10-21.manifest.json",
                                          "core-2026-10-21.tar.gz.gpg",
                                          "core-2026-10-22.manifest.json",
                                          "core-2026-10-22.tar.gz.gpg",
                                          "чужое.txt"])


class Секреты(unittest.TestCase):
    """Бандл секретов (Т3б.5 п.5) — без gpg: `шифр` подменён копированием."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cb = load("core-backup.py")
        self.окружение = dict(os.environ)
        bin_, self.удалённо = поддельный_sftp(self.tmp)
        os.environ["PATH"] = bin_ + os.pathsep + os.environ["PATH"]
        os.environ["FAKE_SFTP_ROOT"] = self.удалённо
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"
        self.root = os.path.join(self.tmp, "blobs")
        self.cb.ЭТЦ_СЕКРЕТОВ = os.path.join(self.tmp, "etc")
        self.cb.КОНФИГ_СЕКРЕТОВ = os.path.join(self.tmp, "config")
        self.боевые_секреты = mi.ОТМЕТКА_СЕКРЕТОВ
        mi.ОТМЕТКА_СЕКРЕТОВ = os.path.join(self.tmp, "state", "core-secrets.json")
        self.stage = os.path.join(self.tmp, "stage")
        os.makedirs(self.stage)
        self.cb.шифр = lambda src, dst, пароль: shutil.copy(src, dst)

    def tearDown(self):
        mi.ОТМЕТКА_СЕКРЕТОВ = self.боевые_секреты
        os.environ.clear()
        os.environ.update(self.окружение)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def файл(self, *части, текст="x"):
        p = os.path.join(self.tmp, *части)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(текст)
        return p

    def состав(self):
        """Боевой состав doctor в миниатюре: ключи — да, кеши и живое — нет."""
        for ч in (("etc", "contextd.env"), ("etc", "gmail.env"),
                  ("config", "env"), ("config", "usage-hosts"), ("config", "tls", "cert.pem"),
                  ("config", "backup-pass"),
                  ("blobs", "tdlib", "key"), ("blobs", "tdlib", "phone"),
                  ("blobs", "tdlib", "td.binlog"), ("blobs", "tdlib", "cursor.json"),
                  ("blobs", "tdlib", "heartbeat"), ("blobs", "tdlib", "raw", "1.json"),
                  ("blobs", "tdlib", "db", "database", "db.sqlite"),
                  ("blobs", "tdlib", "db", "database", "db.sqlite-wal"),
                  ("blobs", "gmail", "token.json"), ("blobs", "gmail", "lock"),
                  ("blobs", "gmail", "raw", "2.json")):
            self.файл(*ч)

    ОЖИДАЕМО = ["blobs/gmail/token.json", "blobs/tdlib/cursor.json", "blobs/tdlib/key",
                "blobs/tdlib/phone", "blobs/tdlib/td.binlog", "config/mara/env",
                "config/mara/tls/cert.pem", "config/mara/usage-hosts",
                "etc/mara/contextd.env", "etc/mara/gmail.env"]

    def test_состав_по_allowlist_без_кешей_и_парольной_фразы(self):
        self.состав()
        файлы, нечитаемо = self.cb.состав_секретов(self.root)
        self.assertEqual(sorted(a for a, _ in файлы), self.ОЖИДАЕМО)
        self.assertEqual(нечитаемо, 0)
        for _, p in файлы:
            self.assertTrue(os.path.isfile(p), p)

    @unittest.skipIf(os.geteuid() == 0, "root читает всё")
    def test_нечитаемое_считается_а_не_роняет(self):
        """`/etc/mara/tdlib.env` под root на doctor: в бандл не попадает,
        счётчик — в отметку, об этом скажет сверка."""
        self.состав()
        os.chmod(self.файл("etc", "tdlib.env"), 0)
        файлы, нечитаемо = self.cb.состав_секретов(self.root)
        self.assertEqual(нечитаемо, 1)
        self.assertNotIn("etc/mara/tdlib.env", [a for a, _ in файлы])

    def test_нет_каталогов_состав_пуст(self):
        self.assertEqual(self.cb.состав_секретов(self.root), ([], 0))
        self.assertEqual(self.cb.бандл_секретов(self.root, [os.path.join(self.tmp, "t")],
                                                "пароль", self.stage, keep=2),
                         {"пропущен": "состав пуст"})
        self.assertFalse(os.path.exists(mi.ОТМЕТКА_СЕКРЕТОВ), "пустой состав — без отметки")

    def test_дата_архива_по_префиксу(self):
        д = self.cb._дата_архива
        self.assertEqual(str(д("/x/secrets-2026-10-26.tar.gz.gpg", "secrets")), "2026-10-26")
        self.assertIsNone(д("/x/secrets-2026-10-26.tar.gz.gpg"), "ядро чужих имён не видит")
        self.assertIsNone(д("/x/core-2026-10-26.tar.gz.gpg", "secrets"))
        self.assertIsNone(д("/x/secrets-битое.tar.gz.gpg", "secrets"))

    def test_запись_локально_сайдкар_600_и_без_огрызка_при_битой_копии(self):
        enc = self.файл("stage", "secrets-2026-10-26.tar.gz.gpg", текст="байты")
        имя = os.path.basename(enc)
        сводка = {"sha256": self.cb.sha(enc), "байт": 5}
        цель = os.path.join(self.tmp, "target")
        self.cb.запись(цель, enc, имя, сводка, self.stage, self.root)
        сайдкар = os.path.join(цель, "secrets-2026-10-26.manifest.json")
        self.assertEqual(sorted(os.listdir(цель)), [os.path.basename(сайдкар), имя])
        self.assertEqual(os.stat(сайдкар).st_mode & 0o777, 0o600)
        with unittest.mock.patch.object(self.cb, "сверить_копию",
                                        side_effect=OSError("копия прочиталась не тем")):
            with self.assertRaisesRegex(OSError, "не тем"):
                self.cb.запись(os.path.join(self.tmp, "t2"), enc, имя, сводка,
                               self.stage, self.root)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "t2")), [], "огрызок убран")
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = ""
        with self.assertRaisesRegex(OSError, "носитель не смонтирован"):
            self.cb.запись(os.path.join(self.tmp, "t3"), enc, имя, сводка,
                           self.stage, self.root)

    def test_ротация_по_префиксу_не_трогает_ядро(self):
        цель = os.path.join(self.tmp, "target")
        for п in ("core", "secrets"):
            for д in ("2026-10-20", "2026-10-21", "2026-10-22"):
                for с in (".tar.gz.gpg", ".manifest.json"):
                    self.файл("target", "%s-%s%s" % (п, д, с))
            self.файл("target", ".%s-2026-10-19.tar.gz.gpg.tmp" % п)
        self.cb.ротация(цель, keep=1, префикс="secrets")
        self.assertEqual(sorted(os.listdir(цель)),
                         [".core-2026-10-19.tar.gz.gpg.tmp"]
                         + sorted("core-%s%s" % (д, с) for д in ("2026-10-20", "2026-10-21", "2026-10-22")
                                  for с in (".tar.gz.gpg", ".manifest.json"))
                         + ["secrets-2026-10-22.manifest.json", "secrets-2026-10-22.tar.gz.gpg"])
        # и на sftp — те же шаблоны
        каталог = os.path.join(self.удалённо, "mara")
        for п in ("core", "secrets"):
            for д in ("2026-10-20", "2026-10-21"):
                self.файл("remote", "mara", "%s-%s.tar.gz.gpg" % (п, д))
        self.файл("remote", "mara", ".secrets-2026-10-19.tar.gz.gpg.tmp")
        self.cb.ротация("sftp://user@host/mara", keep=1, префикс="secrets")
        self.assertEqual(sorted(os.listdir(каталог)),
                         ["core-2026-10-20.tar.gz.gpg", "core-2026-10-21.tar.gz.gpg",
                          "secrets-2026-10-21.tar.gz.gpg"])

    def test_бандл_на_носитель_и_sftp(self):
        self.состав()
        цель = os.path.join(self.tmp, "target")
        url = "sftp://user@host/mara"
        self.файл("target", "secrets-2020-01-01.tar.gz.gpg")   # старое поколение
        r = self.cb.бандл_секретов(self.root, [цель, url], "пароль", self.stage, keep=1)
        имя = r["архив"]
        self.assertTrue(имя.startswith("secrets-") and имя.endswith(".tar.gz.gpg"), имя)
        self.assertEqual((r["носители"], r["файлов"], r["нечитаемо"]), ([цель, url], 10, 0))
        сайдкар = имя.replace(".tar.gz.gpg", ".manifest.json")
        self.assertEqual(sorted(os.listdir(цель)), [сайдкар, имя], "ротация убрала 2020")
        self.assertEqual(sorted(os.listdir(os.path.join(self.удалённо, "mara"))), [сайдкар, имя])
        with open(os.path.join(цель, сайдкар), encoding="utf-8") as fh:
            self.assertEqual(sorted(json.load(fh)),
                             ["created", "sha256", "архив", "байт", "нечитаемо", "файлов"],
                             "в сайдкаре только числа, имена файлов — внутри")
        import tarfile
        with tarfile.open(os.path.join(цель, имя)) as tf:    # `шифр` = копия
            self.assertEqual(sorted(tf.getnames()), sorted(self.ОЖИДАЕМО + ["manifest.json"]))
            манифест = json.load(tf.extractfile("manifest.json"))
        self.assertEqual((sorted(манифест["files"]), манифест["unreadable"]), (self.ОЖИДАЕМО, 0))
        with open(mi.ОТМЕТКА_СЕКРЕТОВ, encoding="utf-8") as fh:
            о = json.load(fh)
        self.assertEqual((о["носители"], о["нечитаемо"], о["archive"]), ([цель, url], 0, имя))
        # мёртвый хост — цель «не записана», носитель — записан; никого — отказ
        r = self.cb.бандл_секретов(self.root, [цель, "sftp://user@dead/mara"], "пароль",
                                   self.stage, keep=1)
        self.assertEqual(r["носители"], [цель])
        with self.assertRaisesRegex(RuntimeError, "ни один носитель"):
            self.cb.бандл_секретов(self.root, ["sftp://user@dead/mara"], "пароль",
                                   self.stage, keep=1)


if __name__ == "__main__":
    unittest.main()
