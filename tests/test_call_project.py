"""Карточки разговора и обязательств (ТЗ §10)."""
import os, sys, json, uuid, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_project as cp
import mara_ingest as mi

EVENT = {"id": "call_1", "occurred": "2026-09-02T14:05:00+03:00",
         "ended": "2026-09-02T14:23:11+03:00", "classification": "personal",
         "payload": {"contact_name": "Анна", "direction": "incoming"}}

SPAN = [{"start_ms": 252000, "end_ms": 260000}]
EXTR = {"requests": [{"action": "прислать смету", "requester": "Анна",
                      "owner": "sergey", "explicit": True, "confidence": 0.93,
                      "due_at": "2026-09-04", "deadline_explicit": True,
                      "deadline_phrase": "до пятницы", "disposition": "task",
                      "evidence": SPAN}],
        "commitments": [{"action": "перезвонить", "promised_to": "Анна",
                         "explicit": True, "confidence": 0.9, "due_at": None,
                         "deadline_explicit": False, "disposition": "task",
                         "evidence": [{"start_ms": 700000, "end_ms": 710000}]}],
        "decisions": [], "constraints": [], "open_questions": [],
        "changed_instructions": [], "followups": [],
        "people_mentioned": ["Анна", "Серёж"], "projects_mentioned": ["ремонт"]}


class Карточка(unittest.TestCase):
    def head(self, text):
        return text.split("---", 2)[1]

    def test_фронтматтер_плоский(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        for line in self.head(text).strip().splitlines():
            if line.startswith("  ") and not line.strip().startswith("- "):
                self.fail("вложенная карта, парсер репо её не понимает: %r" % line)

    def test_обязательные_поля_безопасности(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        for field in ("sensitive: true", "cloud_allowed: false",
                      "model_scope: local-only", "pipeline_version: 1",
                      "type: conversation"):
            self.assertIn(field, text)

    def test_имя_файла_из_даты_и_контакта(self):
        name, _ = cp.conversation_card(EVENT, EXTR, {})
        self.assertEqual(name, "kb/conversations/2026-09-02-1405-anna.md")

    def test_строки_люди_и_проекты_есть(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        self.assertIn("Люди: ", text)
        self.assertIn("Проекты: ", text)

    def test_хозяин_не_попадает_в_люди(self):
        _, text = cp.conversation_card(EVENT, EXTR, {"серёж": "sergey", "анна": "anna"})
        people = [l for l in text.splitlines() if l.startswith("Люди: ")][0]
        self.assertNotIn("sergey", people, "себя в собеседники не записываем")
        self.assertIn("anna", people)

    def test_незнакомое_имя_не_линкуется(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        people = [l for l in text.splitlines() if l.startswith("Люди: ")][0]
        self.assertNotIn("[[", people, "сущности нет в реестре — ссылки быть не должно")

    def test_время_спана_печатается_как_минуты(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        self.assertIn("04:12", text, "252000 мс это 4 минуты 12 секунд")

    def test_разделы_названы_как_в_дайджесте(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        self.assertIn("## Попросили", text)
        self.assertIn("## Ты обещал", text)

    def test_пустые_разделы_не_печатаются(self):
        _, text = cp.conversation_card(EVENT, dict(EXTR, requests=[]), {})
        self.assertNotIn("## Попросили", text)

    def test_хеш_тела_записан(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        line = [l for l in text.splitlines() if l.startswith("content_sha256:")][0]
        self.assertEqual(len(line.split(": ")[1]), 64)


class Обязательства(unittest.TestCase):
    def test_обязательство_из_просьбы_и_обещания(self):
        cards = cp.commitment_cards(EVENT, EXTR, {})
        self.assertEqual(len(cards), 2)
        text = cards[0][1]
        self.assertIn("status: proposed", text)
        self.assertIn("due: 2026-09-04", text)
        self.assertIn("type: commitment", text)

    def test_срок_не_печатается_если_его_нет(self):
        cards = cp.commitment_cards(EVENT, EXTR, {})
        self.assertNotIn("due:", cards[1][1], "срока не было — поля быть не должно")

    def test_needs_review_карточку_не_создаёт(self):
        e = json.loads(json.dumps(EXTR))
        e["requests"][0]["disposition"] = "needs-review"
        e["commitments"] = []
        self.assertEqual(cp.commitment_cards(EVENT, e, {}), [])

    def test_ссылка_на_разговор_есть(self):
        cards = cp.commitment_cards(EVENT, EXTR, {})
        self.assertIn("2026-09-02-1405-anna", cards[0][1])

    def test_изменение_ставит_supersedes(self):
        e = json.loads(json.dumps(EXTR))
        e["requests"] = []
        e["commitments"] = []
        e["changed_instructions"] = [{"action": "прислать договор",
                                      "supersedes": "прислать счёт",
                                      "new_state": "нужен договор",
                                      "explicit": True, "confidence": 0.9,
                                      "disposition": "task", "evidence": SPAN}]
        cards = cp.commitment_cards(EVENT, e, {})
        self.assertEqual(len(cards), 1)
        self.assertIn("supersedes: ", cards[0][1])


class Человек(unittest.TestCase):
    def контакт(self, **kw):
        p = {"contact_name": "Анна Петрова", "contact_source": "call-log",
             "number": "+79990000000"}
        p.update(kw)
        return dict(EVENT, payload=p)

    def test_человек_из_книги_заводится(self):
        path, text = cp.person_card(self.контакт(), {})
        self.assertEqual(path, "entities/people/anna-petrova.md")
        self.assertIn("type: person", text)
        self.assertIn("- +79990000000", text, "номер идёт в алиасы")

    def test_имя_из_текста_человека_не_заводит(self):
        self.assertIsNone(cp.person_card(self.контакт(contact_source=None), {}))

    def test_известный_человек_не_дублируется(self):
        self.assertIsNone(cp.person_card(self.контакт(), {"анна петрова": "anna"}))


class Запись(unittest.TestCase):
    def test_карточки_ложатся_в_волт_атомарно(self):
        vault = tempfile.mkdtemp()
        os.makedirs(os.path.join(vault, ".git"))
        paths = cp.write_cards(vault, cp.all_cards(EVENT, EXTR, {}))
        self.assertTrue(all(os.path.exists(os.path.join(vault, p)) for p in paths))
        self.assertFalse([f for _, _, fs in os.walk(vault) for f in fs
                          if f.endswith(".tmp")], "временных файлов не остаётся")

    def test_повторная_запись_не_плодит_копии(self):
        vault = tempfile.mkdtemp()
        os.makedirs(os.path.join(vault, ".git"))
        first = cp.write_cards(vault, cp.all_cards(EVENT, EXTR, {}))
        second = cp.write_cards(vault, cp.all_cards(EVENT, EXTR, {}))
        self.assertEqual(first, second)
        found = [f for _, _, fs in os.walk(vault) for f in fs if f.endswith(".md")]
        self.assertEqual(len(found), len(first))


class Правка(unittest.TestCase):
    """«Это тоже задача, срок пятница» → карточка, без правки YAML руками (ТЗ §16)."""

    ЧУЖОЕ = "permalink: kb/commitments/smeta"      # Basic Memory дописывает своё

    def волт(self):
        v = tempfile.mkdtemp()
        os.makedirs(os.path.join(v, ".git"))
        os.makedirs(os.path.join(v, "kb/commitments"))
        return v

    def карточка(self, v, name, title, status="proposed", due="2026-09-04"):
        text = ("---\ntitle: %s\ntype: commitment\nstatus: %s\n%s%s\n"
                "origin: call/call_1\naudience:\n  - mara\n---\n\n"
                "- Обещание: %s\n\nЛюди: [[anna]]\n"
                % (title, status, "due: %s\n" % due if due else "", self.ЧУЖОЕ, title))
        p = os.path.join(v, "kb/commitments", name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def правка(self, v, **payload):
        return cp.apply_correction(v, {"id": "correction_1", "occurred_at": "2026-09-02T18:00:00+03:00",
                                       "payload": payload})

    def test_сделано_меняет_проекцию_и_пишет_журнал(self):
        v = self.волт()
        p = self.карточка(v, "smeta.md", "прислать смету")
        было = open(p, encoding="utf-8").read()
        out = self.правка(v, item="прислать смету", status="done")
        text = open(p, encoding="utf-8").read()
        self.assertTrue(out["found"])
        self.assertIn("proposed → done", out["text"])
        self.assertIn("\nstatus: done\n", text)
        self.assertNotIn("status: proposed", text)
        self.assertIn(self.ЧУЖОЕ, text, "чужие ключи фронтматтера пережили правку")
        self.assertIn("\nПравки:\n- ", text)
        self.assertIn("correction/correction_1", text, "правка ссылается на событие")
        # изменились ровно две строки шапки плюс valid_from и хвост тела
        for line in было.splitlines():
            if not line.startswith("status:"):
                self.assertIn(line, text, "нетронутая строка пропала: %r" % line)
        self.assertNotIn("прислать смету", open(os.path.join(v, "_system/context/now.md"),
                                                 encoding="utf-8").read(),
                         "пакет пересобран сразу: сделанное из списка ушло")

    def test_срок_меняется_а_история_остаётся(self):
        v = self.волт()
        p = self.карточка(v, "smeta.md", "прислать смету")
        self.правка(v, item="прислать смету", due="2026-09-05")
        self.правка(v, item="прислать смету", due="2026-09-06", note="Анна попросила")
        text = open(p, encoding="utf-8").read()
        self.assertIn("due: 2026-09-06\n", text)
        self.assertIn("due_explicit: true", text)
        self.assertIn("срок 2026-09-04 → 2026-09-05", text, "старый срок в журнале, не стёрт")
        self.assertIn("срок 2026-09-05 → 2026-09-06; Анна попросила", text)
        self.assertEqual(text.count("Правки:"), 1)

    def test_не_нашёл_отдаёт_открытые_и_ничего_не_пишет(self):
        v = self.волт()
        self.карточка(v, "smeta.md", "прислать смету")
        self.карточка(v, "old.md", "старое", status="done")
        out = self.правка(v, item="покрасить забор", status="done")
        self.assertFalse(out["found"])
        self.assertEqual(out["open"], ["прислать смету"], "только заголовки, только открытые")
        self.assertIn("не нашёл", out["text"])
        self.assertEqual(sorted(os.listdir(os.path.join(v, "kb/commitments"))),
                         ["old.md", "smeta.md"])

    def test_новая_задача_заводит_карточку(self):
        v = self.волт()
        out = self.правка(v, item="покрасить забор", status="open", due="2026-09-05")
        self.assertIn("created", out)
        text = open(os.path.join(v, out["created"]), encoding="utf-8").read()
        for line in ("status: open", "source: mara", "origin: correction/correction_1",
                     "due: 2026-09-05", "due_explicit: true", "sensitive: true",
                     "cloud_allowed: false"):
            self.assertIn(line, text)
        self.assertIn("покрасить забор — до 2026-09-05",
                      open(os.path.join(v, "_system/context/now.md"), encoding="utf-8").read())

    def test_два_похожих_не_угадываем(self):
        v = self.волт()
        self.карточка(v, "a.md", "позвонить Анне")
        self.карточка(v, "b.md", "позвонить Пете")
        out = self.правка(v, item="позвонить", status="done")
        self.assertEqual(sorted(out["ambiguous"]), ["позвонить Анне", "позвонить Пете"])
        for name in ("a.md", "b.md"):
            self.assertIn("status: proposed",
                          open(os.path.join(v, "kb/commitments", name), encoding="utf-8").read())

    def test_открытая_важнее_закрытой_с_тем_же_названием(self):
        v = self.волт()
        self.карточка(v, "old.md", "прислать смету", status="done")
        self.карточка(v, "new.md", "прислать смету")
        out = self.правка(v, item="смету прислать", status="cancelled")
        self.assertEqual(out["card"], "kb/commitments/new.md")

    def test_граница_доверия(self):
        self.assertIsNone(cp.check_correction({"item": "x", "status": "done"}))
        self.assertIn("YYYY-MM-DD", cp.check_correction({"item": "x", "due": "пятница"}))
        self.assertIn("статус", cp.check_correction({"item": "x", "status": "готово"}))
        self.assertTrue(cp.check_correction({"item": ""}))
        self.assertIn("нечего", cp.check_correction({"item": "x"}))


def _id(text):
    return [l for l in text.split("---", 2)[1].splitlines() if l.startswith("id: ")][0][4:]


def _текст(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class Идентичность(unittest.TestCase):
    """Т2.2, ADR-0002: у разговора и обязательства свой uuid7, он в шапке
    полем, в реестре — ключом, и от имени файла не зависит."""

    def стенд(self):
        tmp = tempfile.mkdtemp()
        root, vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(root)
        os.makedirs(os.path.join(vault, ".git"))
        return root, vault, mi.connect(root)

    def звонок(self, con, root, sid, occurred=EVENT["occurred"]):
        eid, _ = mi.put_event(con, {"kind": "call", "source": "phone", "source_id": sid,
                                    "occurred_at": occurred, "ended_at": EVENT["ended"],
                                    "payload": EVENT["payload"]})
        mi.write_json(mi.extraction_path(root, eid), EXTR)
        return eid

    def test_карточки_несут_id_uuid7(self):
        cards = cp.all_cards(EVENT, EXTR, {})
        ids = [_id(text) for rel, text in cards if "/kb/" in "/" + rel]
        self.assertEqual(len(ids), 3, "разговор и два обязательства")
        self.assertEqual(len(set(ids)), 3, "id не повторяются")
        for i in ids:
            self.assertEqual(uuid.UUID(i).version, 7, i)
        conv = [text for rel, text in cards if rel.startswith("kb/conversations/")][0]
        self.assertRegex(conv, r"^---\ntitle: [^\n]+\nid: [0-9a-f-]{36}\ntype: conversation\n",
                         "id стоит сразу за title")

    def test_повторная_проекция_через_реестр_даёт_тот_же_id(self):
        root, vault, con = self.стенд()
        eid = self.звонок(con, root, "a")
        first = cp.run(eid, vault, root)
        ids1 = {rel: _id(_текст(os.path.join(vault, rel)))
                for rel in first if rel.startswith("kb/")}
        second = cp.run(eid, vault, root)
        self.assertEqual(first, second, "пути не меняются")
        ids2 = {rel: _id(_текст(os.path.join(vault, rel)))
                for rel in second if rel.startswith("kb/")}
        self.assertEqual(ids1, ids2, "id при повторной проекции другой")
        self.assertEqual([r[0] for r in con.execute("select actor_type from revisions")],
                         ["projector"] * 2, "по ревизии на обязательство, без повторов")
        # реестр знает объекты с первой проекции и теми же id
        в_базе = {r[0] for r in con.execute("select id from commitments")}
        в_базе |= {r[0] for r in con.execute("select id from conversations")}
        self.assertEqual(в_базе, set(ids1.values()))
        self.assertEqual(con.execute("select count(*) from projections").fetchone()[0], 3)
        for rel, oid in ids1.items():
            self.assertEqual(con.execute("select object_id from projections where path=?",
                                         (rel,)).fetchone()[0], oid)

    def test_два_звонка_в_одну_минуту_не_перезаписывают_друг_друга(self):
        """Полевой тест R12 §16: два звонка одному контакту в одну минуту —
        два разговора, два набора обязательств, ни одной перезаписи."""
        root, vault, con = self.стенд()
        a = self.звонок(con, root, "a")
        b = self.звонок(con, root, "b")
        first = cp.run(a, vault, root)
        second = cp.run(b, vault, root)
        self.assertFalse(set(first) & set(second), "второй звонок лёг на файлы первого")
        conv2 = [r for r in second if r.startswith("kb/conversations/")][0]
        oid2 = _id(_текст(os.path.join(vault, conv2)))
        self.assertTrue(conv2.endswith("--%s.md" % oid2[-8:]), conv2)
        comm2 = [r for r in second if r.startswith("kb/commitments/")]
        for rel in comm2:
            text = _текст(os.path.join(vault, rel))
            self.assertIn("[[%s]]" % os.path.basename(conv2)[:-3], text,
                          "обязательство ссылается не на свой разговор")
        # повтор второго звонка — те же пути, не третий набор
        self.assertEqual(cp.run(b, vault, root), second)
        self.assertEqual(con.execute("select count(*) from conversations").fetchone()[0], 2)
        self.assertEqual(con.execute("select count(*) from commitments").fetchone()[0], 4)

    def test_карточка_без_реестра_с_чужим_source_id_не_затирается(self):
        """Волт старше реестра: файл на пути есть, строки в `projections` нет."""
        root, vault, con = self.стенд()
        eid = self.звонок(con, root, "a")
        rel = "kb/conversations/2026-09-02-1405-anna.md"
        os.makedirs(os.path.join(vault, "kb/conversations"))
        with open(os.path.join(vault, rel), "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: чужой\ntype: conversation\nsource_id: call/old\n---\n")
        written = cp.run(eid, vault, root)
        self.assertNotIn(rel, written)
        self.assertIn("title: чужой", _текст(os.path.join(vault, rel)))

    def test_правка_с_реестром_зеркалит_строку(self):
        root, vault, con = self.стенд()
        os.makedirs(os.path.join(vault, "kb/commitments"))
        событие = {"id": "correction_1", "occurred_at": "2026-09-02T18:00:00+03:00",
                   "payload": {"item": "покрасить забор", "status": "open"}}
        out = cp.apply_correction(vault, событие, con)
        text = _текст(os.path.join(vault, out["created"]))
        row = con.execute("select id, status, source_native_id from commitments").fetchone()
        self.assertEqual(row["id"], _id(text), "id в шапке и в реестре разные")
        self.assertEqual((row["status"], row["source_native_id"]),
                         ("open", "correction/correction_1"))
        событие2 = dict(событие, id="correction_2",
                        payload={"item": "покрасить забор", "status": "done"})
        cp.apply_correction(vault, событие2, con)
        self.assertEqual(con.execute("select status from commitments").fetchone()[0], "done")
        self.assertEqual(con.execute("select count(*) from corrections").fetchone()[0], 1,
                         "строка журнала «статус open → done» в истории")
        ревизии = [dict(r) for r in con.execute("select * from revisions order by version")]
        self.assertEqual([r["version"] for r in ревизии], [1, 2])
        self.assertEqual((ревизии[1]["actor_type"], ревизии[1]["actor_id"],
                          ревизии[1]["reason"]), ("human", "owner", "correction/correction_2"))
        self.assertEqual(con.execute("select version from commitments").fetchone()[0], 2)

    def test_заведённая_правкой_на_занятом_пути_получает_различитель_из_id(self):
        root, vault, con = self.стенд()
        os.makedirs(os.path.join(vault, "kb/commitments"))
        # на пути, который получит «забор», уже лежит чужая карточка с другим
        # названием: похожей она не считается, значит правка заводит новую
        занятый = "%s/%s-%s.md" % (cp.COMM_DIR, mi.now_iso()[:10], cp.slug("забор"))
        with open(os.path.join(vault, занятый), "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: другое\ntype: commitment\nstatus: done\n---\n")
        событие = {"id": "c1", "occurred_at": "2026-09-02T18:00:00+03:00",
                   "payload": {"item": "забор", "status": "open"}}
        новая = cp.apply_correction(vault, событие, con)["created"]
        oid = _id(_текст(os.path.join(vault, новая)))
        self.assertEqual(новая, занятый[:-3] + "--%s.md" % oid[-8:])
        self.assertIn("title: другое", _текст(os.path.join(vault, занятый)))


if __name__ == "__main__":
    unittest.main()
