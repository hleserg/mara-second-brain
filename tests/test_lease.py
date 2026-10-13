"""Т7.6, ADR-0010 решение 7 (N9 аудита): аренда работы не короче шага —
пока дочерний процесс жив, воркер продлевает её; час — и шаг убит."""
import contextlib, io, os, sqlite3, sys, tempfile, time, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import contextd


def шаг(test, код):
    """Скрипт-шаг во временном каталоге: путь годится в `STEP` — `join`
    с абсолютным путём отдаёт его же."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "шаг.py")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(код)
    return p


class Продление(unittest.TestCase):
    def setUp(self):
        аренда = mock.patch.object(mi, "LEASE_SEC", 2)     # продление раз в секунду
        аренда.start()
        self.addCleanup(аренда.stop)

    def test_долгий_шаг_продлевает_аренду(self):
        p = шаг(self, "import time\ntime.sleep(2.6)\n")
        раз = []
        with mock.patch.dict(contextd.STEP, {"долгий": p}):
            ok, _ = contextd.run_step("долгий", "e1", lambda: раз.append(1))
        self.assertTrue(ok)
        self.assertGreaterEqual(len(раз), 2)

    def test_таймаут_убивает_шаг(self):
        p = шаг(self, "import time\ntime.sleep(60)\n")
        начало = time.monotonic()
        with mock.patch.dict(contextd.STEP, {"вечный": p}), \
                mock.patch.object(contextd, "ШАГ_ТАЙМАУТ", 1.5):
            ok, err = contextd.run_step("вечный", "e1")
        self.assertFalse(ok)
        self.assertIn("таймаут", err)
        self.assertLess(time.monotonic() - начало, 10)

    def test_большой_вывод_не_вешает(self):
        """Труба полна, а шаг ещё пишет: `wait` встал бы навсегда."""
        p = шаг(self, "import sys\nsys.stderr.write('ж' * 300000 + 'КОНЕЦ')\nsys.exit(3)\n")
        with mock.patch.dict(contextd.STEP, {"болтун": p}):
            ok, err = contextd.run_step("болтун", "e1", lambda: None)
        self.assertFalse(ok)
        self.assertTrue(err.endswith("КОНЕЦ"))

    def test_сбой_продления_не_роняет_шаг(self):
        p = шаг(self, "import time\ntime.sleep(1.6)\n")

        def занято():
            raise sqlite3.OperationalError("database is locked")
        with mock.patch.dict(contextd.STEP, {"долгий": p}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ok, _ = contextd.run_step("долгий", "e1", занято)
        self.assertTrue(ok)
        self.assertIn("аренда не продлена", out.getvalue())


class АрендаВБазе(unittest.TestCase):
    def setUp(self):
        self.con = mi.connect(tempfile.mkdtemp())
        self.addCleanup(self.con.close)
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "l1", "payload": {}})
        self.jid = mi.add_job(self.con, eid, "project")

    def аренда(self):
        return self.con.execute("select lease_until from jobs where id=?",
                                (self.jid,)).fetchone()[0]

    def test_продлевается_только_живая_своя(self):
        t = int(time.time())
        взята = mi.claim_job(self.con, now=t)
        self.assertEqual(взята["id"], self.jid)
        self.assertTrue(mi.продлить_аренду(self.con, self.jid, now=t + 300))
        self.assertEqual(self.аренда(), t + 300 + mi.LEASE_SEC)
        # истекла — могла уйти другому воркеру: не продлевается
        self.assertFalse(mi.продлить_аренду(self.con, self.jid,
                                            now=t + 300 + mi.LEASE_SEC + 1))
        mi.finish_job(self.con, self.jid, True)
        self.assertFalse(mi.продлить_аренду(self.con, self.jid, now=t + 301), "закрытая — нет")

    def test_такт_продлевает_аренду_своей_работы(self):
        было = []

        def run_step(kind, event_id, продлить):
            было.append(self.аренда())
            time.sleep(1.1)
            продлить()
            было.append(self.аренда())
            return True, ""
        with mock.patch.object(contextd, "run_step", run_step), \
                mock.patch.dict(os.environ, {"MARA_ENV_FILE": "/nonexistent/x"}), \
                contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("MARA_ASR_URL", None)
            os.environ.pop("MARA_LLM_URL", None)
            contextd.такт(self.con)
        self.assertGreater(было[1], было[0])


if __name__ == "__main__":
    unittest.main()
