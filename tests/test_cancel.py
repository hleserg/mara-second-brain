"""Т7.6, ADR-0010 решение 8: отмена работы доходит до воркера — идущий шаг
убит, итог не записан, следующий шаг не поставлен, сверка не ставит заново."""
import contextlib, io, os, sys, tempfile, threading, time, unittest
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import mara_ingest as mi
import contextd
import contextd_reconcile as cr


class _База(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.addCleanup(self.con.close)
        окружение = mock.patch.dict(os.environ, {"MARA_ENV_FILE": "/nonexistent/x"})
        окружение.start()
        self.addCleanup(окружение.stop)
        for k in ("MARA_ASR_URL", "MARA_LLM_URL"):
            os.environ.pop(k, None)
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "к1", "payload": {}})
        self.eid = eid
        self.jid = mi.add_job(self.con, eid, "project")

    def состояние(self):
        return self.con.execute("select state from jobs where id=?",
                                (self.jid,)).fetchone()[0]

    def попытки(self):
        return [tuple(r) for r in self.con.execute(
            "select attempt, outcome from job_attempts where job_id=? order by attempt",
            (self.jid,))]


class Отменить(_База):
    def test_не_шла_закрыта_сразу(self):
        self.assertEqual(mi.отменить(self.con, self.jid), "отменена")
        self.assertEqual(self.состояние(), "cancelled")
        self.assertIsNone(mi.claim_job(self.con))
        self.assertEqual(mi.отменить(self.con, self.jid), "cancelled", "повтор — уже отменена")
        self.assertIsNone(mi.отменить(self.con, "нет такой"))

    def test_отменённая_голова_не_держит_очередь(self):
        """Отмену просили у шедшей работы, её аренда истекла, а `доотменить`
        ещё не прошёл — следующая готовая работа всё равно берётся."""
        t = int(time.time())
        mi.claim_job(self.con, now=t)
        mi.отменить(self.con, self.jid, now=t + 1)
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "к3", "payload": {}})
        вторая = mi.add_job(self.con, eid, "project")
        self.con.execute("update jobs set next_at=? where id=?", (t + 5, вторая))
        self.assertEqual(mi.claim_job(self.con, now=t + mi.LEASE_SEC + 10)["id"], вторая)

    def test_отработавшую_не_отменить(self):
        взята = mi.claim_job(self.con)
        mi.finish_job(self.con, self.jid, True, попытка=взята["attempt"])
        self.assertEqual(mi.отменить(self.con, self.jid), "done")
        self.assertEqual(self.состояние(), "done")

    def test_идущую_воркер_умер_доводится(self):
        t = int(time.time())
        взята = mi.claim_job(self.con, now=t)
        self.assertEqual(mi.отменить(self.con, self.jid, now=t + 1), "запрошена")
        self.assertEqual(self.состояние(), "ready")
        # аренда истекла — заново не берётся, а доводится
        поздно = t + mi.LEASE_SEC + 1
        self.assertIsNone(mi.claim_job(self.con, now=поздно))
        self.assertEqual(mi.доотменить(self.con, now=поздно), 1)
        self.assertEqual(self.состояние(), "cancelled")
        self.assertEqual(self.попытки(), [(взята["attempt"], "cancelled")])


class Воркер(_База):
    def такт(self):
        with contextlib.redirect_stdout(io.StringIO()) as лог:
            job = contextd.такт(self.con)
        return job, лог.getvalue()

    def test_идущий_шаг_убит_итог_не_записан(self):
        """Настоящий шаг отдельным процессом: отмена доходит до него при
        следующей отметке аренды (`LEASE_SEC` = 3 — раз в секунду)."""
        след = os.path.join(self.dir, "дошёл")
        шаг = os.path.join(self.dir, "шаг.py")
        with open(шаг, "w") as fh:
            fh.write("import time, sys\ntime.sleep(30)\nopen(%r, 'w').close()\n" % след)
        аренда = mock.patch.object(mi, "LEASE_SEC", 3)
        аренда.start()
        self.addCleanup(аренда.stop)

        def отменить_позже():
            time.sleep(0.5)
            mi.отменить(mi.connect(self.dir), self.jid)
        threading.Thread(target=отменить_позже, daemon=True).start()
        начало = time.monotonic()
        with mock.patch.dict(contextd.STEP, {"project": шаг}):
            job, лог = self.такт()
        self.assertLess(time.monotonic() - начало, 10, "шаг ждали до конца")
        self.assertEqual(job["id"], self.jid)
        self.assertIn("отменена", лог)
        self.assertEqual(self.состояние(), "cancelled")
        self.assertFalse(os.path.exists(след))
        self.assertEqual(self.попытки(), [(job["attempt"], "cancelled")])
        self.assertEqual(self.con.execute("select count(*) from jobs where event_id=?",
                                          (self.eid,)).fetchone()[0], 1,
                         "следующий шаг (digest) не поставлен")

    def test_отменили_между_захватом_и_запуском(self):
        настоящий = mi.claim_job

        def захват(con, *a, **kw):
            job = настоящий(con, *a, **kw)
            if job:
                mi.отменить(con, job["id"])
            return job
        with mock.patch.object(mi, "claim_job", захват), \
                mock.patch.object(contextd, "run_step", side_effect=AssertionError("запустил")):
            job, лог = self.такт()
        self.assertEqual(self.состояние(), "cancelled")

    def test_брошенная_отмена_доводится_тактом(self):
        t = int(time.time())
        mi.claim_job(self.con, now=t - mi.LEASE_SEC - 10)
        self.con.execute("update jobs set cancel_requested=? where id=?",
                         (mi.now_iso(), self.jid))
        with mock.patch.object(contextd, "run_step", side_effect=AssertionError("запустил")):
            job, лог = self.такт()
        self.assertIsNone(job)
        self.assertEqual(self.состояние(), "cancelled")
        self.assertIn("после смерти воркера", лог)


class Сверка(_База):
    def test_отменённое_извлечение_не_ставится_заново(self):
        # своё событие: у события из setUp есть готовый `project`, и сверка
        # сочла бы его занятым и без отмены
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "к2", "payload": {}})
        os.makedirs(os.path.join(self.dir, "transcripts"))
        open(os.path.join(self.dir, "transcripts", eid + ".jsonl"), "w").close()
        jid = mi.add_job(self.con, eid, "extract")
        self.assertEqual(mi.отменить(self.con, jid), "отменена")
        self.assertEqual(cr.транскрипт_без_извлечения(self.con, self.dir), [])
        self.assertEqual(self.con.execute(
            "select count(*) from jobs where event_id=? and kind='extract'",
            (eid,)).fetchone()[0], 1)


class Команда(_База):
    def test_cancel(self):
        self.con.commit()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            код = contextd.main(["--root", self.dir, "--cancel", self.jid])
        self.assertEqual((код, self.состояние()), (0, "cancelled"))
        self.assertIn("отменена", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            код = contextd.main(["--root", self.dir, "--cancel", self.jid])
        self.assertEqual(код, 1)
        self.assertIn("отменять нечего", out.getvalue())


if __name__ == "__main__":
    unittest.main()
