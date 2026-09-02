"""HTTP-поверхность приёма (ТЗ §4, §20)."""
import os, sys, json, hashlib, tempfile, threading, unittest
import urllib.request, urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import contextd


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        mi.ROOT = cls.dir                      # демон и тест смотрят в одну базу
        cls.vault = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.vault, ".git"))
        os.makedirs(os.path.join(cls.vault, "kb/commitments"))
        cls.srv = contextd.make_server(cls.dir, port=0, vault=cls.vault)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.con = mi.connect(cls.dir)
        cls.dev, cls.token = contextd.pair(cls.con, "тестовый телефон")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, path, data, token=None, raw=False, ctype="application/json"):
        body = data if raw else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", ctype)
        req.add_header("Authorization", "Bearer " + (token or self.token))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_без_токена_401(self):
        code, _ = self.post("/v1/ingest/event", {"kind": "call"}, token="no-such-token")
        self.assertEqual(code, 401)

    def test_событие_создаёт_работу_и_просит_блоб(self):
        sha = hashlib.sha256(b"audio-1").hexdigest()
        code, r = self.post("/v1/ingest/event", {
            "kind": "call", "source": "phone", "source_id": "c1",
            "blob": {"sha256": sha, "bytes": 7, "ext": "wav"}})
        self.assertEqual(code, 200)
        self.assertTrue(r["need_blob"])
        self.assertFalse(r["duplicate"])

    def test_повтор_события_дубль(self):
        ev = {"kind": "call", "source": "phone", "source_id": "c2"}
        self.post("/v1/ingest/event", ev)
        _, r = self.post("/v1/ingest/event", ev)
        self.assertTrue(r["duplicate"])

    def test_битый_хеш_не_успех(self):
        sha = hashlib.sha256("правильное".encode("utf-8")).hexdigest()
        _, r = self.post("/v1/ingest/event", {
            "kind": "call", "source": "phone", "source_id": "c3",
            "blob": {"sha256": sha, "bytes": 10, "ext": "wav"}})
        code, _ = self.post("/v1/ingest/audio?event=" + r["event_id"], "другое".encode("utf-8"),
                            raw=True, ctype="application/octet-stream")
        self.assertEqual(code, 409)
        self.assertFalse(os.path.exists(mi.blob_path(self.dir, sha, "wav")),
                         "частичный файл должен быть удалён")

    def test_правильный_блоб_принимается_и_ставит_работу(self):
        body = "это как бы аудио".encode("utf-8")
        sha = hashlib.sha256(body).hexdigest()
        _, r = self.post("/v1/ingest/event", {
            "kind": "call", "source": "phone", "source_id": "c4",
            "blob": {"sha256": sha, "bytes": len(body), "ext": "wav"}})
        code, got = self.post("/v1/ingest/audio?event=" + r["event_id"], body,
                              raw=True, ctype="application/octet-stream")
        self.assertEqual(code, 200)
        self.assertTrue(os.path.exists(mi.blob_path(self.dir, sha, "wav")))
        row = self.con.execute("select count(*) from jobs where event_id=? and kind='asr'",
                               (r["event_id"],)).fetchone()[0]
        self.assertEqual(row, 1, "после блоба ставится ровно одна работа ASR")
        self.assertEqual(oct(os.stat(mi.blob_path(self.dir, sha, "wav")).st_mode)[-3:],
                         "600", "личный разговор читает только владелец")

    def get(self, path, token=None):
        req = urllib.request.Request(self.base + path)
        req.add_header("Authorization", "Bearer " + (token or self.token))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_статус_работы_отдаётся(self):
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "c5"})
        jid = mi.add_job(self.con, eid, "asr")
        code, d = self.get("/v1/jobs/" + jid)
        self.assertEqual(code, 200)
        self.assertEqual(d["state"], "ready")

    def test_статус_работы_требует_токен(self):
        code, _ = self.get("/v1/jobs/no-such-job", token="wrong-token")
        self.assertEqual(code, 401)

    def test_healthz_и_метрики(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=5) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        with urllib.request.urlopen(self.base + "/metrics", timeout=5) as r:
            body = r.read().decode("utf-8")
        self.assertIn("mara_ingest_queue_depth", body)
        self.assertIn("mara_dlq_count", body)

    def test_отозванное_устройство_401(self):
        dev, token = contextd.pair(self.con, "потерянный")
        self.con.execute("update devices set revoked_at=? where id=?",
                         (mi.now_iso(), dev))
        code, _ = self.post("/v1/ingest/event", {"kind": "call"}, token=token)
        self.assertEqual(code, 401)

    def test_сообщение_и_почта_принимаются_тем_же_контрактом(self):
        code, r = self.post("/v1/ingest/message", {
            "source": "telegram", "source_id": "m1", "payload": {"text": "привет"}})
        self.assertEqual(code, 200)
        self.assertTrue(r["event_id"].startswith("message_"))
        code, r = self.post("/v1/ingest/email", {
            "source": "gmail", "source_id": "e1", "payload": {"subject": "тема"}})
        self.assertEqual(code, 200)
        self.assertTrue(r["event_id"].startswith("email_"))

    def test_тело_сообщения_не_попадает_в_лог(self):
        line = contextd.log_line("POST", "/v1/ingest/message", 200,
                                 {"payload": {"text": "секретная фраза"}})
        self.assertNotIn("секретная фраза", line)

    def test_бутстрап_без_пакета_отдаёт_пусто_а_не_ошибку(self):
        self.assertIsNone(contextd.now_pack(tempfile.mkdtemp()),
                          "брокер может быть ещё не собран, это не сбой")

    def test_бутстрап_отдаёт_пакет_с_подписью(self):
        import context_pack
        with open(os.path.join(self.vault, "kb/commitments", "a.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("---\ntitle: прислать смету\nstatus: proposed\n"
                     "due: 2026-09-04\n---\n\n- Обещание: тело карточки\n")
        sha = context_pack.build_now(self.vault)
        code, d = self.get("/v1/context/bootstrap")
        self.assertEqual(code, 200)
        self.assertEqual(d["now"]["sha256"], sha,
                         "подпись нужна клиенту, чтобы молчать, когда не изменилось")
        self.assertIn("прислать смету", d["now"]["text"])
        self.assertNotIn("тело карточки", d["now"]["text"])
    def test_запрос_контекста_отдаёт_то_что_есть_а_не_обещание(self):
        import context_pack
        with open(os.path.join(self.vault, "kb/commitments", "b.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("---\ntitle: позвонить в банк\nstatus: open\n---\n")
        context_pack.build_now(self.vault)
        code, d = self.post("/v1/context/query", {"text": "что там с банком"})
        self.assertEqual(code, 200)
        self.assertEqual(len(d["packs"]), 1, "пакет ровно один и это now")
        self.assertIn("позвонить в банк", d["packs"][0]["text"])


if __name__ == "__main__":
    unittest.main()
