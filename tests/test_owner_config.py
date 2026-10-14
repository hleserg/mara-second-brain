"""Г7: имя и канон владельца живут вне репозитория — `MARA_OWNER` и
`MARA_OWNER_NAME` в окружении или в `~/.config/mara/env`."""
import importlib.util, json, os, re, subprocess, sys, tempfile, unittest
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import vault_common
import call_extract as ce
import contextd_reconcile as cr


def _модуль(имя):
    spec = importlib.util.spec_from_file_location(
        имя.replace("-", "_")[:-3], os.path.join(СКРИПТЫ, имя))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Окружение(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = os.path.join(self.dir, "mara.env")
        чисто = mock.patch.dict(os.environ, {"MARA_ENV_FILE": self.env})
        чисто.start()
        self.addCleanup(чисто.stop)
        for k in ("MARA_OWNER", "MARA_OWNER_NAME"):
            os.environ.pop(k, None)

    def файл(self, текст):
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write(текст)


class Владелец(_Окружение):
    def test_окружение_файл_умолчание(self):
        self.assertEqual(vault_common.владелец(), vault_common.ВЛАДЕЛЕЦ_ПО_УМОЛЧАНИЮ)
        self.assertFalse(vault_common.владелец_задан())
        self.файл("export MARA_OWNER=ivan-petrov\nMARA_OWNER_NAME='Ваня'\n")
        self.assertEqual((vault_common.владелец(), vault_common.имя_владельца()),
                         ("ivan-petrov", "Ваня"))
        self.assertTrue(vault_common.владелец_задан())
        os.environ["MARA_OWNER"] = "oleg"
        self.assertEqual(vault_common.владелец(), "oleg", "окружение сильнее файла")

    def test_имя_не_задано_пусто(self):
        self.assertEqual(vault_common.имя_владельца(), "")


class Промпт(_Окружение):
    def test_без_имени_просто_владелец(self):
        п = ce.промпт()
        self.assertNotIn("{кто}", п)
        self.assertNotIn("зовут", п)
        self.assertIn("разговора владельца.", п)
        self.assertFalse(ce.конфигурация()["owner_named"])

    def test_имя_из_файла_в_промпте_но_не_в_конфигурации(self):
        self.файл("MARA_OWNER_NAME=Олег\n")
        self.assertIn("разговора владельца. Владельца зовут Олег.", ce.промпт())
        конф = ce.конфигурация()
        self.assertTrue(конф["owner_named"])
        self.assertNotIn("Олег", json.dumps(конф, ensure_ascii=False))

    def test_сводка_дня(self):
        ds = _модуль("daily-summary.py")
        self.assertNotIn("зовут", ds.промпт())
        self.файл("MARA_OWNER_NAME=Олег\n")
        self.assertTrue(ds.промпт().endswith("Владельца зовут Олег."))


class Сверка(_Окружение):
    def волт(self, индекс=True):
        v = os.path.join(self.dir, "vault")
        os.makedirs(os.path.join(v, "_system"), exist_ok=True)
        if индекс:
            with open(os.path.join(v, "_system/entity-index.json"), "w") as fh:
                fh.write("[]")
        return v

    def test_не_задан_при_живом_волте(self):
        н, = cr.владелец_не_задан(self.волт())
        self.assertEqual((н["check"], н["level"]), ("владелец-не-задан", "warn"))

    def test_задан_или_волта_нет_молчит(self):
        self.assertEqual(cr.владелец_не_задан(None), [])
        self.assertEqual(cr.владелец_не_задан(self.волт(индекс=False)), [])
        self.файл("MARA_OWNER=oleg\n")
        self.assertEqual(cr.владелец_не_задан(self.волт()), [])


class Публичность(unittest.TestCase):
    """Г7 механикой: имени владельца нет ни в одном отслеживаемом файле —
    код, тесты, документы. Формы записаны кодами: сам тест не должен быть
    исключением из своего правила. История git старое имя хранит — её не
    переписываем, но новое не добавится."""

    def test_нет_имени_в_коде(self):
        корень = os.path.join(СКРИПТЫ, "..")
        файлы = subprocess.run(["git", "ls-files"],
                               cwd=корень, capture_output=True, text=True).stdout.split()
        if not файлы:
            self.skipTest("не git-клон")
        формы = re.compile("|".join(map(re.escape, [
            "Серг", "Серёг",
            "Серёж", "сергей",
            "s" + "ergey"])), re.I)
        нашлось = []
        for f in файлы:
            try:
                текст = open(os.path.join(корень, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if формы.search(текст):
                нашлось.append(f)
        self.assertEqual(нашлось, [])


if __name__ == "__main__":
    unittest.main()
