"""Т9.1, тревоги: часовая сверка читает то, что `/metrics` только показывал.

Очередь, которую никто не берёт; попытки, брошенные посреди шага; телефон,
который давно не выходил на связь; демон, который не отвечает. Всё
спрашивается у базы или у демона в момент прогона — состояния между
прогонами нет, находка снимается сама, когда причина ушла.
"""
import os, socket, sys, tempfile, threading, time, unittest, unittest.mock
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
    """«Никто не берёт»: срок работы прошёл, а воркер за то же время не начал
    ни одной попытки. Ретраи (срок в будущем) и длинная очередь при живом
    воркере — не тревога."""

    def работа(self, sid, kind, срок_назад_ч, state="ready", аренда=0):
        jid = mi.add_job(self.con, self.событие(sid), kind)
        self.con.execute("update jobs set next_at=?, state=?, lease_until=? where id=?",
                         (int(time.time() - срок_назад_ч * 3600), state, аренда, jid))
        return jid

    def попытка(self, jid, часов_назад):
        self.con.execute("insert into job_attempts(job_id, attempt, started) "
                         "values(?, (select coalesce(max(attempt), 0) + 1 from "
                         "job_attempts where job_id=?), ?)",
                         (jid, jid, назад(hours=часов_назад).isoformat()))

    def test_воркер_не_берёт(self):
        self.работа("a", "asr", 5)
        self.работа("b", "asr", 4)
        self.работа("c", "extract", 1)                # срок прошёл недавно
        self.работа("d", "digest", -2)                # ретрай, срок впереди
        self.работа("e", "project", 30, state="dlq")  # DLQ — своя находка
        f = rc.очередь_стоит(self.con)
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["check"], f[0]["level"], f[0]["count"]),
                         ("очередь-стоит", "warn", 2), "считаются только просроченные")
        self.assertIn("asr — 2, срок прошёл 5 ч назад", f[0]["detail"])
        for нет in ("extract", "digest", "project"):
            self.assertNotIn(нет, f[0]["detail"])
        self.assertIn("/metrics", f[0]["detail"])
        self.assertNotIn("/v1/jobs", f[0]["detail"], "такого пути с петли нет")

    def test_длинная_очередь_при_живом_воркере(self):
        старая = self.работа("a", "asr", 10)
        self.попытка(старая, 10)
        другая = self.работа("b", "asr", 1)
        self.попытка(другая, 1)                       # воркер работает
        self.assertEqual(rc.очередь_стоит(self.con), [])

    def test_ретраи_не_тревога(self):
        """Лестница `RETRY` тянется больше суток с момента `created`; срок
        следующей попытки — в будущем."""
        jid = self.работа("a", "asr", -3)
        self.con.execute("update jobs set created=?, attempts=4 where id=?",
                         (назад(days=1).isoformat(timespec="seconds"), jid))
        self.assertEqual(rc.очередь_стоит(self.con), [])

    def test_в_аренде_не_тревога(self):
        self.работа("a", "asr", 5, аренда=int(time.time()) + 300)
        self.assertEqual(rc.очередь_стоит(self.con), [])

    def test_порог_из_параметра(self):
        self.работа("a", "asr", 2)
        self.assertEqual(rc.очередь_стоит(self.con), [])
        self.assertEqual(len(rc.очередь_стоит(self.con, hours=1)), 1)


class ШагиБрошены(База):
    def попытка(self, sid, kind, outcome, started, следующая=None):
        """Брошенную помечает повторная выдача, заводя попытку 2 — её старт и
        есть время пометки; по умолчанию — сразу же."""
        jid = mi.add_job(self.con, self.событие(sid), kind)
        self.con.execute("insert into job_attempts(job_id, attempt, started, outcome) "
                         "values(?, 1, ?, ?)", (jid, started, outcome))
        if outcome == "lease_expired":
            self.con.execute("insert into job_attempts(job_id, attempt, started) "
                             "values(?, 2, ?)", (jid, следующая or started))

    def test_брошенные_за_сутки_по_виду(self):
        self.попытка("a", "asr", "lease_expired", назад(hours=3).isoformat())
        # 30 ч назад — внутри окна в двое суток: сводка раз в сутки, и
        # суточное окно пропустило бы брошенное незадолго до неё
        self.попытка("b", "asr", "lease_expired", назад(hours=30).isoformat())
        self.попытка("c", "extract", "lease_expired", назад(hours=60).isoformat())  # давно
        self.попытка("d", "extract", "retry", назад(hours=1).isoformat())           # не брошена
        f = rc.шаги_брошены(self.con)
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["check"], f[0]["count"]), ("шаги-брошены", 2))
        self.assertIn("asr 2", f[0]["detail"])
        self.assertNotIn("extract", f[0]["detail"])

    def test_окно_от_пометки_а_не_от_старта(self):
        """Воркер лежал трое суток: попытку начали 80 ч назад, брошенной её
        признали час назад — находка должна дожить до сводки."""
        self.попытка("a", "asr", "lease_expired", назад(hours=80).isoformat(),
                     следующая=назад(hours=1).isoformat())
        f = rc.шаги_брошены(self.con)
        self.assertEqual(len(f), 1)
        self.assertIn("asr 1", f[0]["detail"])

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

    def test_новый_токен_без_событий_оживляет_имя(self):
        """Перевыпущенный токен ещё ничего не сдал, но на связи — имя живо."""
        self.устройство("dev_1", "телефон", назад(hours=100).isoformat(timespec="seconds"))
        self.событие("з1", device="dev_1")
        self.устройство("dev_2", "телефон", назад(hours=1).isoformat(timespec="seconds"))
        self.assertEqual(rc.телефон_молчит(self.con), [])

    def test_время_а_не_строка(self):
        """1 ч назад в −12:00 лексически меньше, чем 60 ч назад в +14:00."""
        self.устройство("dev_1", "телефон", назад(hours=60).astimezone(
            timezone(timedelta(hours=14))).isoformat(timespec="seconds"))
        self.событие("з1", device="dev_1")
        self.устройство("dev_2", "телефон", назад(hours=1).astimezone(
            timezone(timedelta(hours=-12))).isoformat(timespec="seconds"))
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
        self.addCleanup(setattr, mi, "ROOT", mi.ROOT)
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

    def отвечает(self, код):
        import http.server

        class Плохой(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(код)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), Плохой)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return rc.демон_не_отвечает("http://127.0.0.1:%d" % srv.server_address[1])

    def test_ответ_не_200_это_error(self):
        """`/healthz` отвечает, но не 200 — тоже тревога: 503 бросает
        `HTTPError`, а 204 (прокси, чужой слушатель) `urlopen` пропускает
        молча (Codex по #153)."""
        f = self.отвечает(503)
        self.assertEqual(f[0]["check"], "демон-не-отвечает")
        self.assertIn("HTTPError", f[0]["detail"])
        f = self.отвечает(204)
        self.assertEqual(f[0]["check"], "демон-не-отвечает")
        self.assertIn("код 204", f[0]["detail"])

    def test_кривой_адрес_не_выдаётся_за_лежащий_демон(self):
        # `http://` urllib отвергает `URLError`, а не `ValueError` (Codex по #153)
        for адрес in ("не-адрес", "http://[::1", "http://", "127.0.0.1:8788",
                      "ftp://127.0.0.1"):
            f = rc.демон_не_отвечает(адрес)
            self.assertEqual((f[0]["check"], f[0]["level"]),
                             ("демон-адрес-кривой", "warn"), адрес)

    def test_без_адреса_не_проверяем(self):
        self.assertEqual(rc.демон_не_отвечает(""), [])


class Крон(unittest.TestCase):
    """`main` зовёт проверку демона сам — и при нечитаемой базе, и первой."""

    def свободный_порт(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        порт = s.getsockname()[1]
        s.close()
        return порт

    def прогон(self, root, *ещё):
        import io, json, contextlib
        self.addCleanup(setattr, mi, "ROOT", mi.ROOT)
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", ["rc", "--root", root, "--json",
                                                      *ещё]), \
                contextlib.redirect_stdout(out):
            код = rc.main()
        return код, json.loads(out.getvalue())

    def test_лежащий_демон_первым(self):
        root = tempfile.mkdtemp()
        mi.connect(root).close()
        код, f = self.прогон(root, "--vault", tempfile.mkdtemp(), "--daemon-url",
                             "http://127.0.0.1:%d" % self.свободный_порт())
        self.assertEqual(f[0]["check"], "демон-не-отвечает", f)
        self.assertNotEqual(код, 0)

    def test_и_при_нечитаемой_базе(self):
        root = tempfile.mkdtemp()                     # базы нет вовсе
        _, f = self.прогон(root, "--daemon-url",
                           "http://127.0.0.1:%d" % self.свободный_порт())
        виды = [x["check"] for x in f]
        self.assertIn("демон-не-отвечает", виды)
        self.assertIn("база-нет", виды)


class Сверка(База):
    def test_run_зовёт_проверки_сам(self):
        """Проверка, которую `run()` не зовёт, не тревожит никого."""
        jid = mi.add_job(self.con, self.событие("a"), "asr")
        self.con.execute("update jobs set next_at=? where id=?",
                         (int(time.time()) - 10 * 3600, jid))
        self.con.execute("insert into job_attempts(job_id, attempt, started, outcome) "
                         "values(?, 1, ?, 'lease_expired')", (jid, назад(hours=30).isoformat()))
        # повторная выдача 20 ч назад: брошенная в окне, а воркер с тех пор молчит
        self.con.execute("insert into job_attempts(job_id, attempt, started) "
                         "values(?, 2, ?)", (jid, назад(hours=20).isoformat()))
        self.устройство("dev_тел", "телефон", назад(hours=60).isoformat(timespec="seconds"))
        виды = {x["check"] for x in rc.run(self.con, self.root, vault=None)}
        self.assertLessEqual({"очередь-стоит", "шаги-брошены", "телефон-молчит"}, виды)


if __name__ == "__main__":
    unittest.main()
