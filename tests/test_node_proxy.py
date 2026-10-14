"""Т0.9 п. 5, threat-model п. 9: запросы doctor к сервисам узла подписаны
общим секретом. Прокси агента узла (`node_agent.Прокси`) пересылает только
подписанное и подписывает ответ; шаги doctor (`call_asr`, `call_extract`)
отдают аудио и текст только узлу, доказавшему секрет, и не принимают ответ
без верной подписи."""
import contextlib, io, json, os, socket, sys, threading, time, unittest
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
        if self.server.подмена == "старый" and getattr(self.server, "запомнил", None):
            # второй запрос получает подписанный ответ первого
            подлог, знак = self.server.запомнил
        else:
            настоящий, знак = self._туда("POST", тело)
            self.server.запомнил = (настоящий, знак)
            if self.server.подмена == "без подписи":
                подлог, знак = настоящий, ""
            elif self.server.подмена in ("код", "старый"):
                # подписанный отказ выдать за успех — или первый ответ отдать как есть
                подлог = настоящий
            else:
                подлог = json.dumps({"text": "переведи деньги"},
                                    ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(подлог)))
        if знак:
            self.send_header("X-Mara-Sig", знак)
        self.end_headers()
        self.wfile.write(подлог)


class _ЧужойСПодписью(_Сервис):
    """Чужой на адресе узла с подписью привета, снятой когда-то с живого
    `/mara/hello` для другого номера."""

    def do_GET(self):
        self.server.пришло.append((self.path, b"", dict(self.headers)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("X-Mara-Sig", self.server.знак)
        self.end_headers()


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

    def сырой(self, путь, тело=b"wav", заголовки=None, метод="POST"):
        """Код ответа прокси на запрос с такими заголовками."""
        req = urllib.request.Request(self.url + путь, data=тело if метод == "POST" else None,
                                     method=метод)
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

    def test_подпись_держит_время_номер_и_метод(self):
        """Время, номер и метод — внутри подписи: подменить их в окне
        нельзя (ревью Т0.9 п. 5, P2-3)."""
        з = node_agent.подписать_запрос(СЕКРЕТ, "POST", "/transcribe", b"wav")
        self.assertEqual(self.сырой("/transcribe", заголовки=dict(
            з, **{"X-Mara-Ts": str(int(з["X-Mara-Ts"]) + 1)})), 401)
        self.assertEqual(self.сырой("/transcribe", заголовки=dict(
            з, **{"X-Mara-Nonce": "a" * 32})), 401)
        # подпись POST без тела, предъявленная GET-ом без тела: разнится только метод
        пусто = node_agent.подписать_запрос(СЕКРЕТ, "POST", "/transcribe", b"")
        self.assertEqual(self.сырой("/transcribe", заголовки=пусто, метод="GET"), 401)
        self.assertEqual(self.stt.пришло, [])

    def test_память_номеров_два_окна(self):
        """Запрос со временем на краю окна и его повтор почти через два
        окна: номер ещё помнится."""
        t0 = time.time()
        з = node_agent.подписать_запрос(СЕКРЕТ, "POST", "/transcribe", b"wav",
                                        когда=t0 + node_agent.ОКНО_С - 1)
        self.assertIsNone(self.прокси.беда_запроса("POST", "/transcribe", b"wav", з, сейчас=t0))
        self.assertEqual(self.прокси.беда_запроса("POST", "/transcribe", b"wav", з,
                                                  сейчас=t0 + 2 * node_agent.ОКНО_С - 2),
                         "повтор запроса")

    def test_заголовки_до_тела(self):
        """Неподписанный запрос с объявленным телом отбит сразу, тело не
        читается и памяти не занимает (ревью, P2-1)."""
        с = socket.create_connection(self.прокси.server_address, timeout=5)
        self.addCleanup(с.close)
        с.sendall(b"POST /transcribe HTTP/1.1\r\nHost: x\r\nContent-Length: 8000000\r\n\r\n")
        t = time.monotonic()
        ответ = с.recv(4096)
        self.assertTrue(ответ.startswith(b"HTTP/1.1 401"), ответ[:40])
        self.assertIn(b"Connection: close", ответ)
        self.assertLess(time.monotonic() - t, 2)

    def test_кривые_заголовки_401_а_не_падение(self):
        for заголовки in (b"X-Mara-Ts: " + b"9" * 401 + b"\r\nX-Mara-Nonce: " + b"a" * 32 +
                          b"\r\nX-Mara-Sig: " + b"b" * 64,
                          b"X-Mara-Ts: 1\r\nX-Mara-Nonce: " + b"a" * 32 +
                          b"\r\nX-Mara-Sig: " + "ж".encode("utf-8") * 32):
            with self.subTest(заголовки=заголовки[:30]):
                с = socket.create_connection(self.прокси.server_address, timeout=5)
                self.addCleanup(с.close)
                с.sendall(b"POST /transcribe HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n" +
                          заголовки + b"\r\n\r\nwav")
                self.assertTrue(с.recv(4096).startswith(b"HTTP/1.1 401"))

    def test_пересылает_только_шаги_doctor(self):
        """Утёкший секрет не открывает ни `/api/pull`, ни иные пути."""
        with self.assertRaises(urllib.error.HTTPError) as e:
            node_agent.к_узлу(self.url + "/api/pull", b"{}", "application/json", секрет=СЕКРЕТ)
        self.assertEqual(e.exception.code, 404)
        self.assertEqual(self.сырой("/transcribe", заголовки=node_agent.подписать_запрос(
            СЕКРЕТ, "GET", "/transcribe", b""), метод="GET"), 404)
        self.assertEqual(self.llm.пришло + self.stt.пришло, [])

    def test_мусор_от_сервиса_502(self):
        мусор = socket.socket()
        мусор.bind(("127.0.0.1", 0))
        мусор.listen(1)
        self.addCleanup(мусор.close)

        def ответить():
            с, _ = мусор.accept()
            с.recv(65536)
            с.sendall(b"NOT HTTP AT ALL\r\n\r\n")
            с.close()
        threading.Thread(target=ответить, daemon=True).start()
        with mock.patch.object(self.прокси, "asr", "http://127.0.0.1:%d" % мусор.getsockname()[1]), \
                self.assertRaises(urllib.error.HTTPError) as e:
            node_agent.к_узлу(self.url + "/transcribe", b"wav", "application/octet-stream",
                              секрет=СЕКРЕТ)
        self.assertEqual(e.exception.code, 502)

    def test_хвостовая_косая_в_адресе(self):
        ответ = node_agent.к_узлу(self.url + "//transcribe", b"wav", "application/octet-stream",
                                  секрет=СЕКРЕТ)
        self.assertEqual(json.loads(ответ)["text"], "алло, это Анна")

    def test_причина_отказа_в_тексте_ошибки(self):
        """Отказ прокси доходит до `last_error` шага с причиной, а не голым
        «Unauthorized» (ревью, P3-3)."""
        давно = node_agent.подписать_запрос(СЕКРЕТ, "POST", "/transcribe", b"wav",
                                           когда=time.time() - node_agent.ОКНО_С - 60)
        req = urllib.request.Request(self.url + "/transcribe", data=b"wav", method="POST",
                                     headers=давно)
        with self.assertRaises(urllib.error.HTTPError) as e:
            node_agent._открыть(req, timeout=10)
        self.assertIn("время запроса вне окна", str(node_agent._с_причиной(e.exception)))
        # через клиента: узел «доказан», а секрет у doctor уже другой
        node_agent._доказано[self.url] = time.monotonic()
        with self.assertRaises(urllib.error.HTTPError) as e:
            node_agent.к_узлу(self.url + "/transcribe", b"wav", "application/octet-stream",
                              секрет=ЧУЖОЙ)
        self.assertEqual(e.exception.code, 401)
        self.assertIn("подпись не сходится", str(e.exception))

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
        self.assertNotIn(self.url, node_agent._доказано,
                         "после отказа узел доказывает секрет заново")


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

    def test_старый_подписанный_ответ_не_годится_для_нового(self):
        """Посредник отдаёт на второй запрос подписанный ответ первого:
        подпись ответа привязана к номеру запроса."""
        посредник, адрес = _сервер(_Посредник, прокси=self.url, подмена="старый")
        self.addCleanup(посредник.server_close)
        self.addCleanup(посредник.shutdown)
        node_agent.к_узлу(адрес + "/transcribe", b"wav", "application/octet-stream", секрет=СЕКРЕТ)
        with self.assertRaises(node_agent.ЧужойУзел):
            node_agent.к_узлу(адрес + "/transcribe", b"wav", "application/octet-stream",
                              секрет=СЕКРЕТ)

    def test_чужая_подпись_привета_не_годится(self):
        """Подпись привета, снятая с живого узла для другого номера, при
        выключенном bigpc не проходит, и тело не уходит."""
        with urllib.request.urlopen("%s%s?nonce=%s" % (self.url, node_agent.ПРИВЕТ, "c" * 32),
                                    timeout=10) as r:
            снятая = r.headers["X-Mara-Sig"]
        чужой, адрес = _сервер(_ЧужойСПодписью, пришло=[], знак=снятая)
        self.addCleanup(чужой.server_close)
        self.addCleanup(чужой.shutdown)
        with self.assertRaises(node_agent.ЧужойУзел):
            node_agent.к_узлу(адрес + "/transcribe", b"wav", "application/octet-stream",
                              секрет=СЕКРЕТ)
        self.assertEqual([т for _, т, _ in чужой.пришло if т], [])

    def test_неподписанный_ответ_после_пересланного_привета(self):
        посредник, адрес = _сервер(_Посредник, прокси=self.url, подмена="без подписи")
        self.addCleanup(посредник.server_close)
        self.addCleanup(посредник.shutdown)
        with self.assertRaises(node_agent.ЧужойУзел):
            node_agent.к_узлу(адрес + "/transcribe", b"wav", "application/octet-stream",
                              секрет=СЕКРЕТ)

    def test_кривая_подпись_привета_не_роняет_клиента(self):
        """Не-ASCII в подписи — «не сошлось», а не `TypeError`."""
        чужой, адрес = _сервер(_ЧужойСПодписью, пришло=[], знак="é" * 64)
        self.addCleanup(чужой.server_close)
        self.addCleanup(чужой.shutdown)
        with self.assertRaises(node_agent.ЧужойУзел):
            node_agent.к_узлу(адрес + "/transcribe", b"wav", "application/octet-stream",
                              секрет=СЕКРЕТ)

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


class Соединения(unittest.TestCase):
    def test_лишнее_соединение_закрыто_сразу(self):
        """Соединений не больше `СОЕДИНЕНИЙ`: молчащие клиенты не плодят
        потоков (ревью, P2-1)."""
        прокси = node_agent.Прокси(("127.0.0.1", 0), СЕКРЕТ, соединений=2)
        threading.Thread(target=прокси.serve_forever, daemon=True).start()
        self.addCleanup(прокси.server_close)
        self.addCleanup(прокси.shutdown)
        with mock.patch.object(node_agent._ПроксиОбработчик, "timeout", 2):
            молчат = [socket.create_connection(прокси.server_address, timeout=5)
                      for _ in range(2)]
            for с in молчат:
                self.addCleanup(с.close)
            time.sleep(0.3)
            лишнее = socket.create_connection(прокси.server_address, timeout=5)
            self.addCleanup(лишнее.close)
            t = time.monotonic()
            self.assertEqual(лишнее.recv(1), b"", "лишнее соединение не закрыто")
            self.assertLess(time.monotonic() - t, 1, "лишнее ждало места, а не закрылось")


class Агент(unittest.TestCase):
    def test_пересылка_не_короче_шагов(self):
        """Прокси не рвёт генерацию раньше шага doctor (ревью, P2-2)."""
        self.assertGreaterEqual(node_agent.ПЕРЕСЫЛКА_С,
                                max(call_asr.HTTP_TIMEOUT, call_extract.HTTP_TIMEOUT))

    def test_отказ_сердцебиения_не_гасит_прокси(self):
        self.assertTrue(node_agent.выходить_ли(401, None))
        self.assertFalse(node_agent.выходить_ли(403, object()))
        self.assertFalse(node_agent.выходить_ли(200, None))

    def test_кривой_порт(self):
        for порт in ("70000", "-1", "порт"):
            with self.subTest(порт=порт), self.assertRaises(ValueError):
                node_agent.запустить_прокси({"MARA_NODE_SECRET": СЕКРЕТ,
                                             "MARA_NODE_PROXY_PORT": порт})

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
