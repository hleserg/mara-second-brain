"""systemd-юниты из шаблонов в /etc/systemd/system (issue #89).

Настоящий systemd и настоящий sudo не трогаем: подсовываем скрипту свои в
PATH. `sudo` просто исполняет остальное, `systemctl` читает юнит из подменённого
$DEST и пишет каждый свой вызов в файл — по нему и проверяется, что установщик
**не** рестартует сервис. Рестарт contextd перестраивает таблицы реестра
(`_ужать_ledger` в `mara_ingest.connect`), и это гейт Г4, а не побочный эффект
установщика.
"""
import os, stat, shutil, tempfile, subprocess, unittest

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
УСТАНОВЩИК = os.path.join(КОРЕНЬ, "install", "install-units.sh")
ЮНИТЫ = ("contextd.service", "tdlib-ingest.service")


class Установщик(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dest = os.path.join(self.tmp, "systemd")
        self.журнал = os.path.join(self.tmp, "systemctl.log")
        os.makedirs(self.dest)
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        # Настоящий sudo даёт root, наш — нет, поэтому у `install` снимаем смену
        # владельца: без root `-o root` падает на chown. Остальное исполняется
        # как есть, чтобы проверялся настоящий вызов, а не его пересказ.
        self.шим(bin_, "sudo",
                 'if [ "${1:-}" = install ]; then\n'
                 '  shift; args=()\n'
                 '  while [ $# -gt 0 ]; do\n'
                 '    case "$1" in -o|-g) shift 2;; *) args+=("$1"); shift;; esac\n'
                 '  done\n'
                 '  exec install "${args[@]}"\n'
                 'fi\n'
                 'exec "$@"\n')
        # `systemctl cat X` отдаёт файл из подменённого $DEST с той же первой
        # строкой-комментарием, что и настоящий; остальные вызовы только пишутся
        # в журнал. Так тест видит и то, что юнит поставлен, и то, чего
        # установщик не делал.
        self.шим(bin_, "systemctl",
                 'echo "$@" >> "%s"\n'
                 'if [ "${1:-}" = cat ]; then\n'
                 '  echo "# %s/$2"; cat "%s/$2"; fi\n'
                 % (self.журнал, self.dest, self.dest))
        self.env = dict(os.environ,
                        PATH=bin_ + os.pathsep + os.environ["PATH"],
                        REPO="/srv/checkout", TPL_DIR=os.path.join(КОРЕНЬ, "install"),
                        USER_NAME="mara", STATE="/var/lib/mara",
                        VENV_TDLIB="/opt/venv/bin/python", DEST=self.dest)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def шим(self, каталог, имя, тело):
        p = os.path.join(каталог, имя)
        with open(p, "w") as fh:
            fh.write("#!/usr/bin/env bash\n" + тело)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def запуск(self, *args, **правки):
        env = dict(self.env, **правки)
        return subprocess.run(["bash", УСТАНОВЩИК] + list(args), env=env,
                              capture_output=True, text=True)

    def вызовы(self):
        if not os.path.exists(self.журнал):
            return []
        with open(self.журнал) as fh:
            return [l.strip() for l in fh if l.strip()]

    def test_проверка_без_установленных_юнитов_говорит_что_их_нет(self):
        r = self.запуск("--check")
        self.assertEqual(r.returncode, 1, r.stdout)
        for u in ЮНИТЫ:
            self.assertIn("%s в %s нет" % (u, self.dest), r.stdout)

    def test_установка_кладёт_оба_юнита_и_перечитывает(self):
        r = self.запуск("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        for u in ЮНИТЫ:
            self.assertTrue(os.path.exists(os.path.join(self.dest, u)), u)
        self.assertIn("daemon-reload", self.вызовы())

    def test_установка_не_рестартует_сервис(self):
        """Рестарт contextd перестраивает таблицы реестра — это гейт Г4."""
        self.запуск("--apply")
        for вызов in self.вызовы():
            self.assertFalse(вызов.split()[0] in ("restart", "start", "reload-or-restart",
                                                  "try-restart", "enable"),
                             "установщик позвал systemctl %s" % вызов)
        r = self.запуск("--apply")
        self.assertIn("рестарта НЕ делал", r.stdout)

    def test_после_установки_проверка_совпадает(self):
        self.запуск("--apply")
        r = self.запуск("--check")
        self.assertEqual(r.returncode, 0, r.stdout)
        for u in ЮНИТЫ:
            self.assertIn("%s совпадает с шаблоном" % u, r.stdout)

    def test_подставленные_значения_доезжают_до_юнита(self):
        self.запуск("--apply")
        with open(os.path.join(self.dest, "contextd.service")) as fh:
            ctx = fh.read()
        with open(os.path.join(self.dest, "tdlib-ingest.service")) as fh:
            tdl = fh.read()
        self.assertIn("User=mara", ctx)
        self.assertIn("ExecStart=/usr/bin/python3 /srv/checkout/scripts/contextd.py", ctx)
        self.assertIn("/srv/vault /var/lib/mara", ctx)
        self.assertIn("ExecStart=/opt/venv/bin/python /srv/checkout/scripts/tdlib_ingest.py", tdl)

    def test_расхождение_видно_и_даёт_единицу(self):
        self.запуск("--apply")
        путь = os.path.join(self.dest, "contextd.service")
        with open(путь) as fh:
            было = fh.read()
        with open(путь, "w") as fh:
            fh.write(было.replace("User=mara", "User=someoneelse"))
        r = self.запуск("--check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("расходится с шаблоном", r.stdout)
        self.assertIn("-User=someoneelse", r.stdout, "дифф не показан")

    def test_незаполненная_подстановка_останавливает_оба_режима(self):
        """systemd не считает `@STATE@` ошибкой — примет за относительный путь."""
        своё = os.path.join(self.tmp, "tpl")
        os.makedirs(своё)
        for u in ЮНИТЫ:
            with open(os.path.join(КОРЕНЬ, "install", u + ".in")) as fh:
                т = fh.read()
            with open(os.path.join(своё, u + ".in"), "w") as fh:
                fh.write(т.replace("@VENV_TDLIB@", "@VENV@"))
        for режим in ("--check", "--apply"):
            with self.subTest(режим=режим):
                r = self.запуск(режим, TPL_DIR=своё)
                self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                self.assertIn("незаполненная подстановка", r.stderr)
        self.assertEqual(os.listdir(self.dest), [], "юнит всё-таки поставлен")
        self.assertEqual(self.вызовы(), [], "systemctl всё-таки позван")

    def test_амперсанд_в_пути_не_искажается(self):
        """В правой части `s|…|…|` амперсанд означает весь матч."""
        r = self.запуск("--apply", REPO="/srv/a&b")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.dest, "contextd.service")) as fh:
            self.assertIn("ExecStart=/usr/bin/python3 /srv/a&b/scripts/contextd.py",
                          fh.read())

    def test_неверный_режим_ничего_не_делает(self):
        r = self.запуск("--frobnicate")
        self.assertEqual(r.returncode, 2)
        self.assertIn("использование", r.stderr)
        self.assertEqual(os.listdir(self.dest), [])

    def test_systemd_видит_не_то_что_поставили(self):
        """Проверяем не отправленное, а принятое: drop-in проявится здесь."""
        bin_ = os.path.join(self.tmp, "bin")
        self.шим(bin_, "systemctl",
                 'echo "$@" >> "%s"\n'
                 'if [ "${1:-}" = cat ]; then\n'
                 '  echo "# %s/$2"; cat "%s/$2"; echo "RestartSec=999"; fi\n'
                 % (self.журнал, self.dest, self.dest))
        r = self.запуск("--apply")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("systemd видит не то", r.stderr)

    def test_под_root_без_явных_значений_отказ(self):
        """`sudo ./install-units.sh --apply` поставил бы боевой юнит root'у.

        Под sudo `id -un` даёт root, `$HOME` становится /root — и юнит уезжает
        с `User=root` и состоянием в /root. Послеустановочная сверка это не
        поймает: она сравнивает установленное с рендером, а рендер под root тот
        же самый. Ломается первый рестарт, а не установка.
        """
        bin_ = os.path.join(self.tmp, "bin")
        self.шим(bin_, "id",
                 'case "${1:-}" in -u) echo 0;; -un) echo root;; *) exit 1;; esac\n')
        ТРИ = ("USER_NAME", "STATE", "VENV_TDLIB")
        env = {k: v for k, v in self.env.items() if k not in ТРИ}
        for режим in ("--check", "--apply"):
            with self.subTest(режим=режим):
                r = subprocess.run(["bash", УСТАНОВЩИК, режим], env=env,
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn("под root/sudo не запускать", r.stderr)
        # Половина просьбы — не просьба. Склейка `"$A$B$C"` пуста только когда
        # пусты все три, и одной заданной переменной хватало бы, чтобы проехать
        # с двумя остальными из /root. Поэтому каждая по отдельности.
        for задана in ТРИ:
            with self.subTest(задана=задана):
                частично = dict(env)
                частично[задана] = self.env[задана]
                r = subprocess.run(["bash", УСТАНОВЩИК, "--apply"],
                                   env=частично, capture_output=True, text=True)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn("под root/sudo не запускать", r.stderr)
        self.assertEqual(os.listdir(self.dest), [], "юнит всё-таки поставлен")
        self.assertEqual(self.вызовы(), [], "systemctl всё-таки позван")

    def test_под_root_с_явными_значениями_работает(self):
        """Отказ — про угаданные значения, а не про root как таковой."""
        bin_ = os.path.join(self.tmp, "bin")
        self.шим(bin_, "id",
                 'case "${1:-}" in -u) echo 0;; -un) echo root;; *) exit 1;; esac\n')
        r = self.запуск("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.dest, "contextd.service")) as fh:
            self.assertIn("User=mara", fh.read())

    def test_в_шаблонах_нет_системного_имени_пользователя(self):
        """Сторож против возврата: юнит в репозитории обязан быть шаблоном.

        Имени тут нарочно нет — иначе сторож сам стал бы утечкой. Проверка
        формы: в `User=` шаблона стоит подстановка, а не логин, и в путях нет
        ничего похожего на домашний каталог.
        """
        for u in ЮНИТЫ:
            with open(os.path.join(КОРЕНЬ, "install", u + ".in")) as fh:
                строки = fh.read().splitlines()
            with self.subTest(юнит=u):
                users = [l for l in строки if l.startswith("User=")]
                self.assertEqual(users, ["User=@USER@"], u)
                дом = [l for l in строки
                       if "/home/" in l or "/Users/" in l or "/root/" in l]
                self.assertEqual(дом, [], "домашний путь зашит в шаблон")
        # и самих готовых юнитов в репозитории быть не должно
        for u in ЮНИТЫ:
            self.assertFalse(os.path.exists(os.path.join(КОРЕНЬ, "install", u)),
                             "install/%s вернулся — он должен быть только .in" % u)


if __name__ == "__main__":
    unittest.main()
