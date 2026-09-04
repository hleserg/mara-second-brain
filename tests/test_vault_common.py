"""Общие мелочи: окружение из файла и адреса, которых нет в репозитории."""
import os, re, sys, subprocess, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import vault_common


class ТестОкружениеИзФайла(unittest.TestCase):

    def setUp(self):
        for k in ("МАРА_ТЕСТ_А", "МАРА_ТЕСТ_Б"):
            os.environ.pop(k, None)

    tearDown = setUp

    def test_читает_ключи_но_не_затирает_заданное(self):
        путь = os.path.join(tempfile.mkdtemp(), "env")
        with open(путь, "w", encoding="utf-8") as fh:
            fh.write("# комментарий\n"
                     "МАРА_ТЕСТ_А=http://пример:1\n"
                     "\n"
                     "МАРА_ТЕСТ_Б='в кавычках'\n")
        os.environ["МАРА_ТЕСТ_А"] = "уже задано"
        vault_common.load_env(путь)
        # systemd и cron задают своё, файл их не перебивает
        self.assertEqual(os.environ["МАРА_ТЕСТ_А"], "уже задано")
        self.assertEqual(os.environ["МАРА_ТЕСТ_Б"], "в кавычках")

    def test_нет_файла_не_беда(self):
        vault_common.load_env("/нет/такого/файла")   # молча, это не ошибка

    def test_нужен_адрес_умирает_внятно(self):
        """Раньше тут был адрес домашней сети по умолчанию: промах мимо порта
        выглядел как таймаут, а не как ненастроенная машина.

        MARA_ENV_FILE уводим в никуда руками. `нужен_адрес` на промахе читает
        env-файл, а на doctor он лежит на месте: без подмены тест затянул бы в
        свой процесс живой OPENROUTER_API_KEY и назвал бы в сообщении чужой
        путь. Что по умолчанию это `~/.config/mara/env` — проверяет
        `test_путь_по_умолчанию`.
        """
        os.environ.pop("МАРА_ТЕСТ_А", None)
        нет = os.path.join(tempfile.mkdtemp(), "нет-такого")
        было = os.environ.get("MARA_ENV_FILE")
        os.environ["MARA_ENV_FILE"] = нет
        try:
            with self.assertRaises(SystemExit) as e:
                vault_common.нужен_адрес("МАРА_ТЕСТ_А", "коробка с чем-то")
            self.assertIn("МАРА_ТЕСТ_А", str(e.exception))
            self.assertIn(нет, str(e.exception))
            os.environ["МАРА_ТЕСТ_А"] = "http://есть:1"
            self.assertEqual(vault_common.нужен_адрес("МАРА_ТЕСТ_А", "что-то"),
                             "http://есть:1")
        finally:
            os.environ.pop("MARA_ENV_FILE", None)
            if было is not None:
                os.environ["MARA_ENV_FILE"] = было

    def test_путь_по_умолчанию(self):
        было = os.environ.pop("MARA_ENV_FILE", None)
        try:
            self.assertEqual(vault_common.env_file(), "~/.config/mara/env")
        finally:
            if было is not None:
                os.environ["MARA_ENV_FILE"] = было


class ТестРазбораEnv(unittest.TestCase):
    """Файл сорсится ещё и шеллом, поэтому разбор должен совпадать с шелловским."""

    def файл(self, текст, режим="w"):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "env")
        with open(p, режим, **({} if режим == "wb" else {"encoding": "utf-8"})) as fh:
            fh.write(текст)
        return p

    def прочитать(self, путь, ключи):
        было = {k: os.environ.pop(k, None) for k in ключи}
        try:
            vault_common.load_env(путь)
            return {k: os.environ.get(k) for k in ключи}
        finally:
            for k, v in было.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_хвостовой_комментарий_не_попадает_в_url(self):
        p = self.файл("MARA_ASR_URL=http://x:2 # коробка\n")
        self.assertEqual(self.прочитать(p, ["MARA_ASR_URL"])["MARA_ASR_URL"],
                         "http://x:2")

    def test_export_разбирается_как_в_шелле(self):
        p = self.файл("export MARA_MAC=user@host\n")
        self.assertEqual(self.прочитать(p, ["MARA_MAC"])["MARA_MAC"], "user@host")

    def test_битая_строка_не_обрывает_файл(self):
        p = self.файл("=пусто\nMARA_MAC=user@host\n")
        self.assertEqual(self.прочитать(p, ["MARA_MAC", ""])["MARA_MAC"], "user@host")

    def test_не_utf8_не_роняет_разбор(self):
        p = self.файл(b"MARA_MAC=user@host\nX=\xff\xfe\n", "wb")
        self.assertEqual(self.прочитать(p, ["MARA_MAC", "X"])["MARA_MAC"], "user@host")

    def test_mara_env_file_уводит_чтение(self):
        p = self.файл("MARA_MAC=из-файла\n")
        было = os.environ.get("MARA_ENV_FILE")
        os.environ["MARA_ENV_FILE"] = p
        try:
            self.assertEqual(self.прочитать(None, ["MARA_MAC"])["MARA_MAC"], "из-файла")
        finally:
            os.environ.pop("MARA_ENV_FILE", None)
            if было is not None:
                os.environ["MARA_ENV_FILE"] = было


class ТестАдресовВРепозитории(unittest.TestCase):
    """Сторож: репозиторий публичный, домашней сети в нём быть не должно.

    Тесты на сам `load_env` регрессию не ловят — они проверяют функцию в
    вакууме, а вернуть хардкод можно и мимо неё. Ловит только грепалка по
    дереву.
    """

    # 127.* и 0.0.0.0 разрешены: это петля и «слушать везде», не домашняя сеть.
    # 100.64/10 — CGNAT, оттуда адреса tailnet: не RFC1918, а домашняя сеть
    # ровно в том же смысле. Ветка для него отдельная и требует все четыре
    # октета: короткая запись диапазона `100.64/10` в описании правила pf под
    # неё не подходит и остаётся читаемой, а исключать четырёхоктетную форму
    # `100.64.0.0/10` хвостом `(?!/)` нельзя — тот же хвост пропускал бы
    # `http://<адрес>/healthz`, то есть ровно то, что ловим.
    СЕТЬ = re.compile(
        r"\b(?:10\.\d{1,3}|192\.168\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3})"
        r"\.\d{1,3}\b"
        r"|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"
        r"|\b\w+@(?:\d{1,3}\.){3}\d{1,3}\b")
    # примеры диапазонов в тестах и подсказках интерфейса — не адреса владельца.
    # Список короткий намеренно: каждое исключение — это файл, куда адрес может
    # вернуться незамеченным. USER-MANUAL-STEPS.md отсюда убран специально:
    # раздел «дома — по локалке» — самое вероятное место для такого возврата.
    # CoreTest.kt проверяет само правило «частный адрес или нет» — без литералов
    # 192.168/10/172.16 проверять там нечего. Ещё два исключения сняты по
    # ревью: vault-cleanup-step3.md прятал за собой настоящий адрес стенда
    # («отчёт о старой чистке» — ровно то место, где такое и оседает), а
    # подсказка в strings.xml обошлась именем `doctor.local` — приложение
    # такой хост принимает по http наравне с частным адресом (`Адрес.свой`)
    МОЖНО = ("android/app/src/test/java/com/mara/capture/CoreTest.kt",
             "tests/test_vault_common.py")
    # .service и .example тоже смотрим: Environment=MARA_MAC=... в юните и
    # пример env-файла — самые естественные места для адреса, а по расширению
    # они не .py и не .sh
    РАСШИРЕНИЯ = (".py", ".sh", ".kt", ".md", ".xml", ".service", ".example",
                  ".txt", ".yml", ".yaml", ".cron", ".json", ".kts", ".pro")

    def test_сторож_видит_адреса(self):
        # адреса синтетические, и файл этот в МОЖНО: обход его не читает.
        # RFC1918 и `юзер@адрес` проверяются здесь же не для красоты: все
        # живые литералы этих двух веток лежат в исключённых файлах, так что
        # поломка любой из них не уронила бы ни обход, ни что-либо ещё
        self.assertTrue(self.СЕТЬ.search("сервер 192.168.7.3"))
        self.assertTrue(self.СЕТЬ.search("ssh 10.1.2.3"))
        # отдельной строкой: search() встал бы на первом совпадении и
        # ветка 172.16/12 осталась бы непроверенной (круг 5 ревью)
        self.assertTrue(self.СЕТЬ.search("ssh 172.20.0.9"))
        self.assertTrue(self.СЕТЬ.search("scp sergey@203.0.113.9:/tmp"))
        self.assertIsNone(self.СЕТЬ.search("сервис на 203.0.113.9"))
        self.assertTrue(self.СЕТЬ.search("привязка 100.127.9.9"))
        self.assertTrue(self.СЕТЬ.search("http://100.64.1.2:8788/healthz"))
        # без порта и со слэшем сразу за адресом — тоже: круг 2 ревью нашёл,
        # что хвост `(?!/)` пропускал ровно эту форму
        self.assertTrue(self.СЕТЬ.search("http://100.64.1.2/healthz"))
        self.assertTrue(self.СЕТЬ.search("маршрут 100.64.1.2/32"))
        # соседи CGNAT снизу и сверху — обычные публичные адреса
        self.assertIsNone(self.СЕТЬ.search("100.63.0.1"))
        self.assertIsNone(self.СЕТЬ.search("100.128.0.1"))
        # короткая запись диапазона четырёх октетов не даёт и не ловится
        self.assertIsNone(self.СЕТЬ.search("правило NAT для 100.64/10"))

    def test_в_коде_нет_адресов_домашней_сети(self):
        корень = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        # -z и check=True: без них сторож молча зеленел там, где ловить и надо
        # больше всего. Вне git-репозитория `ls-files` даёт rc=128 и пустой
        # список — сравнение пустого с пустым проходило; а имя файла с не-ASCII
        # или пробелом git печатает в кавычках со «\320\275», и `.split()` его
        # разваливал. Имена в этом проекте кириллические сплошь и рядом.
        r = subprocess.run(["git", "ls-files", "-z", "--", "."],
                           cwd=корень, capture_output=True, check=True)
        files = [f for f in r.stdout.decode("utf-8").split("\0") if f]
        self.assertTrue(files, "git ls-files ничего не вернул — сторож слеп")
        найдено = []
        for f in files:
            if f in self.МОЖНО or not f.endswith(self.РАСШИРЕНИЯ):
                continue
            try:
                with open(os.path.join(корень, f), encoding="utf-8") as fh:
                    for n, line in enumerate(fh, 1):
                        м = self.СЕТЬ.search(line)
                        if м:
                            найдено.append("%s:%d %s" % (f, n, м.group()))
            except (OSError, UnicodeDecodeError):
                continue
        self.assertEqual(найдено, [], "адрес домашней сети вернулся в репозиторий")


if __name__ == "__main__":
    unittest.main()
