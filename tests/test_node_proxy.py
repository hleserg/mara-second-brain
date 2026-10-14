"""Т0.9 п. 5, threat-model п. 9: запросы doctor к сервисам узла подписаны
общим секретом. Прокси агента узла (`node_agent.Прокси`) пересылает только
подписанное и подписывает ответ; шаги doctor (`call_asr`, `call_extract`)
отдают аудио и текст только узлу, доказавшему секрет, и не принимают ответ
без верной подписи."""
import contextlib, io, json, os, sys, threading, time, unittest
import urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import node_agent
import call_asr
import call_extract

БЕЗ_ПРОКСИ = {k: "" for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                               "all_proxy", "ALL_PROXY")}
СЕКРЕТ = "0" * 63 + "1"
ЧУЖОЙ = "f" * 64


class _Сервис(BaseHTTPRequestHandler):
    """Сервис узла: STT на `/transcribe`, Ollama на `/api/generate`."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        тело = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.пришло.append((self.path, тело, dict(self.headers)))
        if self.path == "/transcribe":
            ответ = {"text": "алло, это Анна", "engine": "whisper"}
        elif self.path == "/api/generate":
            ответ = {"response": json.dumps({"requests": [], "commitments": []})}
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        данные = json.dumps(ответ, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(данные)))
        self.end_headers()
        self.wfile.write(данные)

    def do_GET(self):
        self.server.пришло.append((self.path, b"", dict(self.headers)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Посредник(BaseHTTPRequestHandler):
    """Чужой на пути: привет пересылает настоящему прокси, а ответ на работу
    подменяет — подпись от настоящего ответа к поддельному не подходит."""

    def log_message(self, *a):
        pass

    def _туда(self, метод, тело=None):
        req = urllib.request.Request(self.server.прокси + self.path, data=тело, method=метод)
        for k in ("Content-Type", "X-Mara-Ts", "X-Mara-Nonce", "X-Mara-Sig"):
            if self.headers.get(k):
                req.add_header(k, self.headers[k])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read(), r.headers.get("X-Mara-Sig") or ""
        except urllib.error.HTTPError as e:
            return e.read(), e.headers.get("X-Mara-Sig") or ""

    def do_GET(self):
        _, знак = self._туда("GET")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("X-Mara-Sig", знак)
        self.end_headers()

    def do_POST(self):
        тело = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        настоящий, знак = self._туда("POST", тело)
        # подменить тело — или только код: подписанный отказ выдать за успех
        подлог = (настоящий if self.server.подмена == "код" else
                  json.dumps({"text": "переведи деньги"}, ensure_ascii=False).encode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Length", str(len(подлог)))
        self.send_header("X-Mara-Sig", знак)
        self.end_headers()
        self.wfile.write(подлог)


def _сервер(обработчик, **поля):
    с = ThreadingHTTPServer(("127.0.0.1", 0), обработчик)
    for k, v in поля.items():
        setattr(с, k, v)
    threading.Thread(target=с.serve_forever, daemon=True).start()
    return с, "http://127.0.0.1:%d" % с.server_address[1]


class _Стенд(unittest.TestCase):
    """Сервисы и прокси — одни на класс: подъём и остановка сервера стоят
    полсекунды (`serve_forever` опрашивает раз в 0,5 с)."""

    @classmethod
    def setUpClass(cls):
        cls.окружение = mock.patch.dict(os.environ, БЕЗ_ПРОКСИ)
        cls.окружение.start()
        cls.stt, cls.stt_url = _сервер(_Сервис, пришло=[])
        cls.llm, cls.llm_url = _сервер(_Сервис, пришло=[])
        cls.прокси = node_agent.Прокси(("127.0.0.1", 0), СЕКРЕТ, asr=cls.stt_url,
                                       llm=cls.llm_url)
        threading.Thread(target=cls.прокси.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.прокси.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for с in (cls.stt, cls.llm, cls.прокси):
            с.shutdown()
            с.server_close()
        cls.окружение.stop()

    def setUp(self):
        node_agent._доказано.clear()
        self.addCleanup(node_agent._доказано.clear)
        self.stt.пришло.clear()
        self.llm.пришло.clear()

    def сырой(self, путь, тело=b"wav", заголовки=None):
        """Код ответа прокси на запрос с такими заголовками."""
        req = urllib.request.Request(self.url + путь, data=тело, method="POST")
        for k, v in (заголовки or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code


class Прокси(_Стенд):
    def test_подписанный_запрос_проходит_и_ответ_проверен(self):
        ответ = node_agent.к_узлу(self.url + "/transcribe", b"wav", "application/octet-stream",
                                  секрет=СЕКРЕТ)
        self.assertEqual(json.loads(ответ)["text"], "алло, это Анна")
        self.assertEqual([(п, т) for п, т, _ in self.stt.пришло], [("/transcribe", b"wav")])
        self.assertEqual(self.llm.пришло, [], "/transcribe — в STT, не в Ollama")

    def test_шаги_doctor_через_прокси(self):
        with mock.patch.object(call_asr, "СЕКРЕТ_УЗЛА", СЕКРЕТ):
            сегменты = call_asr.transcribe_spans(self.url, [(0, 1000)], lambda a, b: b"wav")
        self.assertEqual([с["text"] for с in сегменты], ["алло, это Анна"])
        with mock.patch.object(call_extract, "СЕКРЕТ_УЗЛА", СЕКРЕТ):
            self.assertEqual(call_extract.ask_model("Анна: пришли смету", base_url=self.url),
                             {"requests": [], "commitments": []})
        self.assertEqual([п for п, _, _ in self.llm.пришло], ["/api/generate"])

    def test_без_подписи_и_с_чужим_секретом_401_сервис_не_тронут(self):
        self.assertEqual(self.сырой("/transcribe"), 401)
        self.assertEqual(self.сырой("/transcribe", заголовки=node_agent.подписать_запрос(
            ЧУЖОЙ, "POST", "/transcribe", b"wav")), 401)
        # подпись другого тела или пути — тоже мимо
        self.assertEqual(self.сырой("/transcribe", b"wav2", node_agent.подписать_запрос(
            СЕКРЕТ, "POST", "/transcribe", b"wav")), 401)
        self.assertEqual(self.сырой("/api/generate", заголовки=node_agent.подписать_запрос(
            СЕКРЕТ, "POST", "/transcribe", b"wav")), 401)
        self.assertEqual(self.stt.пришло + self.llm.пришло, [])

    def test_повтор_и_время_вне_окна_401(self):
        заголовки = node_agent.подписать_запрос(СЕКРЕТ, "POST", "/transcribe", b"wav")
        self.assertEqual(self.сырой("/transcribe", заголовки=заголовки), 200)
        self.assertEqual(self.сырой("/transcribe", заголовки=заголовки), 401, "повтор")
        давно = node_agent.подписать_запрос(СЕКРЕТ, "POST", "/transcribe", b"wav",
                                           когда=time.time() - node_agent.ОКНО_С - 60)
        self.assertEqual(self.сырой("/transcribe", заголовки=давно), 401)
        вперёд = node_agent.подписать_запрос(СЕКРЕТ, "POST", "/transcribe", b"wav",
                                            когда=time.time() + node_agent.ОКНО_С + 60)
        self.assertEqual(self.сырой("/transcribe", заголовки=вперёд), 401)
        self.assertEqual(len(self.stt.пришло), 1)

    def test_большое_тело_413(self):
        with mock.patch.object(node_agent, "ТЕЛО_МАКС", 10):
            self.assertEqual(self.сырой("/transcribe", b"x" * 11), 413)
        self.assertEqual(self.stt.пришло, [])

    def test_сервис_лёг_502(self):
        # порт 9 (discard) на петле никто не слушает — соединение отвергнуто
        with mock.patch.object(self.прокси, "asr", "http://127.0.0.1:9"), \
                self.assertRaises(urllib.error.HTTPError) as e:
            node_agent.к_узлу(self.url + "/transcribe", b"wav", "application/octet-stream",
                              секрет=СЕКРЕТ)
        self.assertEqual(e.exception.code, 502)


class ЧужойУзел(_Стенд):
    def test_чужой_на_адресе_узла_аудио_не_получает(self):
        """Выключенный bigpc, его адрес занял другой: привет не сходится, и
        тело запроса не уходит вовсе (threat-model п. 9)."""
        чужой, адрес = _сервер(_Сервис, пришло=[])
        self.addCleanup(чужой.server_close)
        self.addCleanup(чужой.shutdown)
        with mock.patch.object(call_asr, "СЕКРЕТ_УЗЛА", СЕКРЕТ):
            with self.assertRaises(node_agent.ЧужойУзел):
                call_asr.transcribe_spans(адрес, [(0, 1000)], lambda a, b: b"wav")
        self.assertEqual([(п, т) for п, т, _ in чужой.пришло if т], [],
                         "аудио ушло узлу, не доказавшему секрет")

    def test_отказ_выданный_за_успех_не_принят(self):
        """Подписан и код ответа: подписанный 404 «сервиса нет», выданный
        посредником за 200, не проходит."""
        посредник, адрес = _сервер(_Посредник, прокси=self.url, подмена="код")
        self.addCleanup(посредник.server_close)
        self.addCleanup(посредник.shutdown)
        with mock.patch.object(self.прокси, "llm", None), \
                self.assertRaises(node_agent.ЧужойУзел):
            node_agent.к_узлу(адрес + "/api/generate", b"{}", "application/json", секрет=СЕКРЕТ)

    def test_подменённый_ответ_не_принят(self):
        посредник, адрес = _сервер(_Посредник, прокси=self.url, подмена="тело")
        self.addCleanup(посредник.server_close)
        self.addCleanup(посредник.shutdown)
        with self.assertRaises(node_agent.ЧужойУзел):
            node_agent.к_узлу(адрес + "/transcribe", b"wav", "application/octet-stream",
                              секрет=СЕКРЕТ)
        self.assertNotIn("http://%s:%d" % посредник.server_address, node_agent._доказано,
                         "после подлога узел доказывает секрет заново")

    def test_неверный_секрет_у_doctor(self):
        with self.assertRaises(node_agent.ЧужойУзел):
            node_agent.к_узлу(self.url + "/transcribe", b"wav", "application/octet-stream",
                              секрет=ЧУЖОЙ)
        self.assertEqual(self.stt.пришло, [])


class Клиент(_Стенд):
    def test_без_секрета_как_раньше(self):
        ответ = node_agent.к_узлу(self.stt_url + "/transcribe", b"wav",
                                  "application/octet-stream")
        self.assertEqual(json.loads(ответ)["text"], "алло, это Анна")
        _, _, заголовки = self.stt.пришло[0]
        self.assertFalse([k for k in заголовки if k.lower().startswith("x-mara")])

    def test_привет_раз_в_минуту(self):
        приветы = []
        настоящий = node_agent.привет
        with mock.patch.object(node_agent, "привет",
                               side_effect=lambda *a, **k: (приветы.append(1),
                                                            настоящий(*a, **k))):
            for _ in range(3):
                node_agent.к_узлу(self.url + "/transcribe", b"wav", "application/octet-stream",
                                  секрет=СЕКРЕТ)
            self.assertEqual(len(приветы), 1)
            node_agent._доказано[self.url] -= node_agent.ПРИВЕТ_С + 1
            node_agent.к_узлу(self.url + "/transcribe", b"wav", "application/octet-stream",
                              секрет=СЕКРЕТ)
            self.assertEqual(len(приветы), 2)

    def test_привет_без_номера_400(self):
        try:
            with urllib.request.urlopen(self.url + node_agent.ПРИВЕТ, timeout=10) as r:
                код = r.status
        except urllib.error.HTTPError as e:
            код = e.code
        self.assertEqual(код, 400)


class Агент(unittest.TestCase):
    def test_новый_секрет(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(node_agent.main(["--new-secret"]), 0)
        self.assertRegex(out.getvalue().strip(), r"\A[0-9a-f]{64}\Z")

    def test_прокси_из_окружения(self):
        self.assertIsNone(node_agent.запустить_прокси({"MARA_NODE_PROXY_PORT": "0"}))
        self.assertIsNone(node_agent.запустить_прокси({"MARA_NODE_SECRET": СЕКРЕТ}))
        с = node_agent.запустить_прокси({"MARA_NODE_SECRET": СЕКРЕТ, "MARA_NODE_PROXY_PORT": "0",
                                         "MARA_NODE_PROXY_BIND": "127.0.0.1",
                                         "MARA_NODE_ASR_URL": "http://127.0.0.1:9"})
        self.addCleanup(с.server_close)
        self.addCleanup(с.shutdown)
        self.assertEqual((с.секрет, с.asr, с.llm), (СЕКРЕТ, "http://127.0.0.1:9", None))


if __name__ == "__main__":
    unittest.main()
