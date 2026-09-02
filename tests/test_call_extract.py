"""Правила извлечения (ТЗ §9, §20).

Модель тут не зовётся: она недетерминирована, а проверяем мы правила, а не её
настроение. На вход подаётся то, что модель могла бы вернуть.
"""
import os, sys, json, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_extract as ce

OCC = "2026-09-02T14:05:00+03:00"          # среда
SPAN = [{"start_ms": 0, "end_ms": 25000}]


class Правила(unittest.TestCase):
    def test_явная_просьба_становится_задачей(self):
        raw = {"requests": [{"action": "прислать смету", "requester": "Анна",
                             "owner": "sergey", "explicit": True, "confidence": 0.93,
                             "evidence": SPAN}]}
        out = ce.normalize(raw, OCC)
        self.assertEqual(out["requests"][0]["disposition"], "task")

    def test_предположение_не_становится_обязательством(self):
        raw = {"requests": [{"action": "может, покрасить стены", "explicit": False,
                             "confidence": 0.92, "evidence": SPAN}]}
        out = ce.normalize(raw, OCC)
        self.assertEqual(out["requests"][0]["disposition"], "needs-review",
                         "неявное не становится задачей даже при высокой уверенности")

    def test_середина_шкалы_идёт_на_проверку(self):
        raw = {"requests": [{"action": "что-то", "explicit": True, "confidence": 0.7,
                             "evidence": SPAN}]}
        self.assertEqual(ce.normalize(raw, OCC)["requests"][0]["disposition"],
                         "needs-review")

    def test_ниже_порога_не_создаётся(self):
        raw = {"requests": [{"action": "что-то", "confidence": 0.4, "evidence": SPAN}]}
        self.assertEqual(ce.normalize(raw, OCC)["requests"], [])

    def test_явный_дедлайн_парсится(self):
        raw = {"commitments": [{"action": "смета", "owner": "sergey", "confidence": 0.9,
                                "deadline_phrase": "до пятницы", "explicit": True,
                                "evidence": SPAN}]}
        out = ce.normalize(raw, OCC)
        self.assertEqual(out["commitments"][0]["due_at"], "2026-09-04")
        self.assertTrue(out["commitments"][0]["deadline_explicit"])

    def test_завтра_считается_от_времени_разговора(self):
        raw = {"commitments": [{"action": "перезвонить", "confidence": 0.9,
                                "explicit": True, "deadline_phrase": "завтра",
                                "evidence": SPAN}]}
        self.assertEqual(ce.normalize(raw, OCC)["commitments"][0]["due_at"], "2026-09-03")

    def test_размытый_дедлайн_не_выдумывается(self):
        raw = {"commitments": [{"action": "смета", "confidence": 0.9, "explicit": True,
                                "deadline_phrase": "побыстрее", "evidence": SPAN}]}
        out = ce.normalize(raw, OCC)
        self.assertIsNone(out["commitments"][0]["due_at"])
        self.assertFalse(out["commitments"][0]["deadline_explicit"])
        self.assertEqual(out["commitments"][0]["deadline_phrase"], "побыстрее",
                         "исходная фраза сохраняется, ТЗ §9")

    def test_пункт_без_спана_выбрасывается(self):
        raw = {"commitments": [{"action": "нечто", "confidence": 0.99, "evidence": []}]}
        self.assertEqual(ce.normalize(raw, OCC)["commitments"], [])

    def test_спан_без_начала_не_считается_спаном(self):
        raw = {"commitments": [{"action": "нечто", "confidence": 0.99,
                                "evidence": [{"end_ms": 10}]}]}
        self.assertEqual(ce.normalize(raw, OCC)["commitments"], [])

    def test_новое_поручение_вытесняет_старое_через_supersedes(self):
        raw = {"changed_instructions": [{"supersedes": "смета до пятницы",
                                         "new_state": "смету не надо, нужен счёт",
                                         "confidence": 0.9, "explicit": True,
                                         "evidence": SPAN}]}
        out = ce.normalize(raw, OCC)
        self.assertEqual(out["changed_instructions"][0]["supersedes"], "смета до пятницы")
        self.assertEqual(out["changed_instructions"][0]["new_state"],
                         "смету не надо, нужен счёт")

    def test_упомянутые_люди_и_проекты_переносятся(self):
        raw = {"people_mentioned": ["Анна"], "projects_mentioned": ["ремонт"]}
        out = ce.normalize(raw, OCC)
        self.assertEqual(out["people_mentioned"], ["Анна"])
        self.assertEqual(out["projects_mentioned"], ["ремонт"])

    def test_пустой_ответ_модели_не_ломает(self):
        out = ce.normalize({}, OCC)
        self.assertEqual(out["requests"], [])
        self.assertEqual(out["people_mentioned"], [])


class Промпт(unittest.TestCase):
    def test_транскрипт_печатается_со_спанами(self):
        segs = [{"segment_id": "s0001", "start_ms": 0, "end_ms": 25000,
                 "speaker": "unknown-A", "text": "привет"}]
        t = ce.transcript_text(segs)
        self.assertIn("s0001", t)
        self.assertIn("00:00", t)
        self.assertIn("привет", t)

    def test_схема_покрывает_поля_тз(self):
        props = ce.SCHEMA["properties"]
        for key in ("requests", "commitments", "decisions", "constraints",
                    "open_questions", "changed_instructions", "people_mentioned",
                    "projects_mentioned", "followups"):
            self.assertIn(key, props)


if __name__ == "__main__":
    unittest.main()
