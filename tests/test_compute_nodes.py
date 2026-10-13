"""Т7.3, ADR-0010 решения 2, 3 и 8: тяжёлые шаги — на bigpc; узел молчит —
работы ждут с причиной и не тратят попыток; это видно в `/metrics` и в
сверке."""
import contextlib, io, os, sys, tempfile, threading, time, unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import contextd
import contextd_reconcile as cr


class _Ответ404(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


def живой_адрес(test):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ответ404)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    return "http://127.0.0.1:%d" % srv.server_address[1]


def мёртвый_адрес(test):
    """Порт занят нашим сокетом без `listen()`: соединение отвергается, и
    никто другой этот порт за время теста не займёт."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    test.addCleanup(s.close)
    return "http://127.0.0.1:%d" % s.getsockname()[1]


БЕЗ_ПРОКСИ = {k: "" for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                               "all_proxy", "ALL_PROXY")}


class Проба(unittest.TestCase):
    def setUp(self):
        contextd._пробы.clear()
        self.addCleanup(contextd._пробы.clear)
        прокси = mock.patch.dict(os.environ, БЕЗ_ПРОКСИ)
        прокси.start()
        self.addCleanup(прокси.stop)

    def test_кривой_адрес_не_молчание(self):
        self.assertEqual(contextd.проба("bigpc:8770"), (None, True))

    def test_любой_ответ_жив_молчание_нет(self):
        self.assertEqual(contextd.проба(живой_адрес(self)), (True, True))
        self.assertEqual(contextd.проба(мёртвый_адрес(self)), (False, True))

    def test_кэш_на_интервал(self):
        адрес = мёртвый_адрес(self)
        self.assertEqual(contextd.проба(адрес, сейчас=100.0), (False, True))
        self.assertEqual(contextd.проба(адрес, сейчас=100.0 + contextd.ПРОБА_С - 1),
                         (False, False), "в пределах интервала — из кэша")
        self.assertEqual(contextd.проба(адрес, сейчас=100.0 + contextd.ПРОБА_С)[1], True)


class Такт(unittest.TestCase):
    """Воркер не берёт работы молчащего узла: попытки не растут, причина
    видна; лёгкие шаги идут; узел ответил — работа берётся."""

    def setUp(self):
        contextd._пробы.clear()
        self.addCleanup(contextd._пробы.clear)
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.addCleanup(self.con.close)
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "n1", "payload": {}})
        self.asr = mi.add_job(self.con, eid, "asr")
        self.шаги = []
        шаг = mock.patch.object(contextd, "run_step",
                                lambda kind, event_id, *a: self.шаги.append(kind) or (True, ""))
        шаг.start()
        self.addCleanup(шаг.stop)
        self.env = mock.patch.dict(os.environ, dict(
            БЕЗ_ПРОКСИ, MARA_ENV_FILE=os.path.join(self.dir, "нет.env")))
        self.env.start()
        self.addCleanup(self.env.stop)

    def работа(self, jid):
        return dict(self.con.execute("select state, attempts, queued_reason from jobs "
                                     "where id=?", (jid,)).fetchone())

    def такт(self, отмечено=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return contextd.такт(self.con, отмечено)

    def test_молчащий_узел_ждёт_без_попыток(self):
        os.environ["MARA_ASR_URL"] = мёртвый_адрес(self)
        os.environ["MARA_LLM_URL"] = мёртвый_адрес(self)
        for _ in range(3):
            self.assertIsNone(self.такт())
        self.assertEqual(self.работа(self.asr),
                         {"state": "ready", "attempts": 0,
                          "queued_reason": "node_offline:bigpc"})
        self.assertEqual(self.шаги, [])
        self.assertEqual(self.con.execute("select count(*) from job_attempts").fetchone()[0], 0,
                         "попытка не открыта")
        self.assertIsNone(self.con.execute("select last_seen from compute_nodes "
                                           "where id='bigpc'").fetchone())

    def test_лёгкий_шаг_идёт_пока_узел_молчит(self):
        os.environ["MARA_ASR_URL"] = мёртвый_адрес(self)
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "n2", "payload": {}})
        mi.add_job(self.con, eid, "project")
        взята = self.такт()
        self.assertEqual((взята["kind"], self.шаги), ("project", ["project"]))

    def test_узел_ответил_работа_взята(self):
        os.environ["MARA_ASR_URL"] = мёртвый_адрес(self)
        отмечено = [None, None]
        self.такт(отмечено)
        self.assertEqual(self.работа(self.asr)["queued_reason"], "node_offline:bigpc")
        contextd._пробы.clear()
        os.environ["MARA_ASR_URL"] = живой_адрес(self)
        os.environ["MARA_LLM_URL"] = живой_адрес(self)
        взята = self.такт(отмечено)
        self.assertEqual(взята["id"], self.asr)
        self.assertEqual(self.работа(self.asr)["state"], "done")
        self.assertIsNone(self.работа(self.asr)["queued_reason"])
        self.assertIsNotNone(self.con.execute("select last_seen from compute_nodes "
                                              "where id='bigpc'").fetchone()[0])

    def test_адреса_нет_шаг_падает_как_раньше(self):
        """Не задан адрес — это поломка настройки, а не выключенная
        коробка: работа берётся, и шаг сам скажет `нужен_адрес`."""
        os.environ.pop("MARA_ASR_URL", None)
        os.environ.pop("MARA_LLM_URL", None)
        self.assertEqual(self.такт()["id"], self.asr)

    def test_вторая_работа_теряет_причину_и_новая_её_получает(self):
        """Причина живёт на всех `ready` вида, а не на одной взятой: узел
        ответил — снята и у той, что не взяли; узел молчит — её получает и
        работа, пришедшая после первой отметки (за `ПРОБА_С`)."""
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "n3", "payload": {}})
        мёртвый = мёртвый_адрес(self)
        os.environ["MARA_ASR_URL"] = мёртвый
        отмечено = [None, None]
        self.такт(отмечено)
        вторая = mi.add_job(self.con, eid, "asr")
        self.assertIsNone(self.работа(вторая)["queued_reason"])
        отмечено[0] -= contextd.ПРОБА_С            # прошла минута
        contextd._пробы[мёртвый] = (time.monotonic(), False)
        self.такт(отмечено)
        self.assertEqual(self.работа(вторая)["queued_reason"], "node_offline:bigpc")
        # узел ожил: берётся одна, причина снята у обеих
        contextd._пробы.clear()
        os.environ["MARA_ASR_URL"] = живой_адрес(self)
        self.такт(отмечено)
        self.assertEqual({self.работа(j)["queued_reason"] for j in (self.asr, вторая)}, {None})

    def test_адрес_из_файла_как_у_шага(self):
        """Адрес только в `~/.config/mara/env` (там живут адреса домашней
        сети): демон видит его, как и шаг, но в своё окружение не впитывает."""
        env = os.path.join(self.dir, "mara.env")
        with open(env, "w") as fh:
            fh.write("export MARA_ASR_URL=%s\n" % мёртвый_адрес(self))
        os.environ["MARA_ENV_FILE"] = env
        os.environ.pop("MARA_ASR_URL", None)
        self.assertIsNone(self.такт())
        self.assertEqual(self.работа(self.asr)["queued_reason"], "node_offline:bigpc")
        self.assertNotIn("MARA_ASR_URL", os.environ)

    def test_пустое_окружение_сильнее_файла(self):
        """`MARA_ASR_URL=` в окружении юнита и старый адрес в файле: шаг
        возьмёт пустое (`setdefault`) и упадёт на `нужен_адрес` — демон
        должен видеть то же и работу брать, а не ждать узел по чужому адресу."""
        env = os.path.join(self.dir, "mara.env")
        with open(env, "w") as fh:
            fh.write("MARA_ASR_URL=%s\n" % мёртвый_адрес(self))
        os.environ["MARA_ENV_FILE"] = env
        os.environ["MARA_ASR_URL"] = ""
        self.assertIsNone(contextd.адрес_шага("MARA_ASR_URL"))
        self.assertEqual(self.такт()["id"], self.asr)

    def test_кривой_адрес_шаг_падает_как_раньше(self):
        os.environ["MARA_ASR_URL"] = "bigpc:8770"
        self.assertEqual(self.такт()["id"], self.asr)

    def test_упавший_тяжёлый_шаг_сбрасывает_пробу(self):
        адрес = живой_адрес(self)
        os.environ["MARA_ASR_URL"] = адрес
        with mock.patch.object(contextd, "run_step", lambda kind, event_id, *a: (False, "сеть")):
            self.такт()
        self.assertNotIn(адрес, contextd._пробы, "после сбоя — проба заново")

    def test_сердцебиение_воркера(self):
        self.такт()
        self.assertIsNotNone(self.con.execute("select last_seen from compute_nodes "
                                              "where id=?", (contextd.ВОРКЕР,)).fetchone())

    def test_воркер_переживает_любую_беду(self):
        stop = threading.Event()
        звали = []

        def такт(con, отмечено):
            звали.append(1)
            if len(звали) >= 2:
                stop.set()
            raise RuntimeError("что угодно")
        with mock.patch.object(contextd, "такт", такт), \
                mock.patch.object(stop, "wait", lambda t=None: None), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            contextd.worker(stop, self.dir)
        self.assertEqual(len(звали), 2, "поток жив после первой беды")
        self.assertIn("RuntimeError", out.getvalue())

    def test_полуживой_узел_не_свежий(self):
        """Ответил ASR, молчит LLM: `last_seen` узла не пишется — узел жив,
        когда отвечает всё, что ему поручено."""
        os.environ["MARA_ASR_URL"] = живой_адрес(self)
        os.environ["MARA_LLM_URL"] = мёртвый_адрес(self)
        self.assertEqual(contextd.недоступные(self.con),
                         {"asr": None, "extract": "node_offline:bigpc"})
        self.assertIsNone(self.con.execute("select last_seen from compute_nodes").fetchone())


class Метрики(unittest.TestCase):
    def test_ждут_узел_и_его_молчание(self):
        d = tempfile.mkdtemp()
        con = mi.connect(d)
        self.addCleanup(con.close)
        строки = lambda: {l.split()[0]: float(l.split()[1])
                          for l in contextd.metrics(con, d, None).splitlines()
                          if l.startswith(("mara_jobs_waiting_node", "mara_node_last_seen"))}
        self.assertEqual(строки(), {'mara_jobs_waiting_node{node="bigpc"}': 0,
                                    'mara_node_last_seen_seconds{node="bigpc"}': -1})
        eid, _ = mi.put_event(con, {"kind": "call", "source": "phone",
                                    "source_id": "m1", "payload": {}})
        mi.add_job(con, eid, "asr")
        mi.отметить_ожидание(con, {"asr": "node_offline:bigpc"})
        час = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        con.execute("insert into compute_nodes(id,name,last_seen) values('bigpc','bigpc',?)",
                    (час,))
        с = строки()
        self.assertEqual(с['mara_jobs_waiting_node{node="bigpc"}'], 1)
        self.assertTrue(3500 <= с['mara_node_last_seen_seconds{node="bigpc"}'] <= 3700, с)


class Сверка(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.addCleanup(self.con.close)
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "r1", "payload": {}})
        self.jid = mi.add_job(self.con, eid, "asr")
        self.con.execute("update jobs set next_at=0 where id=?", (self.jid,))
        mi.отметить_ожидание(self.con, {"asr": "node_offline:bigpc"})
        self.сердце(0)

    def сердце(self, минут):
        """Сердцебиение воркера `минут` назад."""
        t = (datetime.now(timezone.utc) - timedelta(minutes=минут)).isoformat(timespec="seconds")
        self.con.execute("insert into compute_nodes(id,name,last_seen) values('doctor','doctor',?) "
                         "on conflict(id) do update set last_seen=excluded.last_seen", (t,))

    def давно_ждёт(self, часов):
        t = (datetime.now(timezone.utc) - timedelta(hours=часов)).isoformat(timespec="seconds")
        self.con.execute("update jobs set created=? where id=?", (t, self.jid))

    def видел(self, часов):
        t = (datetime.now(timezone.utc) - timedelta(hours=часов)).isoformat(timespec="seconds")
        self.con.execute("insert into compute_nodes(id,name,last_seen) values('bigpc','bigpc',?) "
                         "on conflict(id) do update set last_seen=excluded.last_seen", (t,))

    def test_молчит_дольше_суток(self):
        self.видел(30)
        self.давно_ждёт(30)
        н, = cr.узел_молчит(self.con)
        self.assertEqual((н["check"], н["level"], н["count"], н["node"]),
                         ("узел-молчит", "warn", 1, "bigpc"))
        self.assertIn("не отвечает по шагам asr 30 ч", н["detail"])

    def test_давно_не_видели_но_работа_свежая(self):
        """Узел молчит неделю, но работа ждёт час — не тревога: мерится
        ожидание работы."""
        self.видел(24 * 7)
        self.давно_ждёт(1)
        self.assertEqual(cr.узел_молчит(self.con), [])

    def test_ночь_не_тревога(self):
        self.видел(8)
        self.assertEqual(cr.узел_молчит(self.con), [])

    def test_ни_разу_не_отвечал_по_старейшей_работе(self):
        self.давно_ждёт(40)
        н, = cr.узел_молчит(self.con)
        self.assertIn("ни разу не отвечал по шагам asr, работы ждут 40 ч", н["detail"])

    def test_ждущие_узел_не_очередь_стоит(self):
        """Работу молчащего узла воркер не берёт нарочно — это не «никто не
        берёт». Без причины та же работа — находка очереди."""
        self.assertEqual(cr.очередь_стоит(self.con), [])
        mi.отметить_ожидание(self.con, {"asr": None})
        self.assertEqual([н["check"] for н in cr.очередь_стоит(self.con)], ["очередь-стоит"])

    def test_встал_воркер_причина_не_прячет(self):
        """Воркер молчит дольше десяти минут: причина ожидания осталась от
        прошлого — работы видит `очередь-стоит`, а не `узел-молчит`."""
        self.сердце(30)
        self.видел(30)
        self.давно_ждёт(30)
        self.assertEqual([н["check"] for н in cr.очередь_стоит(self.con)], ["очередь-стоит"])
        self.assertEqual(cr.узел_молчит(self.con), [])


if __name__ == "__main__":
    unittest.main()
