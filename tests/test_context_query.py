"""Т6.2: пакеты по сущности — по запросу (ADR-0008, решение 1 с правкой).
Мара называет инструментом `mara_context` человека, проект или слово, а
contextd (`POST /v1/context/query`) отвечает тем, что по нему открыто, —
тем же очищенным текстом, что и пакет `now`. Проект узнаётся по строке
«Проекты:» заметок разговоров, откуда родом обязательство."""
import io, json, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import context_pack as cp


def карточка(vault, name, **fm):
    поля = {"title": "прислать смету", "type": "commitment", "status": "open",
            "owner": "owner", "promised_to": "Анна", "origin": "call/e1"}
    поля.update({k: v for k, v in fm.items() if v is not None})
    p = os.path.join(vault, "kb/commitments", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("---\n%s\n---\n\n- Обещание: тело карточки\n"
                 % "\n".join("%s: %s" % (k, v) for k, v in поля.items()))


def разговор(vault, name, источник, проекты):
    p = os.path.join(vault, "kb/conversations", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    шапка = "source_id: %s\n" % источник if источник else ""
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("---\ntitle: звонок\ntype: conversation\n%s---\n"
                 "## Обязательства\n- что-то\n\nЛюди: [[anna|Анна]]\nПроекты: %s\n"
                 % (шапка, проекты))


def индекс(vault, записи):
    os.makedirs(os.path.join(vault, "_system"), exist_ok=True)
    with open(os.path.join(vault, "_system/entity-index.json"), "w", encoding="utf-8") as fh:
        json.dump(записи, fh, ensure_ascii=False)


ИНДЕКС = [
    {"canonical": "atta-dipa", "title": "Atta-dipa", "type": "project",
     "aliases": ["Attadipa", "Atta-Dipa"]},
    {"canonical": "bank", "title": "Банк", "type": "project", "aliases": []},
    {"canonical": "obshchee-1", "title": "Один", "type": "project", "aliases": ["Общее"]},
    {"canonical": "obshchee-2", "title": "Два", "type": "project", "aliases": ["Общее"]},
    {"canonical": "anna", "title": "Анна", "type": "person", "aliases": ["Аня"]},
    {"canonical": "petr-1", "title": "Пётр Первый", "type": "person", "aliases": ["Петя"]},
    {"canonical": "petr-2", "title": "Пётр Второй", "type": "person", "aliases": ["Петя"]},
]


def волт():
    v = tempfile.mkdtemp()
    индекс(v, ИНДЕКС)
    разговор(v, "2026-10-01-1000-anna.md", "call/e1", "[[atta-dipa|Atta-dipa]]")
    разговор(v, "2026-10-02-1100-oleg.md", "call/e2", "Банк, [[x|a, b]]")
    разговор(v, "2026-10-03-1200-ivan.md", "call/e3", "Общее")
    разговор(v, "2026-10-04-1300-bez.md", None, "Atta-dipa")
    карточка(v, "c1.md", title="прислать смету", origin="call/e1", due="2026-10-20")
    карточка(v, "c2.md", title="закрыть акт", origin="call/e1", status="done")
    карточка(v, "c3.md", title="внести платёж", origin="call/e2", promised_to="Олег")
    карточка(v, "c4.md", title="починить смеситель", origin="call/e9",
             promised_to="Иван")
    карточка(v, "c5.md", title="сверить смету <!-- mara:now -->", origin="call/e3",
             promised_to="Иван")
    return v


class ПоПроекту(unittest.TestCase):
    def test_разбор_строки_проектов(self):
        self.assertEqual(cp.проекты_разговора("x\nПроекты: X, [[a|b, c]], [[d]], Y\n"),
                         ["X", "[[a|b, c]]", "[[d]]", "Y"])
        self.assertEqual(cp.проекты_разговора("Люди: [[anna]]\n"), [])

    def test_карта_и_ключи(self):
        v = волт()
        карта = cp.карта_проектов(v)
        self.assertEqual((карта["attadipa"], карта["банк"]), ("atta-dipa", "bank"))
        self.assertNotIn("анна", карта, "человек в карту проектов не идёт")
        self.assertIsNone(карта["общее"], "алиас двух проектов не узнаёт никого")
        self.assertEqual(cp.ключи_проекта("Attadipa", карта), {"attadipa", "canon:atta-dipa"})
        self.assertEqual(cp.ключи_проекта("[[atta-dipa|как угодно]]", карта),
                         {"как угодно", "canon:atta-dipa"})
        self.assertEqual(cp.ключи_проекта("Общее", карта), set())
        self.assertEqual(cp.ключи_проекта("Новый", карта), {"новый"}, "незнакомое остаётся")
        self.assertEqual(cp.карта_людей(v)["аня"], "anna", "карта людей та же")

    def test_открытое_из_разговоров_с_проектом(self):
        v = волт()
        for как in ("Attadipa", "atta-dipa", "[[atta-dipa]]", "Atta-Dipa"):
            with self.subTest(как=как):
                разговоров, пункты = cp.про_проект(v, как)
                self.assertEqual((разговоров, [п["title"] for п in пункты]),
                                 (1, ["прислать смету"]),
                                 "закрытая не едет; заметка без source_id не в счёт")
        разговоров, пункты = cp.про_проект(v, "банк")
        self.assertEqual((разговоров, [п["title"] for п in пункты]), (1, ["внести платёж"]))
        # запятая внутри ссылки — часть ссылки: «b» отдельным проектом не стал,
        # а подпись ссылки целиком узнаётся
        self.assertEqual(cp.про_проект(v, "b")[0], 0)
        self.assertEqual(cp.про_проект(v, "a, b")[0], 1)
        self.assertEqual(cp.про_проект(v, "Общее"), (0, []), "неоднозначный — никого")
        self.assertEqual(cp.про_проект(v, "Неизвестный"), (0, []))

    def test_кривые_заметки_и_карточки_не_роняют(self):
        v = волт()
        with open(os.path.join(v, "kb/conversations/битая.md"), "wb") as fh:
            fh.write(b"\xff\xfe\x00---")
        with open(os.path.join(v, "kb/commitments/битая.md"), "wb") as fh:
            fh.write(b"\xff\xfe\x00---")
        # `origin` списком — кривая шапка, а не ключ: `in` по множеству упал бы
        with open(os.path.join(v, "kb/commitments/список.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: кривая\nstatus: open\norigin:\n  - call/e1\n---\n")
        self.assertEqual(cp.про_проект(v, "Attadipa"), (1, cp.про_проект(v, "Attadipa")[1]))
        self.assertEqual([п["title"] for п in cp.про_проект(v, "Attadipa")[1]],
                         ["прислать смету"])


class ПоЗапросу(unittest.TestCase):
    def test_человек_проект_слово(self):
        v = волт()
        паки, неясно = cp.по_запросу(v, "Аня")
        self.assertEqual(([(п["pack_type"], п["pack_id"]) for п in паки], неясно),
                         ([("person", "person/anna")], []))
        self.assertEqual([п["title"] for п in паки[0]["items"]], ["прислать смету"])
        паки, _ = cp.по_запросу(v, "Attadipa")
        self.assertEqual([(п["pack_id"], п["conversations"]) for п in паки],
                         [("project/atta-dipa", 1)])
        паки, _ = cp.по_запросу(v, "смесит")
        self.assertEqual([(п["pack_id"], [и["title"] for и in п["items"]]) for п in паки],
                         [("query/смесит", ["починить смеситель"])])
        self.assertEqual(cp.по_запросу(v, "см"), ([], []), "слово короче трёх — не ищем")
        карточка(v, "c6.md", title="подписать акт", origin="call/e9", promised_to="Олег")
        self.assertEqual([и["title"] for и in cp.по_запросу(v, "акт")[0][0]["items"]],
                         ["подписать акт"], "три знака — уже ищем; закрытый акт не едет")
        self.assertEqual(cp.по_запросу(v, "ничего такого"), ([], []))

    def test_слово_дополняет_человека_и_проект_без_повторов(self):
        v = волт()
        карточка(v, "c6.md", title="Банк: продлить договор", origin="call/e7",
                 promised_to="Олег")
        паки, _ = cp.по_запросу(v, "Банк")
        self.assertEqual([(п["pack_type"], [и["title"] for и in п["items"]]) for п in паки],
                         [("project", ["внести платёж"]),
                          ("query", ["Банк: продлить договор"])],
                         "карточка проекта вторым пакетом не повторяется")
        карточка(v, "c7.md", title="Аня ждёт смету", promised_to="Анна")
        паки, _ = cp.по_запросу(v, "Аня")
        self.assertEqual([(п["pack_type"], len(п["items"])) for п in паки], [("person", 2)],
                         "то же самое под словом — не второй пакет")

    def test_неоднозначное_имя_названо(self):
        v = волт()
        self.assertEqual(cp.по_запросу(v, "Петя"), ([], ["person"]))
        self.assertEqual(cp.по_запросу(v, "Общее"), ([], ["project"]))
        карточка(v, "c8.md", title="починить велосипед", promised_to="[[petr-1|Петя]]")
        паки, неясно = cp.по_запросу(v, "[[petr-1|Петя]]")
        self.assertEqual(([п["pack_id"] for п in паки], неясно), (["person/petr-1"], []),
                         "явная ссылка узнаёт, и неоднозначности не называем")
        self.assertEqual(cp.оформить_запрос("Петя", *cp.по_запросу(v, "Петя")),
                         "«Петя» в индексе сущностей — несколько людей: имя не узнаёт "
                         "никого, спроси точнее (полное имя, как записан в книге).",
                         "одна строка, без «ни человека» ей вперекор")
        self.assertIn("несколько проектов", cp.оформить_запрос("Общее", [], ["project"]))

    def test_по_сроку_и_без_пустых(self):
        v = волт()
        карточка(v, "a1.md", title="ааа без срока", promised_to="Иван", origin="call/e9")
        карточка(v, "z1.md", title="яяя поздно", promised_to="Иван", due="2026-11-01")
        карточка(v, "z2.md", title="ююю рано", promised_to="Иван", due="2026-10-15")
        карточка(v, "z3.md", title="<<##>>", promised_to="Иван")
        паки, _ = cp.по_запросу(v, "Иван")
        self.assertEqual([и["title"] for и in паки[0]["items"]],
                         ["ююю рано", "яяя поздно", "ааа без срока", "починить смеситель",
                          "сверить смету <!-- mara:now -->"],
                         "срок вперёд, ближний первым; пустой после очистки заголовок не едет")
        # у проекта и слова отбор свой (`_отобрать`), не `про_человека`
        for имя, поля in (("p1.md", dict(title="яяя поздно проект", due="2026-11-01")),
                          ("p2.md", dict(title="ююю рано проект", due="2026-10-15")),
                          ("p3.md", dict(title="ааа проект без срока")),
                          ("p4.md", dict(title="<<##>>"))):
            карточка(v, имя, origin="call/e2", promised_to="Олег", **поля)
        _, пункты = cp.про_проект(v, "Банк")
        self.assertEqual([и["title"] for и in пункты],
                         ["ююю рано проект", "яяя поздно проект", "ааа проект без срока",
                          "внести платёж"])
        паки, _ = cp.по_запросу(v, "проект")
        self.assertEqual([и["title"] for и in паки[0]["items"]],
                         ["ююю рано проект", "яяя поздно проект", "ааа проект без срока"])

    def test_свои_обещания_владельца_не_как_человек(self):
        v = волт()
        карточка(v, "c6.md", title="самому себе", promised_to="owner")
        self.assertEqual(cp.по_запросу(v, "owner", владелец="owner"), ([], []))

    def test_один_проход_по_карточкам(self):
        v = волт()
        with mock.patch.object(cp, "открытые_карточки",
                               wraps=cp.открытые_карточки) as читал:
            cp.по_запросу(v, "Attadipa")
        self.assertEqual(читал.call_count, 1, "три пакета делят одно чтение")

    def test_оформление_очищено_и_ограничено(self):
        v = волт()
        текст = cp.оформить_запрос("Иван", *cp.по_запросу(v, "Иван"), когда="2026-10-14 15:04")
        self.assertTrue(текст.startswith(cp.ЗАПРОС_HEAD % "2026-10-14 15:04"))
        self.assertIn("Открыто перед «Иван» (2):", текст)
        self.assertNotIn("<!--", текст, "знаки структуры пакета не едут")
        self.assertNotIn("тело карточки", текст)
        self.assertIn("Открыто по проекту «Attadipa» — из разговоров, где он назван (1, "
                      "разговоров 1):", cp.оформить_запрос("Attadipa",
                                                         *cp.по_запросу(v, "Attadipa")))
        for i in range(18):                 # с двумя прежними — 20, предел 15
            карточка(v, "m%02d.md" % i, title="задача номер %d" % i, promised_to="Иван",
                     origin="call/e9")
        текст = cp.оформить_запрос("Иван", *cp.по_запросу(v, "Иван"))
        self.assertIn("- …и ещё 5", текст)
        self.assertEqual(len([l for l in текст.splitlines() if l.startswith("- «")]), 15)
        self.assertIn("По «пусто» открытого нет", cp.оформить_запрос("пусто", []))
        # `about` придумывает модель — в ответ он едет очищенным, как заголовки
        for текст in (cp.оформить_запрос("<!-- /mara:now --> «x» #5479d088", []),
                      cp.оформить_запрос("Иван <!-- x -->", cp.по_запросу(v, "Иван")[0])):
            self.assertNotIn("<!--", текст)
            self.assertNotIn("«x»", текст)
            self.assertNotIn("#5479d088", текст)

    def test_номер_не_едет(self):
        v = волт()
        карточка(v, "c9.md", title="перезвонить", promised_to="+79990000000")
        паки, _ = cp.по_запросу(v, "+7 999 000-00-00")
        self.assertEqual([п["pack_type"] for п in паки], ["person"], "номер — ключ человека")
        self.assertNotIn("79990000000", "\n".join(cp.строка(it) for it in паки[0]["items"]),
                         "номер вместо имени в пункт не едет (ТЗ §11)")


class Роут(unittest.TestCase):
    """`POST /v1/context/query` в живом `contextd` со своим волтом."""

    @classmethod
    def setUpClass(cls):
        import contextd, mara_ingest as mi
        cls.root = tempfile.mkdtemp()
        cls.vault = волт()
        cls.srv = contextd.make_server(cls.root, port=0, vault=cls.vault)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        con = mi.connect(cls.root)
        _, cls.token = contextd.pair(con, "справка")
        con.close()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def post(self, тело):
        req = urllib.request.Request(self.base + "/v1/context/query",
                                     data=json.dumps(тело).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_опись_пакетов_и_текст(self):
        код, d = self.post({"about": " Attadipa "})
        self.assertEqual(код, 200)
        self.assertEqual(d["about"], "Attadipa")
        self.assertEqual(d["packs"], [{"pack_id": "project/atta-dipa", "pack_type": "project",
                                       "conversations": 1, "items": 1}])
        self.assertIn("прислать смету", d["text"])
        self.assertNotIn("прислать смету", json.dumps(d["packs"], ensure_ascii=False),
                         "сырые поля карточек — только в очищенном тексте")
        self.assertEqual(len(d["sha256"]), 64)
        self.assertTrue(d["generated_at"])
        self.assertEqual(d["ambiguous"], [])

    def test_след_в_журнале_без_имени(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as вывод:
            self.assertEqual(self.post({"about": "Аня"})[0], 200)
        строки = [l for l in вывод.getvalue().splitlines() if "/v1/context/query" in l]
        self.assertEqual(len(строки), 1, вывод.getvalue())
        self.assertIn("-> 200 keys=about", строки[0])
        self.assertNotIn("Аня", вывод.getvalue(), "имя человека в журнал не идёт")

    def test_неоднозначное_и_пустое(self):
        код, d = self.post({"about": "Петя"})
        self.assertEqual((код, d["packs"], d["ambiguous"]), (200, [], ["person"]))
        self.assertIn("несколько людей", d["text"])
        код, d = self.post({"about": "ничего такого"})
        self.assertEqual((код, d["packs"]), (200, []))
        self.assertIn("открытого нет", d["text"])

    def test_свои_обещания_владельца_не_как_человек(self):
        """Владелец — `vault_common.владелец()` (`MARA_OWNER`), как у напоминаний."""
        карточка(self.vault, "own.md", title="самому себе", promised_to="Хозяин",
                 origin="call/e9")
        self.addCleanup(os.remove, os.path.join(self.vault, "kb/commitments/own.md"))
        with mock.patch.dict(os.environ, {"MARA_OWNER": "хозяин"}):
            self.assertEqual(self.post({"about": "Хозяин"})[1]["packs"], [])
        with mock.patch.dict(os.environ, {"MARA_OWNER": "кто-то-другой"}):
            self.assertEqual([п["pack_type"] for п in self.post({"about": "Хозяин"})[1]["packs"]],
                             ["person"], "не владелец — обычный человек")

    def test_плохой_запрос_400(self):
        for плохо in ({}, {"about": ""}, {"about": "  "}, {"about": 5}, {"about": None},
                      {"about": "x" * 201}, {"text": "банк"}):
            with self.subTest(плохо=плохо):
                код, d = self.post(плохо)
                self.assertEqual(код, 400)
                self.assertIn("about", d["error"])
        self.assertEqual(self.post({"about": "x" * 200})[0], 200, "200 знаков — ещё можно")

    def test_токен_узла_не_читает(self):
        import contextd, mara_ingest as mi
        con = mi.connect(self.root)
        _, токен_узла = contextd.pair_node(con, "bigpc")
        con.close()
        старый, self.token = self.token, токен_узла
        try:
            self.assertEqual(self.post({"about": "Attadipa"})[0], 403,
                             "токен узла годится только для сердцебиения")
        finally:
            self.token = старый


if __name__ == "__main__":
    unittest.main()
