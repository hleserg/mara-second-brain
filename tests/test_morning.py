"""Утро v1 (Т-У.1, `docs/morning-brief.md`): блок дел перед сводкой.

Правила утра проверяются здесь, а не глазами в телеграме: не больше трёх
пунктов, сначала дела с датой, неподтверждённое старше трёх дней не мелькает,
просроченное одно и без слова «просрочено», у пункта источник и время,
пустой день — одна строка, а не тишина.
"""
import os, sys, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from test_existing_scripts import load                     # noqa: E402

TODAY = "2026-10-05"


def card(v, name, title, status="proposed", due=None,
         occurred="2026-10-04 11:04:00+03:00", origin="call/ev1", valid_from=None):
    d = os.path.join(v, "kb/commitments")
    os.makedirs(d, exist_ok=True)
    fm = ["title: %s" % title, "type: commitment", "status: %s" % status,
          "occurred: %s" % occurred, "origin: %s" % origin,
          "sensitive: true", "cloud_allowed: false"]
    if due: fm.append("due: %s" % due)
    if valid_from: fm.append("valid_from: %s" % valid_from)
    with open(os.path.join(d, name + ".md"), "w", encoding="utf-8") as fh:
        fh.write("---\n" + "\n".join(fm) + "\n---\n\n- Обещание: %s\n" % title)


class Утро(unittest.TestCase):
    def setUp(self):
        self.ds = load("daily-summary.py")
        self.v = tempfile.mkdtemp()

    def утро(self):
        return self.ds.morning(self.v, TODAY)

    def test_пустой_день_одна_строка(self):
        self.assertEqual(self.утро(), "С датой на сегодня ничего.")

    def test_сначала_дело_с_датой_потом_это_твоё(self):
        card(self.v, "a", "Перезвонить в сервис")
        card(self.v, "b", "Отправить смету", status="open", due=TODAY,
             occurred="2026-10-03T14:20:00+03:00")
        got = self.утро()
        self.assertLess(got.index("Отправить смету"), got.index("Перезвонить в сервис"))
        self.assertIn("Это твоё?", got)
        self.assertIn("1. Отправить смету", got)
        self.assertIn("2. Перезвонить в сервис", got)

    def test_не_больше_трёх_остальное_цифрой(self):
        for i in range(5):
            card(self.v, "c%d" % i, "Дело номер %d" % i,
                 occurred="2026-10-04T1%d:00:00+03:00" % i)
        got = self.утро()
        self.assertEqual(sum(("Дело номер %d" % i) in got for i in range(5)), 3)
        self.assertIn("Ещё 2 ждут — не сегодня.", got)

    def test_неподтверждённое_старше_трёх_дней_не_мелькает(self):
        card(self.v, "old", "Старая догадка", occurred="2026-10-01T10:00:00+03:00")
        got = self.утро()
        self.assertNotIn("Старая догадка", got)
        self.assertNotIn("ждут", got, "старое не попадает и в счётчик")

    def test_подтверждённое_без_даты_ждёт_в_счётчике(self):
        card(self.v, "o", "Подтверждённое", status="open",
             occurred="2026-09-01T10:00:00+03:00")
        got = self.утро()
        self.assertNotIn("Подтверждённое", got)
        self.assertIn("Ещё 1 ждут", got)

    def test_просроченное_одно_и_без_стыда(self):
        card(self.v, "p1", "Первый хвост", status="open", due="2026-10-01")
        card(self.v, "p2", "Второй хвост", status="open", due="2026-10-03")
        got = self.утро()
        self.assertIn("Второй хвост", got)         # ближайший к сегодня
        self.assertNotIn("Первый хвост", got)
        self.assertIn("Это ещё нужно?", got)
        self.assertNotIn("проср", got.lower())

    def test_у_пункта_источник_и_время(self):
        card(self.v, "a", "Перезвонить в сервис")
        card(self.v, "b", "Купить билеты", status="open", due=TODAY,
             occurred="2026-10-02T09:30:00+03:00", origin="correction/ev2")
        got = self.утро()
        self.assertIn("звонок вчера, 11:04", got)
        self.assertIn("сказано Маре 02.10, 09:30", got)
        self.assertIn("срок сегодня", got)

    def test_закрытое_и_отменённое_не_показываем(self):
        card(self.v, "d", "Сделанное", status="done", due=TODAY)
        card(self.v, "x", "Отменённое", status="cancelled", due=TODAY)
        self.assertEqual(self.утро(), "С датой на сегодня ничего.")

    def test_вчера_закрыто(self):
        card(self.v, "d", "Сделанное", status="done",
             valid_from="2026-10-04T18:00:00+03:00")
        card(self.v, "e", "Давнее", status="done",
             valid_from="2026-09-20T18:00:00+03:00")
        self.assertEqual(self.ds.closed(self.v, "2026-10-04"), 1)


class КороткаяСводка(unittest.TestCase):
    def test_не_больше_трёх_строк_от_модели(self):
        ds = load("daily-summary.py")
        got = ds.three("- раз\n- два\n- три\n- четыре\n- пять")
        self.assertEqual(got, "- раз\n- два\n- три")


if __name__ == "__main__":
    unittest.main()
