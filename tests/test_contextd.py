"""HTTP-поверхность приёма (ТЗ §4, §20)."""
import os, sys, json, hashlib, socket, tempfile, threading, unittest
import urllib.request, urllib.error
from datetime import datetime, timedelta

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
        # устройство поимённо: спаренное, но ни разу не выходившее на связь — −1
        contextd.pair(self.con, "тел\"ефон")
        with urllib.request.urlopen(self.base + "/metrics", timeout=5) as r:
            body = r.read().decode("utf-8")
        self.assertIn('mara_device_last_seen_seconds{name="тел\\"ефон"} -1', body)

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
    def test_событие_телефона_принимается_как_есть(self):
        """Общий фикс контракта: его же шлёт Kotlin-тест приложения.

        Разъедутся стороны — упадёт одна из них, а не телефон в поле.
        """
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "phone-call-event.json")
        with open(p, encoding="utf-8") as fh:
            ev = json.load(fh)
        code, r = self.post("/v1/ingest/event", ev)
        self.assertEqual(code, 200)
        self.assertTrue(r["event_id"].startswith("call_"))
        self.assertTrue(r["need_blob"], "у события есть блоб — сервер обязан его ждать")
        row = self.con.execute("select payload_json, occurred, ended, device_id "
                               "from events where id=?", (r["event_id"],)).fetchone()
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["contact_source"], "call-log",
                         "без этого call_project не заведёт карточку человека")
        self.assertEqual(row["occurred"], ev["occurred_at"])
        self.assertEqual(row["device_id"], self.dev,
                         "устройство берётся из токена, а не из тела")

        # тот же файл под другим именем и от другого рекордера — тот же звонок
        ev["payload"] = dict(ev["payload"], producer="другой рекордер")
        _, снова = self.post("/v1/ingest/event", ev)
        self.assertTrue(снова["duplicate"], "ключ — хеш содержимого, а не имя")
        self.assertEqual(снова["event_id"], r["event_id"])
        self.assertTrue(снова["need_blob"],
                        "аудио так и не приехало — просить его снова, а не закрывать")

    def test_правка_применяется_синхронно(self):
        import context_pack
        with open(os.path.join(self.vault, "kb/commitments", "c.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("---\ntitle: отправить договор\nstatus: open\n---\n")
        было = context_pack.build_now(self.vault)
        code, r = self.post("/v1/ingest/event", {
            "kind": "correction", "source": "mara", "source_id": "k1",
            "payload": {"item": "отправить договор", "status": "done"}})
        self.assertEqual(code, 200)
        self.assertTrue(r["applied"]["found"])
        self.assertIn("open → done", r["applied"]["text"])
        self.assertNotEqual(r["applied"]["pack_sha256"], было, "пакет пересобран до ответа")
        self.assertIn("status: done", open(os.path.join(self.vault, "kb/commitments", "c.md"),
                                           encoding="utf-8").read())
        _, снова = self.post("/v1/ingest/event", {
            "kind": "correction", "source": "mara", "source_id": "k1",
            "payload": {"item": "отправить договор", "status": "done"}})
        self.assertTrue(снова["duplicate"])
        self.assertIsNone(снова["applied"], "дубль второй раз не применяется")

    def test_правка_и_удаление_сообщения_ревизии_а_не_потери(self):
        """ТЗ §11: правки и удаления — ревизии и надгробия. Три события в базе,
        одно состояние наружу через mara_ingest.message_state."""
        новое = {"source": "telegram", "source_id": "7/1", "occurred_at": "2026-09-02T10:00:00+03:00",
                 "payload": {"chat_id": 7, "message_id": 1, "text": "приду в пять"}}
        правка = {"source": "telegram", "source_id": "7/1/edit/1756800600",
                  "occurred_at": "2026-09-02T10:10:00+03:00",
                  "payload": {"chat_id": 7, "message_id": 1, "text": "приду в шесть", "revision_of": "7/1"}}
        удаление = {"source": "telegram", "source_id": "7/1/deleted",
                    "occurred_at": "2026-09-02T10:20:00+03:00",
                    "payload": {"chat_id": 7, "message_id": 1, "tombstone_of": "7/1"}}
        ids = set()
        for ev in (новое, правка):
            code, r = self.post("/v1/ingest/message", ev)
            self.assertEqual(code, 200)
            self.assertFalse(r["duplicate"], "правка с тем же ключом пропала бы как дубль")
            ids.add(r["event_id"])
        self.assertEqual(len(ids), 2)
        self.assertEqual(mi.message_state(self.con, "telegram", "7/1")["text"], "приду в шесть")
        _, r = self.post("/v1/ingest/message", удаление)
        self.assertFalse(r["duplicate"])
        self.assertIsNone(mi.message_state(self.con, "telegram", "7/1"), "надгробие гасит сообщение")
        self.assertEqual(self.con.execute("select count(*) from events where source='telegram' "
                                          "and source_id like '7/1%'").fetchone()[0], 3)
        self.assertIsNone(mi.message_state(self.con, "telegram", "7/2"), "чужого ключа нет — None")
        _, снова = self.post("/v1/ingest/message", новое)
        self.assertTrue(снова["duplicate"], "повторная доставка того же сообщения — дубль")

    def test_метрики_источников_без_подключения_минус_один(self):
        with urllib.request.urlopen(self.base + "/metrics", timeout=5) as r:
            body = r.read().decode("utf-8")
        for m in ("mara_tdlib_lag_seconds -1", "mara_gmail_lag_seconds -1",
                  "mara_whatsapp_lag_seconds -1", "mara_sms_lag_seconds -1",
                  "mara_context_pack_age_seconds", "mara_context_pack_bytes"):
            self.assertIn(m, body)

    def test_кривая_правка_400_и_не_в_базе(self):
        code, r = self.post("/v1/ingest/event", {
            "kind": "correction", "source": "mara", "source_id": "k2",
            "payload": {"item": "что-то", "due": "пятница"}})
        self.assertEqual(code, 400)
        self.assertIn("YYYY-MM-DD", r["error"])
        self.assertEqual(self.con.execute("select count(*) from events where source_id='k2'")
                         .fetchone()[0], 0)

    # ── потери на приёме: N1, N5, N6, N7 из docs/current-state-audit.md ──

    def залить(self, event_id, body):
        return self.post("/v1/ingest/audio?event=" + event_id, body,
                         raw=True, ctype="application/octet-stream")

    def звонок(self, sid, body):
        """Событие звонка с аудио. Возвращает (event_id, ответ сервера)."""
        sha = hashlib.sha256(body).hexdigest()
        ev = {"kind": "call", "source": "phone", "source_id": sid,
              "blob": {"sha256": sha, "bytes": len(body), "ext": "wav"}}
        _, r = self.post("/v1/ingest/event", ev)
        return ev, r

    def test_повтор_события_без_блоба_снова_просит_блоб(self):
        """N1. Ответ на первое событие потерялся, телефон повторяет отправку.
        Пока аудио нет в базе блобов, ответ обязан снова просить блоб: телефон
        трактует `need_blob: false` как «всё принято» и закрывает работу."""
        ev, первый = self.звонок("n1", "аудио которое не доехало".encode("utf-8"))
        self.assertTrue(первый["need_blob"])
        _, повтор = self.post("/v1/ingest/event", ev)
        self.assertTrue(повтор["duplicate"], "то же событие — конечно дубль")
        self.assertTrue(повтор["need_blob"],
                        "блоба нет в базе — повтор обязан просить его снова")

    def test_после_загрузки_повтор_блоб_не_просит(self):
        тело = "аудио которое доехало".encode("utf-8")
        ev, r = self.звонок("n1-ok", тело)
        self.post("/v1/ingest/audio?event=" + r["event_id"], тело,
                  raw=True, ctype="application/octet-stream")
        _, повтор = self.post("/v1/ingest/event", ev)
        self.assertFalse(повтор["need_blob"], "блоб лежит — второй раз не нужен")

    def test_вычищенный_ретеншеном_блоб_заново_не_просим(self):
        """Ретеншен удалил аудио по сроку (ТЗ §5). Повторная загрузка вернула бы
        удалённое намеренно, поэтому просить блоб нельзя."""
        тело = "старое аудио".encode("utf-8")
        ev, r = self.звонок("n1-purged", тело)
        self.post("/v1/ingest/audio?event=" + r["event_id"], тело,
                  raw=True, ctype="application/octet-stream")
        self.con.execute("update blobs set purged_at=? where sha256=?",
                         (mi.now_iso(), hashlib.sha256(тело).hexdigest()))
        _, повтор = self.post("/v1/ingest/event", ev)
        self.assertFalse(повтор["need_blob"], "вычищенное аудио не запрашиваем заново")

    def test_блоб_принят_а_событие_осталось_new_чинится_повтором(self):
        """Демон упал между записью блоба и переводом события в stored. Сегодня
        такое событие навсегда остаётся `new` без работы ASR: повтор с телефона
        видит дубль и уходит. Повтор обязан довести событие до конца."""
        тело = "аудио после падения".encode("utf-8")
        sha = hashlib.sha256(тело).hexdigest()
        ev, r = self.звонок("n1-crash", тело)
        self.post("/v1/ingest/audio?event=" + r["event_id"], тело,
                  raw=True, ctype="application/octet-stream")
        self.con.execute("update events set state='new' where id=?", (r["event_id"],))
        self.con.execute("delete from jobs where event_id=?", (r["event_id"],))
        _, повтор = self.post("/v1/ingest/event", ev)
        self.assertFalse(повтор["need_blob"], "байты на диске — грузить нечего")
        self.assertEqual(self.con.execute("select state from events where id=?",
                                          (r["event_id"],)).fetchone()["state"], "stored")
        self.assertEqual(self.con.execute("select count(*) from jobs where event_id=? "
                                          "and kind='asr'", (r["event_id"],)).fetchone()[0], 1,
                         "ровно одна работа ASR, а не вторая на каждый повтор")

    def test_загрузка_блоба_дважды_не_плодит_работу(self):
        тело = "аудио дважды".encode("utf-8")
        ev, r = self.звонок("n1-twice", тело)
        for _ in range(2):
            code, _ = self.post("/v1/ingest/audio?event=" + r["event_id"], тело,
                                raw=True, ctype="application/octet-stream")
            self.assertEqual(code, 200)
        self.assertEqual(self.con.execute("select count(*) from jobs where event_id=? "
                                          "and kind='asr'", (r["event_id"],)).fetchone()[0], 1)

    def test_параллельные_загрузки_одного_аудио_не_рвут_файл(self):
        """N12. Два события с одним аудио льют его одновременно. На общем
        ".part" второй писатель усекает файл первого, и os.replace публикует
        склейку — либо падает, если сосед уже унёс временный файл."""
        тело = ("параллель " * 4096).encode("utf-8")
        sha = hashlib.sha256(тело).hexdigest()
        _, a = self.звонок("n12-a", тело)
        _, b = self.звонок("n12-b", тело)
        настоящий, сосед = contextd.os.replace, []

        def перехват(src, dst):
            """Влезть ровно между записью временного файла и публикацией."""
            if not сосед:
                сосед.append(None)                 # флаг до вызова: иначе рекурсия
                сосед[0] = self.залить(b["event_id"], тело)
            return настоящий(src, dst)

        contextd.os.replace = перехват
        try:
            code, _ = self.залить(a["event_id"], тело)
        finally:
            contextd.os.replace = настоящий
        self.assertEqual((code, сосед[0][0]), (200, 200), "обе загрузки — успех")
        path = self.con.execute("select path from blobs where sha256=?",
                                (sha,)).fetchone()["path"]
        with open(path, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), sha,
                             "опубликован не тот файл, который обещали хешем")

    def test_блоб_ищется_в_базе_а_не_по_сегодняшней_дате(self):
        """N5. Путь блоба считается от даты загрузки. Августовская запись,
        долитая в сентябре, по вычисленному сегодня пути не находится, и
        повторная загрузка перезаписала бы строку вместе с pin и audio_until."""
        тело = "аудио из прошлого месяца".encode("utf-8")
        sha = hashlib.sha256(тело).hexdigest()
        ev, r = self.звонок("n5", тело)
        self.post("/v1/ingest/audio?event=" + r["event_id"], тело,
                  raw=True, ctype="application/octet-stream")
        прошлое = mi.blob_path(self.dir, sha, "wav",
                               when=datetime.now(mi.TZ) - timedelta(days=45))
        os.makedirs(os.path.dirname(прошлое), mode=0o700, exist_ok=True)
        os.replace(mi.blob_path(self.dir, sha, "wav"), прошлое)
        self.con.execute("update blobs set path=?, pin=1 where sha256=?", (прошлое, sha))
        code, got = self.post("/v1/ingest/audio?event=" + r["event_id"], тело,
                              raw=True, ctype="application/octet-stream")
        self.assertEqual(code, 200)
        self.assertTrue(got["duplicate"], "блоб известен базе, хоть путь и не сегодняшний")
        строка = self.con.execute("select path, pin from blobs where sha256=?",
                                  (sha,)).fetchone()
        self.assertEqual(строка["path"], прошлое, "путь в базе не затирается")
        self.assertEqual(строка["pin"], 1, "закрепление владельца переживает повтор")

    def test_после_401_соединение_закрывается(self):
        """N6. Тело неопознанного запроса остаётся в сокете. При keep-alive
        следующий запрос по тому же соединению читается как продолжение тела —
        HttpURLConnection на телефоне переиспользует сокеты."""
        s = socket.create_connection(self.srv.server_address, timeout=5)
        тело = json.dumps({"kind": "call", "source": "phone"}).encode("utf-8")
        s.sendall(("POST /v1/ingest/event HTTP/1.1\r\nHost: x\r\n"
                   "Authorization: Bearer нет-такого\r\nContent-Type: application/json\r\n"
                   "Content-Length: %d\r\n\r\n" % len(тело)).encode("utf-8") + тело)
        куски = b""
        try:
            while True:
                чанк = s.recv(65536)
                if not чанк:
                    break
                куски += чанк
        except socket.timeout:
            self.fail("сокет остался открыт: непрочитанное тело ждало бы в нём "
                      "следующего запроса телефона")
        finally:
            s.close()
        self.assertIn(b"401", куски.split(b"\r\n")[0])


class ТестМетрикиНеИзЛокалки(unittest.TestCase):
    """/metrics без токена — только с петли (ТЗ §18).

    Сервер поднимается на 127.0.0.2, клиент явно берёт исходный адрес 127.0.0.3:
    наружу ничего не открывается, но демон видит адрес, которого нет в LOOPBACK
    — то есть ровно то, чем для него выглядит любой хост из домашней локалки.
    """

    def test_чужой_адрес_получает_401(self):
        import http.client
        root = tempfile.mkdtemp()
        mi.ROOT = root
        try:
            srv = contextd.make_server(root, 0, host="127.0.0.2")
        except OSError:
            self.skipTest("в этом окружении нет 127.0.0.2")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def запрос(path, откуда="127.0.0.3"):
                c = http.client.HTTPConnection("127.0.0.2", srv.server_address[1],
                                               timeout=5, source_address=(откуда, 0))
                c.request("GET", path)
                r = c.getresponse()
                тело = r.read()
                c.close()
                return r.status, тело

            self.assertEqual(запрос("/metrics")[0], 401)
            код, тело = запрос("/healthz")           # проверка связи с телефона
            self.assertEqual(код, 200)
            # наружу — только «жив»: счётчики выдают распорядок дня владельца
            self.assertEqual(json.loads(тело), {"ok": True})
            код, тело = запрос("/healthz", "127.0.0.1")
            self.assertEqual(код, 200)
            self.assertIn("free_gb", json.loads(тело))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
