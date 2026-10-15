"""Т0.9 п. 2: TLS на contextd и пин сертификата у клиентов.

Сертификат выпускает `contextd.tls_новый` — тем же `openssl`, что на doctor;
нет `openssl` — тесты с сертификатом пропускаются (счёт `skipped` в выводе).
Клиентов два, и код пина у них один и тот же, переписанный: агент узла
(`scripts/node_agent.py`) и плагин Мары (`install/mara-context`), который
ставится на мак без `scripts/`. Оба гоняются против настоящего `contextd`."""
import contextlib
import http.client, importlib.util, io, json, os, shutil, socket, ssl, subprocess, sys
import tempfile, threading, time, unittest, urllib.error, urllib.request
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import mara_ingest as mi
import contextd
import node_agent


def плагин():
    spec = importlib.util.spec_from_file_location(
        "mara_context_tls", os.path.join(HERE, "..", "install", "mara-context", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def выпустить():
    """(каталог, сертификат, ключ, пин) или SkipTest без openssl."""
    if not shutil.which("openssl"):
        raise unittest.SkipTest("нет openssl — сертификат не выпустить")
    каталог = tempfile.mkdtemp()
    cert, key, пин = contextd.tls_новый(os.path.join(каталог, "tls"))
    return каталог, cert, key, пин


ЧУЖОЙ = "sha256:" + "00" * 32


class Пин(unittest.TestCase):
    """Запись пина и правило «пин — только с https://» у обоих клиентов."""

    def test_записи_пина_одинаковы_у_обоих(self):
        п = "9f" * 32
        двоеточия = ":".join(п[i:i + 2].upper() for i in range(0, 64, 2))
        м = плагин()
        for запись in ("sha256:" + п, "SHA256:" + п.upper(), "sha256 Fingerprint=" + двоеточия,
                       "SHA256 Fingerprint=" + двоеточия, двоеточия, п, " %s\n" % п):
            with self.subTest(запись=запись):
                self.assertEqual(node_agent.нормальный_пин(запись), п)
                self.assertEqual(м.нормальный_пин(запись), п)
        for кривой in ("", None, "sha256:", п[:-1], п + "0", "g" * 64, "sha1:" + п,
                       "md5 Fingerprint=" + двоеточия):
            with self.subTest(кривой=кривой):
                self.assertRaises(ValueError, node_agent.нормальный_пин, кривой)
                self.assertRaises(ValueError, м.нормальный_пин, кривой)

    def test_пин_только_с_https(self):
        м = плагин()
        п = "sha256:" + "ab" * 32
        for беда_пина in (node_agent.беда_пина, м.беда_пина):
            with self.subTest(клиент=беда_пина.__module__):
                self.assertIsNone(беда_пина("https://doctor:8789", п))
                self.assertIsNone(беда_пина("HTTPS://doctor:8789", п))
                self.assertIsNone(беда_пина("http://doctor:8788", None), "без пина — как раньше")
                self.assertIn("не https", беда_пина("http://doctor:8788", п))
                self.assertIn("не https", беда_пина("doctor:8789", п))
                self.assertIn("64 знака", беда_пина("https://doctor:8789", "sha256:abc"))

    def test_агент_с_пином_и_http_не_стартует(self):
        env = {"MARA_URL": "http://127.0.0.1:9", "MARA_NODE_TOKEN": "t",
               "MARA_CONTEXT_PIN": "sha256:" + "ab" * 32}
        вывод = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(вывод), \
             mock.patch.object(node_agent, "отправить") as отправить:
            self.assertEqual(node_agent.main(["--once"]), 2)
        отправить.assert_not_called()
        self.assertIn("не https", вывод.getvalue())

    def test_агент_отдаёт_пин_отправке(self):
        env = {"MARA_URL": "https://127.0.0.1:9", "MARA_NODE_TOKEN": "t",
               "MARA_CONTEXT_PIN": "sha256:" + "ab" * 32}
        with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()), \
             mock.patch.object(node_agent, "отправить", return_value=200) as отправить:
            self.assertEqual(node_agent.main(["--once"]), 0)
        self.assertEqual(отправить.call_args.args[3], env["MARA_CONTEXT_PIN"])

    def test_плагин_берёт_пин_из_настроек_hermes(self):
        м = плагин()
        настройки = {"url": "http://127.0.0.1:9", "pin": "sha256:" + "ab" * 32}
        хук = {}

        class Ctx:
            def get_config(self, k):
                return настройки.get(k)

            def register_hook(self, name, fn):
                хук[name] = fn

            def register_tool(self, name, **kw):
                pass

        м.register(Ctx())
        with self.assertLogs(м.log, "WARNING") as журнал:
            self.assertEqual(хук["pre_llm_call"](conversation_history=[]), "")
        self.assertIn("не https", "\n".join(журнал.output))

    def test_агент_с_кривым_пином_держит_прокси(self):
        """Опечатка в пине гасит сердцебиение, но не прокси: иначе вставала
        бы и расшифровка (ревью, P3-7)."""
        env = {"MARA_URL": "http://127.0.0.1:9", "MARA_NODE_TOKEN": "t",
               "MARA_CONTEXT_PIN": "sha256:" + "ab" * 32}
        прокси = mock.Mock(server_address=("127.0.0.1", 1))
        with mock.patch.dict(os.environ, env), redirect_stdout(io.StringIO()), \
             mock.patch.object(node_agent, "запустить_прокси", return_value=прокси), \
             mock.patch.object(node_agent, "отправить") as отправить, \
             mock.patch.object(node_agent.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                node_agent.main([])
        отправить.assert_not_called()

    def test_редирект_с_пином_не_уносит_токен(self):
        """P3-1: 302 с запиненного сервера на http:// — отказ, а не токен
        открытым текстом; прямой `отправить(http, пин)` — тоже отказ."""
        _, cert, key, пин = выпустить()
        ctx = contextd.tls_контекст(cert, key)
        ловушка = []
        слушатель = socket.socket()
        слушатель.bind(("127.0.0.1", 0))
        слушатель.listen(2)

        def сервер():
            while True:
                try:
                    s, _ = слушатель.accept()
                except OSError:
                    return
                s.settimeout(5)
                try:
                    with ctx.wrap_socket(s, server_side=True) as t:
                        # запрос читается целиком: ответ до конца тела клиент
                        # потерял бы в закрытии соединения
                        сырой = b""
                        while b"\r\n\r\n" not in сырой:
                            сырой += t.recv(65536)
                        шапка, тело = сырой.split(b"\r\n\r\n", 1)
                        n = 0
                        for строка in шапка.split(b"\r\n"):
                            if строка.lower().startswith(b"content-length:"):
                                n = int(строка.split(b":")[1])
                        while len(тело) < n:
                            тело += t.recv(65536)
                        t.sendall(b"HTTP/1.1 302 Found\r\n"
                                  b"Location: http://127.0.0.1:1/caught\r\n"
                                  b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

        поток = threading.Thread(target=сервер, daemon=True)
        поток.start()
        base = "https://127.0.0.1:%d" % слушатель.getsockname()[1]
        try:
            м = плагин()
            with mock.patch.object(urllib.request.HTTPHandler, "http_open",
                                   side_effect=lambda *a: ловушка.append(a)) as http_open:
                self.assertEqual(node_agent.отправить(base, "secret-token", {"a": 1}, пин),
                                 302, "редирект — не ответ doctor")
                б = м.Брокер(base, token="t", pin=пин)
                ответ = б.правка({"item": "x", "status": "done"})
                self.assertNotIn("уже принимал", ответ)
            http_open.assert_not_called()
            self.assertEqual(ловушка, [])
            вывод = io.StringIO()
            with redirect_stdout(вывод):
                self.assertEqual(node_agent.отправить(
                    "http://127.0.0.1:1", "secret-token", {"a": 1}, пин), 0)
            self.assertIn("http://", вывод.getvalue())
        finally:
            слушатель.close()

    def test_куски_пина_в_плагине_и_агенте_одинаковы(self):
        """Плагин ставится на мак без `scripts/` и несёт копию кода пина:
        разойдутся — этот тест и скажет (ревью, M2)."""
        import re

        def блоки(путь):
            with open(os.path.join(HERE, путь), encoding="utf-8") as fh:
                текст = fh.read()
            out = {}
            for m in re.finditer(r"(?ms)^(?:class|def) ([\w_]+)\b.*?(?=^\S|\Z)", текст):
                out[m.group(1)] = [
                    л for л in m.group(0).rstrip().splitlines()
                    # у агента `беда_пина` несёт параметр-имя переменной —
                    # сравнивается остов без этих строк и без докстрок
                    if "MARA_NODE_PIN" not in л and "имя" not in л
                    and not л.strip().startswith(('\x22', "#"))]
            return out
        а = блоки(os.path.join("..", "scripts", "node_agent.py"))
        б = блоки(os.path.join("..", "install", "mara-context", "__init__.py"))
        for кусок in ("нормальный_пин", "_ПоПину", "ПинHTTPS", "беда_пина",
                      "ЧужойСертификат", "_ТолькоTLS", "_БезРедиректов"):
            with self.subTest(кусок=кусок):
                self.assertEqual(а[кусок], б[кусок], кусок)

    def test_минимальная_версия_обеих_сторон(self):
        """M3: и сервер, и клиенты — не ниже TLS 1.2."""
        _, cert, key, _ = выпустить()
        self.assertEqual(contextd.tls_контекст(cert, key).minimum_version,
                         ssl.TLSVersion.TLSv1_2)
        м = плагин()
        for обработчик in (node_agent.ПинHTTPS("ab" * 32), м.ПинHTTPS("ab" * 32)):
            self.assertEqual(обработчик._context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_пустой_сертификат_сессии_отказ(self):
        """M1: `getpeercert` без DER — отказ, а не пропуск сверки."""
        заглушка = mock.Mock()
        заглушка.getpeercert.return_value = None
        for модуль in (node_agent, плагин()):
            с = модуль._ПоПину.__new__(модуль._ПоПину)
            с.пин = "ab" * 32
            с.sock = заглушка
            with mock.patch.object(http.client.HTTPSConnection, "connect"):
                with self.assertRaises(модуль.ЧужойСертификат):
                    с.connect()

    def test_плагин_с_пином_и_http_в_сеть_не_ходит(self):
        м = плагин()
        б = м.Брокер("http://127.0.0.1:9", token="t", pin="sha256:" + "ab" * 32)
        with mock.patch("urllib.request.urlopen") as сеть, \
             mock.patch("urllib.request.OpenerDirector.open") as открыть:
            self.assertIn("не https", б.справка({"about": "Анна"}))
            self.assertIn("не https", б.правка({"item": "смета", "status": "done"}))
            self.assertEqual(б.контекст(), "")
        сеть.assert_not_called()
        открыть.assert_not_called()

    def test_пин_из_окружения_читается_на_каждый_поход(self):
        """Как и токен: .env Hermes грузится не раньше плагинов."""
        м = плагин()
        б = м.Брокер("http://127.0.0.1:9", token="t")
        with mock.patch.dict(os.environ, {"MARA_CONTEXT_PIN": "sha256:" + "ab" * 32}):
            self.assertIn("не https", б.справка({"about": "Анна"}))


class Настройка(unittest.TestCase):
    def test_окружение(self):
        self.assertIsNone(contextd.tls_настройка({}))
        self.assertIsNone(contextd.tls_настройка({"MARA_TLS_CERT": "", "MARA_TLS_KEY": ""}))
        self.assertEqual(contextd.tls_настройка({"MARA_TLS_CERT": "c", "MARA_TLS_KEY": "k"}),
                         ("c", "k", 8789))
        self.assertEqual(contextd.tls_настройка({"MARA_TLS_CERT": "c", "MARA_TLS_KEY": "k",
                                                 "MARA_TLS_PORT": "9443"}), ("c", "k", 9443))
        for env in ({"MARA_TLS_CERT": "c"}, {"MARA_TLS_KEY": "k"},
                    {"MARA_TLS_CERT": "c", "MARA_TLS_KEY": "k", "MARA_TLS_PORT": "x"},
                    {"MARA_TLS_CERT": "c", "MARA_TLS_KEY": "k", "MARA_TLS_PORT": "0"},
                    {"MARA_TLS_CERT": "c", "MARA_TLS_KEY": "k", "MARA_TLS_PORT": "70000"},
                    {"MARA_TLS_CERT": "c", "MARA_TLS_KEY": "k", "MARA_TLS_PORT": "-1"}):
            with self.subTest(env=env):
                self.assertRaises(ValueError, contextd.tls_настройка, env)

    def test_команда_отдаёт_tls_демону(self):
        with mock.patch.dict(os.environ, {"MARA_TLS_CERT": "c", "MARA_TLS_KEY": "k"}), \
             mock.patch.object(contextd, "serve", return_value=0) as serve:
            os.environ.pop("MARA_TLS_PORT", None)
            self.assertEqual(contextd.main(["--serve", "--root", tempfile.mkdtemp()]), 0)
        self.assertEqual(serve.call_args.kwargs["tls"], ("c", "k", 8789))

    def test_половина_пары_не_стартует(self):
        вывод = io.StringIO()
        with mock.patch.dict(os.environ, {"MARA_TLS_CERT": "/нет/такого"}), \
             mock.patch.object(contextd, "serve") as serve, redirect_stdout(вывод):
            os.environ.pop("MARA_TLS_KEY", None)
            self.assertEqual(contextd.main(["--serve", "--root", tempfile.mkdtemp()]), 2)
        serve.assert_not_called()
        self.assertIn("MARA_TLS_KEY", вывод.getvalue())

    def test_битый_сертификат_не_стартует(self):
        """Ни воркера, ни открытого порта: молча слушать только его значило
        бы оставить токены на проводе."""
        каталог = tempfile.mkdtemp()
        cert = os.path.join(каталог, "c.crt")
        with open(cert, "w") as fh:
            fh.write("не сертификат")
        вывод = io.StringIO()
        # сервер не поднимается вовсе: поднятый открытый ушёл бы в
        # serve_forever, и тест висел бы, а не падал
        with mock.patch.object(contextd, "worker") as воркер, \
             mock.patch.object(contextd, "make_server",
                               side_effect=AssertionError("сервер поднят")) as сервер, \
             redirect_stdout(вывод):
            self.assertEqual(contextd.serve(каталог, 0, tls=(cert, cert, 0)), 2)
            self.assertEqual(contextd.serve(каталог, 0, tls=(cert + ".нет", cert, 0)), 2)
        воркер.assert_not_called()
        сервер.assert_not_called()
        self.assertIn("TLS не поднялся", вывод.getvalue())

    def test_один_порт_на_двоих_не_стартует(self):
        вывод = io.StringIO()
        with mock.patch.object(contextd, "worker") as воркер, \
             mock.patch.object(contextd, "make_server",
                               side_effect=AssertionError("сервер поднят")), \
             redirect_stdout(вывод):
            self.assertEqual(contextd.serve(tempfile.mkdtemp(), 8788,
                                            tls=("c", "k", 8788)), 2)
        воркер.assert_not_called()
        self.assertIn("совпал", вывод.getvalue())


class Сертификат(unittest.TestCase):
    def test_выпуск(self):
        каталог, cert, key, пин = выпустить()
        self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(key)).st_mode & 0o777, 0o700)
        отпечаток = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-fingerprint",
                                    "-sha256"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(node_agent.нормальный_пин(отпечаток.strip()),
                         node_agent.нормальный_пин(пин), "пин — отпечаток, как у openssl")
        self.assertEqual(contextd.пин_сертификата(cert), пин)
        # сертификат и ключ — пара, и контекст из них собирается
        contextd.tls_контекст(cert, key)

    def test_второй_выпуск_не_перезаписывает(self):
        каталог, cert, key, пин = выпустить()
        with open(key, "rb") as fh:
            было = fh.read()
        with self.assertRaises(FileExistsError):
            contextd.tls_новый(os.path.dirname(cert))
        with open(key, "rb") as fh:
            self.assertEqual(fh.read(), было)
        self.assertEqual(contextd.пин_сертификата(cert), пин)

    def test_чужой_сертификат_без_ключа_не_перезаписывается(self):
        """Остался только сертификат (ключ убрали руками) — всё равно
        отказ: молча выпущенный новый сменил бы пин у всех клиентов."""
        if not shutil.which("openssl"):
            self.skipTest("нет openssl")
        каталог = os.path.join(tempfile.mkdtemp(), "tls")
        os.makedirs(каталог)
        cert = os.path.join(каталог, "contextd.crt")
        with open(cert, "w") as fh:
            fh.write("старый")
        with self.assertRaises(FileExistsError):
            contextd.tls_новый(каталог)
        with open(cert) as fh:
            self.assertEqual(fh.read(), "старый")
        self.assertEqual(os.listdir(каталог), ["contextd.crt"], "ключ не заведён")

    def test_сбой_openssl_не_оставляет_файлов(self):
        каталог = os.path.join(tempfile.mkdtemp(), "tls")
        with mock.patch.object(contextd.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, "openssl")):
            self.assertRaises(subprocess.CalledProcessError, contextd.tls_новый, каталог)
        self.assertEqual(os.listdir(каталог), [])

    def test_командой(self):
        if not shutil.which("openssl"):
            self.skipTest("нет openssl")
        каталог = os.path.join(tempfile.mkdtemp(), "tls")
        вывод = io.StringIO()
        with redirect_stdout(вывод):
            self.assertEqual(contextd.main(["--tls-new", каталог]), 0)
        cert = os.path.join(каталог, "contextd.crt")
        пин = contextd.пин_сертификата(cert)
        self.assertIn("MARA_CONTEXT_PIN=" + пин, вывод.getvalue())
        self.assertIn("MARA_TLS_KEY=" + os.path.join(каталог, "contextd.key"), вывод.getvalue())
        вывод = io.StringIO()
        with redirect_stdout(вывод):
            self.assertEqual(contextd.main(["--tls-pin", cert]), 0)
            self.assertEqual(contextd.main(["--tls-new", каталог]), 1, "второй раз — отказ")
            self.assertEqual(contextd.main(["--tls-pin", cert + ".нет"]), 1)
        self.assertEqual(вывод.getvalue().splitlines()[0], пин)

    def test_пин_не_из_сертификата(self):
        путь = os.path.join(tempfile.mkdtemp(), "x.pem")
        with open(путь, "w") as fh:
            fh.write("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")
        self.assertRaises(ValueError, contextd.пин_сертификата, путь)


class Провод(unittest.TestCase):
    """Настоящий `contextd` за TLS на свободном порту."""

    @classmethod
    def setUpClass(cls):
        cls.каталог, cls.cert, cls.key, cls.пин = выпустить()
        cls.root = tempfile.mkdtemp()
        cls.vault = tempfile.mkdtemp()
        cls.srv = contextd.make_server(cls.root, port=0, vault=cls.vault,
                                       tls=contextd.tls_контекст(cls.cert, cls.key))
        cls.порт = cls.srv.server_address[1]
        cls.base = "https://127.0.0.1:%d" % cls.порт
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        con = mi.connect(cls.root)
        cls.узел, cls.токен_узла = contextd.pair_node(con, "bigpc")
        _, cls.токен = contextd.pair(con, "плагин")
        con.close()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def запрос(self, путь, пин=None):
        with node_agent._без_прокси(пин or self.пин).open(self.base + путь, timeout=5) as r:
            return r.status, json.loads(r.read())

    def test_по_пину_отвечает(self):
        код, тело = self.запрос("/healthz")
        self.assertEqual((код, тело["ok"]), (200, True))

    def test_без_пина_самоподписанный_не_принят(self):
        """https:// без пина — системное доверие, и самоподписанный ему чужой:
        отказ, а не тихое «поверю кому угодно»."""
        with self.assertRaises(urllib.error.URLError) as e:
            urllib.request.urlopen(self.base + "/healthz", timeout=5)
        self.assertIsInstance(e.exception.reason, ssl.SSLCertVerificationError)

    def test_чужой_пин_отказ(self):
        with self.assertRaises(urllib.error.URLError) as e:
            self.запрос("/healthz", пин=ЧУЖОЙ)
        self.assertIsInstance(e.exception.reason, node_agent.ЧужойСертификат)

    def test_http_на_порт_tls(self):
        было = contextd._сорвалось["tls"]
        вывод = io.StringIO()
        with redirect_stdout(вывод):
            c = http.client.HTTPConnection("127.0.0.1", self.порт, timeout=5)
            try:
                c.request("GET", "/healthz")
                c.getresponse().read()
            except (OSError, http.client.HTTPException):
                pass
            finally:
                c.close()
            for _ in range(50):
                if contextd._сорвалось["tls"] > было:
                    break
                time.sleep(0.05)
        self.assertEqual(contextd._сорвалось["tls"], было + 1)
        self.assertIn("tls-рукопожатие HTTP_REQUEST from=127.0.0.1", вывод.getvalue())
        self.assertEqual(self.запрос("/healthz")[0], 200, "сервер жив после чужого")

    def test_молчун_не_держит_приём(self):
        """Соединился и молчит — свой поток и не дольше `РУКОПОЖАТИЕ_С`;
        остальных принимают сразу."""
        with mock.patch.object(contextd, "РУКОПОЖАТИЕ_С", 0.5), redirect_stdout(io.StringIO()):
            молчун = socket.create_connection(("127.0.0.1", self.порт), timeout=5)
            try:
                начало = time.monotonic()
                self.assertEqual(self.запрос("/healthz")[0], 200)
                self.assertLess(time.monotonic() - начало, 3)
                # сервер сам рвёт молчуна: recv отдаёт конец потока
                молчун.settimeout(5)
                self.assertEqual(молчун.recv(1), b"")
                self.assertLess(time.monotonic() - начало, 4)
            finally:
                молчун.close()

    def test_сердцебиение_узла_по_пину(self):
        тело = {"capabilities": [], "checked": [], "accept_jobs": True,
                "minecraft_active": False, "interval_s": 60}
        self.assertEqual(node_agent.отправить(self.base, self.токен_узла, тело, self.пин), 200)
        вывод = io.StringIO()
        with redirect_stdout(вывод):
            self.assertEqual(node_agent.отправить(self.base, self.токен_узла, тело, ЧУЖОЙ), 0)
        self.assertIn("не совпал с пином", вывод.getvalue())
        self.assertEqual(node_agent.отправить(self.base, self.токен_узла, тело), 0,
                         "https без пина — системное доверие, самоподписанный не пройдёт")

    def test_плагин_по_пину(self):
        м = плагин()
        б = м.Брокер(self.base, token=self.токен, pin=self.пин)
        self.assertEqual(б._достать(), None, "пакета нет — None, но поход состоялся")
        ответ = б.справка({"about": "Анна"})
        self.assertNotIn("дозвонился", ответ)
        self.assertIn("Анна", ответ)
        чужой = м.Брокер(self.base, token=self.токен, pin=ЧУЖОЙ)
        self.assertIn("не совпал с MARA_CONTEXT_PIN", чужой.справка({"about": "Анна"}))
        self.assertIn("не совпал с MARA_CONTEXT_PIN", чужой.правка({"item": "x", "status": "done"}))


class ЧужомуНичего(unittest.TestCase):
    """Пин не сошёлся — серверу не ушло ни байта: ни токена, ни тела.

    Сервер здесь — заглушка TLS с другим сертификатом, она записывает всё,
    что пришло после рукопожатия."""

    def test_ни_байта(self):
        _, cert, key, _ = выпустить()
        ctx = contextd.tls_контекст(cert, key)
        слушатель = socket.socket()
        слушатель.bind(("127.0.0.1", 0))
        слушатель.listen(1)
        пришло = []

        def принять():
            s, _ = слушатель.accept()
            s.settimeout(5)
            try:
                with ctx.wrap_socket(s, server_side=True) as t:
                    куски = []
                    while True:
                        try:
                            кусок = t.recv(65536)
                        except (OSError, ssl.SSLError):
                            break
                        if not кусок:
                            break
                        куски.append(кусок)
                    пришло.append(b"".join(куски))
            except (OSError, ssl.SSLError):
                пришло.append(b"")

        поток = threading.Thread(target=принять, daemon=True)
        поток.start()
        base = "https://127.0.0.1:%d" % слушатель.getsockname()[1]
        try:
            тело = {"interval_s": 60}
            вывод = io.StringIO()
            with redirect_stdout(вывод):
                # токен латиницей: кириллица не кодируется в заголовок, и
                # запрос упал бы до соединения — проверять было бы нечего
                self.assertEqual(node_agent.отправить(base, "secret-token-0001", тело, ЧУЖОЙ), 0)
            self.assertIn("не совпал с пином", вывод.getvalue())
            поток.join(10)
        finally:
            слушатель.close()
        self.assertEqual(пришло, [b""], "после рукопожатия — ни байта запроса")


class Демон(unittest.TestCase):
    def test_serve_поднимает_оба_порта(self):
        """`serve` с TLS: открытый и TLS слушают оба, по TLS отвечает тот же
        демон (пин сходится), а по открытому — как раньше."""
        каталог, cert, key, пин = выпустить()
        поднятые = []

        def make_server(*a, **kw):
            srv = настоящий(*a, **kw)
            поднятые.append(srv)
            return srv
        настоящий = contextd.make_server

        def свободный():
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]
        открытый, защищённый = свободный(), свободный()
        root = tempfile.mkdtemp()
        вывод = io.StringIO()
        with mock.patch.object(contextd, "make_server", side_effect=make_server), \
             mock.patch.object(contextd, "worker"), redirect_stdout(вывод):
            поток = threading.Thread(target=contextd.serve, daemon=True, args=(
                root, открытый, "127.0.0.1", (cert, key, защищённый)))
            поток.start()
            try:
                for _ in range(100):
                    if len(поднятые) == 2:
                        break
                    time.sleep(0.05)
                with node_agent._без_прокси(пин).open(
                        "https://127.0.0.1:%d/healthz" % защищённый, timeout=5) as r:
                    self.assertEqual(r.status, 200)
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        "http://127.0.0.1:%d/healthz" % открытый, timeout=5) as r:
                    self.assertEqual(r.status, 200)
            finally:
                for srv in поднятые:
                    srv.shutdown()
                    srv.server_close()
                поток.join(5)
        self.assertIn("TLS на 127.0.0.1:%d, пин %s" % (защищённый, пин), вывод.getvalue())


class Обрыв(unittest.TestCase):
    def test_ошибка_tls_посреди_запроса_не_поломка(self):
        """Порча записи или обрыв по TLS — ушедший клиент: строка «обрыв»,
        а не трейсбек и не счёт в `mara_http_crashes_total`."""
        _, cert, key, _ = выпустить()
        srv = contextd.make_server(tempfile.mkdtemp(), 0,
                                   tls=contextd.tls_контекст(cert, key))
        открытый = contextd.make_server(tempfile.mkdtemp(), 0)
        try:
            было = contextd._упало["http"]
            вывод = io.StringIO()
            with redirect_stdout(вывод):
                try:
                    raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
                except ssl.SSLError:
                    srv.handle_error(None, ("198.51.100.7", 1))
            self.assertEqual(contextd._упало["http"], было)
            self.assertIn("обрыв SSLEOFError from=198.51.100.7", вывод.getvalue())
            # а на открытом порту SSLError — поломка обработчика (ревью, P3-2):
            # прийти он может только от исходящего TLS внутри него
            with redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
                except ssl.SSLError:
                    открытый.handle_error(None, ("198.51.100.7", 1))
            self.assertEqual(contextd._упало["http"], было + 1)
            with contextd._замок:
                contextd._упало["http"] = было
        finally:
            srv.server_close()
            открытый.server_close()


class Метрики(unittest.TestCase):
    def test_открытый_порт_не_с_петли(self):
        root = tempfile.mkdtemp()
        try:
            srv = contextd.make_server(root, 0, host="127.0.0.2", vault=tempfile.mkdtemp())
        except OSError:
            self.skipTest("в этом окружении нет 127.0.0.2")
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def сходить(откуда):
            c = http.client.HTTPConnection("127.0.0.2", srv.server_address[1], timeout=5,
                                           source_address=(откуда, 0))
            c.request("GET", "/healthz")
            c.getresponse().read()
            c.close()

        def метрики():
            con = mi.connect(root)
            try:
                return {строка.split(" ")[0]: int(строка.split(" ")[1])
                        for строка in contextd.metrics(con, root).splitlines()
                        if строка.startswith(("mara_http_plain_remote", "mara_tls_"))}
            finally:
                con.close()
        try:
            было = метрики()
            self.assertIn('mara_http_plain_remote_connections_total{from="lan"}', было)
            self.assertIn('mara_http_plain_remote_connections_total{from="proxy"}', было)
            self.assertIn("mara_tls_handshake_failures_total", было)
            with mock.patch.object(contextd, "TRUSTED_PROXY", ("127.0.0.4",)):
                сходить("127.0.0.3")
                сходить("127.0.0.3")
                сходить("127.0.0.4")
                сходить("127.0.0.1")
            стало = метрики()
            lan = 'mara_http_plain_remote_connections_total{from="lan"}'
            proxy = 'mara_http_plain_remote_connections_total{from="proxy"}'
            self.assertEqual(стало[lan] - было[lan], 2)
            self.assertEqual(стало[proxy] - было[proxy], 1, "петля не считается")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_открытый_адрес_отдельно(self):
        """`MARA_PLAIN_BIND` закрывает открытый порт для локалки, TLS остаётся
        на `MARA_BIND` (`serve`)."""
        каталог, cert, key, _ = выпустить()
        поднято = []

        class Стоп(Exception):
            pass

        def make_server(root, port, vault=None, host="127.0.0.1", tls=None):
            поднято.append((host, port, tls is not None))
            if tls is None:
                raise Стоп
            return mock.Mock(server_address=(host, port))

        with mock.patch.object(contextd, "make_server", side_effect=make_server), \
             mock.patch.object(contextd, "worker"), \
             mock.patch.dict(os.environ, {"MARA_PLAIN_BIND": "127.0.0.1"}), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(Стоп):
                contextd.serve(tempfile.mkdtemp(), 8788, host="0.0.0.0",
                               tls=(cert, key, 8789))
        self.assertEqual(поднято, [("0.0.0.0", 8789, True), ("127.0.0.1", 8788, False)])
        поднято.clear()
        with mock.patch.object(contextd, "make_server", side_effect=make_server), \
             mock.patch.object(contextd, "worker"), redirect_stdout(io.StringIO()):
            os.environ.pop("MARA_PLAIN_BIND", None)
            with self.assertRaises(Стоп):
                contextd.serve(tempfile.mkdtemp(), 8788, host="0.0.0.0")
        self.assertEqual(поднято, [("0.0.0.0", 8788, False)], "без TLS — как раньше")


if __name__ == "__main__":
    unittest.main()
