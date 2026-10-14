"""RES-REC-001 (ТЗ §7.3, §20): зонд `scripts/acr_report_probe.py` —
контролируемая точка приёма для «Отчёта на удалённый сервер» ACR Phone.
Журнал называет форму запроса и ничего из содержания: номер, имя, заметка,
секрет, адрес doctor и аудио в него не попадают ни из полей, ни из
заголовков, ни из тела, которое не форма, ни в вывод процесса."""
import base64, contextlib, hashlib, hmac, http.client, io, json, os, shutil, socket, struct
import sys, tempfile, threading, time, types, unittest
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import acr_report_probe as зонд

ГРАНИЦА = "----mara"
АУДИО = b"\x00\x00\x00\x18ftypM4A " + bytes(range(256)) * 20      # больше 4 КиБ
СЛОВО = "зонд-слово"            # слово в теле (UTF-8)
АСКИ = "zond-word"              # слово в заголовках: http.client шлёт их в latin-1
ТАЙНЫЕ = ("9991234567", "Анна", "Петрова", "смету", СЛОВО, АСКИ, "hidden-token-123",
          "198.51.100.7", "abc123456789", "other-key-1", "John", "Smith", "probe.example")


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


def прочесть(с):
    ответ = b""
    while True:
        кусок = с.recv(65536)
        if not кусок:
            return ответ
        ответ += кусок


class Основа(unittest.TestCase):
    def запустить(self, каталог=None, **kw):
        self.каталог = каталог or tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.каталог, True)
        self.журнал = os.path.join(self.каталог, "state", "acr-probe.jsonl")
        kw.setdefault("тихо", True)
        return self.поднять(зонд.Зонд(("127.0.0.1", 0), журнал=self.журнал, **kw))

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

    def сокет(self):
        с = socket.create_connection(("127.0.0.1", self.порт), timeout=10)
        self.addCleanup(с.close)
        return с

    def сырой(self, байты, закрыть_запись=False):
        """Сырые байты в сокет — для того, что http.client не пошлёт. Код
        ответа или None, если ответа не было."""
        с = self.сокет()
        с.sendall(байты)
        if закрыть_запись:
            с.shutdown(socket.SHUT_WR)
        ответ = прочесть(с)
        с.close()               # зонд дослушивает кривое тело до закрытия
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

    def без_тайного(self, текст=None):
        if текст is None:
            with open(self.журнал, "rb") as fh:
                fh.read().decode("ascii")
            текст = json.dumps(self.записи(), ensure_ascii=False)
        for тайное in ТАЙНЫЕ:
            self.assertNotIn(тайное, текст)


class Зонд(Основа):
    def test_форма_запроса_без_содержания(self):
        self.запустить(секрет=СЛОВО)
        тело = multipart([("Source", "+79991234567"), ("Secret", СЛОВО), ("Date", "1760000000"),
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
        self.assertRegex(по_имени["Source"]["fp"], r"^[0-9a-f]{8}$")
        self.assertEqual(по_имени["Secret"], {"name": "Secret", "length": 10, "secret": "equal"},
                         "у секретного поля — ни формы, ни отпечатка")
        self.assertEqual((по_имени["Date"]["value"], по_имени["Duration"]["value"]),
                         ("1760000000", "65000"), "единицы времени видны")
        self.assertEqual(по_имени["Note"]["shape"], "a a a a", "длины слов имени не видны")
        ф = з["files"][0]
        self.assertEqual((ф["size"], ф["format"], ф["sha256"]),
                         (len(АУДИО), "ftyp:M4A ", hashlib.sha256(АУДИО).hexdigest()))
        self.assertEqual(ф["filename_shape"], "+99999999999_9999-99-99.a9a")
        self.assertNotEqual(ф["fingerprint"], hashlib.sha256(АУДИО).hexdigest()[:16])
        заголовки = {h["name"]: h for h in з["headers"]}
        self.assertEqual(заголовки["authorization"],
                         {"name": "authorization", "length": 23, "secret": "no",
                          "scheme": "bearer"})
        self.assertEqual(заголовки["content-type"],
                         {"name": "content-type", "type": "multipart/form-data",
                          "params": [{"name": "boundary", "length": 8, "shape": "----a"}]})
        self.assertEqual((з["method"], з["path_shape"], з["replied"], з["sent"],
                          з["client_gone_after"], з["body_problem"]),
                         ("POST", "/a/a/a", 200, True, None, None))
        self.assertRegex(з["fingerprint"], r"^[0-9a-f]{16}$")
        self.assertNotEqual(з["fingerprint"], hashlib.sha256(тело).hexdigest()[:16],
                            "отпечаток — HMAC с ключом, не перебираемый sha256")
        старт = self.записи("start")[0]
        self.assertEqual((старт["status"], старт["delay"], старт["secret_set"], старт["label"]),
                         (200, 0.0, True, ""))
        каталог = os.path.dirname(self.журнал)
        ключ = os.path.join(каталог, "acr-probe.key")
        for путь, права in ((self.журнал, 0o600), (каталог, 0o700), (ключ, 0o600)):
            self.assertEqual(oct(os.stat(путь).st_mode & 0o777), oct(права), путь)
        with open(ключ, "rb") as fh:
            self.assertNotIn(fh.read().hex()[:16], json.dumps(self.записи()))
        self.assertEqual(os.listdir(self.каталог), ["state"],
                         "без --keep тело на диск не ложится")
        self.assertEqual(sorted(os.listdir(каталог)), ["acr-probe.jsonl", "acr-probe.key"])

    def test_заголовки_без_содержания(self):
        """Ревью: Host с адресом doctor, секрет в своём заголовке, номер в
        Content-Disposition и Referer, имя в X-Contact — всё писалось как есть."""
        self.запустить(секрет=АСКИ)
        соед = http.client.HTTPConnection("127.0.0.1", self.порт, timeout=10)
        соед.putrequest("POST", "/acr", skip_host=True)
        тело = b"a=1"
        for имя, значение in (
                ("Host", b"198.51.100.7:8799"), ("X-Secret", АСКИ.encode()),
                ("X-Api-Key", b"other-key-1"), ("Cookie", b"session=abc123456789"),
                ("Proxy-Authorization", b"Basic other-key-1"),
                ("Content-Disposition", 'attachment; filename="+79991234567.m4a"'.encode()),
                ("Referer", b"http://x/?number=%2B79991234567"),
                ("X-Contact", "Анна".encode()), ("X-Phone", b"+79991234567"),
                ("X-Type", b"John"), ("Type", b"John"), ("User-Agent", b"okhttp/4.12.0"),
                ("Accept", b"text/+79991234567"),
                ("Content-Type", b"application/x-www-form-urlencoded"),
                ("Content-Length", str(len(тело)).encode())):
            соед.putheader(имя, значение)
        соед.endheaders(тело)
        self.assertEqual(соед.getresponse().status, 200)
        соед.close()
        з = self.запрос()
        заголовки = {h["name"]: h for h in з["headers"]}
        self.assertEqual(заголовки["host"]["shape"], "999.99.999.9:9999")
        self.assertEqual(заголовки["x-secret"], {"name": "x-secret", "length": 9,
                                                 "secret": "equal"})
        for имя in ("x-api-key", "cookie", "proxy-authorization"):
            self.assertEqual(set(заголовки[имя]) - {"scheme"}, {"name", "length", "secret"}, имя)
        self.assertEqual(заголовки["x-contact"], {"name": "x-contact", "length": 4,
                                                  "shape": "a"})
        self.assertEqual(заголовки["x-type"], {"name": "x-type", "length": 4, "shape": "a"})
        self.assertEqual(заголовки["type"], {"name": "type", "length": 4, "shape": "a"},
                         "у заголовков списка показа нет, даже у одноимённых полям")
        self.assertEqual(заголовки["user-agent"]["value"], "okhttp/4.12.0")
        self.assertEqual(заголовки["accept"]["shape"], "a/+99999999999",
                         "служебный заголовок с номером — формой")
        self.assertEqual(заголовки["content-length"]["value"], "3")
        self.assertEqual(заголовки["content-type"], {"name": "content-type",
                                                     "type": "application/x-www-form-urlencoded"})
        соед = http.client.HTTPConnection("127.0.0.1", self.порт, timeout=10)
        соед.request("GET", "/acr", headers={"Host": "probe.example:8799"})
        соед.getresponse().read()
        соед.close()
        host = [h for h in self.запрос(2)["headers"] if h["name"] == "host"]
        self.assertEqual(host, [{"name": "host", "length": 18, "shape": "a.a:9999"}],
                         "имя хоста — тоже формой, не только адрес")
        self.без_тайного()

    def test_служебное_по_одному_правилу(self):
        """Ревью 2, P3-2: тип содержимого, `charset`, служебный заголовок со
        словом или длинный — по тому же правилу «как есть», что и всё."""
        self.запустить(секрет=АСКИ)
        self.послать(b"x", "x/79991234567", заголовки={"User-Agent": "okhttp " + АСКИ})
        self.послать(b"x", "text/plain; charset=+79991234567; boundary=Boundary",
                     заголовки={"User-Agent": "a" * 201})
        первый, второй = self.запрос(1), self.запрос(2)
        заголовки = {h["name"]: h for h in первый["headers"]}
        self.assertEqual((первый["content_type"], заголовки["content-type"]["type"]),
                         ("a/99999999999", "a/99999999999"))
        self.assertEqual(заголовки["user-agent"]["secret"], "contains")
        self.assertNotIn("value", заголовки["user-agent"])
        заголовки = {h["name"]: h for h in второй["headers"]}
        self.assertEqual(заголовки["content-type"]["params"],
                         [{"name": "charset", "length": 12, "shape": "+99999999999"},
                          {"name": "boundary", "length": 8, "shape": "a"}])
        self.assertEqual(заголовки["user-agent"], {"name": "user-agent", "length": 201,
                                                   "shape": "a"})
        self.без_тайного()

    def test_как_пришёл_секрет(self):
        """Вопрос §20 «auth/signature»: слово в Basic, подпись тела HMAC в любом
        заголовке, хеш слова, слово внутри обычного поля и параметра адреса —
        журнал говорит, как, не раскрывая ни слова, ни подписи."""
        self.запустить(секрет=АСКИ)
        тело = b"comment=see+zond-word+here&Secret=zond-word&type=ZOND-WORD"
        ключ = АСКИ.encode()
        sha256 = hmac.new(ключ, тело, "sha256").hexdigest()
        sha1 = base64.b64encode(hmac.new(ключ, тело, "sha1").digest()).decode()
        mac = base64.urlsafe_b64encode(hmac.new(ключ, тело, "sha256").digest()).decode().rstrip("=")
        хеш = hashlib.sha256(ключ).hexdigest()
        basic = base64.urlsafe_b64encode(b"acr:" + ключ).decode().rstrip("=")
        self.послать(тело, "application/x-www-form-urlencoded",
                     путь="/acr?auth=zond-word&note=zond-word", заголовки={
            "Authorization": "Basic " + base64.b64encode(b"acr:" + ключ).decode(),
            "X-Signature": "sha256=" + sha256, "X-Hash": sha1, "X-Note": АСКИ,
            "X-Auth-Token": "Token " + АСКИ, "X-Secret": АСКИ + " x",
            "X-Check": sha256, "X-Check2": хеш, "X-Mac": mac, "X-Basic": basic})
        з = self.запрос()
        заголовки = {h["name"]: h for h in з["headers"]}
        связь = {к: заголовки[к]["secret"] for к in (
            "authorization", "x-signature", "x-hash", "x-note", "x-auth-token", "x-secret",
            "x-check", "x-check2", "x-mac", "x-basic")}
        self.assertEqual(связь, {"authorization": "base64", "x-signature": "hmac-sha256-hex",
                                 "x-hash": "hmac-sha1-base64", "x-note": "equal",
                                 "x-auth-token": "contains", "x-secret": "contains",
                                 "x-check": "hmac-sha256-hex", "x-check2": "hash-sha256-hex",
                                 "x-mac": "hmac-sha256-base64", "x-basic": "base64"})
        self.assertEqual(заголовки["authorization"]["scheme"], "basic")
        self.assertEqual(заголовки["x-auth-token"]["scheme"], "token")
        self.assertNotIn("scheme", заголовки["x-secret"],
                         "первое слово не из `СХЕМЫ` в журнал не идёт")
        по_имени = {п["name"]: п for п in з["fields"]}
        self.assertEqual({k: по_имени[k]["secret"] for k in ("comment", "Secret", "type")},
                         {"comment": "contains", "Secret": "equal",
                          "type": "contains-any-case"})
        self.assertNotIn("value", по_имени["type"], "слово в поле показа — не как есть")
        self.assertEqual({п["name"]: п["secret"] for п in з["query"]},
                         {"auth": "equal", "note": "equal"})
        текст = json.dumps(self.записи())
        for тайное in (АСКИ, "ZOND", sha256, sha1, mac, хеш, basic):
            self.assertNotIn(тайное, текст)

    def test_слово_не_короче_восьми(self):
        """Ревью 2, P3-3: короткое слово даёт ложные совпадения и подсказку по
        цифрам номера."""
        with self.assertRaises(ValueError):
            зонд.Зонд(("127.0.0.1", 0), журнал=os.path.join(tempfile.mkdtemp(), "ж.jsonl"),
                      секрет="12")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            зонд.аргументы().parse_args(["--secret", "1234567"])

    def test_тело_не_форма_без_содержания(self):
        """Ревью: текст и JSON под чужим типом писались первыми байтами в hex,
        номер в имени поля — как есть; текст UTF-16 без метки отдавал младшие
        байты кириллицы знаками пунктуации."""
        self.запустить()
        utf16 = ("Анна Петрова " * 400).encode("utf-16-le")           # без метки, больше 4 КиБ
        for тип, тело in (("text/plain", "+79991234567 Анна".encode()),
                          (None, "source=79991234567&name=Анна".encode()),
                          ("text/json", b'{"src":"+79991234567"}'),
                          ("application/x-www-form-urlencoded",
                           json.dumps({"number": " 79991234567", "name": "Анна"}).encode()),
                          ("application/json", b'{"+79991234567": 1}'),
                          ("application/x-www-form-urlencoded", b"%2B79991234567=1"),
                          ("application/octet-stream", b"\x00\x01+79991234567"),
                          ("application/octet-stream", utf16),
                          ("text/plain", "Анна".encode("utf-16"))):
            self.assertEqual(self.послать(тело, тип)[0], 200)
        (текст, без_типа, text_json, json_как_форма, json_имя, форма_имя, двоичное, без_метки,
         с_меткой) = [self.запрос(n) for n in range(1, 10)]
        self.без_тайного()
        self.assertEqual(текст["files"][0]["text_shape"], "+99999999999 a")
        self.assertFalse({"sha256", "format", "head_shape"} & set(текст["files"][0]))
        self.assertEqual(без_типа["files"][0]["text_shape"], "a=99999999999&a=a")
        self.assertEqual([{k: п[k] for k in ("name", "length", "shape", "kind")}
                          for п in text_json["fields"]],
                         [{"name": "src", "length": 12, "shape": "+99999999999",
                           "kind": "string"}])
        self.assertEqual(json_как_форма["note"], ["json-as:application/x-www-form-urlencoded"])
        self.assertEqual([п["name"] for п in json_как_форма["fields"]], ["number", "name"])
        self.assertEqual(json_имя["fields"][0]["name_shape"], "+99999999999")
        self.assertEqual(форма_имя["fields"][0]["name_shape"], "+99999999999")
        ф = двоичное["files"][0]
        self.assertEqual((ф["format"], ф["head_shape"]), ("unknown", "~~~999999999"))
        self.assertNotIn("sha256", ф, "sha256 короткого тела перебирается по номерам")
        ф = без_метки["files"][0]
        self.assertEqual(ф["format"], "unknown")
        self.assertLessEqual(set(ф["head_shape"]), {"9", "a", "~"})
        self.assertNotIn("sha256", ф, "sha256 — только у узнанного формата")
        self.assertEqual(с_меткой["files"][0]["text_shape"], "a", "UTF-16 с меткой — текст")

    def test_json_массив_и_вложенный(self):
        self.запустить(секрет=СЛОВО)
        for d in ([{"number": "+79991234567", "date": 1760000000}],
                  {"call": {"duration": 65, "type": {"x": "Анна"}}, "secret": СЛОВО, "ok": True},
                  {"a": {"b": {"c": {"d": {"e": 1}, "+79991234567": {"x": 1}}}},
                   "+79991234567": [], "list": [{"+79991234567": "x"}]},
                  {"+79991234567": {"date": "1760000000"}}):
            self.послать(json.dumps(d).encode(), "application/json")
        массив, вложенный, глубокий, имя_номер = [self.запрос(n) for n in range(1, 5)]
        self.без_тайного()
        self.assertEqual(массив["note"], ["json-array:1"])
        self.assertEqual([{k: п.get(k) for k in ("name", "shape", "value", "kind")}
                          for п in массив["fields"]],
                         [{"name": "[0].number", "shape": "+99999999999", "value": None,
                           "kind": "string"},
                          {"name": "[0].date", "shape": None, "value": "1760000000",
                           "kind": "number"}])
        по_имени = {п["name"]: п for п in вложенный["fields"]}
        self.assertEqual(по_имени["call.duration"]["value"], "65")
        self.assertEqual(по_имени["call.type.x"]["shape"], "a")
        self.assertEqual(по_имени["secret"]["secret"], "equal")
        self.assertEqual((по_имени["ok"]["kind"], по_имени["ok"]["shape"]), ("bool", "a"))
        записи = глубокий["fields"]
        self.assertIn({"name": "a.b.c.d", "kind": "object", "length": 1}, записи,
                      "глубже `ГЛУБИНА` — только вид и длина")
        self.assertNotIn("a.b.c.d.e", [п.get("name") for п in записи])
        self.assertIn({"name_shape": "a.a.a.+99999999999", "kind": "object", "length": 1},
                      записи)
        self.assertIn({"name_shape": "+99999999999", "kind": "array", "length": 0}, записи)
        self.assertIn("a[9].+99999999999", [п.get("name_shape") for п in записи])
        запись, = имя_номер["fields"]
        self.assertEqual(запись["name_shape"], "+99999999999.a")
        self.assertNotIn("value", запись, "у имени-формы значение не показывается")

    def test_значения_показа(self):
        """Ревью 2, P3-2: в поле из списка показа как есть — только число,
        слово, версия, длительность и дата; имя с пробелом и номер с дефисами
        — формой."""
        self.запустить()
        пары = [("type", "John Smith"), ("type", "23-45-67"), ("direction", "incoming"),
                ("version", "1.2.3"), ("time", "01:05"), ("duration", "65.5"),
                ("date", "2026-10-14T12:34:56+03:00"), ("date", "14.10.2026 12:34"),
                ("date", "1760000000 +79991234567")]
        тело = "&".join("%s=%s" % (и, з.replace("+", "%2B").replace(" ", "+"))
                        for и, з in пары).encode()
        self.послать(тело, "application/x-www-form-urlencoded",
                     путь="/acr?date=1760000000%20%2B79991234567")
        з = self.запрос()
        self.assertEqual([п.get("value") for п in з["fields"]],
                         [None, None, "incoming", "1.2.3", "01:05", "65.5",
                          "2026-10-14T12:34:56+03:00", "14.10.2026 12:34", None])
        self.assertEqual(з["query"][0]["shape"], "9999999999 +99999999999")
        self.без_тайного()

    def test_отпечатки_и_ключ(self):
        """Отпечатки сравнимы между перезапусками зонда (ключ — в файле рядом
        с журналом), а у разных журналов ключи разные."""
        каталог = tempfile.mkdtemp()
        с = self.запустить(каталог=каталог)
        self.послать(b"Source=%2B79991234567&Date=1", "application/x-www-form-urlencoded")
        self.послать(b"Source=%2B79991234567&Date=2", "application/x-www-form-urlencoded")
        один, два = self.запрос(1), self.запрос(2)
        fp = lambda з: {п["name"]: п["fp"] for п in з["fields"]}
        self.assertEqual(fp(один)["Source"], fp(два)["Source"])
        self.assertNotEqual(fp(один)["Date"], fp(два)["Date"])
        с.shutdown()
        с.server_close()
        журнал = self.журнал
        повтор = зонд.Зонд(("127.0.0.1", 0), журнал=журнал, тихо=True)
        self.поднять(повтор)
        self.послать(b"Source=%2B79991234567&Date=1", "application/x-www-form-urlencoded")
        третий = [з for з in self.записи("received")][-1]
        self.assertEqual(третий["fingerprint"], один["fingerprint"])
        другой = зонд.Зонд(("127.0.0.1", 0), журнал=os.path.join(tempfile.mkdtemp(), "j"),
                           тихо=True)
        self.addCleanup(другой.server_close)
        self.assertNotEqual(другой.ключ, повтор.ключ)
        with open(os.path.join(os.path.dirname(журнал), "acr-probe.key"), "wb") as fh:
            fh.write(b"short")
        with self.assertRaises(ValueError):
            зонд.Зонд(("127.0.0.1", 0), журнал=журнал, тихо=True)

    def test_задержка_и_уход_клиента(self):
        """Вопрос 4 вердикта — таймаут: приход пишется до задержки, а то, что
        клиент закрыл соединение, — с числом секунд от конца тела. Задержка
        досиживается: клиент, закрывший только запись, ответа ещё ждёт."""
        self.запустить(задержка=1.2)
        соед = http.client.HTTPConnection("127.0.0.1", self.порт, timeout=0.4)
        соед.request("POST", "/acr", body=b"a=1",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(socket.timeout):
            соед.getresponse()
        self.assertTrue(self.записи("received"), "приход записан ещё до ответа")
        self.assertFalse(self.записи("outcome"))
        соед.close()
        з = self.запрос()
        self.assertTrue(0.3 <= з["client_gone_after"] < 1.1, з)
        self.assertGreaterEqual(з["seconds"], 1.2)
        self.assertEqual(з["replied"], 200)

    def test_уход_считается_от_конца_тела(self):
        """Ревью 2, P3-1: медленная выгрузка не добавляется к таймауту."""
        self.запустить(задержка=1.2)
        с = self.сокет()
        с.sendall(b"POST /acr HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\n12345")
        time.sleep(0.6)
        с.sendall(b"67890")
        time.sleep(0.3)
        с.close()
        з = self.запрос()
        self.assertGreaterEqual(з["read_seconds"], 0.5)
        self.assertTrue(0.2 <= з["client_gone_after"] < 0.6, з)

    def test_закрывший_запись_получает_ответ(self):
        self.запустить(задержка=0.5)
        с = self.сокет()
        с.sendall(b"POST /acr HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\n\r\nx")
        с.shutdown(socket.SHUT_WR)
        t0 = time.monotonic()
        self.assertTrue(прочесть(с).startswith(b"HTTP/1.1 200"))
        self.assertGreaterEqual(time.monotonic() - t0, 0.45)
        з = self.запрос()
        self.assertTrue(з["sent"])
        self.assertLess(з["client_gone_after"], 0.3)
        self.assertGreaterEqual(з["seconds"], 0.5)

    def test_сброс_клиента(self):
        """Ревью 2, S12: сброс (RST) — тоже уход, и ждать больше некого."""
        self.запустить(задержка=3.0)
        с = socket.create_connection(("127.0.0.1", self.порт), timeout=10)
        с.sendall(b"POST /acr HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\n\r\nx")
        time.sleep(0.2)
        с.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        с.close()
        з = self.запрос()
        self.assertIsNotNone(з["client_gone_after"])
        self.assertLess(з["seconds"], 2.5)

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
        с = self.сокет()
        с.sendall(b"HEAD /acr HTTP/1.1\r\nHost: x\r\n\r\n")
        шапка, _, остаток = прочесть(с).partition(b"\r\n\r\n")
        self.assertIn(b"Content-Length: 2", шапка)
        self.assertEqual(остаток, b"", "на HEAD тела нет")
        записи = [self.запрос(n) for n in range(1, 7)]
        self.без_тайного()
        self.assertEqual([з["method"] for з in записи],
                         ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"])
        self.assertEqual([{k: п.get(k) for k in ("name", "shape", "value")}
                          for п in записи[0]["query"]],
                         [{"name": "number", "shape": "+99999999999", "value": None},
                          {"name": "date", "shape": None, "value": "1760000000"}])

    def test_длинный_query_с_пометкой(self):
        self.запустить()
        self.послать(метод="GET", путь="/acr?" + "&".join("f%d=1" % i for i in range(250)))
        з = self.запрос()
        self.assertEqual((len(з["query"]), з["query_cut"]), (зонд.ПОЛЕЙ_МАКС, 250))

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
                   False, 200, None, 5),
                  (шапка + b"Content-Length: +79991234567\r\n\r\n", False, 400, "bad-length",
                   0),
                  (чанки + b"-0\r\n\r\n", False, 400, "bad-length", 0))
        for байты, закрыть, код, беда, размер in случаи:
            self.assertEqual(self.сырой(байты, закрыть), код, байты[:90])
        for n, (_, _, код, беда, размер) in enumerate(случаи, 1):
            з = self.запрос(n)
            self.assertEqual((з["body_problem"], з["replied"], з["body_size"]),
                             (беда, код, размер), n)
            self.assertEqual("fields" in з, беда is None, n)
        длина = [h for h in self.запрос(7)["headers"] if h["name"] == "content-length"]
        self.assertEqual(длина, [{"name": "content-length", "length": 12,
                                  "shape": "+99999999999"}])
        self.без_тайного()

    def test_обрыв_по_тишине(self):
        """Тишина сокета дольше `timeout` — обрыв с тем, что успело прийти."""
        self.assertTrue(0 < зонд._Обработчик.timeout <= 600)
        with mock.patch.object(зонд._Обработчик, "timeout", 0.3):
            self.запустить()
            с = self.сокет()
            с.sendall(b"POST /acr HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n0123456789")
            self.assertTrue(прочесть(с).startswith(b"HTTP/1.1 400"))
            с.close()
        з = self.запрос()
        self.assertEqual((з["body_problem"], з["body_size"]), ("cut", 10))

    def test_кривой_multipart_и_суррогат(self):
        self.запустить()
        self.послать(multipart([("Source", "+79991234567")]), "multipart/form-data")
        вложенный = ('--A\r\nContent-Disposition: form-data; name="files"\r\n'
                     'Content-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\n'
                     'Content-Disposition: file; filename="+79991234567.m4a"\r\n'
                     'Content-Type: audio/mp4\r\n\r\n').encode() + АУДИО + b"\r\n--B--\r\n--A--\r\n"
        self.послать(вложенный, "multipart/form-data; boundary=A")
        self.послать(b'{"a": "\\ud83d", "\\ud83d": "+79991234567"}', "application/json")
        rfc2231 = ("--C\r\nContent-Disposition: form-data; name*=utf-8''Source\r\n\r\n"
                   "+79991234567\r\n"
                   "--C\r\nContent-Disposition: form-data; name=\"meta\"\r\n"
                   "Content-Type: application/json\r\n\r\n"
                   "{\"number\": \"+79991234567\", \"date\": 1760000000}\r\n--C--\r\n").encode()
        self.послать(rfc2231, "multipart/form-data; boundary=C")
        без_границы, влож, суррогат, части = [self.запрос(n) for n in range(1, 5)]
        self.без_тайного()
        self.assertIn("multipart-no-parts", без_границы["note"])
        self.assertTrue(без_границы["files"][0]["text_shape"].startswith(
            "------a  a-a: a-a; a=\"a\"    +99999999999"))
        self.assertEqual(влож["note"], ["nested:multipart/mixed"])
        self.assertEqual((влож["files"][0]["format"], влож["files"][0]["size"]),
                         ("ftyp:M4A ", len(АУДИО)))
        self.assertEqual([п.get("shape") for п in суррогат["fields"]], ["·", "+99999999999"])
        self.assertNotIn("parse_error", части)
        self.assertEqual(части["note"], ["json-part"])
        self.assertEqual([(п["name"], п.get("value", п.get("shape"))) for п in части["fields"]],
                         [("Source", "+99999999999"), ("meta.number", "+99999999999"),
                          ("meta.date", "1760000000")])

    def test_сверх_предела(self):
        self.запустить(предел=10)
        self.assertEqual(self.послать(multipart([("Source", "x" * 50)]),
                                      "multipart/form-data; boundary=" + ГРАНИЦА)[0], 413)
        з = self.запрос()
        self.assertEqual((з["body_problem"], з["replied"], з["body_size"]), ("limit", 413, 0))
        self.assertNotIn("fields", з)

    def test_expect_100_continue(self):
        """Ревью 2, P3-6: клиент с `Expect: 100-continue` тело шлёт после «100»,
        а сверх предела получает 413 сразу и тела не шлёт."""
        self.запустить(предел=10)
        с = self.сокет()
        с.sendall(b"POST /acr HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n"
                  b"Expect: 100-continue\r\n\r\n")
        self.assertTrue(с.recv(1024).startswith(b"HTTP/1.1 100"))
        с.sendall(b"a=1")
        self.assertTrue(прочесть(с).startswith(b"HTTP/1.1 200"))
        с = self.сокет()
        с.sendall(b"POST /acr HTTP/1.1\r\nHost: x\r\nContent-Length: 50\r\n"
                  b"Expect: 100-continue\r\n\r\n")
        self.assertTrue(прочесть(с).startswith(b"HTTP/1.1 413"))
        с.close()
        self.assertEqual(self.запрос(1)["body_size"], 3)
        self.assertEqual(self.запрос(2)["body_problem"], "limit")

    def test_tls_на_открытый_порт(self):
        """Адрес с https:// в ACR — зонд это называет, а не молчит."""
        self.запустить()
        self.assertIsNone(self.сырой(b"\x16\x03\x01\x00\x05hello"))
        отказ = self.записи("rejected")[0]
        self.assertEqual(отказ["reason"], "tls-handshake")

    def test_не_http_без_содержания(self):
        self.запустить(тихо=False)
        вывод = io.StringIO()
        with contextlib.redirect_stdout(вывод):
            self.assertEqual(self.сырой(b"+79991234567 /acr HTTP/1.1\r\nHost: x\r\n\r\n"), 501)
        отказ = self.записи("rejected")[0]
        self.assertEqual(отказ["reason"], "http-501")
        self.assertTrue(отказ["line_shape"].startswith("+99999999999 /a a/9.9"))
        self.assertIn("не разобран", вывод.getvalue(), "отказ виден в окне зонда")
        self.без_тайного()
        self.без_тайного(вывод.getvalue())

    def test_путь_с_кривым_адресом(self):
        """Ревью 2, P3-4: `urlsplit` падал на `[` до строки прихода."""
        self.запустить()
        self.assertEqual(self.сырой(b"POST http://[198.51.100.7/acr HTTP/1.1\r\nHost: x\r\n"
                                    b"Content-Length: 0\r\n\r\n"), 200)
        з = self.запрос()
        self.assertEqual(з["path_shape"], "a://[999.99.999.9/a")
        self.без_тайного()

    def test_вывод_процесса_без_содержания(self):
        """Ревью 2, S9 и S10: ни строка о запросе в stdout, ни упавший
        обработчик в stderr не называют адрес телефона, путь и содержание."""
        self.запустить(тихо=False, секрет=СЛОВО)
        вывод, ошибки = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(вывод), contextlib.redirect_stderr(ошибки):
            self.послать(multipart([("Source", "+79991234567"), ("Secret", СЛОВО),
                                    ("Note", "Анна Петрова")]),
                         "multipart/form-data; boundary=" + ГРАНИЦА,
                         путь="/+79991234567/acr")
            self.запрос()
            with mock.patch.object(зонд._Обработчик, "_тело", side_effect=RuntimeError("x")):
                self.assertIsNone(self.сырой(b"POST /acr HTTP/1.1\r\nHost: x\r\n\r\n"))
            до = time.monotonic() + 5
            while "RuntimeError" not in ошибки.getvalue() and time.monotonic() < до:
                time.sleep(0.02)
        self.assertIn("#1", вывод.getvalue())
        self.без_тайного(вывод.getvalue())
        self.assertIn("запрос упал: RuntimeError", ошибки.getvalue())
        self.assertNotIn("127.0.0.1", ошибки.getvalue())
        self.assertNotIn("Traceback", ошибки.getvalue())

    def test_дослушивание_после_кривого_тела(self):
        """Ревью 2, S14: после ответа на кривое или лишнее тело зонд
        дослушивает клиента (`_дослушать`), после обычного ответа — нет. На
        петле это не увидеть по ответу: `socketserver` закрывает запись раньше
        `close`, и RST приходит после FIN, — поэтому проверяется проводка."""
        self.запустить(предел=10)
        with mock.patch.object(зонд._Обработчик, "_дослушать", autospec=True) as дослушать:
            self.assertEqual(self.сырой(b"POST /acr HTTP/1.1\r\nHost: x\r\n"
                                        b"Content-Length: 50\r\n\r\n" + b"x" * 50), 413)
            self.запрос(1)
            self.assertEqual(дослушать.call_count, 1)
            self.послать(b"a=1", "application/x-www-form-urlencoded")
            self.запрос(2)
            self.assertEqual(дослушать.call_count, 1)

    def test_дослушать(self):
        """Ревью 2, S14: после ответа на кривое тело зонд дочитывает, что
        клиент ещё шлёт: закрытие с непрочитанным дало бы RST вместо ответа."""
        свой, чужой = socket.socketpair()
        self.addCleanup(свой.close)
        self.addCleanup(чужой.close)
        чужой.sendall(b"x" * 100)
        чужой.shutdown(socket.SHUT_WR)
        зонд._Обработчик._дослушать(types.SimpleNamespace(connection=свой))
        self.assertEqual(свой.recv(10), b"", "присланное дочитано")
        self.assertEqual(чужой.recv(10), b"", "своя запись закрыта")

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

    def test_права_журнала_и_ключа_исправляются(self):
        каталог = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, каталог, True)
        журнал = os.path.join(каталог, "acr-probe.jsonl")
        ключ = os.path.join(каталог, "acr-probe.key")
        for путь, содержимое in ((журнал, b""), (ключ, os.urandom(32))):
            with open(путь, "wb") as fh:
                fh.write(содержимое)
            os.chmod(путь, 0o644)
        с = зонд.Зонд(("127.0.0.1", 0), журнал=журнал, тихо=True)
        с.server_close()
        for путь in (журнал, ключ):
            self.assertEqual(oct(os.stat(путь).st_mode & 0o777), oct(0o600), путь)


class Аргументы(Основа):
    def test_show_max_mb_status_label(self):
        каталог = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, каталог, True)
        self.журнал = os.path.join(каталог, "acr-probe.jsonl")
        a = зонд.аргументы().parse_args(["--port", "0", "--bind", "127.0.0.1",
                                          "--log", self.журнал, "--show", "Mode, date",
                                          "--max-mb", "1", "--status", "202",
                                          "--label", "шаг 7: перезагрузка\n"])
        self.поднять(зонд.зонд_из(a, тихо=True))
        self.assertEqual(self.послать(b"mode=auto&date=1760000000&direction=out",
                                      "application/x-www-form-urlencoded")[0], 202)
        з = self.запрос()
        self.assertEqual([п.get("value", п.get("shape")) for п in з["fields"]],
                         ["auto", "1760000000", "a"])
        старт = self.записи("start")[0]
        self.assertEqual((старт["show"], старт["max_bytes"], старт["status"], старт["label"]),
                         (["mode", "date"], 1 << 20, 202, "шаг 7: перезагрузка"))

    def test_негодные_значения(self):
        for argv in (["--status", "99"], ["--status", "600"], ["--delay", "-1"],
                     ["--max-mb", "0"], ["--port", "x"], ["--secret", "short"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                зонд.аргументы().parse_args(argv)


class Правила(unittest.TestCase):
    def test_показ_как_есть(self):
        self.assertEqual(зонд.поле("type", "123456")["value"], "123456")
        for значение in ("1234567", "+7 999 123-45-67", "٧٩٩٩١٢٣٤٥٦٧", "x" * 65,
                         "1577836799", "2000000000", "2000000000000", "Входящий",
                         "John Smith", "23-45-67"):
            self.assertNotIn("value", зонд.поле("type", значение), значение)
        for значение in ("1577836800", "1999999999", "1577836800000", "1999999999999"):
            self.assertEqual(зонд.поле("date", значение)["value"], значение)
        self.assertNotIn("value", зонд.поле("note", "auto"), "не из списка показа")
        self.assertNotIn("value", зонд.поле("type", "zond-word", секрет="zond-word"))
        for значение in ("799912.34567", "999.1234.5678"):
            self.assertNotIn("value", зонд.поле("version", значение), "цифр больше шести")
        # base64 слова, похожий на слово показа: связь названа, значение — нет
        з = зонд.поле("type", "YWFhYWFhYWE", секрет="aaaaaaaa")
        self.assertEqual((з["secret"], "value" in з), ("base64", False))
        ф = зонд.файл("File", "a.m4a", "audio/mp4", b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 100)
        self.assertEqual(ф["format"], "ftyp:M4A ")
        self.assertNotIn("sha256", ф, "sha256 — от 4 КиБ и у узнанного формата")

    def test_секретные_имена(self):
        for имя in ("Secret", "api_token", "Password", "X-Api-Key", "Authorization",
                    "Signature", "Cookie", "X-Hash", "X-Acr-Sig"):
            з = зонд.поле(имя, "type")
            self.assertEqual(set(з), {"name", "length"}, имя)

    def test_имя_поля(self):
        self.assertEqual(зонд.имя_поля("Source"), {"name": "Source"})
        for имя in ("+79991234567", "a1234567", "Анна", "x" * 41, ""):
            self.assertIn("name_shape", зонд.имя_поля(имя), имя)
        self.assertIn("name_shape", зонд.имя_поля("zond-word", "zond-word"))

    def test_тип_форма_байтов_текст(self):
        self.assertEqual(зонд.базовый_тип("audio/mp4; x=1"), "audio/mp4")
        self.assertEqual(зонд.базовый_тип("Аудио/mp4"), "a/a9")
        self.assertEqual(зонд.форма_байтов(b"\x00Ab9=<>~ z"), "~aa9~~~~~a")
        self.assertTrue(зонд.текст_ли(b"\xff\xfe" + "Анна".encode("utf-16-le")))
        self.assertFalse(зонд.текст_ли("Анна".encode("utf-16-le")))

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
