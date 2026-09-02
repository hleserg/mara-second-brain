"""Медленный тест с живой моделью на bigpc.

В обычный прогон не входит: коробка не круглосуточная, а тест ходит по сети и
греет видеокарту. Включается явно:

    MARA_LIVE=1 python3 -m unittest tests.test_call_extract_live -v

Он проверяет не качество формулировок, а то, ради чего затевалась схема:
модель возвращает JSON с нашими ключами, различает просьбу и обещание, и не
превращает «побыстрее» в дату.
"""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_extract as ce

SEGS = [
    {"segment_id": "s0001", "start_ms": 0, "end_ms": 25000,
     "text": "Серёж, привет, это Аня. Пришли мне смету по ремонту до пятницы, "
             "пожалуйста, иначе бригада не выйдет."},
    {"segment_id": "s0002", "start_ms": 23000, "end_ms": 48000,
     "text": "Хорошо, пришлю. И я перезвоню в понедельник, скажу по плитке."},
    {"segment_id": "s0003", "start_ms": 46000, "end_ms": 71000,
     "text": "И может быть покрасим стены, но это потом решим, побыстрее бы."},
]
OCC = "2026-09-02T14:05:00+03:00"


@unittest.skipUnless(os.environ.get("MARA_LIVE") == "1",
                     "нужен MARA_LIVE=1 и включённая GPU-коробка")
class Живая(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = ce.normalize(ce.ask_model(ce.transcript_text(SEGS)), OCC)

    def test_схема_соблюдена(self):
        for key in ce.LISTS + ce.NAMES:
            self.assertIn(key, self.out)

    def test_просьба_с_явным_сроком_разобрана(self):
        due = [r.get("due_at") for r in self.out["requests"] + self.out["commitments"]]
        self.assertIn("2026-09-04", due, "«до пятницы» должно стать датой")

    def test_побыстрее_не_стало_датой(self):
        for lst in ce.LISTS:
            for it in self.out[lst]:
                if (it.get("deadline_phrase") or "").strip() == "побыстрее":
                    self.assertIsNone(it["due_at"])

    def test_у_каждого_пункта_есть_спан(self):
        for lst in ce.LISTS:
            for it in self.out[lst]:
                self.assertTrue(it.get("evidence"), "пункт без спана не должен пройти")


if __name__ == "__main__":
    unittest.main()
