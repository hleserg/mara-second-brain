"""Приём: дедуп, аренда работ, расписание ретраев (ТЗ §17, §20)."""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi

EV = {"kind": "call", "source": "phone", "source_id": "call-1",
      "occurred_at": "2026-09-02T14:05:00+03:00",
      "blob": {"sha256": "a" * 64, "bytes": 10, "mime": "audio/m4a", "ext": "m4a"}}


class Ingest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)

    def test_повтор_того_же_аудио_не_создаёт_второй_звонок(self):
        first, dup1 = mi.put_event(self.con, dict(EV))
        second, dup2 = mi.put_event(self.con, dict(EV))
        self.assertEqual(first, second)
        self.assertFalse(dup1)
        self.assertTrue(dup2)

    def test_дедуп_по_хешу_а_не_по_имени_источника(self):
        mi.put_event(self.con, dict(EV))
        _, dup = mi.put_event(self.con, dict(EV, source_id="другое-имя-того-же-файла"))
        self.assertTrue(dup, "аудио с тем же sha256 — тот же звонок")

    def test_событие_без_аудио_дедупится_по_source_id(self):
        m = {"kind": "message", "source": "telegram", "source_id": "msg-7"}
        mi.put_event(self.con, dict(m))
        _, dup = mi.put_event(self.con, dict(m))
        self.assertTrue(dup)

    def test_разные_источники_не_склеиваются(self):
        a = {"kind": "message", "source": "telegram", "source_id": "7"}
        b = {"kind": "message", "source": "sms", "source_id": "7"}
        mi.put_event(self.con, dict(a))
        _, dup = mi.put_event(self.con, dict(b))
        self.assertFalse(dup, "одинаковый id у разных источников — разные события")

    def test_аренда_работы_не_отдаёт_её_дважды(self):
        eid, _ = mi.put_event(self.con, dict(EV))
        mi.add_job(self.con, eid, "asr")
        self.assertIsNotNone(mi.claim_job(self.con))
        self.assertIsNone(mi.claim_job(self.con), "работа под арендой")

    def test_расписание_ретраев_из_тз(self):
        for attempts, want in enumerate(mi.RETRY):
            got = mi.next_delay(attempts)
            self.assertLessEqual(abs(got - want), want * 0.2 + 1,
                                 "попытка %d: %d вместо ~%d" % (attempts, got, want))

    def test_после_шестой_попытки_dlq(self):
        eid, _ = mi.put_event(self.con, dict(EV))
        jid = mi.add_job(self.con, eid, "asr")
        for _ in range(7):
            job = mi.claim_job(self.con, now=2 ** 31)
            if job is None:
                break
            mi.finish_job(self.con, job["id"], False, "тестовая ошибка")
        state = self.con.execute("select state from jobs where id=?", (jid,)).fetchone()[0]
        self.assertEqual(state, "dlq")

    def test_успех_закрывает_работу(self):
        eid, _ = mi.put_event(self.con, dict(EV))
        jid = mi.add_job(self.con, eid, "asr")
        mi.finish_job(self.con, jid, True)
        state = self.con.execute("select state from jobs where id=?", (jid,)).fetchone()[0]
        self.assertEqual(state, "done")

    def test_просроченная_аренда_возвращает_работу(self):
        eid, _ = mi.put_event(self.con, dict(EV))
        mi.add_job(self.con, eid, "asr")
        job = mi.claim_job(self.con)
        self.assertIsNotNone(job)
        # воркер упал, не закрыв работу: через LEASE_SEC она снова свободна
        again = mi.claim_job(self.con, now=2 ** 31)
        self.assertIsNotNone(again, "аренда протухла — работу надо отдать другому")
        self.assertEqual(again["id"], job["id"])

    def test_путь_блоба_раскладывает_по_годам(self):
        p = mi.blob_path(self.dir, "b" * 64, "m4a")
        self.assertIn("/calls/", p)
        self.assertTrue(p.endswith("b" * 64 + ".m4a"))


if __name__ == "__main__":
    unittest.main()
