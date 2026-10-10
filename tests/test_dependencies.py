"""Зависимости: границы и файлы с хешами (Т0.9 п.6, threat-model §4 п.10).

Сторонний код питон-часть импортирует ровно в двух местах, и оба импорта
ленивые — внутри функций (ADR-0012 §В). Первая редакция того ADR искала
импорты по началу строки и утверждала, что зависимостей нет вовсе; этот
тест разбирает модули целиком (`ast`), чтобы третье имя не прошло так же
незамеченным. Файлы `install/requirements-*.txt` описывают venv на doctor
для установки с `--require-hashes` и для сканирования в CI (job `osv`):
каждая строка прибита к версии и несёт хеш, иначе ни то ни другое не
работает.
"""
import ast
import glob
import os
import sys
import unittest

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
СТОРОННИЕ = {"presidio_analyzer", "telegram"}
ФАЙЛЫ = {
    "install/requirements-venv.txt": "presidio-analyzer",
    "install/requirements-venv-tdlib.txt": "python-telegram",
}


def импорты(путь):
    with open(путь, encoding="utf-8") as fh:
        дерево = ast.parse(fh.read(), путь)
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.Import):
            for имя in узел.names:
                yield имя.name.split(".")[0]
        elif isinstance(узел, ast.ImportFrom) and узел.module and узел.level == 0:
            yield узел.module.split(".")[0]


def строки_требований(путь):
    """Логические строки requirements: продолжения `\\` склеены, комментарии
    и пустые выброшены."""
    out, тек = [], ""
    with open(путь, encoding="utf-8") as fh:
        for строка in fh:
            строка = строка.rstrip("\n")
            if not тек and (not строка.strip() or строка.lstrip().startswith("#")):
                continue
            if строка.endswith("\\"):
                тек += строка[:-1]
                continue
            out.append((тек + строка).split())
            тек = ""
    return out


class Границы(unittest.TestCase):
    def test_сторонних_имён_в_scripts_ровно_два(self):
        свои = {os.path.splitext(os.path.basename(p))[0]
                for p in glob.glob(os.path.join(КОРЕНЬ, "scripts", "*.py"))}
        чужие = {}
        for путь in sorted(glob.glob(os.path.join(КОРЕНЬ, "scripts", "*.py"))):
            for имя in импорты(путь):
                if имя_стороннее(имя, свои):
                    чужие.setdefault(имя, set()).add(os.path.basename(путь))
        self.assertEqual(set(чужие), СТОРОННИЕ,
                         "новое стороннее имя — сперва ADR-0012 §В и requirements: %r" % чужие)

    def test_гейт_и_тесты_без_сторонних(self):
        # сами тесты и run-tests.sh идут на голом python3 раннера
        свои = {os.path.splitext(os.path.basename(p))[0]
                for p in glob.glob(os.path.join(КОРЕНЬ, "scripts", "*.py"))}
        свои |= {os.path.splitext(os.path.basename(p))[0]
                 for p in glob.glob(os.path.join(КОРЕНЬ, "tests", "*.py"))}
        for путь in sorted(glob.glob(os.path.join(КОРЕНЬ, "tests", "*.py"))):
            for имя in импорты(путь):
                self.assertFalse(имя_стороннее(имя, свои),
                                 "%s импортирует %s" % (os.path.basename(путь), имя))


def имя_стороннее(имя, свои):
    return (имя not in sys.stdlib_module_names and имя not in свои_или_пакеты(свои)
            and имя_не_служебное(имя))


def свои_или_пакеты(свои):
    return свои | {"scripts", "tests"}


def имя_не_служебное(имя):
    return имя != "__future__"


class ФайлыТребований(unittest.TestCase):
    def test_каждая_строка_прибита_и_с_хешем(self):
        for файл in list(ФАЙЛЫ) + ["install/requirements-venv-models.txt"]:
            строки = строки_требований(os.path.join(КОРЕНЬ, файл))
            self.assertTrue(строки, файл)
            for части in строки:
                первая = части[0]
                self.assertTrue("==" in первая or первая.startswith("https://"),
                                "%s: не прибито: %s" % (файл, первая))
                хеши = [x for x in части[1:] if x.startswith("--hash=sha256:")]
                self.assertEqual(len(хеши), 1, "%s: без хеша: %s" % (файл, первая))
                self.assertEqual(len(хеши[0]) - len("--hash=sha256:"), 64, первая)

    def test_каждое_стороннее_имя_прибито_в_своём_файле(self):
        for файл, пакет in ФАЙЛЫ.items():
            имена = {части[0].split("==")[0] for части in
                     строки_требований(os.path.join(КОРЕНЬ, файл))}
            self.assertIn(пакет, имена, файл)

    def test_версии_верхушки_как_в_adr(self):
        """ADR-0012 §В называет версии; файлы не имеют права разъехаться с ним молча."""
        with open(os.path.join(КОРЕНЬ, "docs/adr/0012-adopted-components.md"),
                  encoding="utf-8") as fh:
            adr = fh.read()
        пины = {}
        for файл in ФАЙЛЫ:
            for части in строки_требований(os.path.join(КОРЕНЬ, файл)):
                имя, _, версия = части[0].partition("==")
                пины[имя] = версия
        for пакет in ("presidio-analyzer", "spacy", "python-telegram"):
            self.assertIn("| %s | %s" % (пакет, пины[пакет]), adr,
                          "%s %s не как в ADR-0012 §В" % (пакет, пины[пакет]))


if __name__ == "__main__":
    unittest.main()
