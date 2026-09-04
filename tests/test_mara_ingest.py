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

    def test_гонка_на_вставке_события_даёт_дубль_а_не_поломку(self):
        """N7. put_event делает select, потом insert. Телефон, проснувшись,
        досылает очередь разом: между этими двумя запросами успевает вставить
        другой запрос. Это дубль, а не 500 в ответ телефону."""
        второй = mi.connect(self.dir)

        class Опережающий:
            """Соединение, которое перед insert пускает вперёд конкурента."""

            def __init__(self, con):
                self.con, self.сработал = con, False

            def execute(self, sql, args=()):
                if sql.lstrip().startswith("insert into events") and not self.сработал:
                    self.сработал = True
                    mi.put_event(второй, dict(EV))
                return self.con.execute(sql, args)

        обгон = Опережающий(self.con)
        eid, dup = mi.put_event(обгон, dict(EV))
        self.assertTrue(обгон.сработал, "конкурент обязан был вклиниться")
        self.assertTrue(dup, "чужая вставка того же ключа — дубль")
        self.assertEqual(self.con.execute("select count(*) from events").fetchone()[0], 1)
        self.assertEqual(self.con.execute("select id from events").fetchone()["id"], eid)


class ГонкаЗаАренду:
    """Соединение, пускающее второго воркера ровно между `select` и `update`.

    Иначе гонку не поймать: она живёт в микросекундах между двумя запросами, и
    тест на потоках зеленел бы через раз. Здесь окно открывается руками.
    """

    def __init__(self, con, второй=None, в_щели=None):
        self.con, self.второй, self.влез = con, второй, False
        self.в_щели = в_щели

    def execute(self, sql, args=()):
        if sql.startswith("update jobs set lease_until") and not self.влез:
            self.влез = True
            if self.в_щели:
                self.в_щели(self.con)
            else:
                mi.claim_job(self.второй)
        return self.con.execute(sql, args)


class Работы(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.eid, _ = mi.put_event(self.con, dict(EV))

    def test_аренду_не_получают_двое_в_одну_щель(self):
        mi.add_job(self.con, self.eid, "asr")
        второй = mi.connect(self.dir)
        гонка = ГонкаЗаАренду(self.con, второй)
        self.assertIsNone(mi.claim_job(гонка),
                          "работа выдана обоим: аренда взята без проверки")
        self.assertTrue(гонка.влез, "щель не открылась — тест ничего не проверил")

    def test_доигравшему_не_выдают_уже_отработавшую(self):
        # аренда истекла на середине транскрипта, второй воркер работу выбрал,
        # а первый в этот момент доиграл: `finish_job` ставит `done` и сбрасывает
        # `lease_until` в ноль, не глядя на аренду. По одному `lease_until<?`
        # второй увидел бы `0 < now` и час GPU ушёл бы на уже сделанное
        jid = mi.add_job(self.con, self.eid, "asr")
        гонка = ГонкаЗаАренду(self.con, в_щели=lambda c: c.execute(
            "update jobs set state='done', lease_until=0 where id=?", (jid,)))
        self.assertIsNone(mi.claim_job(гонка),
                          "выдана отработавшая работа: в аренде нет проверки state")
        self.assertTrue(гонка.влез, "щель не открылась — тест ничего не проверил")

    def test_вторая_работа_того_же_вида_не_заводится(self):
        a = mi.add_job(self.con, self.eid, "asr")
        b = mi.add_job(self.con, self.eid, "asr")
        self.assertEqual(a, b, "повтор должен вернуть ту же работу")
        self.assertEqual(1, self.con.execute(
            "select count(*) from jobs where event_id=? and kind='asr'",
            (self.eid,)).fetchone()[0], "вторая расшифровка — второй час GPU")

    def test_работы_разных_видов_не_мешают_друг_другу(self):
        mi.add_job(self.con, self.eid, "asr")
        mi.add_job(self.con, self.eid, "extract")
        self.assertEqual(2, self.con.execute(
            "select count(*) from jobs where event_id=?", (self.eid,)).fetchone()[0])

    def test_поверх_отработавшей_работа_ставится_заново(self):
        jid = mi.add_job(self.con, self.eid, "asr")
        self.con.execute("update jobs set state='done' where id=?", (jid,))
        новая = mi.add_job(self.con, self.eid, "asr")
        self.assertNotEqual(jid, новая,
                            "починка руками поверх done не должна блокироваться")

    def test_поверх_брошенной_в_dlq_тоже(self):
        jid = mi.add_job(self.con, self.eid, "asr")
        self.con.execute("update jobs set state='dlq' where id=?", (jid,))
        self.assertNotEqual(jid, mi.add_job(self.con, self.eid, "asr"),
                            "сверка чинит именно dlq — ей нельзя мешать")


if __name__ == "__main__":
    unittest.main()
