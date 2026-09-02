"""Формат дайджеста (ТЗ §16). Рендер без модели и без сети."""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_digest as cd

EVENT = {"id": "call_1", "occurred": "2026-09-02T14:05:00+03:00",
         "ended": "2026-09-02T14:23:11+03:00",
         "payload": {"contact_name": "Анна"}}
ПУСТО = {"requests": [], "commitments": [], "decisions": [], "open_questions": [],
         "changed_instructions": [], "constraints": [], "followups": []}


def с(**kw):
    d = dict(ПУСТО)
    d.update(kw)
    return d


class Рендер(unittest.TestCase):
    def test_заголовок_с_контактом_и_временем(self):
        text, _ = cd.render(EVENT, ПУСТО, 0)
        self.assertTrue(text.startswith("Звонок · Анна · 14:05–14:23"), text[:60])

    def test_пустые_разделы_не_печатаются(self):
        text, _ = cd.render(EVENT, ПУСТО, 0)
        self.assertNotIn("Попросили", text)
        self.assertNotIn("Ты обещал", text)

    def test_просьба_попадает_в_свой_раздел(self):
        e = с(requests=[{"action": "прислать смету", "disposition": "task",
                         "due_at": "2026-09-04",
                         "evidence": [{"start_ms": 252000, "end_ms": 260000}]}])
        text, items = cd.render(EVENT, e, 1)
        self.assertIn("Попросили", text)
        self.assertIn("прислать смету", text)
        self.assertIn("04:12", text, "у пункта есть метка времени для цитаты")
        self.assertEqual(items[0]["disposition"], "task")

    def test_возможная_задача_отдельным_разделом(self):
        e = с(requests=[{"action": "покрасить стены", "disposition": "needs-review",
                         "evidence": [{"start_ms": 60000, "end_ms": 61000}]}])
        text, items = cd.render(EVENT, e, 0)
        self.assertIn("Возможно задача", text)
        self.assertNotIn("Попросили", text, "непрошедшее порог идёт только в «возможно»")
        self.assertEqual(items[0]["disposition"], "needs-review")

    def test_созданные_задачи_считаются(self):
        text, _ = cd.render(EVENT, ПУСТО, 2)
        self.assertIn("Создано", text)
        self.assertIn("2 задачи", text)

    def test_одна_задача_склоняется(self):
        text, _ = cd.render(EVENT, ПУСТО, 1)
        self.assertIn("1 задача", text)

    def test_пять_задач_склоняются(self):
        text, _ = cd.render(EVENT, ПУСТО, 5)
        self.assertIn("5 задач", text)

    def test_изменение_показывает_что_на_что(self):
        e = с(changed_instructions=[{"action": "нужен договор",
                                     "supersedes": "прислать счёт",
                                     "new_state": "нужен договор",
                                     "disposition": "task",
                                     "evidence": [{"start_ms": 0, "end_ms": 1}]}])
        text, _ = cd.render(EVENT, e, 0)
        self.assertIn("Изменилось", text)
        self.assertIn("прислать счёт", text)
        self.assertIn("нужен договор", text)

    def test_срок_виден_в_строке(self):
        e = с(commitments=[{"action": "перезвонить", "disposition": "task",
                            "due_at": "2026-09-07",
                            "evidence": [{"start_ms": 0, "end_ms": 1}]}])
        text, _ = cd.render(EVENT, e, 1)
        self.assertIn("2026-09-07", text)

    def test_дайджест_не_содержит_расшифровки(self):
        e = с(requests=[{"action": "прислать смету", "disposition": "task",
                         "quote": "тут была бы вся расшифровка целиком",
                         "evidence": [{"start_ms": 0, "end_ms": 1}]}])
        text, _ = cd.render(EVENT, e, 1)
        self.assertNotIn("расшифровка целиком", text,
                         "в телеграм уходит выжимка, а не транскрипт (ТЗ §16)")


class Транспорт(unittest.TestCase):
    def test_без_токена_текст_не_теряется(self):
        state = cd.deliver("текст", token=None, chat_id=None)
        self.assertEqual(state, "no-transport",
                         "нет токена — дайджест остаётся в базе, а не пропадает")


if __name__ == "__main__":
    unittest.main()
