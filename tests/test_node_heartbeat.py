"""Т7.2, ADR-0010 решение 5: сердцебиение с узла на doctor — токен узла,
закрытый список полей, пауза и пропавший сервис как причины ждать."""
import contextlib, io, json, os, sys, tempfile, threading, time, unittest
import urllib.error, urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import mara_ingest as mi
import contextd
import contextd_reconcile as cr
import node_agent

БЕЗ_ПРОКСИ = {k: "" for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                               "all_proxy", "ALL_PROXY")}


def сердце(**kw):
    d = {"capabilities": ["asr", "llm"], "checked": ["asr", "llm"], "models": ["qwen3.5:9b"],
         "accept_jobs": True,
         "minecraft_active": False, "interval_s": 60}
    d.update(kw)
    return d


class Разбор(unittest.TestCase):
    def test_закрытый_список_и_умолчания(self):
        с, беда = contextd.разобрать_сердцебиение({"capabilities": ["llm", "asr", "asr"],
                                                   "лишнее": "x" * 10 ** 5})
        self.assertIsNone(беда)
        self.assertEqual(с, {"capabilities": ["asr", "llm"], "checked": [], "models": [],
                             "models_complete": False, "versions": {},
                             "minecraft_active": False, "accept_jobs": True,
                             "interval_s": contextd.СЕРДЦЕ_ИНТЕРВАЛ_С})

    def test_кривые_поля_отказ(self):
        for плохо in ({"capabilities": "asr"}, {"capabilities": ["ASR"]},
                      {"models": ["x" * 81]}, {"vram_free_mb": -1}, {"vram_free_mb": True},
                      {"load": "high"}, {"accept_jobs": "да"}, {"interval_s": 5},
                      {"versions": {"agent": 1}}, {"models": ["a\nb"]},
                      {"checked": "asr"}):
            with self.subTest(плохо=плохо):
                с, беда = contextd.разобрать_сердцебиение(плохо)
                self.assertIsNone(с)
                self.assertTrue(беда)


class _База(unittest.TestCase):
    def setUp(self):
        contextd._пробы.clear()
        self.addCleanup(contextd._пробы.clear)
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.addCleanup(self.con.close)
        окружение = mock.patch.dict(os.environ, dict(
            БЕЗ_ПРОКСИ, MARA_ENV_FILE=os.path.join(self.dir, "нет.env")))
        окружение.start()
        self.addCleanup(окружение.stop)
        for k in ("MARA_ASR_URL", "MARA_LLM_URL"):
            os.environ.pop(k, None)

    def отчитаться(self, узел="bigpc", **kw):
        с, беда = contextd.разобрать_сердцебиение(сердце(**kw))
        assert беда is None, беда
        contextd.принять_сердцебиение(self.con, узел, с)

    def состарить(self, узел, секунд):
        row = self.con.execute("select capabilities_json from compute_nodes where id=?",
                               (узел,)).fetchone()
        d = json.loads(row[0])
        d["heartbeat_at"] = (datetime.now(mi.TZ) - timedelta(seconds=секунд)).isoformat(
            timespec="seconds")
        self.con.execute("update compute_nodes set capabilities_json=? where id=?",
                         (json.dumps(d), узел))


class Свежесть(_База):
    def test_три_интервала(self):
        self.отчитаться(interval_s=60)
        self.assertIsNotNone(contextd.сердцебиение_узла(self.con, "bigpc"))
        self.состарить("bigpc", 179)
        self.assertIsNotNone(contextd.сердцебиение_узла(self.con, "bigpc"))
        self.состарить("bigpc", 181)
        self.assertIsNone(contextd.сердцебиение_узла(self.con, "bigpc"))

    def test_проба_не_сердцебиение(self):
        contextd.отметить_узел(self.con, "bigpc", "heavy")
        self.assertIsNone(contextd.сердцебиение_узла(self.con, "bigpc"))
        # и сердцебиение потом записывается поверх строки пробы
        self.отчитаться()
        self.assertIsNotNone(contextd.сердцебиение_узла(self.con, "bigpc"))
        # а проба после него возможности не затирает
        contextd.отметить_узел(self.con, "bigpc", "heavy")
        self.assertIsNotNone(contextd.сердцебиение_узла(self.con, "bigpc"))


class Причины(_База):
    """Сердцебиение может только добавить причину ждать."""

    def test_пауза(self):
        self.отчитаться(accept_jobs=False)
        self.assertEqual(contextd.недоступные(self.con),
                         {"asr": "node_paused:bigpc", "extract": "node_paused:bigpc"})

    def test_нет_сервиса(self):
        self.отчитаться(capabilities=["llm"])
        self.assertEqual(contextd.недоступные(self.con),
                         {"asr": "node_no_capability:bigpc", "extract": None})

    def test_непроверенное_не_держит(self):
        """Ревью Т7.2, P2: агенту не задали адрес STT — он про него молчит, и
        «нет возможности» не ставится: судит проба doctor, как без агента."""
        self.отчитаться(capabilities=[], checked=["llm"])
        self.assertEqual(contextd.недоступные(self.con),
                         {"asr": None, "extract": "node_no_capability:bigpc"})
        self.отчитаться(capabilities=[], checked=[])
        self.assertEqual(contextd.недоступные(self.con), {"asr": None, "extract": None})

    def test_всё_хорошо_не_отменяет_пробу(self):
        """Узел сказал «беру», но с doctor адрес не отвечает — ждём, как
        молчащий: дорогу к сервису проверяет doctor."""
        self.отчитаться()
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.addCleanup(s.close)
        os.environ["MARA_ASR_URL"] = "http://127.0.0.1:%d" % s.getsockname()[1]
        self.assertEqual(contextd.недоступные(self.con)["asr"], "node_offline:bigpc")

    def test_устаревшее_сердцебиение_не_держит(self):
        self.отчитаться(accept_jobs=False)
        self.состарить("bigpc", 3600)
        self.assertEqual(contextd.недоступные(self.con), {"asr": None, "extract": None})

    def test_такт_не_берёт_работу_на_паузе(self):
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "h1", "payload": {}})
        jid = mi.add_job(self.con, eid, "asr")
        self.отчитаться(accept_jobs=False)
        with mock.patch.object(contextd, "run_step", side_effect=AssertionError("взял")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(contextd.такт(self.con))
        self.assertEqual(tuple(self.con.execute(
            "select state, attempts, queued_reason from jobs where id=?", (jid,)).fetchone()),
            ("ready", 0, "node_paused:bigpc"))


class ЖивойАгентНеМолодит(_База):
    """Ревью Т7.2, P1: агент жив и отчитывается, а doctor до сервиса не
    достаёт (адрес сменился, фаервол) — работы ждут `node_offline`, и
    `узел-молчит` обязан сработать: сердцебиение `last_seen` не пишет."""

    def test_узел_молчит_при_живом_агенте(self):
        self.отчитаться()
        self.assertIsNone(self.con.execute(
            "select last_seen from compute_nodes where id='bigpc'").fetchone()[0])
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "p1", "payload": {}})
        jid = mi.add_job(self.con, eid, "asr")
        когда = (datetime.now(mi.TZ) - timedelta(hours=30)).isoformat(timespec="seconds")
        self.con.execute("update jobs set queued_reason='node_offline:bigpc', created=?, "
                         "next_at=0 where id=?", (когда, jid))
        contextd.отметить_узел(self.con, contextd.ВОРКЕР, "core")
        н, = cr.узел_молчит(self.con, hours=24)
        self.assertEqual((н["check"], н["node"]), ("узел-молчит", "bigpc"))


class Сверка(_База):
    def ждущая(self, причина, часов):
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "w%s" % причина, "payload": {}})
        jid = mi.add_job(self.con, eid, "asr")
        когда = (datetime.now(mi.TZ) - timedelta(hours=часов)).isoformat(timespec="seconds")
        self.con.execute("update jobs set queued_reason=?, created=?, next_at=0 where id=?",
                         (причина, когда, jid))
        contextd.отметить_узел(self.con, contextd.ВОРКЕР, "core")

    def test_пауза_дольше_порога(self):
        self.отчитаться(accept_jobs=False)
        self.ждущая("node_paused:bigpc", 30)
        н, = cr.узел_молчит(self.con, hours=24)
        self.assertEqual((н["check"], н["node"], н["count"]), ("узел-не-берёт", "bigpc", 1))
        self.assertIn("на паузе", н["detail"])
        # живой узел не делает паузу свежей: мерится ожидание работы
        self.assertEqual(cr.узел_молчит(self.con, hours=31), [])

    def test_очередь_не_стоит_при_паузе(self):
        self.ждущая("node_no_capability:bigpc", 30)
        self.assertEqual(cr.очередь_стоит(self.con, hours=1), [])


class Метрики(_База):
    def test_ждут_по_любой_причине_и_возраст_сердцебиения(self):
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "m1", "payload": {}})
        jid = mi.add_job(self.con, eid, "asr")
        self.con.execute("update jobs set queued_reason='node_paused:bigpc' where id=?", (jid,))
        строки = contextd.metrics(self.con, self.dir, None).splitlines()
        self.assertIn('mara_jobs_waiting_node{node="bigpc"} 1', строки)
        self.assertIn('mara_node_heartbeat_seconds{node="bigpc"} -1', строки)
        self.отчитаться()
        строки = contextd.metrics(self.con, self.dir, None).splitlines()
        v = [l for l in строки if l.startswith('mara_node_heartbeat_seconds{node="bigpc"}')]
        self.assertTrue(v and 0 <= int(v[0].split()[1]) <= 5, v)


class Агент(unittest.TestCase):
    def test_tasklist_в_866(self):
        """Ревью Т7.2, P2: русская Windows пишет `tasklist` в OEM-866 с
        неразрывным пробелом 0xFF — агент читает байты и не падает."""
        строка = '"javaw.exe","1234","Console","1","512 340 КБ"\r\n'.encode("cp866")
        строка = строка.replace(b"512 340", b"512\xff340")

        class Р:
            stdout = строка
        self.assertEqual(node_agent.процессы(lambda *a, **k: Р(), windows=True), {"javaw.exe"})

        class Пусто:
            stdout = None
        self.assertEqual(node_agent.процессы(lambda *a, **k: Пусто(), windows=True), set())

    def test_кривой_ответ_ollama(self):
        with mock.patch.object(node_agent, "отвечает", lambda *a: (True, b'{"models": null}')):
            self.assertEqual(node_agent.модели("http://x"), ([], False))

    def test_сбой_оборота_не_роняет(self):
        with mock.patch.dict(os.environ, {"MARA_URL": "http://127.0.0.1:9",
                                          "MARA_NODE_TOKEN": "t"}), \
                mock.patch.object(node_agent, "собрать", side_effect=TypeError("x")), \
                contextlib.redirect_stdout(io.StringIO()) as лог:
            self.assertEqual(node_agent.main(["--once"]), 1)
        self.assertIn("оборот не удался: TypeError", лог.getvalue())


class _Ollama(BaseHTTPRequestHandler):
    def do_GET(self):
        тело = json.dumps({"models": [{"name": "qwen3.5:9b"}, {"name": "bge-m3"}]}).encode()
        self.send_response(200 if self.path == "/api/tags" else 404)
        self.end_headers()
        self.wfile.write(тело if self.path == "/api/tags" else b"")

    def log_message(self, *a):
        pass


class Сеть(unittest.TestCase):
    """Через настоящий `contextd` и настоящий агент: токен узла годится
    только для сердцебиения, а сердцебиение — только с токеном узла."""

    @classmethod
    def setUpClass(cls):
        cls.окружение = mock.patch.dict(os.environ, БЕЗ_ПРОКСИ)
        cls.окружение.start()
        cls.dir = tempfile.mkdtemp()
        mi.ROOT = cls.dir
        cls.srv = contextd.make_server(cls.dir, port=0, vault=tempfile.mkdtemp())
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.con = mi.connect(cls.dir)
        cls.узел, cls.токен_узла = contextd.pair_node(cls.con, "bigpc")
        cls.телефон, cls.токен = contextd.pair(cls.con, "телефон")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.окружение.stop()

    def запрос(self, путь, токен, тело=None):
        req = urllib.request.Request(self.url + путь, method="POST" if тело is not None else "GET",
                                     data=None if тело is None else json.dumps(тело).encode())
        req.add_header("Authorization", "Bearer " + токен)
        req.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with contextlib.redirect_stdout(io.StringIO()), opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_узел_по_токену(self):
        self.assertEqual(contextd.узел_устройства(self.con, self.узел), "bigpc")
        self.assertIsNone(contextd.узел_устройства(self.con, self.телефон))
        for плохо in ("Big PC", contextd.ВОРКЕР):
            with self.subTest(узел=плохо), self.assertRaises(ValueError):
                contextd.pair_node(self.con, плохо)
        with self.assertRaises(ValueError):
            contextd.pair(self.con, contextd.УЗЕЛ_ПРЕФИКС + "gtr")

    def test_отозванный_и_без_heartbeat_и_чужая_строка(self):
        dev, токен = contextd.pair_node(self.con, "gtr")
        contextd.set_scopes(self.con, dev, ["call"])
        self.assertEqual(self.запрос(contextd.ПУТЬ_СЕРДЦА, токен, сердце())[0], 403)
        contextd.set_scopes(self.con, dev, ["heartbeat"])
        self.con.execute("insert into compute_nodes(id, name, role) values('gtr_old', 'gtr', 'x')")
        код, ответ = self.запрос(contextd.ПУТЬ_СЕРДЦА, токен, сердце())
        self.assertEqual(код, 409, ответ)
        contextd.revoke(self.con, dev)
        self.assertEqual(self.запрос(contextd.ПУТЬ_СЕРДЦА, токен, сердце())[0], 401)

    def test_токен_узла_только_для_сердцебиения(self):
        код, ответ = self.запрос(contextd.ПУТЬ_СЕРДЦА, self.токен_узла, сердце())
        self.assertEqual((код, ответ["node"]), (200, "bigpc"))
        self.assertEqual(self.запрос("/v1/context/bootstrap", self.токен_узла)[0], 403)
        self.assertEqual(self.запрос("/v1/ingest/event", self.токен_узла,
                                     {"kind": "call", "source": "x", "source_id": "y"})[0], 403)
        self.assertEqual(self.запрос(contextd.ПУТЬ_СЕРДЦА, self.токен, сердце())[0], 403)
        self.assertEqual(self.запрос(contextd.ПУТЬ_СЕРДЦА, self.токен_узла,
                                     {"interval_s": 1})[0], 400)

    def test_агент_до_doctor(self):
        оллама = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
        threading.Thread(target=оллама.serve_forever, daemon=True).start()
        self.addCleanup(оллама.server_close)
        self.addCleanup(оллама.shutdown)
        env = {"MARA_NODE_LLM_URL": "http://127.0.0.1:%d" % оллама.server_address[1],
               "MARA_NODE_INTERVAL": "30"}
        тело = node_agent.собрать(env=env, имена={"javaw.exe"})
        self.assertEqual((тело["capabilities"], тело["checked"], тело["models"],
                          тело["models_complete"], тело["minecraft_active"]),
                         (["llm"], ["llm"], ["bge-m3", "qwen3.5:9b"], True, True))
        self.assertEqual(node_agent.отправить(self.url, self.токен_узла, тело), 200)
        с = contextd.сердцебиение_узла(self.con, "bigpc")
        self.assertEqual((с["capabilities"], с["interval_s"], с["minecraft_active"]),
                         (["llm"], 30, True))
        self.assertEqual(node_agent.отправить(self.url, "wrong-token", тело), 401)
        self.assertEqual(node_agent.отправить(self.url, "кривой", тело), 0)


if __name__ == "__main__":
    unittest.main()
