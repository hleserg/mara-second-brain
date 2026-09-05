"""Диагностика вместо молчания в бэкапных скриптах (issue #38).

Оба дефекта тихие: список носителей с пробелом внутри пути распадается на
несуществующие куски и пропускается по `continue`, а пустой носитель под
`pipefail` роняет тест восстановления кодом `ls` — раньше, чем печатается
заготовленный диагноз. В обоих случаях в логе не остаётся ничего, по чему
видно, что копии нет."""
import io, os, sys, time, shutil, subprocess, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


# Разбор носителей и порог читаются при импорте, поэтому сверку в этих
# тестах запускаем подпроцессом: в этом процессе модуль уже загружен.
СВЕРКА = ("import sys; sys.path.insert(0, %r);"
          " import contextd_reconcile as rc;"
          " print([(f['check'], f['detail']) for f in rc.бэкап_ядра()])"
          % os.path.join(ROOT, "scripts"))


def прогнать(скрипт, **env):
    окр = dict(os.environ, **env)
    return subprocess.run(["bash", os.path.join(ROOT, "scripts", скрипт)],
                          capture_output=True, text=True, env=окр)


class Носители(unittest.TestCase):
    def test_пробел_в_пути_валит_скрипт_волта(self):
        r = прогнать("vault-backup.sh", TARGETS="/mnt/a /mnt/мой диск")
        self.assertEqual(r.returncode, 1)
        self.assertIn("не абсолютный", r.stderr)

    def test_пробел_в_пути_валит_бэкап_ядра(self):
        r = subprocess.run([sys.executable,
                            os.path.join(ROOT, "scripts", "core-backup.py"),
                            "--targets", "/mnt/a /mnt/мой диск"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("не абсолютный", r.stderr)

    def test_пробел_в_пути_виден_сверке(self):
        # Сверка разбирает тот же список: разойдись она с бэкапом, носитель,
        # на который бэкап не пишет, она считала бы живым. Но падать ей
        # нельзя — с ней замолчали бы DLQ, сердцебиения и ретеншен, поэтому
        # ждём находку, а не ненулевой код.
        r = subprocess.run([sys.executable, "-c", СВЕРКА],
                           capture_output=True, text=True,
                           env=dict(os.environ,
                                    MARA_CORE_TARGETS="/mnt/a /mnt/мой диск"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("бэкап-ядра-конфиг", r.stdout)
        # Именно текст: имя находки то же самое, что у пустого списка
        # носителей, и без текста тест зеленел бы, даже когда жалоба
        # разбора до находки не доезжает.
        self.assertIn("не абсолютный", r.stdout)


class Порог(unittest.TestCase):
    """Порог живёт строкой в crontab, и единственный способ проверить, что
    его оттуда читают, — прогнать сверку с ним в окружении. Вызов `_порог`
    из теста доказывает лишь, что функция читает имя, которое ей дали:
    имя приходит из теста, а не из кода, и опечатка в коде так не видна."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        цель = os.path.join(self.tmp, "backup")
        os.makedirs(цель)
        архив = os.path.join(цель, "core-2026-09-04.tar.gz.gpg")
        open(архив, "wb").close()
        час_назад = time.time() - 3600
        os.utime(архив, (час_назад, час_назад))
        self.цель = цель

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def сверка(self, суток):
        r = subprocess.run(
            [sys.executable, "-c", СВЕРКА], capture_output=True, text=True,
            env=dict(os.environ, MARA_CORE_TARGETS=self.цель,
                     MARA_STATE=self.tmp,
                     MARA_BACKUP_ALLOW_SAME_DEV="1",
                     MARA_CORE_BACKUP_MAX_DAYS=суток))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_порог_из_крона_меняет_поведение_сверки(self):
        # Архиву час: при пороге в полторы минуты он обязан считаться
        # протухшим, при пороге в сутки — свежим. Если строку в crontab не
        # читают, оба прогона дадут одно и то же.
        self.assertIn("бэкап-ядра-устарел", self.сверка("0.001"))
        self.assertNotIn("бэкап-ядра-устарел", self.сверка("1"))

    def test_значение_из_крона_совпадает_с_дефолтом_кода(self):
        # Разъехавшись, эти двое молчат: в бою побеждает crontab, и слабина
        # в 0.2 суток теряется ровно там, где заведена, — на боевой машине.
        with io.open(os.path.join(ROOT, "install", "mara.cron"),
                     encoding="utf-8") as ф:
            строки = ф.readlines()
        присвоения = [i for i, l in enumerate(строки)
                      if l.startswith("MARA_CORE_BACKUP_MAX_DAYS=")]
        работы = [i for i, l in enumerate(строки) if "core-backup.py" in l]
        self.assertEqual(len(присвоения), 1, присвоения)
        self.assertEqual(len(работы), 1, работы)
        # `VAR=` в crontab действует только на работы ниже себя — сказано в
        # самом файле, рядом с этим присвоением. Значение сверялось, место
        # нет: присвоение, уехавшее под работу, оставляет этот тест зелёным
        # и порог мёртвым. Дыра не из этого PR, чинится тем же движением,
        # что и у соседа; нашёл ревьюер, круг 2.
        self.assertLess(присвоения[0], работы[0],
                        "присвоение ниже работы — работа его не увидит")
        код = ("import sys; sys.path.insert(0, %r);"
               " import contextd_reconcile as rc; print(rc.БЭКАП_СУТКИ)"
               % os.path.join(ROOT, "scripts"))
        окр = {k: v for k, v in os.environ.items()
               if k != "MARA_CORE_BACKUP_MAX_DAYS"}
        r = subprocess.run([sys.executable, "-c", код], env=окр,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(float(строки[присвоения[0]].split("=", 1)[1]),
                         float(r.stdout))


class ПустойНоситель(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_нет_бандлов_это_диагноз_а_не_голый_код(self):
        # MIRROR=/proc — единственный способ взять «другое устройство» в
        # тестах; до самого зеркала дело не доходит, скрипт выходит раньше.
        r = прогнать("vault-restore-test.sh", FROM=self.tmp, MIRROR="/proc")
        self.assertEqual(r.returncode, 1)
        self.assertIn("нет бандлов", r.stderr)


if __name__ == "__main__":
    unittest.main()
