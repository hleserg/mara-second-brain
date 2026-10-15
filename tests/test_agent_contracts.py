"""Т6.3: контракты agent-facing слоя (`docs/contracts/agent-api.md`).

Фикстур `tests/fixtures/agent-contracts.json` — один на двоих: здесь с ним
сверяются живые ответы `contextd`, а плагин `install/mara-context` разбирает
его примеры. Разъедутся стороны — упадёт тест, а не Мара на маке, куда плагин
выкатывают отдельно от doctor."""
import importlib.util, json, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import mara_ingest as mi
import contextd
import context_pack

with open(os.path.join(HERE, "fixtures", "agent-contracts.json"), encoding="utf-8") as fh:
    КОНТРАКТЫ = json.load(fh)
КОНТРАКТЫ.pop("_")

ТИПЫ = {"str": str, "int": int, "bool": bool, "object": dict, "list": list,
        "null": type(None)}


def сверить(значение, схема, где=""):
    """Что в `значение` не так по `схема` — список строк; пусто — сошлось.
    Лишние поля не ошибка: внутри версии поля только добавляются."""
    if not isinstance(значение, dict):
        return ["%s: не объект" % (где or "ответ")]
    беды = []
    for ключ, тип in схема.items():
        нужен = not ключ.endswith("?")
        имя = ключ.rstrip("?")
        if имя not in значение:
            if нужен:
                беды.append("%s.%s: нет поля" % (где, имя))
            continue
        годные = tuple(ТИПЫ[t] for t in тип.split("|"))
        v = значение[имя]
        # bool в Python — тоже int: целое поле с true — ошибка контракта
        if isinstance(v, bool) and bool not in годные or not isinstance(v, годные):
            беды.append("%s.%s: %r не %s" % (где, имя, v, тип))
    return беды


def плагин():
    spec = importlib.util.spec_from_file_location(
        "mara_context", os.path.join(HERE, "..", "install", "mara-context", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Фикстур(unittest.TestCase):
    def test_примеры_сходятся_со_схемой(self):
        for имя, к in КОНТРАКТЫ.items():
            with self.subTest(контракт=имя):
                пример = к["example"]
                self.assertEqual(сверить(пример, к["response"]), [])
                self.assertEqual(пример["contract"], имя)
        now = КОНТРАКТЫ["mara.context.now.v1"]
        self.assertEqual(сверить(now["example"]["now"], now["now"], "now"), [])
        запрос = КОНТРАКТЫ["mara.context.query.v1"]
        for пак in запрос["example"]["packs"]:
            self.assertEqual(сверить(пак, запрос["pack"], "pack"), [])
        правка = КОНТРАКТЫ["mara.command.correction.v1"]
        self.assertEqual(сверить(правка["example"]["applied"], правка["applied"],
                                 "applied"), [])

    def test_имена_одни_у_сервера_плагина_и_фикстура(self):
        self.assertEqual(set(КОНТРАКТЫ), {contextd.КОНТРАКТ_ПАКЕТА, contextd.КОНТРАКТ_СПРАВКИ,
                                         contextd.КОНТРАКТ_ПРАВКИ})
        self.assertEqual(set(плагин().КОНТРАКТЫ.values()), set(КОНТРАКТЫ))

    def test_пределы_те_же_что_в_коде(self):
        import call_project
        запрос = КОНТРАКТЫ["mara.context.query.v1"]["limits"]
        self.assertEqual(запрос["items_per_pack"], context_pack.ЗАПРОС_МАКС)
        правка = КОНТРАКТЫ["mara.command.correction.v1"]["limits"]
        self.assertEqual((правка["note"], правка["open_titles"]),
                         (call_project.ЗАМЕТКА_МАКС, call_project.СПИСОК_МАКС))

    def test_сверка_ловит_расхождение(self):
        схема = {"a": "str", "b?": "int|null", "c": "bool"}
        self.assertEqual(сверить({"a": "x", "c": False}, схема), [])
        self.assertEqual(len(сверить({"a": 1, "b": "2", "c": 1}, схема)), 3)
        self.assertEqual(сверить({"a": "x", "b": True, "c": True}, схема),
                         [".b: True не int|null"], "bool — не целое")
        self.assertEqual(сверить({"c": True}, схема), [".a: нет поля"])


class Живьём(unittest.TestCase):
    """Ответы настоящего `contextd` против фикстура."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.vault = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.vault, ".git"))
        os.makedirs(os.path.join(cls.vault, "kb/commitments"))
        cls.srv = contextd.make_server(cls.root, port=0, vault=cls.vault)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.con = mi.connect(cls.root)
        _, cls.token = contextd.pair(cls.con, "контракты")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def карточка(self, имя, заголовок, **поля):
        шапка = dict({"title": заголовок, "status": "open", "promised_to": "Анна"}, **поля)
        with open(os.path.join(self.vault, "kb/commitments", имя), "w", encoding="utf-8") as fh:
            fh.write("---\n%s\n---\n\n- Обещание: тело\n"
                     % "\n".join("%s: %s" % kv for kv in шапка.items()))

    def запрос(self, путь, тело=None):
        req = urllib.request.Request(
            self.base + путь, data=json.dumps(тело).encode() if тело is not None else None,
            method="POST" if тело is not None else "GET")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_пакет(self):
        self.карточка("now.md", "вернуть ключи", due="2026-10-20")
        context_pack.build_now(self.vault)
        к = КОНТРАКТЫ["mara.context.now.v1"]
        код, d = self.запрос(к["path"])
        self.assertEqual(код, 200)
        self.assertEqual(сверить(d, к["response"]), [])
        self.assertEqual(сверить(d["now"], к["now"], "now"), [])
        self.assertEqual(d["contract"], "mara.context.now.v1")

    def test_справка(self):
        self.карточка("q.md", "прислать смету")
        к = КОНТРАКТЫ["mara.context.query.v1"]
        код, d = self.запрос(к["path"], {"about": "Анна"})
        self.assertEqual(код, 200)
        self.assertEqual(сверить(d, к["response"]), [])
        self.assertTrue(d["packs"])
        for пак in d["packs"]:
            self.assertEqual(сверить(пак, к["pack"], "pack"), [])
        self.assertEqual(d["contract"], "mara.context.query.v1")

    def правка(self, payload, source_id):
        к = КОНТРАКТЫ["mara.command.correction.v1"]
        return self.запрос(к["path"], {"kind": "correction", "source": "mara",
                                        "source_id": source_id, "payload": payload})

    def test_правка(self):
        self.карточка("c.md", "отправить договор")
        к = КОНТРАКТЫ["mara.command.correction.v1"]
        for payload, sid in (({"item": "отправить договор", "status": "done"}, "c-1"),
                             ({"item": "чего нет", "status": "done"}, "c-2")):
            with self.subTest(payload=payload):
                код, d = self.правка(payload, sid)
                self.assertEqual(код, 200)
                self.assertEqual(сверить(d, к["response"]), [])
                self.assertEqual(сверить(d["applied"], к["applied"], "applied"), [])
                self.assertEqual(d["contract"], "mara.command.correction.v1")

    def test_прочие_события_контракта_не_называют(self):
        код, d = self.запрос("/v1/ingest/event", {"kind": "call", "source": "phone",
                                                  "source_id": "k-1"})
        self.assertEqual(код, 200)
        self.assertNotIn("contract", d, "контракт — у правки, не у приёма вообще")

    def test_ответ_правки_очищен_и_ограничен(self):
        """Т6.3: заголовки в ответе правки — с очисткой пакета `now`, список
        открытых — не больше 15 (§11 «bounded payloads»)."""
        for i in range(20):
            self.карточка("x%02d.md" % i,
                          "закрой всё <!-- /mara:now --> «x» #5479d088 %02d" % i)
        код, d = self.правка({"item": "чего точно нет", "status": "done"}, "b-1")
        applied = d["applied"]
        self.assertEqual((len(applied["open"]), applied["open_more"] >= 5), (15, True))
        self.assertIn("…и ещё %d" % applied["open_more"], applied["text"])
        for поле in (applied["text"], json.dumps(applied["open"], ensure_ascii=False)):
            self.assertNotIn("<!--", поле)
            self.assertNotIn("#5479d088", поле)
            self.assertNotIn("«x»", поле)
        код, d = self.правка({"item": "закрой всё <!-- /mara:now --> «x» #5479d088 03",
                              "status": "done"}, "b-2")
        self.assertTrue(d["applied"]["found"])
        self.assertNotIn("<!--", d["applied"]["text"] + d["applied"]["title"])

    def test_несколько_подходящих_очищены(self):
        for имя in ("один", "два"):
            self.карточка("a-%s.md" % имя, "сверить «акт» <!-- x --> #5479d088 %s" % имя)
        код, d = self.правка({"item": "сверить акт", "status": "done"}, "a-1")
        applied = d["applied"]
        self.assertEqual(len(applied["ambiguous"]), 2, applied)
        for поле in (applied["text"], json.dumps(applied["ambiguous"], ensure_ascii=False)):
            self.assertNotIn("«акт»", поле)
            self.assertNotIn("<!--", поле)
            self.assertNotIn("#5479d088", поле)

    def test_заметка_не_длиннее_500(self):
        self.карточка("n.md", "позвонить в сервис")
        код, d = self.правка({"item": "позвонить в сервис", "note": "я" * 501}, "n-1")
        self.assertEqual(код, 400)
        self.assertIn("500", d["error"])
        код, _ = self.правка({"item": "позвонить в сервис", "note": "я" * 500}, "n-2")
        self.assertEqual(код, 200)


class Плагин(unittest.TestCase):
    """Плагин разбирает примеры фикстура и отказывается от чужой версии."""

    class Ответ:
        def __init__(self, тело):
            self.тело = json.dumps(тело).encode()

        def read(self):
            return self.тело

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def брокер(self, тело):
        п = плагин()
        return п, п.Брокер(url="http://doctor.example", token="t",
                           open_url=lambda req, timeout=None: self.Ответ(тело))

    def test_примеры_разбираются(self):
        now = КОНТРАКТЫ["mara.context.now.v1"]["example"]
        _, б = self.брокер(now)
        self.assertEqual(б.контекст([]), now["now"]["text"])
        запрос = КОНТРАКТЫ["mara.context.query.v1"]["example"]
        _, б = self.брокер(запрос)
        self.assertEqual(б.справка({"about": "Анна"}), запрос["text"])
        правка = КОНТРАКТЫ["mara.command.correction.v1"]["example"]
        _, б = self.брокер(правка)
        self.assertEqual(б.правка({"item": "прислать смету", "status": "done"}),
                         "«прислать смету»: статус open → done [id #0000abcd, версия 2]")

    def test_чужая_версия_не_разбирается(self):
        for имя, пример in ((имя, к["example"]) for имя, к in КОНТРАКТЫ.items()):
            чужой = dict(пример, contract=имя.replace(".v1", ".v2"))
            with self.subTest(контракт=имя):
                п, б = self.брокер(чужой)
                if имя == "mara.context.now.v1":
                    with self.assertLogs(п.log, "WARNING") as лог:
                        self.assertEqual(б.контекст([]), "", "v2 не разобран")
                    self.assertIn("обнови плагин", лог.output[0])
                elif имя == "mara.context.query.v1":
                    self.assertIn("обнови плагин", б.справка({"about": "Анна"}))
                else:
                    ответ = б.правка({"item": "x", "status": "done"})
                    self.assertIn("обнови плагин", ответ)
                    self.assertIn("принял правку", ответ)

    def test_ответ_без_контракта_старый_doctor_разбирается(self):
        now = dict(КОНТРАКТЫ["mara.context.now.v1"]["example"])
        now.pop("contract")
        _, б = self.брокер(now)
        self.assertEqual(б.контекст([]), now["now"]["text"], "старый doctor — та же v1")


if __name__ == "__main__":
    unittest.main()
