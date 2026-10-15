"""Т6.3: контракты agent-facing слоя (`docs/contracts/agent-api.md`).

Фикстур `tests/fixtures/agent-contracts.json` — один на двоих: здесь с ним
сверяются живые ответы `contextd`, а плагин `install/mara-context` разбирает
его примеры. Разъедутся стороны — упадёт тест, а не Мара на маке, куда плагин
выкатывают отдельно от doctor."""
import importlib.util, json, os, sys, tempfile, threading, unittest
import urllib.error, urllib.parse, urllib.request

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
        self.assertEqual((запрос["items_per_pack"], запрос["about"]),
                         (context_pack.ЗАПРОС_МАКС, contextd.ЗАПРОС_О_МАКС))
        правка = КОНТРАКТЫ["mara.command.correction.v1"]["limits"]
        self.assertEqual(правка, {"item": call_project.ПУНКТ_МАКС,
                                  "note": call_project.ЗАМЕТКА_МАКС,
                                  "open_titles": call_project.СПИСОК_МАКС,
                                  "ambiguous_titles": call_project.СПИСОК_МАКС,
                                  "idempotency_key": contextd.КЛЮЧ_МАКС})

    def test_пример_пакета_как_настоящий(self):
        """Текст пакета едет в ход с маркерами — пример тоже с ними."""
        текст = КОНТРАКТЫ["mara.context.now.v1"]["example"]["now"]["text"]
        self.assertTrue(текст.startswith(context_pack.MARK_OPEN + "\n"), текст)
        self.assertTrue(текст.endswith(context_pack.MARK_CLOSE + "\n"), текст)

    def test_сверка_ловит_расхождение(self):
        схема = {"a": "str", "b?": "int|null", "c": "bool"}
        self.assertEqual(сверить({"a": "x", "c": False}, схема), [])
        self.assertEqual(len(сверить({"a": 1, "b": "2", "c": 1}, схема)), 3)
        self.assertEqual(сверить({"a": "x", "b": True, "c": True}, схема),
                         [".b: True не int|null"], "bool — не целое")
        self.assertEqual(сверить({"c": True}, схема), [".a: нет поля"])


class Живьём(unittest.TestCase):
    """Ответы настоящего `contextd` против фикстура. Волт и реестр — свои у
    каждого теста: счёт открытых и подходящих не зависит от порядка."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.vault = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.vault, ".git"))
        os.makedirs(os.path.join(self.vault, "kb/commitments"))
        self.srv = contextd.make_server(self.root, port=0, vault=self.vault)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.con = mi.connect(self.root)
        _, self.token = contextd.pair(self.con, "контракты")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.con.close()

    def карточка(self, имя, заголовок, **поля):
        шапка = dict({"title": заголовок, "status": "open", "promised_to": "Анна"}, **поля)
        with open(os.path.join(self.vault, "kb/commitments", имя), "w", encoding="utf-8") as fh:
            fh.write("---\n%s\n---\n\n- Обещание: тело\n"
                     % "\n".join("%s: %s" % kv for kv in шапка.items()))

    def запрос(self, путь, тело=None, токен=None, сырое=None):
        данные = сырое if сырое is not None else (
            json.dumps(тело).encode() if тело is not None else None)
        req = urllib.request.Request(self.base + путь, data=данные,
                                     method="POST" if данные is not None else "GET")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + (токен or self.token))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def правка(self, payload, source_id, **ещё):
        к = КОНТРАКТЫ["mara.command.correction.v1"]
        return self.запрос(к["path"], dict({"kind": "correction", "source": "mara",
                                             "source_id": source_id, "payload": payload},
                                            **ещё))

    def сверить_правку(self, d):
        к = КОНТРАКТЫ["mara.command.correction.v1"]
        self.assertEqual(сверить(d, к["response"]), [])
        self.assertEqual(d["contract"], "mara.command.correction.v1")
        if d["applied"] is not None:
            self.assertEqual(сверить(d["applied"], к["applied"], "applied"), [])
            if "current" in d["applied"]:
                self.assertEqual(сверить(d["applied"]["current"], к["current"], "current"), [])

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

    def test_метод_путь_и_право(self):
        """Устройство с ровно тем правом, что назвал фикстур, проходит;
        со списком без него — 403."""
        self.карточка("p.md", "отнести пальто")
        context_pack.build_now(self.vault)
        тела = {"mara.context.now.v1": None,
                "mara.context.query.v1": {"about": "Анна"},
                "mara.command.correction.v1": {"kind": "correction", "source": "mara",
                                               "source_id": "s-1", "payload": {
                                                   "item": "отнести пальто", "note": "x"}}}
        for имя, к in КОНТРАКТЫ.items():
            with self.subTest(контракт=имя):
                self.assertEqual(к["method"], "GET" if тела[имя] is None else "POST")
                dev, токен = contextd.pair(self.con, "право " + имя)
                contextd.set_scopes(self.con, dev, [к["scope"]])
                код, d = self.запрос(к["path"], тела[имя], токен=токен)
                self.assertEqual(код, 200, d)
                self.assertEqual(d["contract"], имя)
                чужой, токен = contextd.pair(self.con, "без права " + имя)
                contextd.set_scopes(self.con, чужой, ["call"])
                self.assertEqual(self.запрос(к["path"], тела[имя], токен=токен)[0], 403)

    def test_ветки_правки_по_схеме(self):
        """Каждая ветка ответа правки — по схеме фикстура: применено, «уже
        так», не нашёл, заведено, дубль, конфликт версий."""
        self.карточка("c.md", "отправить договор")
        for payload, sid in (({"item": "отправить договор", "status": "done"}, "c-1"),
                             ({"item": "отправить договор", "status": "done"}, "c-2"),
                             ({"item": "чего нет", "status": "done"}, "c-3"),
                             ({"item": "новое дело", "status": "open"}, "c-4"),
                             ({"item": "новое дело", "status": "open"}, "c-4")):
            with self.subTest(payload=payload, sid=sid):
                код, d = self.правка(payload, sid)
                self.assertEqual(код, 200, d)
                self.сверить_правку(d)
        self.assertTrue(d["duplicate"])
        self.assertIsNone(d["applied"], "у дубля итога нет")

    def test_конфликт_по_схеме_и_без_лишнего(self):
        код, d = self.правка({"item": "забрать <!-- x --> «посылку»", "status": "open"}, "k-1")
        self.assertTrue(d["applied"]["created"], d)
        код, d = self.правка({"item": "забрать посылку", "status": "done"}, "k-2")
        ид, версия = d["applied"]["id"], d["applied"]["version"]
        код, d = self.правка({"item": "забрать посылку", "status": "cancelled",
                              "id": ид, "expected_version": версия + 5,
                              "note": "сказал <!-- /mara:now --> «посылку» не брать",
                              "лишнее": "я" * 5000}, "k-3")
        self.assertEqual(код, 200, d)
        applied = d["applied"]
        self.assertEqual(applied["error"], "version_conflict")
        self.сверить_правку(d)
        # то, на что опирается плагин: версии и статус
        self.assertEqual((applied["current_version"], applied["expected_version"]),
                         (версия, версия + 5))
        self.assertEqual(applied["current"]["status"], "done")
        self.assertNotIn("лишнее", applied["attempted_patch"])
        self.assertLess(len(json.dumps(d, ensure_ascii=False)), 4000)
        for поле in (applied["text"], applied["title"], applied["current"]["title"],
                     json.dumps(applied["attempted_patch"], ensure_ascii=False)):
            self.assertNotIn("<!--", поле)
            self.assertNotIn("«посылку»", поле)

    def test_статус_из_шапки_в_конфликте_очищен(self):
        """Статус в реестре — из шапки карточки, а её правят рукой: в тексте
        конфликта он идёт с той же очисткой."""
        код, d = self.правка({"item": "вернуть дрель", "status": "open"}, "s-1")
        путь = os.path.join(self.vault, d["applied"]["created"])
        with open(путь, encoding="utf-8") as fh:
            текст = fh.read()
        with open(путь, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("status: open", "status: done <!-- x --> «да»", 1))
        код, d = self.правка({"item": "вернуть дрель", "note": "перенести"}, "s-2")
        ид, версия = d["applied"]["id"], d["applied"]["version"]
        код, d = self.правка({"item": "вернуть дрель", "status": "cancelled", "id": ид,
                              "expected_version": версия + 1}, "s-3")
        applied = d["applied"]
        self.assertEqual(applied.get("error"), "version_conflict", applied)
        for поле in (applied["text"], applied["current"]["status"]):
            self.assertNotIn("<!--", поле)
            self.assertNotIn("«да»", поле)

    def test_пределы_запроса(self):
        self.карточка("l.md", "я" * 200)
        код, d = self.правка({"item": "я" * 201, "status": "done"}, "l-1")
        self.assertEqual(код, 400)
        self.assertIn("200", d["error"])
        self.assertEqual(self.правка({"item": "я" * 200, "status": "done"}, "l-2")[0], 200)
        к = КОНТРАКТЫ["mara.context.query.v1"]
        self.assertEqual(self.запрос(к["path"], {"about": "я" * 201})[0], 400)
        self.assertEqual(self.запрос(к["path"], {"about": "я" * 200})[0], 200)
        код, d = self.правка({"item": "я" * 200, "note": "x"}, "l-3",
                             idempotency_key="k" * 129)
        self.assertEqual(код, 400)
        self.assertIn("128", d["error"])

    def test_кривая_правка_получает_400(self):
        """Контракт обещает 400 с текстом, а не обрыв соединения."""
        к = КОНТРАКТЫ["mara.command.correction.v1"]
        for payload in ("строка", 5, [1, 2], True):
            with self.subTest(payload=payload):
                код, d = self.правка(payload, "p-%r" % (payload,))
                self.assertEqual((код, d["error"]), (400, "payload — объект"))
        for sid in (None, "", "   ", 5):
            with self.subTest(source_id=sid):
                тело = {"kind": "correction", "source": "mara",
                        "payload": {"item": "x", "status": "done"}}
                if sid is not None:
                    тело["source_id"] = sid
                код, d = self.запрос(к["path"], тело)
                self.assertEqual(код, 400)
                self.assertIn("source_id", d["error"])
        код, d = self.запрос(к["path"], сырое=b'{"kind": "correction", "source": "mara", '
                                             b'"source_id": "u-1", "payload": '
                                             b'{"item": "\\udcff", "status": "done"}}')
        self.assertEqual(код, 400)
        self.assertIn("суррогат", d["error"])

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
        self.assertEqual((len(applied["open"]), applied["open_more"]), (15, 5))
        self.assertIn("…и ещё 5", applied["text"])
        for поле in (applied["text"], json.dumps(applied["open"], ensure_ascii=False)):
            self.assertNotIn("<!--", поле)
            self.assertNotIn("#5479d088", поле)
            self.assertNotIn("«x»", поле)
        код, d = self.правка({"item": "закрой всё <!-- /mara:now --> «x» #5479d088 03",
                              "status": "done"}, "b-2")
        self.assertTrue(d["applied"]["found"])
        self.assertNotIn("<!--", d["applied"]["text"] + d["applied"]["title"])

    def test_открытые_по_сроку(self):
        """Порядок «не нашёл» — как в пакете: со сроком раньше, без срока в
        конце; статус без регистра; пустой заголовок не едет."""
        self.карточка("a.md", "без срока")
        self.карточка("b.md", "позже", due="2026-11-20")
        self.карточка("c.md", "раньше", due="2026-10-20")
        self.карточка("d.md", "заглавный статус", status="OPEN", due="2026-10-25")
        self.карточка("e.md", "<<##>>")
        код, d = self.правка({"item": "чего нет", "status": "done"}, "o-1")
        self.assertEqual(d["applied"]["open"],
                         ["раньше", "заглавный статус", "позже", "без срока"])

    def test_несколько_подходящих_очищены(self):
        for имя in ("один", "два"):
            self.карточка("a-%s.md" % имя, "сверить «акт» <!-- x --> #5479d088 %s" % имя)
        код, d = self.правка({"item": "сверить акт", "status": "done"}, "a-1")
        applied = d["applied"]
        self.assertEqual(len(applied["ambiguous"]), 2, applied)
        self.сверить_правку(d)
        for поле in (applied["text"], json.dumps(applied["ambiguous"], ensure_ascii=False)):
            self.assertNotIn("«акт»", поле)
            self.assertNotIn("<!--", поле)
            self.assertNotIn("#5479d088", поле)

    def test_несколько_подходящих_ограничены_и_с_кодом(self):
        """Неоднозначность — не больше 15 имён и число остальных; у имени —
        код карточки: обрезка делает длинные заголовки одинаковыми, и
        различить их можно только по коду (ревью #176)."""
        общее = "согласовать с подрядчиком график работ " * 3
        for i in range(20):
            self.карточка("g%02d.md" % i, общее + "этап %02d" % i,
                          id="01999999-0000-7000-8000-0000000000%02d" % i)
        # первой по имени файла — чтобы попасть в показ
        self.карточка("a-bad.md", общее + "кривой id", id="<!-- x --> #bad")
        код, d = self.правка({"item": общее.strip(), "status": "done"}, "g-1")
        applied = d["applied"]
        self.сверить_правку(d)
        self.assertEqual((len(applied["ambiguous"]), applied["ambiguous_more"]), (15, 6))
        self.assertEqual(len(applied["ambiguous_ids"]), 15)
        self.assertIn(None, applied["ambiguous_ids"], "не id в шапке — null, а не как есть")
        # в аудит — все 21 и как есть: реестру нужен полный след
        след = [json.loads(r[0]) for r in self.con.execute(
            "select detail_json from audit_events where action='correction'")]
        неясно, = [д for д in след if д["outcome"] == "ambiguous"]
        self.assertEqual(len(неясно["ambiguous_ids"]), 21)
        self.assertIn("<!-- x --> #bad", неясно["ambiguous_ids"])
        self.assertIn("…и ещё 6", applied["text"])
        self.assertIn("#00000000", applied["text"])
        self.assertEqual(len(set(applied["ambiguous"])), 1, "обрезка сделала имена одинаковыми")
        # код из ответа ведёт ровно в одну карточку
        код, d = self.правка({"item": общее.strip(), "status": "done", "id": "#00000003"}, "g-2")
        self.assertTrue(d["applied"]["applied"], d["applied"])
        self.assertTrue(d["applied"]["card"].endswith("g03.md"))
        self.assertNotIn("<!--", json.dumps(applied, ensure_ascii=False))

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
            for версия in (имя.replace(".v1", ".v2"), "", 0, False):
                чужой = dict(пример, contract=версия)
                with self.subTest(контракт=имя, версия=версия):
                    п, б = self.брокер(чужой)
                    if имя == "mara.context.now.v1":
                        with self.assertLogs(п.log, "WARNING") as лог:
                            self.assertEqual(б.контекст([]), "", "чужая не разобрана")
                        self.assertIn("обнови плагин", лог.output[0])
                    elif имя == "mara.context.query.v1":
                        ответ = б.справка({"about": "Анна"})
                        self.assertIn("обнови плагин", ответ)
                        self.assertIn(repr(версия), ответ, "пустое имя видно в ответе")
                    else:
                        ответ = б.правка({"item": "x", "status": "done"})
                        self.assertIn("обнови плагин", ответ)
                        self.assertIn("итог не разобрал", ответ)

    def test_кэш_переживает_чужое(self):
        """Чужая версия и ответ не объектом старый пакет не стирают: пока
        doctor говорит непонятное, прежний список полезнее пустоты."""
        хороший = КОНТРАКТЫ["mara.context.now.v1"]["example"]
        п = плагин()
        ответы = [хороший, dict(хороший, contract="mara.context.now.v2"), [1],
                  dict(хороший, now="строка"), хороший]
        б = п.Брокер(url="http://doctor.example", token="t", ttl=0,
                     open_url=lambda req, timeout=None: self.Ответ(ответы.pop(0)))
        with self.assertLogs(п.log, "WARNING"):
            for _ in range(5):
                self.assertEqual(б.контекст([]), хороший["now"]["text"])

    def test_правка_без_итога_и_не_json(self):
        п = плагин()
        пример = КОНТРАКТЫ["mara.command.correction.v1"]["example"]
        for тело, ждём in ((dict(пример, applied=None), "итога не прислал"),
                           (dict(пример, applied=[1]), "без итога"),
                           (dict(пример, applied={"found": True, "card": "x"}), "без текста")):
            with self.subTest(тело=тело["applied"]):
                б = п.Брокер(url="http://doctor.example", token="t",
                             open_url=lambda req, timeout=None, т=тело: self.Ответ(т))
                ответ = б.правка({"item": "x", "status": "done"})
                self.assertIn(ждём, ответ)
                self.assertNotIn('"card"', ответ, "служебные поля модели не едут")

        class Сырой(self.Ответ):
            def __init__(self, тело):
                self.тело = тело
        б = п.Брокер(url="http://doctor.example", token="t",
                     open_url=lambda req, timeout=None: Сырой(b"<html>"))
        self.assertIn("не JSON", б.правка({"item": "x", "status": "done"}))
        self.assertIn("не JSON", б.справка({"about": "Анна"}))

    def test_запросы_плагина_по_контракту(self):
        """Что плагин шлёт — метод, путь и тело — сходится с фикстуром."""
        п = плагин()
        ушло = []

        def ловец(req, timeout=None):
            ушло.append(req)
            return self.Ответ({"text": "ок"})
        б = п.Брокер(url="http://doctor.example", token="t", open_url=ловец)
        б.правка({"item": "смета", "status": "done", "due": "2026-10-20", "note": "x",
                  "id": "#0000abcd", "expected_version": 2})
        б.справка({"about": "Анна"})
        б.контекст([])
        правка, справка, пакет = ушло
        for req, имя in ((правка, "mara.command.correction.v1"),
                         (справка, "mara.context.query.v1"), (пакет, "mara.context.now.v1")):
            к = КОНТРАКТЫ[имя]
            with self.subTest(контракт=имя):
                self.assertEqual(req.get_method(), к["method"])
                self.assertEqual(urllib.parse.urlsplit(req.full_url).path, к["path"])
                if "request" in к:
                    тело = json.loads(req.data)
                    self.assertEqual(сверить(тело, к["request"]), [])
        тело = json.loads(правка.data)
        self.assertEqual(сверить(тело["payload"], КОНТРАКТЫ["mara.command.correction.v1"]
                                 ["payload"], "payload"), [])
        self.assertEqual(set(тело["payload"]) - {к.rstrip("?") for к in КОНТРАКТЫ[
            "mara.command.correction.v1"]["payload"]}, set(), "лишних полей нет")

    def test_схема_инструментов_знает_пределы(self):
        п = плагин()
        пределы = КОНТРАКТЫ["mara.command.correction.v1"]["limits"]
        свойства = п.СХЕМА["parameters"]["properties"]
        self.assertEqual((свойства["item"]["maxLength"], свойства["note"]["maxLength"]),
                         (пределы["item"], пределы["note"]))
        self.assertEqual(п.СХЕМА_СПРАВКИ["parameters"]["properties"]["about"]["maxLength"],
                         КОНТРАКТЫ["mara.context.query.v1"]["limits"]["about"])

    def test_ответ_без_контракта_старый_doctor_разбирается(self):
        now = dict(КОНТРАКТЫ["mara.context.now.v1"]["example"])
        now.pop("contract")
        _, б = self.брокер(now)
        self.assertEqual(б.контекст([]), now["now"]["text"], "старый doctor — та же v1")


if __name__ == "__main__":
    unittest.main()
