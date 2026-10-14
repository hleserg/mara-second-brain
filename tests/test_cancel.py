"""Т7.6, ADR-0010 решение 8: отмена работы доходит до воркера — идущий шаг
убит, итог не записан, следующий шаг не поставлен, сверка не ставит заново."""
import contextlib, io, os, sqlite3, sys, tempfile, threading, time, unittest
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

    def работ(self):
        return self.con.execute("select count(*) from jobs where event_id=?",
                                (self.eid,)).fetchone()[0]

    def аренда_истекла(self):
        """Две отметки подряд упёрлись в занятую базу (или умер воркер):
        срок аренды в прошлом, попытка открыта."""
        self.con.execute("update jobs set lease_until=? where id=?",
                         (int(time.time()) - 1, self.jid))


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
        mi.отменить(self.con, self.jid)
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
        """Попытку умершего воркера `доотменить` закрывает брошенной —
        `lease_expired` без `finished`: длительность не выдумывается, в
        сумму длительностей не уходит весь простой демона (ревью #165)."""
        t = int(time.time())
        взята = mi.claim_job(self.con, now=t)
        self.assertEqual(mi.отменить(self.con, self.jid), "запрошена")
        self.assertEqual(self.состояние(), "ready")
        # аренда истекла — заново не берётся, а доводится
        поздно = t + mi.LEASE_SEC + 1
        self.assertIsNone(mi.claim_job(self.con, now=поздно))
        self.assertEqual(mi.доотменить(self.con, now=поздно), 1)
        self.assertEqual(self.состояние(), "cancelled")
        self.assertEqual(self.попытки(), [(взята["attempt"], "lease_expired")])
        self.assertIsNone(self.con.execute(
            "select finished from job_attempts where job_id=?", (self.jid,)).fetchone()[0])
        строки = contextd.metrics(self.con, self.dir, None).splitlines()
        self.assertIn('mara_job_attempts_abandoned_total{kind="project"} 1', строки)
        self.assertFalse([l for l in строки if 'outcome="cancelled"' in l], строки)

    def test_истёкшая_аренда_идущего_шага_не_отмена_сразу(self):
        """Шаг идёт, а срок аренды в прошлом (отметки упёрлись в занятую
        базу): попытка открыта — значит, шла. «Отменена» сразу дала бы потом
        `done` поверх `cancelled` и следующий шаг (ревью #165, P2)."""
        взята = mi.claim_job(self.con)
        self.аренда_истекла()
        self.assertEqual(mi.отменить(self.con, self.jid), "запрошена")
        # шаг успел до отметки — засчитан, как обещает `--cancel`
        self.assertEqual(contextd.закрыть_работу(self.con, взята, True, ""), "ок")
        self.assertEqual(self.состояние(), "done")
        self.assertEqual(self.работ(), 2, "следующий шаг поставлен")

    def test_сбой_после_просьбы_закрывается_отменой(self):
        """Флаг стоит, шаг упал раньше отметки: не ретрай и не DLQ у
        работы, которую владелец отменил, — `cancelled` (ревью #165)."""
        self.con.execute("update jobs set attempts=? where id=?", (len(mi.RETRY) - 1, self.jid))
        взята = mi.claim_job(self.con)
        self.assertEqual(mi.отменить(self.con, self.jid), "запрошена")
        self.assertEqual(contextd.закрыть_работу(self.con, взята, False, "упал"), "отменена")
        self.assertEqual(self.состояние(), "cancelled")
        self.assertEqual(self.попытки(), [(взята["attempt"], "cancelled")])
        self.assertEqual(self.con.execute("select attempts from jobs where id=?",
                                          (self.jid,)).fetchone()[0], len(mi.RETRY) - 1)
        self.assertEqual(self.работ(), 1)

    def test_итог_закрытой_работы_не_пишется(self):
        """`finish_job` и `закрыть_работу` пишут только в `ready`: отменённую
        не превратить в `done` со следующим шагом и не увести в ретрай."""
        self.assertEqual(mi.отменить(self.con, self.jid), "отменена")
        for ok in (True, False):
            mi.finish_job(self.con, self.jid, ok, "сбой")
            self.assertIsNone(contextd.закрыть_работу(
                self.con, {"id": self.jid, "kind": "project", "event_id": self.eid}, ok, ""))
        self.assertEqual(self.состояние(), "cancelled")
        self.assertEqual(self.con.execute("select attempts from jobs where id=?",
                                          (self.jid,)).fetchone()[0], 0)
        self.assertEqual(self.работ(), 1)


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
        self.assertIn("%s project %s начата" % (self.jid, self.eid), лог,
                      "id работы в журнале — по нему `--cancel`")
        self.assertIn("%s project %s отменена" % (self.jid, self.eid), лог)
        self.assertEqual(self.состояние(), "cancelled")
        self.assertFalse(os.path.exists(след))
        self.assertEqual(self.попытки(), [(job["attempt"], "cancelled")])
        self.assertEqual(self.работ(), 1, "следующий шаг (digest) не поставлен")

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
        self.assertEqual(self.попытки(), [(job["attempt"], "cancelled")])

    def test_брошенная_отмена_доводится_тактом(self):
        """Воркер взял работу и умер посреди шага (попытка открыта, аренда
        истекла), отмену попросили потом: первый же такт её доводит, а шаг
        заново не запускает."""
        взята = mi.claim_job(self.con)
        self.аренда_истекла()
        self.assertEqual(mi.отменить(self.con, self.jid), "запрошена")
        with mock.patch.object(contextd, "run_step", side_effect=AssertionError("запустил")):
            job, лог = self.такт()
        self.assertIsNone(job)
        self.assertEqual(self.состояние(), "cancelled")
        self.assertEqual(self.попытки(), [(взята["attempt"], "lease_expired")])
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
    def команда(self, *арг):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            код = contextd.main(["--root", self.dir, "--cancel", *арг])
        return код, out.getvalue()

    def test_cancel(self):
        self.con.commit()
        код, out = self.команда(self.jid)
        self.assertEqual((код, self.состояние()), (0, "cancelled"))
        self.assertIn("отменена", out)
        код, out = self.команда(self.jid)
        self.assertEqual(код, 1)
        self.assertIn("отменять нечего", out)
        self.assertEqual(self.команда("нет такой")[0], 1)

    def test_по_событию(self):
        """Id события печатают журнал воркера и сверка: живая работа у
        события одна — отменяется она; несколько — список и код 1."""
        код, out = self.команда(self.eid)
        self.assertEqual((код, self.состояние()), (0, "cancelled"))
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "к4", "payload": {}})
        a, b = mi.add_job(self.con, eid, "asr"), mi.add_job(self.con, eid, "nudge")
        код, out = self.команда(eid)
        self.assertEqual(код, 1)
        self.assertIn(a, out)
        self.assertIn(b, out)
        self.assertEqual({r[0] for r in self.con.execute(
            "select state from jobs where event_id=?", (eid,))}, {"ready"})

    def test_идущая_и_аренда(self):
        """Шаг числится идущим: текст честен и про живого воркера (до
        отметки), и про умершего (закроется после истечения аренды)."""
        mi.claim_job(self.con)
        код, out = self.команда(self.jid)
        self.assertEqual((код, self.состояние()), (0, "ready"))
        self.assertIn("отмена запрошена", out)
        self.assertIn("через", out)
        self.аренда_истекла()
        self.assertIn("она уже истекла", self.команда(self.jid)[1])

    def test_база_занята(self):
        with mock.patch.object(mi, "отменить",
                               side_effect=sqlite3.OperationalError("database is locked")):
            код, out = self.команда(self.jid)
        self.assertEqual(код, 2)
        self.assertIn("база занята", out)
        with mock.patch.object(mi, "отменить",
                               side_effect=sqlite3.OperationalError("no such table: jobs")):
            with self.assertRaises(sqlite3.OperationalError):
                self.команда(self.jid)


if __name__ == "__main__":
    unittest.main()
