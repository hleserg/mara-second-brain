"""Карточки разговора и обязательств (ТЗ §10)."""
import os, sys, json, uuid, tempfile, unittest, sqlite3, contextlib, io

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

    def test_одно_действие_просьбой_и_обещанием_даёт_два_файла(self):
        """Ревью P2-6: один slug внутри одного прогона — второй файл затирал
        первый молча, в реестре оставался один объект."""
        root, vault, con = self.стенд()
        extr = dict(EXTR, commitments=[dict(EXTR["requests"][0], promised_to="Анна")])
        eid, _ = mi.put_event(con, {"kind": "call", "source": "phone", "source_id": "a",
                                    "occurred_at": EVENT["occurred"],
                                    "ended_at": EVENT["ended"], "payload": EVENT["payload"]})
        mi.write_json(mi.extraction_path(root, eid), extr)
        written = cp.run(eid, vault, root)
        обязательства = [r for r in written if r.startswith("kb/commitments/")]
        self.assertEqual(len(обязательства), 2)
        self.assertEqual(len(set(обязательства)), 2, "один путь выдан дважды")
        self.assertTrue(any("--" in r for r in обязательства), обязательства)
        self.assertEqual(con.execute("select count(*) from commitments").fetchone()[0], 2)
        self.assertEqual(cp.run(eid, vault, root), written, "повтор — те же пути")

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
        # найдена по словам, без `expected_version` — ADR-0003 п.3: долг
        # проверки виден в причине ревизии, не только в ответе
        self.assertEqual((ревизии[1]["actor_type"], ревизии[1]["actor_id"],
                          ревизии[1]["reason"]),
                         ("human", "owner", "correction/correction_2; legacy_title_match"))
        self.assertEqual(con.execute("select version from commitments").fetchone()[0], 2)

    def test_правка_только_заметкой_доезжает_до_реестра(self):
        """Codex по #117, P1: заметка без смены шапки давала `changed: {}`,
        и карточка в реестр не переносилась."""
        root, vault, con = self.стенд()
        os.makedirs(os.path.join(vault, "kb/commitments"))
        событие = {"id": "c1", "occurred_at": "2026-09-02T18:00:00+03:00",
                   "payload": {"item": "забор", "status": "open"}}
        oid = cp.apply_correction(vault, событие, con)["id"]
        out = cp.apply_correction(vault, dict(событие, id="c2",
                                              payload={"item": "забор", "note": "краска куплена"}),
                                  con)
        self.assertTrue(out.get("applied"))
        self.assertEqual(out["id"], oid)
        з = con.execute("select field, new_json from corrections where object_id=? "
                        "and field='note'", (oid,)).fetchone()
        self.assertIsNotNone(з, "заметка не дошла до corrections")
        self.assertIn("краска куплена", з["new_json"])

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


class _СтендПравки(unittest.TestCase):
    """Волт с реестром и две правки: завести и поправить. Тестов не несёт —
    наследники не должны прогонять чужие."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.root, self.vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(self.root)
        os.makedirs(os.path.join(self.vault, ".git"))
        os.makedirs(os.path.join(self.vault, "kb/commitments"))
        self.con = mi.connect(self.root)

    def завести(self, item, n):
        out = cp.apply_correction(self.vault, {"id": "c%d" % n, "occurred_at": когда(),
                                               "payload": {"item": item, "status": "open"}},
                                  self.con)
        return out["id"], out["created"]

    def правка(self, n, **payload):
        return cp.apply_correction(self.vault, {"id": "c%d" % n, "occurred_at": когда(),
                                                "payload": payload}, self.con)


class ПравкаПоКоду(_СтендПравки):
    """Т2.3, часть 2 (ADR-0003 п.3–4): код карточки в пакете и в контракте
    `mara_correction`, `expected_version` и конфликт вместо перезаписи."""

    def test_ответ_несёт_id_и_версию(self):
        oid, rel = self.завести("покрасить забор", 1)
        self.assertEqual(uuid.UUID(oid).version, 7)
        out = self.правка(2, item="покрасить забор", status="done")
        self.assertEqual((out["id"], out["version"]), (oid, 2))

    def test_код_из_пакета_попадает_точно_в_карточку(self):
        a, _ = self.завести("позвонить маме", 1)
        # второе название без общих слов сверх «позвонить»: иначе поиск по
        # словам счёл бы его правкой первой карточки и не завёл вторую
        b, _ = self.завести("позвонить в банк про ипотеку", 2)
        # по словам «позвонить» — двое, по коду — одна
        out = self.правка(3, item="позвонить", status="done")
        self.assertIn("ambiguous", out)
        out = self.правка(4, item="позвонить", status="done", id="#" + b[-8:])
        self.assertTrue(out["found"])
        self.assertEqual(out["id"], b)
        self.assertEqual(self.con.execute("select status from commitments where id=?",
                                          (a,)).fetchone()[0], "open", "соседа не тронули")
        out = self.правка(5, item="позвонить", status="cancelled", id=a)
        self.assertEqual(out["id"], a, "полный id тоже адрес")

    def test_неизвестный_код_не_угадывается_по_словам(self):
        self.завести("покрасить забор", 1)
        out = self.правка(2, item="покрасить забор", status="done", id="#00000000")
        self.assertFalse(out["found"])
        self.assertIn("не нашёл карточку с кодом #00000000", out["text"])
        self.assertEqual(self.con.execute("select status from commitments").fetchone()[0],
                         "open")

    def test_расхождение_версии_это_конфликт_а_не_перезапись(self):
        oid, rel = self.завести("покрасить забор", 1)
        self.правка(2, item="покрасить забор", due="2026-10-10")      # версия 2
        out = self.правка(3, item="покрасить забор", status="done", id=oid,
                          expected_version=1)
        self.assertEqual(out["error"], "version_conflict")
        self.assertEqual((out["entity_id"], out["expected_version"], out["current_version"]),
                         (oid, 1, 2))
        self.assertEqual(out["current"]["status"], "open")
        self.assertEqual(out["attempted_patch"]["status"], "done")
        self.assertIn("не правил", out["text"])
        r = self.con.execute("select status, version from commitments").fetchone()
        self.assertEqual((r["status"], r["version"]), ("open", 2), "ничего не применено")
        with open(os.path.join(self.vault, rel), encoding="utf-8") as fh:
            self.assertIn("status: open", fh.read())
        тревога = self.con.execute("select kind, state, object_id, id from alerts").fetchone()
        self.assertEqual(tuple(тревога)[:3], ("version_conflict", "open", oid))
        self.assertEqual(тревога["id"], out["conflict_id"])

    def test_совпавшая_версия_применяется_и_проверка_отмечена(self):
        oid, _ = self.завести("покрасить забор", 1)
        out = self.правка(2, item="покрасить забор", status="done", id=oid,
                          expected_version=1)
        self.assertTrue(out["found"] and out["version_checked"])
        self.assertEqual(out["version"], 2)
        self.assertEqual(self.con.execute("select count(*) from alerts").fetchone()[0], 0)

    def test_без_строки_в_реестре_версия_не_проверяется_и_это_сказано(self):
        p = os.path.join(self.vault, "kb/commitments/x.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: забор\nid: 01999999-0000-7000-8000-000000000009\n"
                     "type: commitment\nstatus: open\n---\n")
        out = self.правка(1, item="забор", status="done", expected_version=5)
        self.assertTrue(out["found"])
        self.assertFalse(out["version_checked"])
        self.assertEqual(self.con.execute("select status from commitments").fetchone()[0],
                         "done", "карточка перенесена после правки")

    def test_карточка_без_id_в_шапке_адресуется_id_из_ответа(self):
        """Ревью P2: до `--write-ids` шапка без `id:`, а строка в реестре
        есть — id из ответа обязан быть адресом, версия — проверяться."""
        p = os.path.join(self.vault, "kb/commitments/x.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: забор\ntype: commitment\nstatus: open\n---\n")
        out = self.правка(1, item="забор", due="2026-10-10")
        oid = out["id"]
        self.assertEqual(out["version"], 1)
        with open(p, encoding="utf-8") as fh:
            self.assertNotIn("\nid: ", fh.read(), "шапку перенос не правит")
        out = self.правка(2, item="что угодно", status="done", id=oid, expected_version=1)
        self.assertTrue(out["found"] and out["version_checked"])
        self.assertEqual((out["id"], out["version"]), (oid, 2))
        out = self.правка(3, item="забор", status="cancelled", expected_version=1)
        self.assertEqual(out["error"], "version_conflict")

    def test_уже_так_несёт_id_и_версию_и_не_конфликт(self):
        """ADR-0003 п.5: правка без изменений — «уже так», даже со старой
        версией; и адрес в ответе есть."""
        oid, _ = self.завести("покрасить забор", 1)
        self.правка(2, item="покрасить забор", due="2026-10-10")      # версия 2
        out = self.правка(3, item="покрасить забор", status="open", id=oid,
                          expected_version=1)
        self.assertNotIn("error", out)
        self.assertIn("уже так", out["text"])
        self.assertEqual((out["id"], out["version"]), (oid, 2))
        self.assertEqual(self.con.execute("select count(*) from alerts").fetchone()[0], 0)

    def test_заметка_коммутативна_и_на_старую_версию_принимается(self):
        """ADR-0003 п.5: только `status` и `due` двигают версию; заметка с
        ними не пересекается, и правка «одна заметка» на несовпавшую версию — не
        конфликт, а слияние, о котором сказано."""
        oid, rel = self.завести("покрасить забор", 1)
        self.правка(2, item="покрасить забор", due="2026-10-10")      # версия 2
        out = self.правка(3, item="покрасить забор", note="краска куплена", id=oid,
                          expected_version=1)
        self.assertNotIn("error", out)
        self.assertTrue(out["applied"] and out["version_checked"])
        self.assertEqual(out["merged"], "commutative")
        self.assertEqual((out["id"], out["version"]), (oid, 2), "версию заметка не двигает")
        with open(os.path.join(self.vault, rel), encoding="utf-8") as fh:
            self.assertIn("краска куплена", fh.read())
        self.assertEqual(self.con.execute("select count(*) from alerts").fetchone()[0], 0)
        # а заметка вместе со статусом на старую версию — по-прежнему конфликт
        out = self.правка(4, item="покрасить забор", status="done", note="ещё",
                          id=oid, expected_version=1)
        self.assertEqual(out["error"], "version_conflict")
        # на совпавшую версию слияния нет и слово про него не звучит
        out = self.правка(5, item="покрасить забор", note="третья", id=oid,
                          expected_version=2)
        self.assertNotIn("merged", out)
        # версии больше текущей нет — это не старая заметка, а конфликт
        # (ревью #120, P3-4)
        out = self.правка(6, item="покрасить забор", note="четвёртая", id=oid,
                          expected_version=7)
        self.assertEqual(out["error"], "version_conflict")

    def test_повтор_конфликта_не_плодит_тревог(self):
        oid, _ = self.завести("покрасить забор", 1)
        self.правка(2, item="покрасить забор", due="2026-10-10")
        a = self.правка(3, item="покрасить забор", status="done", id=oid, expected_version=1)
        b = self.правка(4, item="покрасить забор", status="cancelled", id=oid,
                        expected_version=1)
        self.assertEqual(a["conflict_id"], b["conflict_id"])
        self.assertEqual(self.con.execute("select count(*) from alerts").fetchone()[0], 1)

    def test_код_в_верхнем_регистре_и_список_в_шапке(self):
        oid, _ = self.завести("покрасить забор", 1)
        out = self.правка(2, item="x", status="done", id="#" + oid[-8:].upper())
        self.assertEqual(out["id"], oid)
        p = os.path.join(self.vault, "kb/commitments/y.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: кривая\nid:\n  - a\n  - b\ntype: commitment\n"
                     "status: open\n---\n")
        out = self.правка(3, item="кривая", status="done", id="#" + oid[-8:])
        self.assertEqual(out["id"], oid, "список в шапке не роняет и не ловится")

    def test_два_совпадения_по_коду_несут_полные_id(self):
        import unittest.mock
        oid, _ = self.завести("забор", 1)
        with unittest.mock.patch.object(cp, "_совпал", return_value=True):
            self.завести("другое", 2)
            out = self.правка(3, item="x", status="done", id="#" + oid[-8:])
        self.assertIn("ambiguous_ids", out)
        self.assertEqual(len(out["ambiguous_ids"]), 2)

    def test_граница_доверия_для_id_и_версии(self):
        self.assertIsNone(cp.check_correction({"item": "x", "status": "done",
                                               "id": "#5479d088", "expected_version": 3}))
        self.assertIn("id", cp.check_correction({"item": "x", "status": "done", "id": "../x"}))
        self.assertIn("id", cp.check_correction({"item": "x", "status": "done", "id": "#abc"}))
        self.assertIn("expected_version",
                      cp.check_correction({"item": "x", "status": "done",
                                           "expected_version": "много"}))
        self.assertIn("expected_version",
                      cp.check_correction({"item": "x", "status": "done", "expected_version": 0}))
        self.assertIn("expected_version",
                      cp.check_correction({"item": "x", "status": "done", "expected_version": "²"}))
        self.assertIn("id", cp.check_correction({"item": "x", "status": "done", "id": ["a"]}))
        self.assertIsNone(cp.check_correction({"item": "x", "status": "done",
                                               "id": "#5479D088", "expected_version": "3"}))


def когда():
    return "2026-09-02T18:00:00+03:00"




class СледПравки(_СтендПравки):
    """Т2.5, §5.2 «след правки сохраняется»: на каждую команду правки — строка
    `audit_events`, с исходом, включая отказы, которых ревизии не видят.
    Содержимого в ней нет: имена полей, версии, исход."""

    def след(self):
        return [dict(r) for r in self.con.execute(
            "select * from audit_events where action='correction' order by occurred, id")]

    def деталь(self, row):
        return json.loads(row["detail_json"])

    def test_каждый_исход_оставляет_строку_аудита(self):
        oid, _ = self.завести("покрасить забор", 1)                         # created
        self.правка(2, item="покрасить забор", due="2026-10-10")           # applied, legacy
        self.правка(3, item="покрасить забор", status="open")              # noop
        self.правка(4, item="покрасить забор", status="done", id=oid,
                    expected_version=1)                                     # conflict
        self.правка(5, item="покрасить забор", status="done", id="#00000000")   # not_found
        self.завести("позвонить в банк про ипотеку", 6)
        self.завести("позвонить маме", 7)
        self.правка(8, item="позвонить", status="done")                    # ambiguous
        исходы = [(self.деталь(r)["event"], self.деталь(r)["outcome"]) for r in self.след()]
        self.assertEqual(исходы, [("c1", "created"), ("c2", "applied"), ("c3", "noop"),
                                  ("c4", "conflict"), ("c5", "not_found"),
                                  ("c6", "created"), ("c7", "created"),
                                  ("c8", "ambiguous")])
        след = self.след()
        self.assertTrue(all(r["actor_type"] == "human" and r["actor_id"] == "owner"
                            and r["object_kind"] == "commitment" for r in след))
        self.assertEqual([r["object_id"] for r in след[:4]], [oid] * 4,
                         "у конфликта и «уже так» адрес есть")
        self.assertIsNone(след[4]["object_id"], "не нашли — адреса нет")
        д = self.деталь(след[3])
        self.assertEqual((д["expected_version"], д["version_checked"]), (1, True))
        self.assertEqual(д["conflict_id"], self.con.execute(
            "select id from alerts").fetchone()[0])
        self.assertEqual(self.деталь(след[1])["version_checked"], False)
        self.assertEqual(len(self.деталь(след[7])["ambiguous_ids"]), 2)

    def test_аудит_не_несёт_содержимого(self):
        self.завести("покрасить забор у соседа", 1)
        self.правка(2, item="покрасить забор у соседа", note="секретная заметка")
        for r in self.след():
            self.assertNotIn("забор", r["detail_json"])
            self.assertNotIn("секретная", r["detail_json"])
        self.assertEqual(self.деталь(self.след()[1])["fields"], ["note"])

    def test_проверенная_правка_без_legacy_в_причине(self):
        """ADR-0003 п.3: `legacy_title_match` — только у правки, версию
        которой проверить было нечем или не просили."""
        oid, _ = self.завести("покрасить забор", 1)
        self.правка(2, item="покрасить забор", due="2026-10-10", id=oid, expected_version=1)
        self.правка(3, item="покрасить забор", status="done")
        причины = [r[0] for r in self.con.execute(
            "select reason from revisions order by version")]
        self.assertEqual(причины, ["correction/c1", "correction/c2",
                                   "correction/c3; legacy_title_match"])

    def test_аудит_и_перенос_одной_транзакцией(self):
        """Падение переноса не оставляет строки аудита без ревизии и наоборот."""
        self.завести("покрасить забор", 1)
        было = cp.li.перенести_карточку

        def упасть(*a, **kw):
            raise RuntimeError("смоделированный сбой переноса")
        cp.li.перенести_карточку = упасть
        try:
            with self.assertRaises(RuntimeError):
                self.правка(2, item="покрасить забор", status="done")
        finally:
            cp.li.перенести_карточку = было
        self.assertEqual(len(self.след()), 1, "от упавшей правки следа нет")
        self.assertEqual(self.con.execute("select version from commitments").fetchone()[0], 1)
        # и наоборот: не легла строка аудита — не лёг и перенос
        было = mi.audit

        def упасть(*a, **kw):
            raise RuntimeError("смоделированный сбой аудита")
        mi.audit = упасть
        try:
            with self.assertRaises(RuntimeError):
                self.правка(3, item="покрасить забор", status="done")
        finally:
            mi.audit = было
        self.assertEqual(self.con.execute("select version from commitments").fetchone()[0], 1)
        self.assertEqual(self.con.execute("select count(*) from revisions").fetchone()[0], 1)

    def test_сбой_реестра_возвращает_карточку(self):
        """Codex по #120, P1: реестр откатился — файл тоже, иначе повтор
        видит «уже так», и реестр не догонит никогда."""
        oid, rel = self.завести("покрасить забор", 1)
        p = os.path.join(self.vault, rel)
        with open(p, encoding="utf-8") as fh:
            было_текст = fh.read()
        было = mi.audit
        mi.audit = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("сбой аудита"))
        try:
            with self.assertRaises(RuntimeError):
                self.правка(2, item="покрасить забор", status="done", note="заметка")
            with self.assertRaises(RuntimeError):
                self.правка(3, item="заменить крышу", status="open")
        finally:
            mi.audit = было
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), было_текст, "правленая карточка возвращена")
        self.assertEqual(os.listdir(os.path.join(self.vault, "kb/commitments")),
                         [os.path.basename(rel)], "заведённая карточка снята")
        # и повтор правки после починки — настоящая правка, не «уже так»
        out = self.правка(4, item="покрасить забор", status="done")
        self.assertTrue(out["applied"])
        self.assertEqual(out["version"], 2)

    def test_упавший_commit_тоже_возвращает_карточку(self):
        """Codex по #120, круг 2: откат бывает и на `commit` внешней
        транзакции — после того, как `_в_реестр` отработал без ошибки."""
        oid, rel = self.завести("покрасить забор", 1)
        p = os.path.join(self.vault, rel)
        with open(p, encoding="utf-8") as fh:
            было_текст = fh.read()

        class ломаная(mi.транзакция):
            def __exit__(self, тип, *a):
                if self.точка or тип:
                    return super().__exit__(тип, *a)
                self.con.execute("rollback")
                raise sqlite3.OperationalError("commit упал")
        было = mi.транзакция
        mi.транзакция = ломаная
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.правка(2, item="покрасить забор", status="done")
            with self.assertRaises(sqlite3.OperationalError):
                self.правка(3, item="заменить крышу", status="open")
        finally:
            mi.транзакция = было
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), было_текст)
        self.assertEqual(os.listdir(os.path.join(self.vault, "kb/commitments")),
                         [os.path.basename(rel)])
        self.assertEqual(self.con.execute("select version from commitments").fetchone()[0], 1)
        self.assertFalse(self.con.in_transaction, "соединение не осталось в транзакции")
        out = self.правка(4, item="покрасить забор", status="done")
        self.assertTrue(out["applied"] and out["version"] == 2)

    def test_спорную_карточку_правка_не_считает_успехом(self):
        """Codex по #120, круг 3: перенос отвергает карточку молча (None) —
        правка обязана это считать отказом: файл назад, аудита `applied` нет."""
        oid, rel = self.завести("покрасить забор", 1)
        # карточка с чужим id в шапке: занят объектом с другим ключом — спор
        p = os.path.join(self.vault, "kb/commitments/krysha.md")
        текст = ("---\ntitle: заменить крышу\nid: %s\ntype: commitment\nstatus: open\n"
                 "source_id: commitment/call_9/requests/1\n---\n" % oid)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                self.правка(2, item="заменить крышу", status="done")
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), текст, "файл возвращён")
        self.assertEqual([json.loads(r["detail_json"])["outcome"] for r in self.след()],
                         ["created"], "аудита об успехе нет")
        self.assertEqual(self.con.execute("select count(*) from commitments").fetchone()[0], 1)

    def test_тревога_конфликта_и_её_аудит_одной_транзакцией(self):
        """Ревью #120, P3-1: упал аудит — нет и тревоги."""
        oid, _ = self.завести("покрасить забор", 1)
        self.правка(2, item="покрасить забор", due="2026-10-10")
        было = mi.audit
        mi.audit = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("сбой аудита"))
        try:
            with self.assertRaises(RuntimeError):
                self.правка(3, item="покрасить забор", status="done", id=oid,
                            expected_version=1)
        finally:
            mi.audit = было
        self.assertEqual(self.con.execute("select count(*) from alerts").fetchone()[0], 0)


class EvidenceВРеестре(unittest.TestCase):
    """Т2.4, ADR-0004 п.5: ссылки обязательства ложатся в `evidence_refs`
    одной транзакцией с переносом карточки, карточка рисует диапазон и код
    сегмента, фронтматтер несёт машиночитаемый список."""

    def setUp(self):
        import call_asr
        tmp = tempfile.mkdtemp()
        self.root, self.vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(self.root)
        os.makedirs(os.path.join(self.vault, ".git"))
        self.con = mi.connect(self.root)
        self.eid, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "ev1",
            "occurred_at": EVENT["occurred"], "ended_at": EVENT["ended"],
            "payload": EVENT["payload"]})
        segs = [{"segment_id": "s0011", "start_ms": 250000, "end_ms": 275000,
                 "speaker": "unknown-A", "text": "смета"},
                {"segment_id": "s0029", "start_ms": 700000, "end_ms": 725000,
                 "speaker": "unknown-A", "text": "перезвоню"}]
        call_asr.записать_сегменты(self.con, self.eid, None, segs)
        self.сег = {r["seq"]: r["id"] for r in self.con.execute(
            "select seq, id from transcript_segments")}

    def извлечение(self, **правки):
        extr = json.loads(json.dumps(EXTR))
        extr["requests"][0]["evidence"] = [{"segment": "s0011", "segment_id": self.сег[11],
                                            "start_ms": 252000, "end_ms": 260000}]
        extr["commitments"][0]["evidence"] = [{"segment": "s0029", "segment_id": self.сег[29],
                                               "start_ms": 700000, "end_ms": 725000}]
        extr.update(правки)
        mi.write_json(mi.extraction_path(self.root, self.eid), extr)
        return extr

    def строки(self):
        return [dict(r) for r in self.con.execute(
            "select * from evidence_refs order by start_ms")]

    def test_ссылки_ложатся_в_реестр_вместе_с_объектом(self):
        self.извлечение()
        written = cp.run(self.eid, self.vault, self.root)
        строки = self.строки()
        self.assertEqual(len(строки), 2)
        oids = {r["id"] for r in self.con.execute("select id from commitments")}
        self.assertEqual({r["object_id"] for r in строки}, oids)
        r = строки[0]
        self.assertEqual((r["object_kind"], r["kind"], r["segment_id"], r["start_ms"],
                          r["end_ms"], r["producer"]),
                         ("commitment", "audio", self.сег[11], 252000, 260000, "model"))
        карточка = [w for w in written if "smetu" in w or "prislat" in w][0]
        text = _текст(os.path.join(self.vault, карточка))
        self.assertIn("· 04:12–04:20 · #%s" % self.сег[11][-8:], text)
        self.assertIn("evidence:\n  - %s 252000-260000" % self.сег[11], text)

    def test_повтор_проекции_не_дублирует_ссылки(self):
        self.извлечение()
        cp.run(self.eid, self.vault, self.root)
        cp.run(self.eid, self.vault, self.root)
        self.assertEqual(len(self.строки()), 2)

    def test_старое_извлечение_без_segment_id_строк_не_даёт(self):
        mi.write_json(mi.extraction_path(self.root, self.eid), EXTR)
        written = cp.run(self.eid, self.vault, self.root)
        self.assertEqual(self.строки(), [])
        text = _текст(os.path.join(self.vault, [w for w in written if "prislat" in w][0]))
        self.assertIn("· 04:12\n", text, "старая метка без диапазона и кода")
        self.assertNotIn("evidence:", text)

    def test_чужой_сегмент_отклоняется_в_аудит(self):
        другой, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "ev2",
            "occurred_at": EVENT["occurred"], "payload": {}})
        import call_asr
        call_asr.записать_сегменты(self.con, другой, None, [
            {"segment_id": "s0001", "start_ms": 0, "end_ms": 25000, "text": "чужое"}])
        чужой = self.con.execute("select id from transcript_segments where seq=1").fetchone()[0]
        extr = self.извлечение()
        extr["requests"][0]["evidence"] = [{"segment": "s0001", "segment_id": чужой,
                                            "start_ms": 0, "end_ms": 25000}]
        extr["commitments"][0]["evidence"][0]["end_ms"] = 999999      # за границами
        mi.write_json(mi.extraction_path(self.root, self.eid), extr)
        written = cp.run(self.eid, self.vault, self.root)
        self.assertEqual(self.строки(), [])
        # ADR п.3: пункт с отклонённой ссылкой — в ревью, карточки нет; в
        # волте только разговор, и никакой карточки с чужим кодом сегмента
        self.assertEqual([w for w in written if w.startswith("kb/commitments/")], [],
                         "карточка не рисует то, что реестр отверг")
        аудит = [dict(r) for r in self.con.execute(
            "select * from audit_events where action='evidence_rejected' order by id")]
        self.assertEqual(len(аудит), 2)
        self.assertEqual((аудит[0]["actor_type"], аудит[0]["actor_id"], аудит[0]["object_kind"],
                          аудит[0]["object_id"]), ("rule", "call_project", "event", self.eid))
        д = json.loads(аудит[0]["detail_json"])
        self.assertEqual((д["list"], д["item"], д["segment_id"]), ("requests", 1, чужой))
        self.assertEqual(sorted(д), ["end_ms", "item", "list", "segment_id", "start_ms", "why"])
        for r in аудит:
            self.assertNotIn("чужое", r["detail_json"])
            self.assertNotIn("смета", r["detail_json"])

    def test_повторная_проекция_отзывает_ссылки_пункта_ушедшего_в_ревью(self):
        """Codex по #122: после восстановления базы пункт, бывший карточкой,
        уходит в ревью — его прежние строки `evidence_refs` отзываются с
        аудитом; объект и файл карточки остаются (территория владельца)."""
        self.извлечение()
        written = cp.run(self.eid, self.vault, self.root)
        карточка = [w for w in written if "prislat" in w][0]
        oid = self.con.execute("select id from commitments where source_native_id=?",
                               ("commitment/%s/requests/1" % self.eid,)).fetchone()[0]
        self.assertEqual(len([r for r in self.строки() if r["object_id"] == oid]), 1)
        # «расшифровка переделана»: ссылка пункта теперь в чужой сегмент
        другой, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "ev9",
            "occurred_at": EVENT["occurred"], "payload": {}})
        import call_asr
        call_asr.записать_сегменты(self.con, другой, None, [
            {"segment_id": "s0001", "start_ms": 0, "end_ms": 25000, "text": "чужое"}])
        чужой = self.con.execute("select id from transcript_segments where seq=1").fetchone()[0]
        extr = self.извлечение()
        extr["requests"][0]["evidence"] = [{"segment": "s0001", "segment_id": чужой,
                                            "start_ms": 0, "end_ms": 25000}]
        mi.write_json(mi.extraction_path(self.root, self.eid), extr)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            cp.run(self.eid, self.vault, self.root)
        self.assertEqual([r for r in self.строки() if r["object_id"] == oid], [],
                         "отвергнутое больше не выдаётся за каноническое")
        self.assertEqual(len(self.строки()), 1, "второй пункт — на месте")
        self.assertIn("отозвано ссылок evidence: 1", err.getvalue())
        а = self.con.execute("select object_id, detail_json from audit_events where "
                             "action='evidence_withdrawn'").fetchone()
        self.assertEqual(а["object_id"], oid)
        self.assertEqual(json.loads(а["detail_json"])["refs"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.vault, карточка)), "файл не трогаем")
        self.assertEqual(self.con.execute("select count(*) from commitments").fetchone()[0], 2)
        # и повтор — без второго отзыва: отзывать уже нечего
        with contextlib.redirect_stderr(io.StringIO()):
            cp.run(self.eid, self.vault, self.root)
        self.assertEqual(self.con.execute("select count(*) from audit_events where "
                                          "action='evidence_withdrawn'").fetchone()[0], 1)

    def test_разделы_разговора_тоже_сверяются(self):
        """Codex по #122: `decisions`/`open_questions` рисуются в карточке
        разговора через `stamp` — чужой сегмент там тоже отклоняется."""
        другой, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "ev8",
            "occurred_at": EVENT["occurred"], "payload": {}})
        import call_asr
        call_asr.записать_сегменты(self.con, другой, None, [
            {"segment_id": "s0001", "start_ms": 0, "end_ms": 25000, "text": "чужое"}])
        чужой = self.con.execute("select id from transcript_segments where seq=1").fetchone()[0]
        extr = self.извлечение(decisions=[{"action": "договорились о цене", "explicit": True,
                                           "confidence": 0.9, "disposition": "task",
                                           "evidence": [{"segment": "s0001", "segment_id": чужой,
                                                         "start_ms": 0, "end_ms": 25000}]}])
        mi.write_json(mi.extraction_path(self.root, self.eid), extr)
        written = cp.run(self.eid, self.vault, self.root)
        conv = _текст(os.path.join(self.vault, [w for w in written
                                                if w.startswith("kb/conversations/")][0]))
        self.assertIn("договорились о цене", conv)
        self.assertNotIn("#" + чужой[-8:], conv, "чужой код в карточке разговора не рисуется")
        self.assertNotIn("договорились о цене · 00:00", conv,
                         "вместо отклонённой ссылки не выдумывается 00:00 (Codex, круг 2)")
        self.assertIn("- договорились о цене · на проверку\n", conv)
        а = [json.loads(r[0]) for r in self.con.execute(
            "select detail_json from audit_events where action='evidence_rejected'")]
        self.assertEqual([(x["list"], x["item"]) for x in а], [("decisions", 1)])

    def test_ссылка_без_интервала_равна_сегменту(self):
        """ADR п.1: подынтервал необязателен — без него ссылка равна сегменту."""
        extr = self.извлечение()
        extr["requests"][0]["evidence"] = [{"segment": "s0011", "segment_id": self.сег[11]}]
        mi.write_json(mi.extraction_path(self.root, self.eid), extr)
        written = cp.run(self.eid, self.vault, self.root)
        r = [x for x in self.строки() if x["segment_id"] == self.сег[11]][0]
        self.assertEqual((r["start_ms"], r["end_ms"]), (250000, 275000))
        text = _текст(os.path.join(self.vault, [w for w in written if "prislat" in w][0]))
        self.assertIn("· 04:10–04:35 · #%s" % self.сег[11][-8:], text)

    def test_список_evidence_читается_парсером(self):
        """Список во фронтматтере — машиночитаемый на деле: парсер карточек
        отдаёт его списком, а не пустой строкой (ревью PR #122)."""
        self.извлечение()
        written = cp.run(self.eid, self.vault, self.root)
        text = _текст(os.path.join(self.vault, [w for w in written if "prislat" in w][0]))
        fm, _ = cp.context_pack.mb.frontmatter(text)
        self.assertEqual(fm["evidence"], ["%s 252000-260000" % self.сег[11]])
        self.assertEqual(fm["audience"], ["mara"])


if __name__ == "__main__":
    unittest.main()
