"""Карточки разговора и обязательств (ТЗ §10)."""
import os, sys, json, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_project as cp

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


if __name__ == "__main__":
    unittest.main()
