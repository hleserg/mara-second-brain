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
    def волт(self, индекс=("ivan-petrov",)):
        v = os.path.join(self.dir, "vault")
        os.makedirs(os.path.join(v, "_system"), exist_ok=True)
        if индекс is not None:
            with open(os.path.join(v, "_system/entity-index.json"), "w") as fh:
                json.dump([{"canonical": к, "type": "person", "aliases": [], "title": к}
                           for к in индекс], fh)
        return v

    def находки(self, v):
        return [(н["check"], н["level"]) for н in cr.владелец_не_задан(v)]

    def test_не_задан(self):
        self.assertEqual(self.находки(self.волт()), [("владелец-не-задан", "error")])

    def test_не_в_индексе(self):
        self.файл("MARA_OWNER=oleg\n")
        self.assertEqual(self.находки(self.волт()), [("владелец-не-в-индексе", "error")])

    def test_канон_без_учёта_регистра_и_без_имени(self):
        self.файл("MARA_OWNER=Ivan-Petrov\n")
        self.assertEqual(vault_common.владелец(), "ivan-petrov")
        self.assertEqual(self.находки(self.волт()), [("имя-владельца-не-задано", "warn")])
        self.файл("MARA_OWNER=Ivan-Petrov\nMARA_OWNER_NAME=Ваня\n")
        self.assertEqual(self.находки(self.волт()), [])

    def test_без_волта_или_индекса_молчит(self):
        self.assertEqual(cr.владелец_не_задан(None), [])
        self.assertEqual(cr.владелец_не_задан(self.волт(индекс=None)), [])


class Застава(_Окружение):
    """Проектор не пишет в боевой волт от имени владельца, которого нет в
    индексе: иначе выкат без `MARA_OWNER` молча метил бы карточки
    `owner: owner`."""

    def волт(self):
        v = os.path.join(self.dir, "vault")
        os.makedirs(os.path.join(v, "_system"), exist_ok=True)
        with open(os.path.join(v, "_system/entity-index.json"), "w") as fh:
            json.dump([{"canonical": "ivan-petrov"}], fh)
        return v

    def test_нет_канона_исключение(self):
        import call_project as cp
        with mock.patch.object(cp, "OWNER", "owner"):
            with self.assertRaises(RuntimeError) as ош:
                cp.застава_владельца(self.волт())
        self.assertIn("MARA_OWNER", str(ош.exception))
        with mock.patch.object(cp, "OWNER", "ivan-petrov"):
            cp.застава_владельца(self.волт())
        # индекса нет — судить не по чему
        cp.застава_владельца(os.path.join(self.dir, "пусто"))

    def test_шаг_проекции_стоит_до_записи(self):
        import call_project as cp
        with mock.patch.object(cp, "OWNER", "owner"), self.assertRaises(RuntimeError):
            cp.run("call_нет", self.волт(), root=os.path.join(self.dir, "блобы"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "блобы")),
                         "до заставы ничего не открыто и не создано")

    def test_правка_словами_тоже_стоит(self):
        import call_project as cp
        with mock.patch.object(cp, "OWNER", "owner"), self.assertRaises(RuntimeError):
            cp.apply_correction(self.волт(), {"id": "c1", "payload": {"item": "смета"}})


class Публичность(unittest.TestCase):
    """Г7 механикой: имени владельца нет ни в одном отслеживаемом файле —
    код, тесты, документы. Формы записаны кодами: сам тест не должен быть
    исключением из своего правила. Полное имя, уменьшительные (с «ё» и без)
    и латиница; короткий латинский префикс из `claude-usage.py` и
    GitHub-хэндл — не здесь: их владелец оставил решением 2026-09-03. История
    git старое имя хранит — её не переписываем, но новое не добавится."""

    def test_нет_имени_ни_в_одном_файле(self):
        корень = os.path.join(СКРИПТЫ, "..")
        r = subprocess.run(["git", "ls-files", "-z"], cwd=корень, capture_output=True)
        файлы = [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f]
        if r.returncode or not файлы:
            self.skipTest("не git-клон")

        def слово(*коды):
            return "".join(map(chr, коды))
        с, е, р, г, ё, ж = 0x421, 0x435, 0x440, 0x433, 0x451, 0x436
        формы = re.compile("|".join(map(re.escape, [
            слово(с, е, р, г), слово(с, е, р, ё, г), слово(с, е, р, е, г),
            слово(с, е, р, ё, ж), слово(с, е, р, е, ж),
            "s" + "erge", "s" + "eryo", "s" + "erezh", "s" + "erega", "s" + "eriozh"])), re.I)
        нашлось, не_прочитано = [], []
        for f in файлы:
            путь = os.path.join(корень, f)
            try:
                with open(путь, "rb") as fh:
                    сырьё = fh.read()
            except OSError:
                не_прочитано.append(f)
                continue
            if b"\0" in сырьё[:8192]:
                continue                       # двоичный: картинки, jar
            if формы.search(сырьё.decode("utf-8", "replace")):
                нашлось.append(f)
        self.assertEqual(нашлось, [])
        self.assertEqual(не_прочитано, [], "отслеживаемый файл не читается — проверка неполна")


if __name__ == "__main__":
    unittest.main()
