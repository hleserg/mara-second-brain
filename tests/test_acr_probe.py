"""RES-REC-001 (ТЗ §7.3, §20): зонд `scripts/acr_report_probe.py` —
контролируемая точка приёма для «Отчёта на удалённый сервер» ACR Phone.
Журнал называет форму запроса и ничего из содержания: номер, имя, заметка,
секрет и аудио в него не попадают."""
import http.client, json, os, sys, tempfile, threading, unittest

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import acr_report_probe as зонд

ГРАНИЦА = "----mara"
АУДИО = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 100


def multipart(поля, файл=None):
    куски = []
    for имя, значение in поля:
        куски.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (ГРАНИЦА, имя, значение)).encode("utf-8"))
    if файл:
        имя_файла, тело = файл
        куски.append(('--%s\r\nContent-Disposition: form-data; name="File"; '
                      'filename="%s"\r\nContent-Type: audio/mp4\r\n\r\n'
                      % (ГРАНИЦА, имя_файла)).encode("utf-8") + тело + b"\r\n")
    куски.append(("--%s--\r\n" % ГРАНИЦА).encode("utf-8"))
    return b"".join(куски)


class Зонд(unittest.TestCase):
    def запустить(self, **kw):
        self.каталог = tempfile.mkdtemp()
        self.журнал = os.path.join(self.каталог, "state", "acr-probe.jsonl")
        с = зонд.Зонд(("127.0.0.1", 0), журнал=self.журнал, **kw)
        threading.Thread(target=с.serve_forever, daemon=True).start()
        self.addCleanup(с.server_close)
        self.addCleanup(с.shutdown)
        self.порт = с.server_address[1]
        return с

    def послать(self, тело, тип, путь="/report", заголовки=None, chunked=False):
        соед = http.client.HTTPConnection("127.0.0.1", self.порт, timeout=10)
        h = {"Content-Type": тип, **(заголовки or {})}
        if chunked:
            соед.putrequest("POST", путь)
            for k, v in h.items():
                соед.putheader(k, v)
            соед.putheader("Transfer-Encoding", "chunked")
            соед.endheaders()
            for i in range(0, len(тело), 50):
                кусок = тело[i:i + 50]
                соед.send(b"%x\r\n%s\r\n" % (len(кусок), кусок))
            соед.send(b"0\r\n\r\n")
        else:
            соед.request("POST", путь, body=тело, headers=h)
        ответ = соед.getresponse()
        ответ.read()
        соед.close()
        return ответ.status

    def записи(self):
        with open(self.журнал, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh]

    def test_форма_запроса_без_содержания(self):
        self.запустить(секрет="слово")
        тело = multipart([("Source", "+79991234567"), ("Secret", "слово"), ("Date", "1760000000"),
                          ("Duration", "65000"), ("Note", "Анна Петрова просила смету")],
                         ("+79991234567_2026-10-14.m4a", АУДИО))
        self.assertEqual(self.послать(тело, "multipart/form-data; boundary=" + ГРАНИЦА,
                                      "/api/recording/upload",
                                      {"Authorization": "Bearer hidden-token-123"}), 200)
        with open(self.журнал, encoding="utf-8") as fh:
            сырьё = fh.read()
        for тайное in ("9991234567", "Анна", "смету", "слово", "hidden-token-123", "ftypM4A"):
            self.assertNotIn(тайное, сырьё)
        з = self.записи()[0]
        по_имени = {п["name"]: п for п in з["fields"]}
        self.assertEqual(по_имени["Source"]["shape"], "+99999999999")
        self.assertEqual(по_имени["Secret"], {"name": "Secret", "length": 5,
                                              "matches_secret": True})
        self.assertEqual((по_имени["Date"]["value"], по_имени["Duration"]["value"]),
                         ("1760000000", "65000"), "единицы времени видны")
        self.assertEqual(з["files"][0]["size"], len(АУДИО))
        self.assertTrue(з["files"][0]["magic"].endswith(b"ftypM4A ".hex()))
        self.assertEqual(з["headers"]["authorization"], "<%d знаков>" % len("Bearer hidden-token-123"))
        self.assertEqual(з["path_shape"], "/aaa/aaaaaaaaa/aaaaaa")
        self.assertEqual(oct(os.stat(self.журнал).st_mode & 0o777), oct(0o600))
        self.assertFalse([f for f in os.listdir(self.каталог) if f != "state"],
                         "без --keep тело на диск не ложится")

    def test_chunked_и_keep(self):
        хранить = os.path.join(tempfile.mkdtemp(), "файлы")
        self.запустить(keep=хранить)
        тело = multipart([("Source", "x")], ("a.m4a", АУДИО))
        self.assertEqual(self.послать(тело, "multipart/form-data; boundary=" + ГРАНИЦА,
                                      chunked=True), 200)
        з = self.записи()[0]
        self.assertEqual(з["headers"]["transfer-encoding"], "chunked")
        self.assertTrue(з["files"][0]["kept"])
        with open(os.path.join(хранить, з["files"][0]["sha256"]), "rb") as fh:
            self.assertEqual(fh.read(), АУДИО)

    def test_форма_urlencoded_и_json(self):
        self.запустить()
        self.послать("phone=%2B79991234567&duration=12&direction=out".encode(),
                     "application/x-www-form-urlencoded")
        self.послать(json.dumps({"number": "+79991234567", "date": 1760000000000}).encode(),
                     "application/json")
        форма_, json_ = self.записи()
        self.assertEqual({п["name"]: п.get("value", п.get("shape")) for п in форма_["fields"]},
                         {"phone": "+99999999999", "duration": "12", "direction": "out"})
        self.assertEqual({п["name"]: п.get("value", п.get("shape")) for п in json_["fields"]},
                         {"number": "+99999999999", "date": "1760000000000"})

    def test_код_ответа_и_чужой_секрет(self):
        self.запустить(статус=404, секрет="слово")
        self.assertEqual(self.послать(multipart([("Secret", "другое")]),
                                      "multipart/form-data; boundary=" + ГРАНИЦА), 404)
        з = self.записи()[0]
        self.assertEqual(з["replied"], 404)
        self.assertFalse(з["fields"][0]["matches_secret"])

    def test_сверх_предела_не_разбирается(self):
        self.запустить(предел=10)
        self.послать(multipart([("Source", "x" * 50)]), "multipart/form-data; boundary=" + ГРАНИЦА)
        з = self.записи()[0]
        self.assertTrue(з["truncated"])
        self.assertNotIn("fields", з)

    def test_самопроверка(self):
        self.assertEqual(зонд.self_check(), 0)


if __name__ == "__main__":
    unittest.main()
