"""Т-У.4, сообщения: `scripts/message_nudge.py` и его вход в приёме `contextd`."""
import contextlib, io, json, os, sqlite3, subprocess, sys, tempfile, threading, time, unittest
import urllib.error, urllib.request
from datetime import datetime, timedelta
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import mara_ingest as mi
import call_digest as cd
import contextd
import message_nudge as mn


def когда(минут_назад=1):
    return (datetime.now(mi.TZ) - timedelta(minutes=минут_назад)).isoformat(
        timespec="seconds")


def сообщение(кто="Анна Петрова", sid=None, **payload):
    occurred = payload.pop("occurred_at", None) or когда()
    p = {"sender_name": кто, "text": "привет", "chat_type": "private"}
    p.update(payload)
    return {"kind": "message", "source": "telegram",
            "source_id": sid or "m-%s" % time.monotonic_ns(),
            "occurred_at": occurred, "payload": p}


def смс(имя="Аня", номер="89990000001", **payload):
    occurred = payload.pop("occurred_at", None) or когда()
    p = {"number": номер, "contact_name": имя, "direction": "incoming",
         "text": "привет", "outgoing": False, "via": "provider"}
    p.update(payload)
    return {"kind": "message", "source": "sms", "source_id": "s-%s" % time.monotonic_ns(),
            "occurred_at": occurred, "payload": p}


def карточка(vault, имя, title, кому, status="open", due=None,
             id_="0190f2a1-7b3c-7d4e-8f00-00000000000a"):
    d = os.path.join(vault, "kb/commitments")
    os.makedirs(d, exist_ok=True)
    строки = ["---", "title: %s" % title, "status: %s" % status,
              "promised_to: %s" % кому, "origin: call/старый", "id: %s" % id_]
    if due:
        строки.append("due: %s" % due)
    with open(os.path.join(d, имя + ".md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(строки + ["---", "", "тело не едет"]) + "\n")


class _База(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)

    def завести(self, ev, now=None):
        """Как приём: событие и решение о напоминании одной транзакцией."""
        with mi.транзакция(self.con):
            eid, dup = mi.put_event(self.con, ev)
            jid = None if dup else mn.завести(self.con, eid, ev, now)
        return eid, jid

    def работы(self):
        return self.con.execute("select event_id from jobs where kind='nudge' "
                                "order by rowid").fetchall()


class Повод(_База):
    """Шаг ставится на первое за день свежее входящее сообщение человека —
    и только на него."""

    def test_первое_за_день_и_только_оно(self):
        _, первое = self.завести(сообщение())
        _, второе = self.завести(сообщение())
        _, другой = self.завести(сообщение("Олег"))
        self.assertIsNotNone(первое)
        self.assertIsNone(второе)
        self.assertIsNotNone(другой)
        self.assertEqual(len(self.работы()), 2)

    def test_смс_день_по_номеру(self):
        """Ключ дня у SMS — номер: книгу правили, подпись сменилась, человек
        тот же; другой номер — другой человек."""
        self.assertIsNotNone(self.завести(смс("Аня", "+7 999 000-00-01"))[1])
        self.assertIsNone(self.завести(смс("Анна Петрова", "89990000001"))[1])
        self.assertIsNotNone(self.завести(смс("Аня", "89990000002"))[1])
        self.assertIsNone(self.завести(смс(direction="outgoing", номер="89990000003"))[1])

    def test_не_повод(self):
        for ev in (сообщение(outgoing=True), сообщение(via="export"),
                   сообщение(revision_of="7/2"), сообщение(""),
                   сообщение(chat_type="group"), сообщение(chat_type=None),
                   сообщение(occurred_at=когда(61))):
            with self.subTest(payload=ev["payload"]):
                self.assertIsNone(self.завести(ev)[1])
        self.assertEqual(self.работы(), [])
        # невыбранные не считаются «уже сегодня»: следом живое — повод
        self.assertIsNotNone(self.завести(сообщение())[1])

    def test_вчерашнее_напоминание_не_держит_сегодняшнее(self):
        _, jid = self.завести(сообщение())
        вчера = (datetime.now(mi.TZ) - timedelta(days=1)).replace(
            hour=23, minute=59).isoformat(timespec="seconds")
        self.con.execute("update jobs set created=? where id=?", (вчера, jid))
        self.assertIsNotNone(self.завести(сообщение())[1])

    def test_день_по_часам_владельца(self):
        """Граница дня — полночь `MARA_TZ_HOURS`, а не UTC: работа, заведённая
        в 23:30 по UTC, по Москве — уже завтра."""
        now = datetime(2026, 9, 2, 0, 30, tzinfo=mi.TZ).timestamp()
        self.assertEqual(datetime.fromtimestamp(mn.начало_дня(now), mi.TZ),
                         datetime(2026, 9, 2, tzinfo=mi.TZ))


class Шаг(_База):
    def setUp(self):
        super().setUp()
        снято = mock.patch.dict(os.environ, clear=False)
        снято.start()
        self.addCleanup(снято.stop)
        for k in cd.КЛЮЧИ:
            os.environ.pop(k, None)
        self.vault = os.path.join(self.dir, "vault")
        self.env = os.path.join(self.dir, "contextd.env")
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_HOME_CHANNEL=123456789\n")
        self.ушло = []
        self.ответ = "sent"
        было = cd.deliver
        cd.deliver = lambda текст, token, chat: self.ушло.append(текст) or self.ответ
        self.addCleanup(setattr, cd, "deliver", было)

    def шаг(self, eid, now=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            r = mn.run(eid, self.dir, self.env, self.vault, now)
        self.err = err.getvalue()
        return r

    def строки(self, eid):
        return [tuple(r) for r in self.con.execute(
            "select state, error from outbox where kind=? and object_id=? "
            "order by rowid", (mn.ВИД, eid))]

    def test_называет_открытое_перед_ним(self):
        карточка(self.vault, "a", "прислать смету", "Анна Петрова", due="2026-09-20",
                 id_="0190f2a1-7b3c-7d4e-8f00-0000000000aa")
        карточка(self.vault, "b", "позвонить про договор", "анна петрова",
                 status="proposed")
        карточка(self.vault, "c", "уже сделано", "Анна Петрова", status="done")
        карточка(self.vault, "d", "чужое", "Олег")
        eid, _ = self.завести(сообщение())
        self.assertIsNotNone(self.шаг(eid))
        self.assertEqual(self.ушло, ["Пишет «Анна Петрова». Ты обещал:\n"
                                     "• «прислать смету» — до 2026-09-20 #000000aa\n"
                                     "• «позвонить про договор» #0000000a"])
        self.assertEqual(self.строки(eid), [("sent", None)])
        # повтор шага второй раз не шлёт
        self.assertIsNone(self.шаг(eid))
        self.assertEqual(len(self.ушло), 1)

    def test_нечего_напомнить_молчит(self):
        карточка(self.vault, "d", "чужое", "Олег")
        eid, _ = self.завести(сообщение())
        self.assertIsNone(self.шаг(eid))
        self.assertEqual((self.ушло, self.строки(eid)), ([], []))

    def test_старое_не_шлёт(self):
        """Ретрай шага позже часа после сообщения — уже не напоминание."""
        карточка(self.vault, "a", "прислать смету", "Анна Петрова")
        eid, _ = self.завести(сообщение())
        self.assertIsNone(self.шаг(eid, now=time.time() + mn.СВЕЖЕСТЬ_С + 120))
        self.assertEqual(self.ушло, [])

    def test_без_транспорта_не_копится(self):
        карточка(self.vault, "a", "прислать смету", "Анна Петрова")
        eid, _ = self.завести(сообщение())
        self.ответ = "no-transport"
        self.шаг(eid)
        self.assertEqual(self.строки(eid), [("skipped", "no-transport")])
        self.assertIn("не копится", self.err)
        # `--outbox` дайджестов чужой вид не трогает
        with contextlib.redirect_stdout(io.StringIO()):
            cd.outbox(self.dir, self.env)
        self.assertEqual(self.строки(eid), [("skipped", "no-transport")])

    def test_отказ_телеграма_ретрай(self):
        карточка(self.vault, "a", "прислать смету", "Анна Петрова")
        eid, _ = self.завести(сообщение())
        self.ответ = "failed"
        with self.assertRaises(RuntimeError):
            self.шаг(eid)
        self.ответ = "sent"
        self.шаг(eid)
        self.assertEqual([s for s, _ in self.строки(eid)], ["failed", "sent"])
        self.assertEqual(len(self.ушло), 2)

    def test_исключение_транспорта_ретрай_и_не_копится(self):
        """Транспорт бросил — строка `pending`, шаг — ретрай. Пришёл ретрай
        уже устаревшим — строка гаснет, а не ждёт будущего `--outbox`; сбоев
        два и больше — шаг падает, чтобы постоянная беда дошла до DLQ; один
        сбой у опоздавшего сообщения — не беда."""
        карточка(self.vault, "a", "прислать смету", "Анна Петрова")
        eid, jid = self.завести(сообщение())

        def бросить(текст, token, chat):
            raise OSError("сеть")
        cd.deliver = бросить
        with self.assertRaises(OSError):
            self.шаг(eid)
        self.assertEqual(self.строки(eid), [("pending", "OSError: сеть")])
        self.con.execute("update jobs set attempts=2 where id=?", (jid,))
        поздно = time.time() + mn.СВЕЖЕСТЬ_С + 120
        with self.assertRaises(RuntimeError):
            self.шаг(eid, now=поздно)
        self.assertEqual(self.строки(eid), [("skipped", "устарело")])
        self.con.execute("update jobs set attempts=1 where id=?", (jid,))
        self.assertIsNone(self.шаг(eid, now=поздно))

    def test_ждущая_строка_гаснет_на_любом_выходе(self):
        """Транспорт бросил, а к ретраю карточку закрыли — строка не висит
        `pending`."""
        карточка(self.vault, "a", "прислать смету", "Анна Петрова")
        eid, _ = self.завести(сообщение())

        def бросить(текст, token, chat):
            raise OSError("сеть")
        cd.deliver = бросить
        with self.assertRaises(OSError):
            self.шаг(eid)
        карточка(self.vault, "a", "прислать смету", "Анна Петрова", status="done")
        self.assertIsNone(self.шаг(eid))
        self.assertEqual(self.строки(eid), [("skipped", "открытого нет")])

    def test_служебное_не_повод(self):
        self.assertIsNone(self.завести(сообщение(text="", media=[]))[1])

    def test_брошенная_посреди_отправки_не_повторяется(self):
        """Не больше одного раза: `sending` от убитого шага могло уйти."""
        карточка(self.vault, "a", "прислать смету", "Анна Петрова")
        eid, _ = self.завести(сообщение())
        with mi.транзакция(self.con):
            oid = mi.в_outbox(self.con, mn.ВИД, {"text": "x"}, "event", eid)
            self.con.execute("update outbox set state='sending', last_attempt=? "
                             "where id=?", (когда(30), oid))
        self.assertIsNone(self.шаг(eid))
        self.assertEqual(self.ушло, [])
        self.assertEqual(self.строки(eid)[0][0], "skipped")

    def test_удалённое_не_напоминает(self):
        карточка(self.vault, "a", "прислать смету", "Анна Петрова")
        eid, _ = self.завести(сообщение(sid="7/1"))
        mi.put_event(self.con, {"kind": "message", "source": "telegram",
                                "source_id": "7/1/deleted", "occurred_at": когда(),
                                "payload": {"tombstone_of": "7/1"}})
        self.assertIsNone(self.шаг(eid))
        self.assertEqual(self.ушло, [])

    def test_смс_по_номеру(self):
        """SMS: имя из книги и номер; номер узнаёт карточку, где имени нет,
        а в шапку номер не едет."""
        карточка(self.vault, "a", "вернуть дрель", "+7 (999) 000-00-01")
        eid, _ = self.завести(смс())
        self.assertIsNotNone(self.шаг(eid))
        self.assertEqual(self.ушло, ["Пишет «Аня». Ты обещал:\n• «вернуть дрель» #0000000a"])

    def test_кривой_волт_молчит(self):
        eid, _ = self.завести(сообщение())
        with mock.patch("context_pack.про_человека", side_effect=ValueError("x")):
            self.assertIsNone(self.шаг(eid))
        self.assertIn("обещания не прочитаны (ValueError)", self.err)

    def test_карточка_про_владельца_не_напоминается(self):
        карточка(self.vault, "a", "себе", "Анна Петрова")
        eid, _ = self.завести(сообщение())
        with mock.patch("call_project.OWNER", "анна петрова"):
            self.assertIsNone(self.шаг(eid))

    def test_шаг_отдельным_процессом(self):
        """Как его зовёт воркер: `contextd.STEP`, `--event`, база из
        `MARA_BLOBS`, волт из `VAULT`; токена нет — код 0, строка `skipped`."""
        карточка(self.vault, "a", "прислать смету", "Анна Петрова")
        eid, _ = self.завести(сообщение())
        self.con.commit()
        env = dict(os.environ, MARA_BLOBS=self.dir, VAULT=self.vault,
                   MARA_CONTEXTD_ENV=os.path.join(self.dir, "нет.env"))
        r = subprocess.run([sys.executable, os.path.join(СКРИПТЫ, contextd.STEP["nudge"]),
                            "--event", eid], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.строки(eid), [("skipped", "no-transport")])


class Приём(unittest.TestCase):
    """Вход в `contextd`: `/v1/ingest/message` ставит шаг, а сбой решения
    приём не роняет."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        mi.ROOT = cls.dir
        cls.srv = contextd.make_server(cls.dir, port=0, vault=tempfile.mkdtemp())
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.con = mi.connect(cls.dir)
        cls.dev, cls.token = contextd.pair(cls.con, "тестовый телефон")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, data):
        req = urllib.request.Request(self.base + "/v1/ingest/message", method="POST",
                                     data=json.dumps(data).encode("utf-8"))
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.token)
        # прокси окружения мимо петли
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as r:
            return json.loads(r.read())

    def работа(self, eid):
        return self.con.execute("select kind from jobs where event_id=?",
                                (eid,)).fetchall()

    def test_сообщение_ставит_шаг_дубль_нет(self):
        ev = сообщение("Олег Приёмов")
        ev.pop("kind")
        r = self.post(ev)
        self.assertEqual([tuple(x) for x in self.работа(r["event_id"])], [("nudge",)])
        повтор = self.post(ev)
        self.assertTrue(повтор["duplicate"])
        self.assertEqual(len(self.con.execute(
            "select 1 from jobs where kind='nudge' and event_id=?",
            (r["event_id"],)).fetchall()), 1)

    def test_сбой_решения_не_роняет_приём(self):
        ev = сообщение("Анна Сбоева")
        ev.pop("kind")
        with mock.patch.object(mn, "завести", side_effect=RuntimeError("Анна Сбоева")), \
                contextlib.redirect_stdout(io.StringIO()) as лог:
            r = self.post(ev)
        self.assertFalse(r["duplicate"])
        self.assertEqual(self.работа(r["event_id"]), [])
        self.assertIn("напоминание %s: RuntimeError" % r["event_id"], лог.getvalue())
        self.assertNotIn("Сбоева", лог.getvalue())

    def test_откаченная_транзакция_не_глотается(self):
        """SQLite сам откатил транзакцию приёма (диск полон): события уже нет,
        и ответить «принято» с квитанцией на несуществующее нельзя."""
        ev = сообщение("Анна Откатова", sid="откат")
        ev.pop("kind")
        # с ключом квитанция легла бы в autocommit на событие, которого нет;
        # её держит и внешний ключ `ingest_attempts.event_id`, а
        # `contextd.напомнить` не даёт до неё дойти
        ev["idempotency_key"] = "ключ-отката"

        def откат(con, *a):
            con.execute("rollback")
            raise sqlite3.OperationalError("database or disk is full")
        with mock.patch.object(mn, "завести", side_effect=откат), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(Exception):
                self.post(ev)
        self.assertIsNone(self.con.execute(
            "select 1 from events where source_id='откат'").fetchone())
        self.assertIsNone(self.con.execute(
            "select 1 from ingest_attempts where idempotency_key='ключ-отката'").fetchone())


if __name__ == "__main__":
    unittest.main()
