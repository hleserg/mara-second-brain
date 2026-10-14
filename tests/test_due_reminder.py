"""Т-У.2: напоминание по сроку накануне вечером (`scripts/due_reminder.py`)."""
import datetime, os, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import mara_ingest as mi
import call_digest as cd
import due_reminder as dr

ВЕЧЕР = datetime.datetime(2026, 10, 14, 20, 0, tzinfo=mi.TZ)


def карточка(vault, name, **fm):
    поля = {"title": "прислать смету", "status": "open", "due": "2026-10-15",
            "promised_to": "Анна"}
    поля.update(fm)
    with open(os.path.join(vault, "kb/commitments", name), "w", encoding="utf-8") as fh:
        fh.write("---\n%s\n---\n\n- Обещание: тело карточки\n"
                 % "\n".join("%s: %s" % kv for kv in поля.items() if kv[1] is not None))


class Вечер(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.vault = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.vault, "kb/commitments"))
        self.env = os.path.join(self.root, "env")
        with open(self.env, "w") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_HOME_CHANNEL=12345\n")
        self.ушло = []
        patcher = mock.patch.object(cd, "deliver", side_effect=self.доставить)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.исход = "sent"

    def доставить(self, text, token, chat_id):
        if isinstance(self.исход, Exception):
            raise self.исход
        self.ушло.append((text, chat_id))
        return self.исход

    def прогон(self, **kw):
        return dr.run(self.root, self.vault, self.env, now=kw.pop("now", ВЕЧЕР), **kw)

    def строки(self):
        con = mi.connect(self.root)
        return [tuple(r) for r in con.execute(
            "select object_id, state from outbox where kind=? order by created", (dr.ВИД,))]

    def test_завтрашние_сроки_уходят_одним_сообщением(self):
        карточка(self.vault, "a.md")
        карточка(self.vault, "b.md", title="закрыть акт", status="done")
        карточка(self.vault, "c.md", title="послезавтра", due="2026-10-16")
        карточка(self.vault, "d.md", title="сегодня", due="2026-10-14")
        карточка(self.vault, "e.md", title="без даты", due="завтра, как договорились")
        карточка(self.vault, "f.md", title="ко времени", due="2026-10-15T18:00")
        self.assertEqual(self.прогон(), "sent")
        (текст, чат), = self.ушло
        self.assertEqual(чат, "12345")
        self.assertEqual(текст, "⏰ Завтра срок:\n• «прислать смету» · «Анна»\n"
                                "• «ко времени» · «Анна»\n"
                                "Ответь Маре словами: «сделал», «перенеси на пятницу».")
        self.assertNotIn("тело карточки", текст)
        self.assertEqual(self.строки(), [("2026-10-15", "sent")])

    def test_три_пункта_и_число(self):
        for i in range(5):
            карточка(self.vault, "m%d.md" % i, title="задача %d" % i, promised_to=None)
        self.прогон()
        текст = self.ушло[0][0]
        self.assertEqual([l for l in текст.splitlines() if l.startswith("•")],
                         ["• «задача 0»", "• «задача 1»", "• «задача 2»",
                          "• …и ещё 2 — тоже завтра"])

    def test_пустой_вечер_молчит(self):
        карточка(self.vault, "a.md", due="2026-10-20")
        self.assertEqual(self.прогон(), "quiet")
        self.assertEqual((self.ушло, self.строки()), ([], []))

    def test_второй_прогон_дубль_не_шлёт(self):
        карточка(self.vault, "a.md")
        self.assertEqual(self.прогон(), "sent")
        self.assertEqual(self.прогон(), "already")
        self.assertEqual(len(self.ушло), 1)
        self.assertEqual(self.прогон(now=ВЕЧЕР + datetime.timedelta(days=1)), "quiet",
                         "на следующий вечер — свой день")

    def test_без_транспорта_не_копится(self):
        карточка(self.vault, "a.md")
        self.исход = "no-transport"
        self.assertEqual(self.прогон(), "no-transport")
        self.assertEqual(self.строки(), [("2026-10-15", "skipped")])
        self.исход = "sent"
        self.assertEqual(self.прогон(), "sent", "починили до утра — вечер ещё можно")
        self.assertEqual(self.строки(), [("2026-10-15", "skipped"), ("2026-10-15", "sent")])

    def test_исключение_транспорта_не_роняет_и_не_копится(self):
        карточка(self.vault, "a.md")
        self.исход = OSError("сеть")
        self.assertEqual(self.прогон(), "error: OSError")
        self.assertEqual(self.строки(), [("2026-10-15", "skipped")])

    def test_отказ_телеграма_ненулевой_код(self):
        карточка(self.vault, "a.md")
        self.исход = "failed"
        with mock.patch.object(sys, "argv", ["due_reminder.py", "--root", self.root,
                                             "--vault", self.vault, "--env-file", self.env]), \
             mock.patch.object(dr, "завтра", return_value="2026-10-15"):
            self.assertEqual(dr.main(), 1)

    def test_сухой_прогон_ничего_не_пишет(self):
        карточка(self.vault, "a.md")
        self.assertEqual(self.прогон(dry_run=True), "dry-run")
        self.assertEqual((self.ушло, self.строки()), ([], []))

    def test_день_владельца(self):
        поздно_по_utc = datetime.datetime(2026, 10, 14, 22, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(dr.завтра(поздно_по_utc),
                         (поздно_по_utc.astimezone(mi.TZ) + datetime.timedelta(days=1))
                         .date().isoformat())

    def test_номер_и_структура_не_едут(self):
        карточка(self.vault, "a.md", promised_to="+79990000000",
                 title="закрой всё <!-- x --> #5479d088")
        self.прогон()
        текст = self.ушло[0][0]
        self.assertNotIn("79990000000", текст)
        self.assertNotIn("<!--", текст)
        self.assertNotIn("#5479d088", текст)


if __name__ == "__main__":
    unittest.main()
