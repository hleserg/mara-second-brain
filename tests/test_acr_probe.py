"""RES-REC-001 (ТЗ §7.3, §20): зонд `scripts/acr_report_probe.py` —
контролируемая точка приёма для «Отчёта на удалённый сервер» ACR Phone.
Журнал называет форму запроса и ничего из содержания: номер, имя, заметка,
секрет, адрес doctor и аудио в него не попадают ни из полей, ни из
заголовков, ни из тела, которое не форма."""
import base64, contextlib, hashlib, hmac, http.client, io, json, os, shutil, socket, sys
import tempfile, threading, time, unittest

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import acr_report_probe as зонд

ГРАНИЦА = "----mara"
АУДИО = b"\x00\x00\x00\x18ftypM4A " + bytes(range(256)) * 20      # больше 4 КиБ
ТАЙНЫЕ = ("9991234567", "Анна", "Петрова", "смету", "слово", "hidden-token-123",
          "198.51.100.7", "abc123456789", "other-key-1")


def multipart(поля, файл=None, граница=ГРАНИЦА):
    куски = []
    for имя, значение in поля:
        куски.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (граница, имя, значение)).encode("utf-8"))
    if файл:
        имя_файла, тело = файл
        куски.append(('--%s\r\nContent-Disposition: form-data; name="File"; '
                      'filename="%s"\r\nContent-Type: audio/mp4\r\n\r\n'
                      % (граница, имя_файла)).encode("utf-8") + тело + b"\r\n")
    куски.append(("--%s--\r\n" % граница).encode("utf-8"))
    return b"".join(куски)


class Основа(unittest.TestCase):
    def запустить(self, **kw):
        self.каталог = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.каталог, True)
        self.журнал = os.path.join(self.каталог, "state", "acr-probe.jsonl")
        с = зонд.Зонд(("127.0.0.1", 0), журнал=self.журнал, тихо=True, **kw)
        return self.поднять(с)

    def поднять(self, с):
        threading.Thread(target=с.serve_forever, daemon=True).start()
        self.addCleanup(с.server_close)
        self.addCleanup(с.shutdown)
        self.порт = с.server_address[1]
        return с

    def послать(self, тело=b"", тип=None, путь="/acr", заголовки=None, метод="POST",
                таймаут=10):
        соед = http.client.HTTPConnection("127.0.0.1", self.порт, timeout=таймаут)
        h = dict(заголовки or {})
        if тип:
            h["Content-Type"] = тип
        соед.request(метод, путь, body=тело, headers=h)
        ответ = соед.getresponse()
        данные = ответ.read()
        соед.close()
        return ответ.status, данные

    def сырой(self, байты, закрыть_запись=False):
        """Сырые байты в сокет — для того, что http.client не пошлёт. Код
        ответа или None, если ответа не было."""
        с = socket.create_connection(("127.0.0.1", self.порт), timeout=10)
        try:
            с.sendall(байты)
            if закрыть_запись:
                с.shutdown(socket.SHUT_WR)
            ответ = b""
            while True:
                кусок = с.recv(65536)
                if not кусок:
                    break
                ответ += кусок
        finally:
            с.close()
        return int(ответ.split()[1]) if ответ.startswith(b"HTTP/") else None

    def записи(self, фаза=None):
        with open(self.журнал, encoding="ascii") as fh:
            все = [json.loads(l) for l in fh]
        return [з for з in все if фаза is None or з["phase"] == фаза]

    def запрос(self, номер=1, таймаут=5):
        """Приход и исход запроса `номер` одним словарём; исход пишется после
        ответа — ждём его."""
        до = time.monotonic() + таймаут
        while True:
            по_фазе = {з["phase"]: з for з in self.записи() if з.get("id") == номер}
            if "outcome" in по_фазе or time.monotonic() > до:
                return {**по_фазе.get("received", {}), **по_фазе.get("outcome", {})}
            time.sleep(0.02)

    def без_тайного(self):
        with open(self.журнал, "rb") as fh:
            сырьё = fh.read()
        сырьё.decode("ascii")
        текст = json.dumps(self.записи(), ensure_ascii=False)
        for тайное in ТАЙНЫЕ:
            self.assertNotIn(тайное, текст)


class Зонд(Основа):
    def test_форма_запроса_без_содержания(self):
        self.запустить(секрет="слово")
        тело = multipart([("Source", "+79991234567"), ("Secret", "слово"), ("Date", "1760000000"),
                          ("Duration", "65000"), ("Note", "Анна Петрова просила смету")],
                         ("+79991234567_2026-10-14.m4a", АУДИО))
        self.assertEqual(self.послать(тело, "multipart/form-data; boundary=" + ГРАНИЦА,
                                      "/api/recording/upload",
                                      {"Authorization": "Bearer hidden-token-123"}),
                         (200, b"ok"))
        з = self.запрос()
        self.без_тайного()
        по_имени = {п["name"]: п for п in з["fields"]}
        self.assertEqual(по_имени["Source"]["shape"], "+99999999999")
        self.assertEqual(по_имени["Secret"], {"name": "Secret", "length": 5, "secret": "equal"})
        self.assertEqual((по_имени["Date"]["value"], по_имени["Duration"]["value"]),
                         ("1760000000", "65000"), "единицы времени видны")
        self.assertEqual(по_имени["Note"]["shape"], "aaaa aaaaaaa aaaaaaa aaaaa")
        ф = з["files"][0]
        self.assertEqual((ф["size"], ф["format"], ф["sha256"]),
                         (len(АУДИО), "ftyp:M4A ", hashlib.sha256(АУДИО).hexdigest()))
        self.assertEqual(ф["filename_shape"], "+99999999999_9999-99-99.a9a")
        заголовки = {h["name"]: h for h in з["headers"]}
        self.assertEqual(заголовки["authorization"],
                         {"name": "authorization", "length": 23, "secret": "no",
                          "scheme": "bearer"})
        self.assertEqual(заголовки["content-type"]["type"], "multipart/form-data")
        self.assertEqual((з["method"], з["path_shape"], з["replied"], з["sent"],
                          з["client_gone_after"], з["body_problem"]),
                         ("POST", "/aaa/aaaaaaaaa/aaaaaa", 200, True, None, None))
        self.assertRegex(з["fingerprint"], r"^[0-9a-f]{16}$")
        self.assertNotEqual(з["fingerprint"], hashlib.sha256(тело).hexdigest()[:16],
                            "отпечаток — HMAC с ключом прогона, не перебираемый sha256")
        self.assertEqual(заголовки["content-type"]["params"],
                         [{"name": "boundary", "length": 8, "shape": "----aaaa"}])
        старт = self.записи("start")[0]
        self.assertEqual((старт["status"], старт["delay"], старт["secret_set"]), (200, 0.0, True))
        self.assertEqual(oct(os.stat(self.журнал).st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(os.stat(os.path.dirname(self.журнал)).st_mode & 0o777), oct(0o700))
        self.assertFalse([f for f in os.listdir(self.каталог) if f != "state"],
                         "без --keep тело на диск не ложится")

    def test_заголовки_без_содержания(self):
        """Ревью: Host с адресом doctor, секрет в своём заголовке, номер в
        Content-Disposition и Referer, имя в X-Contact — всё писалось как есть."""
        self.запустить(секрет="слово")
        соед = http.client.HTTPConnection("127.0.0.1", self.порт, timeout=10)
        соед.putrequest("POST", "/acr", skip_host=True)
        тело = b"a=1"
        for имя, значение in (
                ("Host", b"198.51.100.7:8799"), ("X-Secret", "слово".encode()),
                ("X-Api-Key", b"other-key-1"), ("Cookie", b"session=abc123456789"),
                ("Proxy-Authorization", b"Basic other-key-1"),
                ("Content-Disposition", 'attachment; filename="+79991234567.m4a"'.encode()),
                ("Referer", b"http://x/?number=%2B79991234567"),
                ("X-Contact", "Анна".encode()), ("X-Phone", b"+79991234567"),
                ("User-Agent", b"okhttp/4.12.0"), ("Accept", b"text/+79991234567"),
                ("Content-Type", b"application/x-www-form-urlencoded"),
                ("Content-Length", str(len(тело)).encode())):
            соед.putheader(имя, значение)
        соед.endheaders(тело)
        self.assertEqual(соед.getresponse().status, 200)
        соед.close()
        з = self.запрос()
        self.без_тайного()
        заголовки = {h["name"]: h for h in з["headers"]}
        self.assertEqual(заголовки["host"]["shape"], "999.99.999.9:9999")
        соед = http.client.HTTPConnection("127.0.0.1", self.порт, timeout=10)
        соед.request("GET", "/acr", headers={"Host": "probe.example:8799"})
        соед.getresponse().read()
        соед.close()
        host = [h for h in self.запрос(2)["headers"] if h["name"] == "host"]
        self.assertEqual(host, [{"name": "host", "length": 18, "shape": "aaaaa.aaaaaaa:9999"}],
                         "имя хоста — тоже формой, не только адрес")
        self.assertEqual(заголовки["x-secret"], {"name": "x-secret", "length": 5,
                                                 "secret": "equal"})
        for имя in ("x-api-key", "cookie", "proxy-authorization"):
            self.assertEqual(set(заголовки[имя]) - {"scheme"}, {"name", "length", "secret"}, имя)
        self.assertEqual(заголовки["x-contact"], {"name": "x-contact", "length": 4,
                                                  "shape": "aaaa"})
        self.assertEqual(заголовки["user-agent"]["value"], "okhttp/4.12.0")
        self.assertEqual(заголовки["accept"]["shape"], "aaaa/+99999999999",
                         "служебный заголовок с номером — формой")
        self.assertEqual(заголовки["content-length"]["value"], "3")
        self.assertEqual(заголовки["content-type"], {"name": "content-type",
                                                     "type": "application/x-www-form-urlencoded"})

    def test_как_пришёл_секрет(self):
        """Вопрос §20 «auth/signature»: слово в Basic, подпись тела HMAC, слово
        внутри обычного поля — журнал говорит, как, не раскрывая ни слова, ни подписи."""
        self.запустить(секрет="zond-word")
        тело = b"comment=see+zond-word+here&Secret=zond-word"
        sha256 = hmac.new(b"zond-word", тело, "sha256").hexdigest()
        sha1 = base64.b64encode(hmac.new(b"zond-word", тело, "sha1").digest()).decode()
        self.послать(тело, "application/x-www-form-urlencoded", заголовки={
            "Authorization": "Basic " + base64.b64encode(b"acr:zond-word").decode(),
            "X-Signature": "sha256=" + sha256, "X-Hash": sha1, "X-Note": "zond-word",
            "X-Auth-Token": "Token zond-word"})
        з = self.запрос()
        заголовки = {h["name"]: h for h in з["headers"]}
        self.assertEqual((заголовки["authorization"]["secret"],
                          заголовки["authorization"]["scheme"]), ("base64", "basic"))
        self.assertEqual(заголовки["x-signature"]["secret"], "hmac-sha256-hex")
        self.assertEqual(заголовки["x-hash"]["secret"], "hmac-sha1-base64")
        self.assertEqual((заголовки["x-auth-token"]["secret"],
                          заголовки["x-auth-token"]["scheme"]), ("contains", "token"))
        self.assertTrue(заголовки["x-note"]["has_secret"])
        self.assertNotIn("value", заголовки["x-note"])
        по_имени = {п["name"]: п for п in з["fields"]}
        self.assertTrue(по_имени["comment"]["has_secret"])
        self.assertEqual(по_имени["Secret"]["secret"], "equal")
        текст = json.dumps(self.записи())
        for тайное in ("zond-word", sha256, sha1):
            self.assertNotIn(тайное, текст)

    def test_тело_не_форма_без_содержания(self):
        """Ревью: текст и JSON под чужим типом писались первыми байтами в hex,
        номер в имени поля — как есть."""
        self.запустить()
        for тип, тело in (("text/plain", "+79991234567 Анна".encode()),
                          (None, "source=79991234567&name=Анна".encode()),
                          ("text/json", b'{"src":"+79991234567"}'),
                          ("application/x-www-form-urlencoded",
                           json.dumps({"number": " 79991234567", "name": "Анна"}).encode()),
                          ("application/json", b'{"+79991234567": 1}'),
                          ("application/x-www-form-urlencoded", b"%2B79991234567=1"),
                          ("application/octet-stream", b"\x00\x01+79991234567")):
            self.assertEqual(self.послать(тело, тип)[0], 200)
        текст, без_типа, text_json, json_как_форма, json_имя, форма_имя, двоичное = [
            self.запрос(n) for n in range(1, 8)]
        self.без_тайного()
        self.assertEqual(текст["files"][0]["text_shape"], "+99999999999 aaaa")
        self.assertFalse({"sha256", "format", "head_shape"} & set(текст["files"][0]))
        self.assertEqual(без_типа["files"][0]["text_shape"], "aaaaaa=99999999999&aaaa=aaaa")
        self.assertEqual(text_json["fields"], [{"name": "src", "length": 12,
                                                "shape": "+99999999999", "kind": "string"}])
        self.assertEqual(json_как_форма["note"], ["json-as:application/x-www-form-urlencoded"])
        self.assertEqual([п["name"] for п in json_как_форма["fields"]], ["number", "name"])
        self.assertEqual(json_имя["fields"][0]["name_shape"], "+99999999999")
        self.assertEqual(форма_имя["fields"][0]["name_shape"], "+99999999999")
        ф = двоичное["files"][0]
        self.assertEqual((ф["format"], ф["head_shape"]), ("unknown", "~~+999999999"))
        self.assertNotIn("sha256", ф, "sha256 короткого тела перебирается по номерам")

    def test_json_массив_и_вложенный(self):
        self.запустить(секрет="слово")
        self.послать(json.dumps([{"number": "+79991234567", "date": 1760000000}]).encode(),
                     "application/json")
        self.послать(json.dumps({"call": {"duration": 65, "type": {"x": "Анна"}},
                                 "secret": "слово", "ok": True}).encode(),
                     "application/json")
        массив, вложенный = self.запрос(1), self.запрос(2)
        self.без_тайного()
        self.assertEqual(массив["note"], ["json-array:1"])
        self.assertEqual(массив["fields"], [
            {"name": "[0].number", "length": 12, "shape": "+99999999999", "kind": "string"},
            {"name": "[0].date", "length": 10, "value": "1760000000", "kind": "number"}])
        по_имени = {п["name"]: п for п in вложенный["fields"]}
        self.assertEqual(по_имени["call.duration"]["value"], "65")
        self.assertEqual(по_имени["call.type.x"]["shape"], "aaaa")
        self.assertEqual(по_имени["secret"]["secret"], "equal")
        self.assertEqual((по_имени["ok"]["kind"], по_имени["ok"]["shape"]), ("bool", "aaaa"))

    def test_задержка_и_уход_клиента(self):
        """Вопрос 4 вердикта — таймаут: приход пишется до задержки, а уход
        клиента во время задержки — с числом секунд."""
        self.запустить(задержка=2.0)
        соед = http.client.HTTPConnection("127.0.0.1", self.порт, timeout=0.4)
        соед.request("POST", "/acr", body=b"a=1",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(socket.timeout):
            соед.getresponse()
        self.assertTrue(self.записи("received"), "приход записан ещё до ответа")
        self.assertFalse(self.записи("outcome"))
        соед.close()
        з = self.запрос()
        self.assertIsNotNone(з["client_gone_after"])
        self.assertTrue(0.3 <= з["client_gone_after"] < 1.9, з)
        self.assertLess(з["seconds"], 1.9, "ушёл клиент — зонд не досиживает задержку")
        self.assertEqual(з["replied"], 200)

    def test_задержка_дождавшегося(self):
        self.запустить(задержка=0.5)
        t0 = time.monotonic()
        self.assertEqual(self.послать(b"a=1", "application/x-www-form-urlencoded"), (200, b"ok"))
        self.assertGreaterEqual(time.monotonic() - t0, 0.5)
        з = self.запрос()
        self.assertEqual((з["client_gone_after"], з["sent"]), (None, True))
        self.assertGreaterEqual(з["seconds"], 0.5)

    def test_все_методы(self):
        self.запустить()
        for метод in ("GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"):
            код, данные = self.послать(метод=метод,
                                       путь="/acr?number=%2B79991234567&date=1760000000")
            self.assertEqual((код, данные), (200, b"" if метод == "HEAD" else b"ok"), метод)
        с = socket.create_connection(("127.0.0.1", self.порт), timeout=10)
        с.sendall(b"HEAD /acr HTTP/1.0\r\n\r\n")
        ответ = b""
        while True:
            кусок = с.recv(65536)
            if not кусок:
                break
            ответ += кусок
        с.close()
        шапка, _, остаток = ответ.partition(b"\r\n\r\n")
        self.assertIn(b"Content-Length: 2", шапка)
        self.assertEqual(остаток, b"", "на HEAD тела нет")
        записи = [self.запрос(n) for n in range(1, 7)]
        self.без_тайного()
        self.assertEqual([з["method"] for з in записи],
                         ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"])
        self.assertEqual(записи[0]["query"], [
            {"name": "number", "length": 12, "shape": "+99999999999"},
            {"name": "date", "length": 10, "value": "1760000000"}])

    def test_кривые_тела(self):
        """Ревью: кусок -5, кусок в терабайты, обрыв тела — молча без строки
        в журнале или MemoryError. Теперь — ответ и строка с бедой."""
        self.запустить(предел=1 << 20)
        шапка = b"POST /acr HTTP/1.1\r\nHost: x\r\nContent-Type: text/plain\r\n"
        чанки = шапка + b"Transfer-Encoding: chunked\r\n\r\n"
        случаи = ((чанки + b"-5\r\nhello\r\n0\r\n\r\n", False, 400, "bad-length", 0),
                  (чанки + b"fffffffffff\r\nhello", True, 413, "limit", 0),
                  (чанки + b"10\r\nabc", True, 400, "cut", 3),
                  (шапка + b"Content-Length: -1\r\n\r\n", False, 400, "bad-length", 0),
                  (шапка + b"Content-Length: 100\r\n\r\n0123456789", True, 400, "cut", 10),
                  (чанки + b"5;ext=1\r\nhello\r\n0\r\nX-Trailer: +79991234567\r\n\r\n",
                   False, 200, None, 5))
        for байты, закрыть, код, беда, размер in случаи:
            self.assertEqual(self.сырой(байты, закрыть), код, байты[:90])
        for n, (_, _, код, беда, размер) in enumerate(случаи, 1):
            з = self.запрос(n)
            self.assertEqual((з["body_problem"], з["replied"], з["body_size"]),
                             (беда, код, размер), n)
            self.assertEqual("fields" in з, беда is None, n)
        self.без_тайного()

    def test_кривой_multipart_и_суррогат(self):
        self.запустить()
        тело = multipart([("Source", "+79991234567")])
        self.послать(тело, "multipart/form-data")
        вложенный = ('--A\r\nContent-Disposition: form-data; name="files"\r\n'
                     'Content-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\n'
                     'Content-Disposition: file; filename="+79991234567.m4a"\r\n'
                     'Content-Type: audio/mp4\r\n\r\n').encode() + АУДИО + b"\r\n--B--\r\n--A--\r\n"
        self.послать(вложенный, "multipart/form-data; boundary=A")
        self.послать(b'{"a": "\\ud83d", "\\ud83d": "+79991234567"}', "application/json")
        без_границы, влож, суррогат = self.запрос(1), self.запрос(2), self.запрос(3)
        self.без_тайного()
        self.assertIn("multipart-no-parts", без_границы["note"])
        self.assertTrue(без_границы["files"][0]["text_shape"].startswith(
            "------aaaa  aaaaaaa-aaaaaaaaaaa: aaaa-aaaa; aaaa=\"aaaaaa\"    +99999999999"))
        self.assertEqual(влож["note"], ["nested:multipart/mixed"])
        self.assertEqual((влож["files"][0]["format"], влож["files"][0]["size"]),
                         ("ftyp:M4A ", len(АУДИО)))
        self.assertEqual([п.get("shape") for п in суррогат["fields"]], ["·", "+99999999999"])

    def test_сверх_предела(self):
        self.запустить(предел=10)
        self.assertEqual(self.послать(multipart([("Source", "x" * 50)]),
                                      "multipart/form-data; boundary=" + ГРАНИЦА)[0], 413)
        з = self.запрос()
        self.assertEqual((з["body_problem"], з["replied"], з["body_size"]), ("limit", 413, 0))
        self.assertNotIn("fields", з)

    def test_tls_на_открытый_порт(self):
        """Адрес с https:// в ACR — зонд это называет, а не молчит."""
        self.запустить()
        self.assertIsNone(self.сырой(b"\x16\x03\x01\x00\x05hello"))
        отказ = self.записи("rejected")[0]
        self.assertEqual(отказ["reason"], "tls-handshake")

    def test_не_http_без_содержания(self):
        self.запустить()
        self.assertEqual(self.сырой(b"+79991234567 /acr HTTP/1.1\r\nHost: x\r\n\r\n"), 501)
        отказ = self.записи("rejected")[0]
        self.assertEqual(отказ["reason"], "http-501")
        self.assertTrue(отказ["line_shape"].startswith("+99999999999 /aaa aaaa/9.9"))
        self.без_тайного()

    def test_вывод_процесса_молчит(self):
        """Адрес телефона и путь не уходят в stderr: ни строка о запросе, ни отказ."""
        self.запустить()
        поток = io.StringIO()
        with contextlib.redirect_stderr(поток):
            self.послать(b"a=1", "application/x-www-form-urlencoded")
            self.сырой(b"BREW /acr HTTP/1.1\r\n\r\n")
            self.запрос()
        self.assertEqual(поток.getvalue(), "")

    def test_keep_права(self):
        хранить = os.path.join(tempfile.mkdtemp(), "файлы")
        self.addCleanup(shutil.rmtree, os.path.dirname(хранить), True)
        старая = os.umask(0)
        try:
            self.запустить(keep=хранить)
            self.послать(multipart([("Source", "x")], ("a.m4a", АУДИО)),
                         "multipart/form-data; boundary=" + ГРАНИЦА)
            з = self.запрос()
        finally:
            os.umask(старая)
        путь = os.path.join(хранить, з["files"][0]["kept"])
        with open(путь, "rb") as fh:
            self.assertEqual(fh.read(), АУДИО)
        self.assertEqual(oct(os.stat(путь).st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(os.stat(хранить).st_mode & 0o777), oct(0o700))

    def test_права_журнала_исправляются(self):
        каталог = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, каталог, True)
        журнал = os.path.join(каталог, "acr-probe.jsonl")
        with open(журнал, "w") as fh:
            fh.write("")
        os.chmod(журнал, 0o644)
        с = зонд.Зонд(("127.0.0.1", 0), журнал=журнал, тихо=True)
        с.server_close()
        self.assertEqual(oct(os.stat(журнал).st_mode & 0o777), oct(0o600))


class Аргументы(Основа):
    def test_show_max_mb_status(self):
        каталог = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, каталог, True)
        self.журнал = os.path.join(каталог, "acr-probe.jsonl")
        a = зонд.аргументы().parse_args(["--port", "0", "--bind", "127.0.0.1",
                                          "--log", self.журнал, "--show", "Mode, date",
                                          "--max-mb", "1", "--status", "202"])
        self.поднять(зонд.зонд_из(a, тихо=True))
        self.assertEqual(self.послать(b"mode=auto&date=1760000000&direction=out",
                                      "application/x-www-form-urlencoded")[0], 202)
        з = self.запрос()
        self.assertEqual([п.get("value", п.get("shape")) for п in з["fields"]],
                         ["auto", "1760000000", "aaa"])
        старт = self.записи("start")[0]
        self.assertEqual((старт["show"], старт["max_bytes"], старт["status"]),
                         (["mode", "date"], 1 << 20, 202))

    def test_негодные_значения(self):
        for argv in (["--status", "99"], ["--status", "600"], ["--delay", "-1"],
                     ["--max-mb", "0"], ["--port", "x"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                зонд.аргументы().parse_args(argv)


class Правила(unittest.TestCase):
    def test_показ_как_есть(self):
        self.assertEqual(зонд.поле("type", "123456")["value"], "123456")
        for значение in ("1234567", "+7 999 123-45-67", "٧٩٩٩١٢٣٤٥٦٧", "x" * 65,
                         "1577836799", "2000000000", "2000000000000", "Входящий"):
            self.assertNotIn("value", зонд.поле("type", значение), значение)
        for значение in ("1577836800", "1999999999", "1577836800000", "1999999999999"):
            self.assertEqual(зонд.поле("date", значение)["value"], значение)
        self.assertNotIn("value", зонд.поле("note", "auto"), "не из списка показа")

    def test_секретные_имена(self):
        for имя in ("Secret", "api_token", "Password", "X-Api-Key", "Authorization",
                    "Signature", "Cookie", "X-Hash"):
            з = зонд.поле(имя, "type")
            self.assertEqual(set(з), {"name", "length"}, имя)

    def test_имя_поля(self):
        self.assertEqual(зонд.имя_поля("Source"), {"name": "Source"})
        for имя in ("+79991234567", "a1234567", "Анна", "x" * 41, ""):
            self.assertIn("name_shape", зонд.имя_поля(имя), имя)

    def test_полей_не_больше_предела(self):
        тело = "&".join("f%d=%d" % (i, i) for i in range(250)).encode()
        итог = зонд.разобрать("application/x-www-form-urlencoded", тело)
        self.assertEqual((len(итог["fields"]), итог["note"]),
                         (зонд.ПОЛЕЙ_МАКС, ["fields-cut:%d" % зонд.ПОЛЕЙ_МАКС]))

    def test_самопроверка(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(зонд.self_check(), 0)


if __name__ == "__main__":
    unittest.main()
