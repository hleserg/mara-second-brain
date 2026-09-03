"""Сквозной прогон: файл записи на диске превращается в карточку разговора.

Настоящие whisper и ollama живут на bigpc и в тестах недоступны, поэтому оба
поднимаются рядом заглушками. Проверяется не качество расшифровки, а то, что
приём, нарезка, извлечение, проекция и дайджест соединены и переживают повтор.

Отдельно проверяется граница: аудио остаётся вне волта, вне git и вне
фильтров R2 (ТЗ §11, §13).
"""
import os, sys, json, time, shutil, hashlib, tempfile, threading, subprocess, unittest
import urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

ЕСТЬ_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

РАСШИФРОВКА = "Серёж привет пришли смету до пятницы"
ИЗВЛЕЧЕНИЕ = {
    "requests": [{"action": "прислать смету", "requester": "Анна", "owner": "sergey",
                  "explicit": True, "confidence": 0.93, "deadline_phrase": "до пятницы",
                  "evidence": [{"start_ms": 0, "end_ms": 25000}]}],
    "commitments": [], "decisions": [], "constraints": [], "open_questions": [],
    "changed_instructions": [], "followups": [],
    "people_mentioned": ["Анна"], "projects_mentioned": [],
}


class Заглушка(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        self.server.hits.append((self.path, len(raw)))
        if self.path.endswith("/sendMessage"):
            self.server.telegram.append(urllib.parse.unquote_plus(
                raw.decode("utf-8")))
            body = {"ok": True}
        elif self.path == "/transcribe":
            body = {"text": РАСШИФРОВКА}
        else:                                  # ollama /api/generate
            body = {"response": json.dumps(ИЗВЛЕЧЕНИЕ, ensure_ascii=False)}
        out = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def карточка(vault):
    with open(os.path.join(vault, "kb/conversations/2026-09-02-1405-anna.md"),
              encoding="utf-8") as fh:
        return fh.read()


def поднять():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Заглушка)
    srv.hits, srv.telegram = [], []
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@unittest.skipUnless(ЕСТЬ_FFMPEG, "нет ffmpeg/ffprobe — нарезку проверить нечем")
class Сквозной(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bigpc = поднять()
        base = "http://127.0.0.1:%d" % cls.bigpc.server_address[1]
        cls.blobs = tempfile.mkdtemp(prefix="mara-blobs-")
        cls.vault = tempfile.mkdtemp(prefix="mara-vault-")
        os.makedirs(os.path.join(cls.vault, ".git"))
        cls.env0 = dict(os.environ)
        os.environ.update({
            "MARA_BLOBS": cls.blobs, "VAULT": cls.vault,
            "MARA_ASR_URL": base, "MARA_LLM_URL": base,
            "MARA_ENV_FILE": os.path.join(cls.blobs, "нет-такого.env"),
            # шаги идут отдельными процессами, подменять функцию бесполезно:
            # телеграм заменяем адресом, а не заглушкой в памяти
            "MARA_TELEGRAM_API": base + "/bot%s/sendMessage",
            "TELEGRAM_BOT_TOKEN": "test-token",   # уходит в путь URL: только ascii
            "TELEGRAM_HOME_CHANNEL": "@тест",
        })
        import mara_ingest as mi
        import contextd
        mi.ROOT = cls.blobs
        cls.mi, cls.cd = mi, contextd
        cls.tmp = tempfile.mkdtemp(prefix="mara-fixture-")
        cls.audio = os.path.join(cls.tmp, "sample-call.m4a")
        subprocess.run(["bash", os.path.join(ROOT, "tests/fixtures/make_sample_call.sh"),
                        cls.audio], check=True, capture_output=True)
        with open(cls.audio, "rb") as fh:
            cls.raw = fh.read()
        cls.sha = hashlib.sha256(cls.raw).hexdigest()

        cls.srv = contextd.make_server(cls.blobs, 0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        con = mi.connect(cls.blobs)
        _, cls.token = contextd.pair(con, "e2e")
        cls.stop = threading.Event()
        threading.Thread(target=contextd.worker, args=(cls.stop, cls.blobs),
                         daemon=True).start()

        cls.event_id = cls.приём(cls)
        cls.дубль = cls.приём(cls)
        cls.дождаться(cls)

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.srv.shutdown()
        cls.bigpc.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.clear()                    # соседние тесты не должны видеть
        os.environ.update(cls.env0)           # наши MARA_* и VAULT

    def запрос(self, path, data, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")

    def приём(self):
        ev = {"schema": "mara.event.v1", "kind": "call", "source": "android-capture",
              "source_id": "rec-001", "occurred_at": "2026-09-02T14:05:00+03:00",
              "ended_at": "2026-09-02T14:05:30+03:00", "classification": "personal",
              "payload": {"contact_name": "Анна", "direction": "incoming",
                          "producer": "huawei-oem"},
              "blob": {"sha256": self.sha, "ext": "m4a", "bytes": len(self.raw)}}
        got = self.запрос(self, "/v1/ingest/event",
                          json.dumps(ev, ensure_ascii=False).encode("utf-8"))
        self.запрос(self, "/v1/ingest/audio?event=" + got["event_id"], self.raw,
                    "application/octet-stream")
        return got["event_id"]

    def дождаться(self, сек=180):
        con = self.mi.connect(self.blobs)
        предел = time.time() + сек
        while time.time() < предел:
            state = con.execute("select state from events where id=?",
                                (self.event_id,)).fetchone()["state"]
            dlq = con.execute("select count(*) from jobs where state='dlq'").fetchone()[0]
            if state == "done" or dlq:
                break
            time.sleep(0.5)

    # --- собственно проверки ------------------------------------------------

    def test_очередь_дошла_до_конца_без_dlq(self):
        con = self.mi.connect(self.blobs)
        bad = con.execute("select kind,last_error from jobs where state='dlq'").fetchall()
        self.assertEqual([dict(r) for r in bad], [], "работа в DLQ — пайплайн порвался")
        self.assertEqual(con.execute("select state from events where id=?",
                                     (self.event_id,)).fetchone()["state"], "done")

    def test_расшифровка_и_извлечение_на_диске(self):
        self.assertTrue(os.path.exists(
            self.mi.transcript_path(self.blobs, self.event_id)), "нет транскрипта")
        with open(self.mi.extraction_path(self.blobs, self.event_id),
                  encoding="utf-8") as fh:
            extr = json.load(fh)
        self.assertEqual(extr["requests"][0]["disposition"], "task")

    def test_карточка_разговора_в_волте(self):
        path = os.path.join(self.vault, "kb/conversations/2026-09-02-1405-anna.md")
        self.assertTrue(os.path.exists(path), "карточки разговора нет")
        text = карточка(self.vault)
        self.assertIn("cloud_allowed: false", text)
        self.assertIn("прислать смету", text)

    def test_обязательство_стало_карточкой(self):
        d = os.path.join(self.vault, "kb/commitments")
        self.assertTrue(os.listdir(d), "просьба выше порога не стала обязательством")

    def test_дайджест_записан_и_доставлен(self):
        con = self.mi.connect(self.blobs)
        row = con.execute("select text,state from digests where event_id=?",
                          (self.event_id,)).fetchone()
        self.assertIsNotNone(row, "дайджест не сохранён")
        self.assertEqual(row["state"], "sent")
        self.assertIn("Попросили", row["text"])
        self.assertIn("Попросили", "\n".join(self.bigpc.telegram),
                      "в транспорт ушёл тот же текст, что лёг в базу")

    def test_аудио_не_попало_в_волт(self):
        плохие = [f for _, _, fs in os.walk(self.vault) for f in fs
                  if f.endswith((".m4a", ".wav", ".mp3", ".opus", ".amr"))]
        self.assertEqual(плохие, [], "аудио в волте — оно уедет в R2 первым же синком")

    def test_блобы_лежат_вне_дерева_волта(self):
        self.assertFalse(os.path.abspath(self.blobs).startswith(
            os.path.abspath(self.vault) + os.sep), "блобы внутри волта")
        with open(os.path.join(ROOT, "config/r2-filters.txt"), encoding="utf-8") as fh:
            filters = fh.read()
        self.assertNotIn("mara-blobs", filters,
                         "блобы вне дерева волта, исключать их в фильтрах незачем")

    def test_расшифровка_не_ушла_в_волт_целиком(self):
        self.assertNotIn(РАСШИФРОВКА, карточка(self.vault),
                         "в карточку идут выжимки и метки, а не реплики (ТЗ §10)")

    def test_повтор_не_создаёт_вторую_карточку(self):
        self.assertEqual(self.дубль, self.event_id, "дедуп по хешу аудио не сработал")
        found = [f for _, _, fs in os.walk(os.path.join(self.vault, "kb")) for f in fs]
        self.assertEqual(len(found), len(set(found)), "карточки задвоились")
        self.assertEqual(len(os.listdir(os.path.join(
            self.vault, "kb/conversations"))), 1)

    def test_куски_ушли_в_asr_целиком(self):
        куски = [h for h in self.bigpc.hits if h[0] == "/transcribe"]
        self.assertEqual(len(куски), 2, "тридцать секунд это два окна по 25 с")
        self.assertTrue(all(n > 1000 for _, n in куски), "в whisper ушёл пустой кусок")


if __name__ == "__main__":
    unittest.main()
