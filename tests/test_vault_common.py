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
        выглядел как таймаут, а не как ненастроенная машина."""
        os.environ.pop("МАРА_ТЕСТ_А", None)
        with self.assertRaises(SystemExit) as e:
            vault_common.нужен_адрес("МАРА_ТЕСТ_А", "коробка с чем-то")
        self.assertIn("МАРА_ТЕСТ_А", str(e.exception))
        self.assertIn("~/.config/mara/env", str(e.exception))
        os.environ["МАРА_ТЕСТ_А"] = "http://есть:1"
        self.assertEqual(vault_common.нужен_адрес("МАРА_ТЕСТ_А", "что-то"),
                         "http://есть:1")


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

    # 127.* и 0.0.0.0 разрешены: это петля и «слушать везде», не домашняя сеть
    СЕТЬ = re.compile(
        r"\b(?:10\.\d{1,3}|192\.168\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3})"
        r"\.\d{1,3}\b|\b\w+@(?:\d{1,3}\.){3}\d{1,3}\b")
    # примеры диапазонов в тестах и подсказках интерфейса — не адреса владельца
    МОЖНО = ("android/app/src/main/res/values/strings.xml",
             "android/app/src/test/java/com/mara/capture/CoreTest.kt",
             "docs/vault-cleanup-step3.md",
             "docs/USER-MANUAL-STEPS.md",
             "tests/test_vault_common.py")

    def test_в_коде_нет_адресов_домашней_сети(self):
        корень = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        files = subprocess.run(["git", "ls-files", "scripts", "install", "android",
                                "tests", "docs", "README.md"],
                               cwd=корень, capture_output=True, text=True).stdout.split()
        найдено = []
        for f in files:
            if f in self.МОЖНО or not f.endswith((".py", ".sh", ".kt", ".md", ".xml")):
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
