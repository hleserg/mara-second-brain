"""Расписание из репозитория в crontab (P0-5).

Настоящий crontab не трогаем: подсовываем скрипту свой `crontab` в PATH,
который читает и пишет обычный файл. Проверяем ровно то, ради чего
установщик написан: чужие строки уцелели, свои заменились, поломка отката
не оставляет машину без расписания."""
import os, sys, stat, shutil, tempfile, subprocess, unittest

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
УСТАНОВЩИК = os.path.join(КОРЕНЬ, "install", "install-cron.sh")
ЧУЖОЕ = "*/2 * * * * /home/sergey/scripts/mc-healthcheck.sh"
РУКАМИ = "40 4 * * * /usr/bin/python3 %s/scripts/blob_retention.py" % КОРЕНЬ


class Установщик(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.таблица = os.path.join(self.tmp, "crontab.txt")
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        шим = os.path.join(bin_, "crontab")
        with open(шим, "w") as fh:
            fh.write("#!/usr/bin/env bash\n"
                     'f="%s"\n'
                     'if [ "${1:-}" = "-l" ]; then cat "$f" 2>/dev/null; exit $?; fi\n'
                     'if [ "${1:-}" = "-" ]; then cat > "$f"; exit 0; fi\n'
                     'cp "$1" "$f"\n' % self.таблица)
        os.chmod(шим, os.stat(шим).st_mode | stat.S_IEXEC)
        self.env = dict(os.environ, PATH=bin_ + os.pathsep + os.environ["PATH"],
                        REPO=КОРЕНЬ, STATE=os.path.join(self.tmp, "state"),
                        VENV="/usr/bin/python3")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def запуск(self, *args):
        return subprocess.run(["bash", УСТАНОВЩИК] + list(args), env=self.env,
                              capture_output=True, text=True)

    def таблицу(self, текст=""):
        with open(self.таблица, "w") as fh:
            fh.write(текст)

    def прочесть(self):
        with open(self.таблица, encoding="utf-8") as fh:
            return fh.read()

    def test_проверка_на_пустом_кроне_говорит_о_расхождении(self):
        self.таблицу("")
        r = self.запуск("--check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("расходится", r.stdout)

    def test_установка_ставит_блок_и_не_трогает_чужое(self):
        self.таблицу(ЧУЖОЕ + "\n")
        r = self.запуск("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        стало = self.прочесть()
        self.assertIn(ЧУЖОЕ, стало, "чужой крон снесли — так нельзя")
        self.assertIn("core-backup.py", стало, "бэкап ядра не поставлен")
        with open(os.path.join(КОРЕНЬ, "install/mara.cron"), encoding="utf-8") as fh:
            self.assertIn("@REPO@", fh.read())
        self.assertNotIn("@REPO@", стало, "плейсхолдер уехал в живой crontab")
        self.assertEqual(self.запуск("--check").returncode, 0,
                         "сразу после установки проверка обязана быть чистой")

    def test_строку_поставленную_руками_заменяет_блоком(self):
        self.таблицу(ЧУЖОЕ + "\n" + РУКАМИ + "\n")
        r = self.запуск("--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("мимо блока", r.stdout)
        self.запуск("--apply")
        стало = self.прочесть()
        self.assertEqual(стало.count("blob_retention.py"), 1,
                         "старая строка осталась рядом с новой — работа дважды")
        self.assertIn(ЧУЖОЕ, стало)

    def test_строку_через_тильду_тоже_заменяет(self):
        """Половина живого крона записана как ~/mara-second-brain/...: по
        полному пути такие строки не находятся и работали бы вторым
        экземпляром рядом с новыми."""
        тильда = "*/10 * * * * ~/%s/scripts/mara-watchdog.sh" % os.path.basename(КОРЕНЬ)
        self.таблицу(ЧУЖОЕ + "\n" + тильда + "\n")
        self.env["HOME"] = os.path.dirname(КОРЕНЬ)
        self.assertIn("мимо блока", self.запуск("--check").stdout)
        self.запуск("--apply")
        стало = self.прочесть()
        self.assertEqual(стало.count("mara-watchdog.sh"), 1,
                         "строка через тильду осталась — сторож пойдёт дважды")
        self.assertIn(ЧУЖОЕ, стало)

    def test_повторная_установка_ничего_не_добавляет(self):
        self.таблицу(ЧУЖОЕ + "\n")
        self.запуск("--apply")
        первое = self.прочесть()
        self.запуск("--apply")
        self.assertEqual(первое, self.прочесть(), "установщик не идемпотентен")

    def test_копия_старого_crontab_сохраняется(self):
        self.таблицу(ЧУЖОЕ + "\n")
        self.запуск("--apply")
        копии = os.listdir(os.path.join(self.tmp, "state"))
        self.assertTrue(копии, "копия не сделана — откатываться не с чего")
        with open(os.path.join(self.tmp, "state", копии[0]), encoding="utf-8") as fh:
            self.assertIn(ЧУЖОЕ, fh.read())
    def test_блок_без_закрывающей_строки_не_съедает_чужое(self):
        """END стёрли правкой через `crontab -e`. awk с флагом дальше не
        сбрасывается, и всё, что ниже BEGIN, пропадает из разбора — а новые
        работы crontab -e дописывает как раз в конец."""
        начало = ("# >>> mara-second-brain: install/mara.cron, "
                  "правки руками затрёт >>>")
        self.таблицу(начало + "\n0 1 * * * /bin/true\n" + ЧУЖОЕ + "\n")
        r = self.запуск("--check")
        self.assertIn("маркеры", r.stderr)
        r = self.запуск("--apply")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn(ЧУЖОЕ, self.прочесть(), "чужую работу снесли молча")

    def test_нечитаемый_crontab_не_повод_ставить_с_нуля(self):
        """`crontab -l` отвечает единицей и на «крона нет», и на «нет прав».
        Приняв второе за первое, установщик затёр бы чужое расписание, а копия
        легла бы пустой — откатываться было бы нечем."""
        with open(os.path.join(self.tmp, "bin", "crontab"), "w") as fh:
            fh.write("#!/usr/bin/env bash\n"
                     'if [ "${1:-}" = "-l" ]; then\n'
                     '  echo "You (sergey) are not allowed to use this program" >&2\n'
                     "  exit 1\n"
                     "fi\n"
                     'cp "$1" "%s"\n' % self.таблица)
        self.таблицу(ЧУЖОЕ + "\n")
        r = self.запуск("--apply")
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("не читается", r.stderr)
        self.assertIn(ЧУЖОЕ, self.прочесть(), "расписание затёрли по отказу -l")


if __name__ == "__main__":
    unittest.main()
