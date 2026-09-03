"""Общие мелочи: окружение из файла и адреса, которых нет в репозитории."""
import os, sys, tempfile, unittest

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


if __name__ == "__main__":
    unittest.main()
