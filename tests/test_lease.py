"""Т7.6, ADR-0010 решение 7 (N9 аудита): аренда работы не короче шага —
пока дочерний процесс жив, воркер продлевает её; час — и шаг убит."""
import contextlib, io, os, shutil, sqlite3, subprocess, sys, tempfile, time, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import contextd


def шаг(test, код):
    """Скрипт-шаг во временном каталоге: путь годится в `STEP` — `join`
    с абсолютным путём отдаёт его же."""
    d = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, d, True)
    p = os.path.join(d, "шаг.py")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(код)
    return p


class Продление(unittest.TestCase):
    def setUp(self):
        аренда = mock.patch.object(mi, "LEASE_SEC", 3)     # продление раз в секунду
        аренда.start()
        self.addCleanup(аренда.stop)

    def test_долгий_шаг_продлевает_аренду(self):
        p = шаг(self, "import time\ntime.sleep(2.6)\n")
        раз = []
        with mock.patch.dict(contextd.STEP, {"долгий": p}):
            ok, _ = contextd.run_step("долгий", "e1", lambda: раз.append(1))
        self.assertTrue(ok)
        self.assertGreaterEqual(len(раз), 2)

    def test_идущий_шаг_виден_для_остановки(self):
        """`погасить_шаг` гасит то, что в `_шаг`: пока шаг идёт, он там,
        кончился — его там нет."""
        p = шаг(self, "import time\ntime.sleep(1.6)\n")
        видел = []
        with mock.patch.dict(contextd.STEP, {"долгий": p}):
            contextd.run_step("долгий", "e1", lambda: видел.append(contextd._шаг) or True)
        self.assertTrue(видел and видел[0] is not None)
        self.assertIsNone(contextd._шаг)

    def убит(self, pid):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        return False

    def шаг_с_внуком(self):
        """Шаг, породивший внука с теми же трубами (как ffmpeg без своих),
        и номер внука в файле."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        файл = os.path.join(d, "внук")
        return шаг(self, "import subprocess, sys, time\n"
                         "в = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                         "open(%r, 'w').write(str(в.pid))\n"
                         "time.sleep(60)\n" % файл), файл

    def test_таймаут_убивает_шаг_и_внуков(self):
        """`kill` одного дочернего оставил бы внука держать трубы, и
        `communicate` ждал бы его минуту, а то и вечно."""
        p, файл = self.шаг_с_внуком()
        начало = time.monotonic()
        with mock.patch.dict(contextd.STEP, {"вечный": p}), \
                mock.patch.object(contextd, "ШАГ_ТАЙМАУТ", 2):
            ok, err = contextd.run_step("вечный", "e1")
        self.assertFalse(ok)
        self.assertIn("таймаут", err)
        self.assertLess(time.monotonic() - начало, 15)
        self.assertTrue(self.убит(int(open(файл).read())), "внук жив")

    def test_потерянная_аренда_останавливает_шаг(self):
        p, файл = self.шаг_с_внуком()
        начало = time.monotonic()
        with mock.patch.dict(contextd.STEP, {"долгий": p}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ok, err = contextd.run_step("долгий", "e1", lambda: False)
        self.assertIsNone(ok, "итог шага — не наш")
        self.assertEqual(err, contextd.АРЕНДА_ПОТЕРЯНА)
        self.assertLess(time.monotonic() - начало, 15)
        self.assertIn("аренда потеряна", out.getvalue())
        self.assertTrue(self.убит(int(open(файл).read())))

    def test_большой_вывод_не_вешает(self):
        """Труба полна, а шаг ещё пишет: `wait` встал бы навсегда."""
        p = шаг(self, "import sys\nsys.stderr.write('ж' * 300000 + 'КОНЕЦ')\nsys.exit(3)\n")
        # регресс к `wait` — не зависание прогона, а падение за секунды
        with mock.patch.dict(contextd.STEP, {"болтун": p}), \
                mock.patch.object(contextd, "ШАГ_ТАЙМАУТ", 20):
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

    def test_продлевается_только_своя(self):
        t = int(time.time())
        взята = mi.claim_job(self.con, now=t)
        self.assertEqual(взята["id"], self.jid)
        п = взята["attempt"]
        self.assertTrue(mi.продлить_аренду(self.con, self.jid, п, now=t + 300))
        self.assertEqual(self.аренда(), t + 300 + mi.LEASE_SEC)
        mi.finish_job(self.con, self.jid, True, попытка=п)
        self.assertFalse(mi.продлить_аренду(self.con, self.jid, п, now=t + 301),
                         "закрытая — нет")

    def test_взятая_другим_не_продлевается(self):
        """Ревью #161, P1: аренда истекла, работу взял второй захват — его
        `lease_until` снова в будущем, но продление первого обязано ответить
        «не твоя», иначе оба шага допишут итог."""
        t = int(time.time())
        первый = mi.claim_job(self.con, now=t)
        второй = mi.claim_job(self.con, now=t + mi.LEASE_SEC + 1)
        self.assertEqual(второй["id"], self.jid)
        self.assertFalse(mi.продлить_аренду(self.con, self.jid, первый["attempt"],
                                            now=t + mi.LEASE_SEC + 2))
        self.assertTrue(mi.продлить_аренду(self.con, self.jid, второй["attempt"],
                                           now=t + mi.LEASE_SEC + 2))

    def test_истёкшая_ничья_продлевается(self):
        """Две отметки подряд упёрлись в занятую базу, аренда истекла, но
        никто работу не взял: она всё ещё наша, и почти готовый шаг не
        убивается. После продления чужой захват её уже не возьмёт."""
        t = int(time.time())
        п = mi.claim_job(self.con, now=t)["attempt"]
        поздно = t + mi.LEASE_SEC + 100
        self.assertTrue(mi.продлить_аренду(self.con, self.jid, п, now=поздно))
        self.assertIsNone(mi.claim_job(self.con, now=поздно + 1))

    def test_граница_и_чужое_состояние(self):
        t = int(time.time())
        п = mi.claim_job(self.con, now=t)["attempt"]
        до = self.аренда()
        # ровно на границе захват другим в ту же секунду не проходит
        # (`lease_until<?` — строгое), а продление — проходит
        self.assertIsNone(mi.claim_job(self.con, now=до))
        self.assertTrue(mi.продлить_аренду(self.con, self.jid, п, now=до))
        # работа уже в DLQ, а попытка будто открыта — не продлевается
        self.con.execute("update jobs set state='dlq', lease_until=? where id=?",
                         (t + 10 ** 6, self.jid))
        self.assertFalse(mi.продлить_аренду(self.con, self.jid, п, now=t + 1))

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

    def test_потерянная_аренда_работу_не_закрывает(self):
        """Итог шага, чья аренда ушла другому, не пишется: работа остаётся
        как есть, без попытки и без DLQ."""
        with mock.patch.object(contextd, "run_step",
                               lambda *a: (None, contextd.АРЕНДА_ПОТЕРЯНА)), \
                mock.patch.dict(os.environ, {"MARA_ENV_FILE": "/nonexistent/x"}), \
                contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("MARA_ASR_URL", None)
            os.environ.pop("MARA_LLM_URL", None)
            contextd.такт(self.con)
        r = self.con.execute("select state, attempts from jobs where id=?",
                             (self.jid,)).fetchone()
        self.assertEqual((r[0], r[1]), ("ready", 0))

    def test_итог_после_чужого_захвата_не_пишется(self):
        """Ревью #161, P2: последнее продление прошло, потом работу взял
        другой, а шаг кончился сам до следующего витка — его успех не
        закрывает чужую работу и не заводит следующий шаг."""
        def run_step(kind, event_id, продлить):
            mi.claim_job(self.con, now=int(time.time()) + mi.LEASE_SEC + 1)
            return True, ""
        with mock.patch.object(contextd, "run_step", run_step), \
                mock.patch.dict(os.environ, {"MARA_ENV_FILE": "/nonexistent/x"}), \
                contextlib.redirect_stdout(io.StringIO()) as лог:
            os.environ.pop("MARA_ASR_URL", None)
            os.environ.pop("MARA_LLM_URL", None)
            contextd.такт(self.con)
        self.assertEqual(tuple(self.con.execute(
            "select state, attempts from jobs where id=?", (self.jid,)).fetchone()),
            ("ready", 0))
        self.assertEqual(self.con.execute("select count(*) from jobs").fetchone()[0], 1)
        self.assertIn("итог не записан", лог.getvalue())

    def test_остановка_демона_гасит_шаг(self):
        """Ревью #161, P3: шаг в своей сессии, и Ctrl-C до него не доходит —
        `serve` гасит идущий шаг сам, иначе он пишет сиротой."""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        with mock.patch.object(contextd, "_шаг", p):
            contextd.погасить_шаг()
        self.assertIsNotNone(p.wait(timeout=10))


if __name__ == "__main__":
    unittest.main()
