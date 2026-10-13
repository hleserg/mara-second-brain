"""Т9.1, тревоги: часовая сверка читает то, что `/metrics` только показывал.

Очередь, которую никто не берёт; попытки, брошенные посреди шага; телефон,
который давно не выходил на связь; демон, который не отвечает. Всё
спрашивается у базы или у демона в момент прогона — состояния между
прогонами нет, находка снимается сама, когда причина ушла.
"""
import os, socket, sys, tempfile, threading, unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import contextd
import contextd_reconcile as rc


def назад(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw))


class База(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.con = mi.connect(self.root)
        self.addCleanup(self.con.close)

    def событие(self, sid, source="phone", device="dev_тел", via=None, kind="call"):
        payload = {"via": via} if via else {}
        eid, _ = mi.put_event(self.con, {"kind": kind, "source": source,
                                         "source_id": sid, "device_id": device,
                                         "payload": payload})
        return eid

    def устройство(self, dev, name, когда, revoked=False):
        self.con.execute("insert into devices(id, name, token_sha256, created, last_seen, "
                         "revoked_at) values(?,?,?,?,?,?)",
                         (dev, name, "h" + dev, когда, когда,
                          mi.now_iso() if revoked else None))


class ОчередьСтоит(База):
    def работа(self, sid, kind, created, state="ready"):
        jid = mi.add_job(self.con, self.событие(sid), kind)
        self.con.execute("update jobs set created=?, state=? where id=?",
                         (created, state, jid))

    def test_свежая_очередь_не_тревога(self):
        self.работа("a", "asr", назад(hours=2).isoformat(timespec="seconds"))
        self.assertEqual(rc.очередь_стоит(self.con), [])

    def test_старая_работа_называется_по_виду(self):
        self.работа("a", "asr", назад(hours=20).isoformat(timespec="seconds"))
        self.работа("b", "asr", назад(hours=1).isoformat(timespec="seconds"))
        self.работа("c", "extract", назад(hours=30).isoformat(timespec="seconds"))
        self.работа("d", "digest", назад(days=5).isoformat(timespec="seconds"),
                    state="dlq")                          # DLQ — своя находка
        f = rc.очередь_стоит(self.con)
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["check"], f[0]["level"], f[0]["count"]),
                         ("очередь-стоит", "warn", 3))
        self.assertIn("asr — 2, старейшей 20 ч", f[0]["detail"])
        self.assertIn("extract — 1, старейшей 30 ч", f[0]["detail"])
        self.assertNotIn("digest", f[0]["detail"])

    def test_возраст_по_времени_а_не_по_строке(self):
        # 10 ч назад в −12:00: по строке это «вчерашнее», по времени — свежее
        self.работа("a", "asr", назад(hours=10).astimezone(
            timezone(timedelta(hours=-12))).isoformat(timespec="seconds"))
        self.assertEqual(rc.очередь_стоит(self.con), [])
        # 20 ч назад в +14:00, того же вида: по строке «сегодняшнее» и больше
        # первой, по времени — старое; `min` по строке взял бы первую
        self.работа("b", "asr", назад(hours=20).astimezone(
            timezone(timedelta(hours=14))).isoformat(timespec="seconds"))
        self.assertEqual(len(rc.очередь_стоит(self.con)), 1)

    def test_порог_из_параметра(self):
        self.работа("a", "asr", назад(hours=5).isoformat(timespec="seconds"))
        self.assertEqual(len(rc.очередь_стоит(self.con, hours=4)), 1)


class ШагиБрошены(База):
    def попытка(self, sid, kind, outcome, started):
        jid = mi.add_job(self.con, self.событие(sid), kind)
        self.con.execute("insert into job_attempts(job_id, attempt, started, outcome) "
                         "values(?, 1, ?, ?)", (jid, started, outcome))

    def test_брошенные_за_сутки_по_виду(self):
        self.попытка("a", "asr", "lease_expired", назад(hours=3).isoformat())
        self.попытка("b", "asr", "lease_expired", назад(hours=5).isoformat())
        self.попытка("c", "extract", "lease_expired", назад(hours=30).isoformat())  # давно
        self.попытка("d", "extract", "retry", назад(hours=1).isoformat())           # не брошена
        f = rc.шаги_брошены(self.con)
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["check"], f[0]["count"]), ("шаги-брошены", 2))
        self.assertIn("asr 2", f[0]["detail"])
        self.assertNotIn("extract", f[0]["detail"])

    def test_нет_брошенных_нет_находки(self):
        self.попытка("a", "asr", "ok", назад(hours=1).isoformat())
        self.assertEqual(rc.шаги_брошены(self.con), [])


class ТелефонМолчит(База):
    def test_телефон_по_тому_что_сдавал(self):
        """Молчит давно и сдавал звонки — тревога. Мак, который шлёт только
        правки, и импортёр WhatsApp (`via: export`) телефонами не считаются,
        как бы давно ни молчали."""
        старое = назад(hours=60).isoformat(timespec="seconds")
        self.устройство("dev_тел", "телефон", старое)
        self.событие("звонок")
        self.устройство("dev_мак", "мак", старое)
        self.событие("правка", source="mara", device="dev_мак", kind="correction")
        self.устройство("dev_имп", "импорт", старое)
        self.событие("чат", source="whatsapp", device="dev_имп", via="export",
                     kind="message")
        f = rc.телефон_молчит(self.con)
        self.assertEqual([x["device"] for x in f], ["телефон"])
        self.assertEqual(f[0]["level"], "warn")
        self.assertIn("60 ч", f[0]["detail"])

    def test_на_связи_или_отозван_не_тревога(self):
        self.устройство("dev_тел", "телефон", назад(hours=3).isoformat(timespec="seconds"))
        self.событие("смс", source="sms", kind="message")
        self.устройство("dev_стар", "старый", назад(days=30).isoformat(timespec="seconds"),
                        revoked=True)
        self.событие("звонок", device="dev_стар")
        self.assertEqual(rc.телефон_молчит(self.con), [])

    def test_перевыпуск_токена_одно_имя(self):
        """Два устройства с одним именем (перевыпуск токена): молчит имя
        только тогда, когда молчат оба."""
        self.устройство("dev_1", "телефон", назад(hours=100).isoformat(timespec="seconds"))
        self.событие("з1", device="dev_1")
        self.устройство("dev_2", "телефон", назад(hours=1).isoformat(timespec="seconds"))
        self.событие("з2", device="dev_2")
        self.assertEqual(rc.телефон_молчит(self.con), [])


class ДемонНеОтвечает(unittest.TestCase):
    def test_живой_демон_молчит(self):
        каталог = tempfile.mkdtemp()
        mi.ROOT = каталог
        srv = contextd.make_server(каталог, port=0, vault=tempfile.mkdtemp())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertEqual(rc.демон_не_отвечает(
            "http://127.0.0.1:%d/" % srv.server_address[1]), [])

    def test_лежащий_демон_это_error(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        порт = s.getsockname()[1]
        s.close()                                   # порт свободен — отказ в соединении
        f = rc.демон_не_отвечает("http://127.0.0.1:%d" % порт)
        self.assertEqual((f[0]["check"], f[0]["level"]), ("демон-не-отвечает", "error"))
        self.assertIn("/healthz", f[0]["detail"])

    def test_ответ_не_200_это_error(self):
        """`/healthz` отвечает, но не 200 — тоже тревога (`HTTPError`)."""
        import http.server

        class Плохой(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Плохой)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        f = rc.демон_не_отвечает("http://127.0.0.1:%d" % srv.server_address[1])
        self.assertEqual(f[0]["check"], "демон-не-отвечает")
        self.assertIn("HTTPError", f[0]["detail"])

    def test_без_адреса_не_проверяем(self):
        self.assertEqual(rc.демон_не_отвечает(""), [])


class Сверка(База):
    def test_run_зовёт_проверки_сам(self):
        """Проверка, которую `run()` не зовёт, не тревожит никого."""
        jid = mi.add_job(self.con, self.событие("a"), "asr")
        self.con.execute("update jobs set created=? where id=?",
                         (назад(hours=40).isoformat(timespec="seconds"), jid))
        self.con.execute("insert into job_attempts(job_id, attempt, started, outcome) "
                         "values(?, 1, ?, 'lease_expired')", (jid, назад(hours=1).isoformat()))
        self.устройство("dev_тел", "телефон", назад(hours=60).isoformat(timespec="seconds"))
        виды = {x["check"] for x in rc.run(self.con, self.root, vault=None)}
        self.assertLessEqual({"очередь-стоит", "шаги-брошены", "телефон-молчит"}, виды)


if __name__ == "__main__":
    unittest.main()
