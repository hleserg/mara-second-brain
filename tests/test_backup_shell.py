"""Диагностика вместо молчания в бэкапных скриптах (issue #38).

Оба дефекта тихие: список носителей с пробелом внутри пути распадается на
несуществующие куски и пропускается по `continue`, а пустой носитель под
`pipefail` роняет тест восстановления кодом `ls` — раньше, чем печатается
заготовленный диагноз. В обоих случаях в логе не остаётся ничего, по чему
видно, что копии нет."""
import os, sys, shutil, subprocess, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


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
        # ждём находку, а не ненулевой код. Подпроцессом, потому что разбор
        # идёт при импорте: в этом же процессе модуль уже загружен.
        код = ("import sys; sys.path.insert(0, %r);"
               " import contextd_reconcile as rc;"
               " print([f['check'] for f in rc.бэкап_ядра()])"
               % os.path.join(ROOT, "scripts"))
        r = subprocess.run([sys.executable, "-c", код],
                           capture_output=True, text=True,
                           env=dict(os.environ,
                                    MARA_CORE_TARGETS="/mnt/a /mnt/мой диск"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("бэкап-ядра-конфиг", r.stdout)


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
