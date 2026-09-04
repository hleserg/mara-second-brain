"""HTTP-поверхность приёма (ТЗ §4, §20)."""
import os, sys, io, json, hashlib, socket, tempfile, threading, unittest
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
        self.assertIn("last_error", d, "с петли отдаётся целиком")

    def test_last_error_только_с_петли(self):
        """Хвост stderr шага наружу не отдаём (ADR-0009, «Откат» п. 3).

        Здесь — прямо у `job_view`, чтобы проверка не скипалась там, где
        нет 127.0.0.2. Тот же состав ответа через живой HTTP с не-петлевого
        адреса — в `ТестLastErrorНеИзЛокалки` ниже.
        """
        row = {"id": "j1", "kind": "asr", "state": "dlq", "attempts": 3,
               "next_at": 0, "last_error": "ffmpeg: /srv/mara-blobs/ab/cd.m4a"}
        self.assertIn("last_error", contextd.job_view(row, True))
        наружу = contextd.job_view(row, False)
        self.assertNotIn("last_error", наружу)
        self.assertEqual(наружу["state"], "dlq", "остальные поля пропали")
        self.assertIn("last_error", row, "исходную строку испортили")

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


class ТестLastErrorНеИзЛокалки(unittest.TestCase):
    """`last_error` наружу не отдаётся (ADR-0009, «Откат и миграция» п. 3).

    Приём тот же, что у `ТестМетрикиНеИзЛокалки`: сервер на 127.0.0.2, клиент
    с исходного адреса 127.0.0.3. Наружу ничего не открывается, но демон видит
    адрес не из `LOOPBACK` — то есть ровно то, чем для него выглядит телефон
    из домашней локалки. Токен при этом верный: проверяем состав ответа, а не
    доступ. Заодно закрываем контракт `tokenOk()` (`Api.kt`): несуществующая
    работа с верным токеном отвечает 404, а не 401.
    """

    def test_снаружи_поля_нет_но_404_на_месте(self):
        import http.client
        root = tempfile.mkdtemp()
        mi.ROOT = root
        try:
            srv = contextd.make_server(root, 0, host="127.0.0.2")
        except OSError:
            self.skipTest("в этом окружении нет 127.0.0.2")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            con = mi.connect(root)
            _, токен = contextd.pair(con, "тестовый телефон")
            eid, _ = mi.put_event(con, {"kind": "call", "source": "phone",
                                        "source_id": "c9"})
            jid = mi.add_job(con, eid, "asr")
            con.execute("update jobs set last_error=? where id=?",
                        ("ffmpeg: /srv/mara-blobs/ab/cd.m4a", jid))
            con.commit()

            def запрос(path, откуда="127.0.0.3"):
                c = http.client.HTTPConnection("127.0.0.2", srv.server_address[1],
                                               timeout=5, source_address=(откуда, 0))
                c.request("GET", path,
                          headers={"Authorization": "Bearer " + токен})
                r = c.getresponse()
                тело = r.read()
                c.close()
                return r.status, json.loads(тело or b"{}")

            код, d = запрос("/v1/jobs/" + jid)
            self.assertEqual(код, 200)
            self.assertNotIn("last_error", d, "хвост stderr ушёл в локалку")
            self.assertEqual(sorted(d), ["attempts", "id", "kind", "next_at",
                                         "state"], "выпало не только last_error")
            self.assertEqual(запрос("/v1/jobs/no-such-job")[0], 404,
                             "телефон отличает свой токен по 404 против 401")
            код, d = запрос("/v1/jobs/" + jid, "127.0.0.1")
            self.assertEqual(код, 200)
            self.assertIn("last_error", d, "с петли отдаётся целиком")
        finally:
            srv.shutdown()
            srv.server_close()


class ТестЛимитПопыток(unittest.TestCase):
    """Подбор токена упирается в 429, верный токен — нет (ТЗ §18)."""

    def setUp(self):
        contextd._неудачи.clear()

    tearDown = setUp

    def test_после_лимита_429_а_верный_токен_проходит(self):
        dir = tempfile.mkdtemp()
        mi.ROOT = dir
        srv = contextd.make_server(dir, port=0, vault=tempfile.mkdtemp())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        con = mi.connect(dir)
        _, токен = contextd.pair(con, "телефон")
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            def дёрнуть(t):
                req = urllib.request.Request(base + "/v1/context/bootstrap")
                req.add_header("Authorization", "Bearer " + t)
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    e.read()
                    return e.code

            коды = [дёрнуть("mimo-%d" % i) for i in range(contextd.ПОПЫТОК + 2)]
            self.assertEqual(коды[:contextd.ПОПЫТОК], [401] * contextd.ПОПЫТОК)
            self.assertEqual(коды[-1], 429, "подбор не упёрся в лимит: %r" % коды)
            # свой не страдает: за одним NAT с подбирающим может быть телефон
            self.assertEqual(дёрнуть(токен), 200)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_чужой_forwarded_for_не_учитывается(self):
        """Без MARA_TRUSTED_PROXY заголовок игнорируется, иначе лимит обходится."""
        class Заголовки(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)

            def get_all(self, k, d=None):
                v = dict.get(self, k)
                return [v] if v is not None else d

        # адреса — из TEST-NET (RFC 5737), а не из 10/8 и 192.168/16: репозиторий
        # публичный, и сторож в test_vault_common грепает дерево на адреса
        # домашней сети, не разбирая, выдумка это в тесте или нет
        class Фейк:
            client_address = ("203.0.113.7", 1234)
            headers = Заголовки({"X-Forwarded-For": "198.51.100.1"})

        self.assertEqual(contextd.клиент(Фейк()), "203.0.113.7")
        прежние = contextd.TRUSTED_PROXY
        contextd.TRUSTED_PROXY = ("203.0.113.7",)
        try:
            self.assertEqual(contextd.клиент(Фейк()), "198.51.100.1")
        finally:
            contextd.TRUSTED_PROXY = прежние


class ТестПоРевьюГраницы(unittest.TestCase):
    """Находки ревью PR #14: кривая длина, память на дублях, заголовки."""

    def поднять(self):
        каталог = tempfile.mkdtemp()
        mi.ROOT = каталог
        srv = contextd.make_server(каталог, port=0, vault=tempfile.mkdtemp())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        con = mi.connect(каталог)
        _, токен = contextd.pair(con, "телефон")
        return каталог, srv, con, токен

    def test_отрицательная_длина_не_обходит_лимит(self):
        """Content-Length: -1 проходил `n > лимит`, а read(-1) читает до EOF.

        Теперь такая длина — 400: разобрать её нельзя, границы тела мы не знаем.
        """
        import http.client
        каталог, srv, con, токен = self.поднять()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.putrequest("POST", "/v1/ingest/event")
            c.putheader("Authorization", "Bearer " + токен)
            c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", "-1")
            c.endheaders()
            c.send(b'{"kind": "call"}')
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 400)
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_дубль_не_поднимает_тело_в_память(self):
        """Сток на выходах ingest_audio — Никуда, а не буфер в памяти."""
        каталог = tempfile.mkdtemp()
        mi.ROOT = каталог
        con = mi.connect(каталог)
        сырьё = b"x" * (contextd.КУСОК + 7)
        sha = hashlib.sha256(сырьё).hexdigest()
        eid, _ = mi.put_event(con, {"kind": "call", "source": "phone",
                                    "source_id": "дубль",
                                    "blob": {"sha256": sha, "bytes": len(сырьё),
                                             "ext": "m4a"}})
        contextd.ingest_audio(con, каталог, eid, io.BytesIO(сырьё), len(сырьё))

        class Считающий(io.BytesIO):
            """Ловит попытку сложить тело в память."""
            накопил = 0

            def write(self, b):
                Считающий.накопил += len(b)
                return len(b)

        было = contextd.Никуда
        contextd.Никуда = Считающий
        try:
            поток = io.BytesIO(сырьё)
            код, ответ = contextd.ingest_audio(con, каталог, eid, поток, len(сырьё))
        finally:
            contextd.Никуда = было
        self.assertTrue(ответ.get("duplicate"), ответ)
        # тело обязано быть вычитано целиком: хвост иначе уедет в следующий
        # запрос на том же соединении
        self.assertEqual(поток.tell(), len(сырьё))
        self.assertEqual(Считающий.накопил, len(сырьё), "сток не тот, что заявлен")
        self.assertEqual(contextd.Никуда().write(b"12345"), 5)

    def test_второй_forwarded_for_не_подменяет_клиента(self):
        """При двух заголовках берём последний — дописанный прокси, не клиентом."""
        class Заголовки:
            def __init__(self, *значения):
                self.значения = list(значения)

            def get_all(self, k, d=None):
                return self.значения or d

            def get(self, k, d=None):
                return self.значения[0] if self.значения else d

        class Фейк:
            client_address = ("203.0.113.7", 1234)
            headers = Заголовки("198.51.100.9", "198.51.100.4")

        прежние = contextd.TRUSTED_PROXY
        contextd.TRUSTED_PROXY = ("203.0.113.7",)
        try:
            self.assertEqual(contextd.клиент(Фейк()), "198.51.100.4")
        finally:
            contextd.TRUSTED_PROXY = прежние

    def test_401_с_телом_закрывает_соединение(self):
        """Иначе хвост тела уедет в следующий запрос и сервер ответит 400."""
        import http.client
        каталог, srv, con, токен = self.поднять()
        contextd._неудачи.clear()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.request("POST", "/v1/ingest/event", body=b'{"kind": "call"}',
                      headers={"Authorization": "Bearer nope",
                               "Content-Type": "application/json"})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 401)
            self.assertEqual(r.getheader("Connection"), "close")
            c.close()
        finally:
            contextd._неудачи.clear()
            srv.shutdown()
            srv.server_close()


class ТестРазмерТела(unittest.TestCase):
    """JSON и аудио меряются разными линейками (ТЗ §6.1)."""

    def test_json_больше_мегабайта_413(self):
        dir = tempfile.mkdtemp()
        mi.ROOT = dir
        srv = contextd.make_server(dir, port=0, vault=tempfile.mkdtemp())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        con = mi.connect(dir)
        _, токен = contextd.pair(con, "телефон")
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            тело = json.dumps({"kind": "call", "note": "я" * (contextd.MAX_JSON)})
            req = urllib.request.Request(base + "/v1/ingest/event",
                                         data=тело.encode("utf-8"), method="POST")
            req.add_header("Authorization", "Bearer " + токен)
            req.add_header("Content-Type", "application/json")
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(e.exception.code, 413)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_аудио_льётся_кусками_а_не_в_память(self):
        """Файл больше одного КУСКА проходит целиком и с верным хешем."""
        dir = tempfile.mkdtemp()
        mi.ROOT = dir
        con = mi.connect(dir)
        сырьё = os.urandom(contextd.КУСОК + 12345)
        sha = hashlib.sha256(сырьё).hexdigest()
        eid, _ = mi.put_event(con, {"kind": "call", "source": "phone",
                                    "source_id": "поток",
                                    "blob": {"sha256": sha, "bytes": len(сырьё),
                                             "ext": "m4a"}})
        код, ответ = contextd.ingest_audio(con, dir, eid, io.BytesIO(сырьё), len(сырьё))
        self.assertEqual((код, ответ["bytes"]), (200, len(сырьё)))
        self.assertEqual(hashlib.sha256(
            open(mi.blob_path(dir, sha, "m4a"), "rb").read()).hexdigest(), sha)

class ТестГонкаFinishStored(unittest.TestCase):
    """Два запроса по одному событию заводят одну работу ASR, а не две.

    Вторая работа стоит часа GPU и второго дайджеста в телеграм: `call_digest`
    шлёт сообщение до записи в базу, так что дубль виден владельцу.
    """

    def test_обгон_на_переводе_в_stored_не_плодит_работу(self):
        каталог = tempfile.mkdtemp()
        mi.ROOT = каталог
        con = mi.connect(каталог)
        сха = hashlib.sha256(b"a").hexdigest()
        eid, _ = mi.put_event(con, {"kind": "call", "source": "phone",
                                    "source_id": "гонка",
                                    "blob": {"sha256": сха, "bytes": 1, "ext": "m4a"}})
        второй = mi.connect(каталог)

        class Опережающий:
            """Соединение, пускающее конкурента вперёд прямо перед update."""

            def __init__(self, con):
                self.con, self.сработал = con, False

            def execute(self, sql, args=()):
                if "state='stored'" in sql and not self.сработал:
                    self.сработал = True
                    contextd.finish_stored(второй, каталог, eid)
                return self.con.execute(sql, args)

            def __getattr__(self, name):
                return getattr(self.con, name)

        обгон = Опережающий(con)
        contextd.finish_stored(обгон, каталог, eid)
        self.assertTrue(обгон.сработал, "конкурент обязан был вклиниться")
        работ = con.execute("select count(*) from jobs where event_id=? and kind='asr'",
                            (eid,)).fetchone()[0]
        self.assertEqual(работ, 1, "гонка завела вторую работу ASR")


class ТестКривойДлины(unittest.TestCase):
    """Круг 2 ревью PR #14: длина, которую нельзя разобрать, и мусор на диске."""

    def поднять(self):
        каталог = tempfile.mkdtemp()
        mi.ROOT = каталог
        srv = contextd.make_server(каталог, port=0, vault=tempfile.mkdtemp())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        con = mi.connect(каталог)
        _, токен = contextd.pair(con, "телефон")
        return каталог, srv, con, токен

    def запрос(self, srv, путь, заголовки, тело=b"{}", токен=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.putrequest("POST", путь, skip_accept_encoding=True)
        if токен:
            c.putheader("Authorization", "Bearer " + токен)
        for k, v in заголовки.items():
            c.putheader(k, v)
        c.endheaders()
        c.send(тело)
        r = c.getresponse()
        r.read()
        код = r.status
        c.close()
        return код

    def test_нечисловая_длина_это_400_а_не_traceback(self):
        """int() на заголовке падал ValueError: ответа нет, соединение рвётся.

        Худший путь — без токена: `отлуп` зовётся из `authed`, то есть дыра
        была доступна анониму.
        """
        каталог, srv, con, токен = self.поднять()
        try:
            for кривая in ("abc", "1e3", "0x10", "5 5", "+5", "\u00b2"):
                self.assertEqual(
                    self.запрос(srv, "/v1/ingest/event",
                                {"Content-Length": кривая}, b"", токен), 400,
                    "длина %r должна давать 400" % кривая)
                self.assertEqual(
                    self.запрос(srv, "/v1/ingest/event",
                                {"Content-Length": кривая}, b""), 401,
                    "без токена длина %r роняла обработчик" % кривая)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_длинное_число_в_длине_не_роняет_обработчик(self):
        """Круг 3. `int()` от строки длиннее 4300 цифр кидает ValueError, а
        `isdigit` её пропускал. Заголовок влезает в 64 КиБ, `отлуп` зовёт
        `длина` до проверки токена — то есть анониму хватало одного запроса,
        чтобы получить трейсбек вместо ответа."""
        каталог, srv, con, токен = self.поднять()
        try:
            for цифр in (20, 4301, 5000):
                длинная = "1" * цифр
                self.assertEqual(
                    self.запрос(srv, "/v1/ingest/event",
                                {"Content-Length": длинная}, b"", токен), 400,
                    "длина из %d цифр должна давать 400" % цифр)
                self.assertEqual(
                    self.запрос(srv, "/v1/ingest/event",
                                {"Content-Length": длинная}, b""), 401,
                    "без токена длина из %d цифр роняла обработчик" % цифр)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_две_длины_и_chunked_рядом_с_длиной_это_400(self):
        """Два разных Content-Length и `Transfer-Encoding` вместе с длиной:
        границу тела в обоих случаях выбирает не сервер, а отправитель, и
        остаток тела уезжает в следующий запрос по тому же соединению."""
        каталог, srv, con, токен = self.поднять()
        try:
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                           timeout=10)
            c.putrequest("POST", "/v1/ingest/event", skip_accept_encoding=True)
            c.putheader("Authorization", "Bearer " + токен)
            c.putheader("Content-Length", "2")
            c.putheader("Content-Length", "9")
            c.endheaders()
            c.send(b"{}")
            self.assertEqual(c.getresponse().status, 400)
            c.close()
            self.assertEqual(
                self.запрос(srv, "/v1/ingest/event",
                            {"Content-Length": "2", "Transfer-Encoding": "chunked"},
                            b"{}", токен), 400)
            self.assertEqual(con.execute("select count(*) from events").fetchone()[0], 0)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_json_не_объект_это_400_а_не_traceback(self):
        """`5` и `[]` — валидный json, но дальше по коду сплошь data.get."""
        каталог, srv, con, токен = self.поднять()
        try:
            for тело in (b"5", b"[]", b'"text"'):
                self.assertEqual(
                    self.запрос(srv, "/v1/ingest/event",
                                {"Content-Length": str(len(тело))}, тело, токен),
                    400, "тело %r роняло обработчик" % тело)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_chunked_без_длины_не_создаёт_пустое_событие(self):
        """n=0 при chunked означал «пустое тело»: событие заводилось, а тело
        оставалось в сокете и ломало следующий запрос."""
        каталог, srv, con, токен = self.поднять()
        try:
            self.assertEqual(
                self.запрос(srv, "/v1/ingest/event",
                            {"Transfer-Encoding": "chunked"}, b"0\r\n\r\n", токен),
                400)
            self.assertEqual(con.execute("select count(*) from events").fetchone()[0], 0)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_обрыв_заливки_не_оставляет_part(self):
        """Потоковая заливка открыла файл до чтения тела; уборщика .part нет."""
        каталог = tempfile.mkdtemp()
        mi.ROOT = каталог
        con = mi.connect(каталог)
        сырьё = os.urandom(contextd.КУСОК + 100)
        sha = hashlib.sha256(сырьё).hexdigest()
        eid, _ = mi.put_event(con, {"kind": "call", "source": "phone",
                                    "source_id": "обрыв",
                                    "blob": {"sha256": sha, "bytes": len(сырьё),
                                             "ext": "m4a"}})

        class Рвётся(io.BytesIO):
            def read(self, n=-1):
                if self.tell() >= contextd.КУСОК:
                    raise ConnectionResetError("клиент отвалился")
                return super().read(n)

        with self.assertRaises(ConnectionResetError):
            contextd.ingest_audio(con, каталог, eid, Рвётся(сырьё), len(сырьё))
        каталог_блоба = os.path.dirname(mi.blob_path(каталог, sha, "m4a"))
        мусор = [f for f in os.listdir(каталог_блоба) if f.endswith(".part")]
        self.assertEqual(мусор, [], "обрыв оставил недописанный файл")

    def test_несошедшийся_хеш_тоже_не_оставляет_part(self):
        каталог = tempfile.mkdtemp()
        mi.ROOT = каталог
        con = mi.connect(каталог)
        sha = hashlib.sha256(b"wanted").hexdigest()
        eid, _ = mi.put_event(con, {"kind": "call", "source": "phone",
                                    "source_id": "не-сошёлся",
                                    "blob": {"sha256": sha, "bytes": 5, "ext": "m4a"}})
        код, _ = contextd.ingest_audio(con, каталог, eid, io.BytesIO(b"other"), 5)
        self.assertEqual(код, 409)
        каталог_блоба = os.path.dirname(mi.blob_path(каталог, sha, "m4a"))
        self.assertEqual([f for f in os.listdir(каталог_блоба) if f.endswith(".part")],
                         [])


class ТестScopes(unittest.TestCase):
    """Allowlist видов у устройства (ADR-0009, откат п. 2)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        mi.ROOT = self.dir
        self.srv = contextd.make_server(self.dir, port=0, vault=tempfile.mkdtemp())
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.con = mi.connect(self.dir)
        self.dev, self.token = contextd.pair(self.con, "телефон")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def послать(self, kind, source_id):
        body = json.dumps({"kind": kind, "source": "phone",
                           "source_id": source_id}).encode("utf-8")
        req = urllib.request.Request(self.base + "/v1/ingest/event", data=body,
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.read()
            return e.code

    def звонок(self, source_id, тело, token=None, **ещё):
        """Событие звонка с обещанным блобом. Возвращает id события."""
        sha = hashlib.sha256(тело).hexdigest()
        ev = {"kind": "call", "source": "phone", "source_id": source_id,
              "blob": {"sha256": sha, "bytes": len(тело), "ext": "wav"}}
        ev.update(ещё)                       # `blob=` подменяется целиком
        body = json.dumps(ev).encode("utf-8")
        req = urllib.request.Request(self.base + "/v1/ingest/event", data=body,
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + (token or self.token))
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())["event_id"]

    def залить(self, eid, тело, token=None):
        """Дозагрузка аудио. Возвращает (код, значение заголовка Connection)."""
        url = self.base + "/v1/ingest/audio?event=" + eid
        req = urllib.request.Request(url, data=тело, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Authorization", "Bearer " + (token or self.token))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
                return r.status, r.headers.get("Connection")
        except urllib.error.HTTPError as e:
            e.read()
            return e.code, e.headers.get("Connection")

    def test_колонка_добавляется_к_старой_базе(self):
        """Существующая база без колонки доезжает сама, данные целы."""
        каталог = tempfile.mkdtemp()
        путь = os.path.join(каталог, "contextd.db")
        import sqlite3
        con = sqlite3.connect(путь)
        con.execute("create table devices(id text primary key, name text, "
                    "token_sha256 text not null, created text, last_seen text, "
                    "revoked_at text)")
        con.execute("insert into devices(id,name,token_sha256) values('d1','старое','x')")
        con.commit(); con.close()
        con = mi.connect(каталог)
        колонки = {r["name"] for r in con.execute("pragma table_info(devices)")}
        self.assertIn("scopes", колонки)
        row = con.execute("select name, scopes from devices where id='d1'").fetchone()
        self.assertEqual(row["name"], "старое")
        self.assertIsNone(row["scopes"], "миграция не должна ничего разрешать сама")

    def test_null_пускает_всё(self):
        self.assertEqual(self.послать("call", "s1"), 200)
        self.assertEqual(self.послать("message", "s2"), 200)

    def test_список_пускает_своё_и_отбивает_чужое(self):
        contextd.set_scopes(self.con, self.dev, ["call"])
        self.assertEqual(self.послать("call", "s3"), 200)
        self.assertEqual(self.послать("message", "s4"), 403)

    def test_отбитое_событие_в_базу_не_попало(self):
        contextd.set_scopes(self.con, self.dev, ["call"])
        self.послать("message", "s5")
        n = self.con.execute("select count(*) c from events "
                             "where source_id='s5'").fetchone()["c"]
        self.assertEqual(n, 0, "403 всё равно записал событие")

    def test_пустой_список_снимает_ограничение(self):
        contextd.set_scopes(self.con, self.dev, ["call"])
        self.assertEqual(self.послать("message", "s6"), 403)
        found, val = contextd.set_scopes(self.con, self.dev, [])
        self.assertTrue(found)
        self.assertIsNone(val)
        self.assertEqual(self.послать("message", "s7"), 200)

    def test_история_даёт_то_что_устройство_реально_слало(self):
        self.assertEqual(self.послать("call", "s8"), 200)
        self.assertEqual(self.послать("message", "s9"), 200)
        self.assertEqual(contextd.scopes_from_history(self.con, self.dev),
                         ["call", "message"])

    def test_гонка_миграции_не_валит_открытие(self):
        """Колонку добавил сосед между `pragma` и `alter` — это не ошибка.

        Гонку тут не ждут случайно, а устраивают: подменённый `sqlite3.connect`
        добавляет колонку ровно в тот момент, когда наш `pragma table_info` уже
        отработал и сказал «колонки нет». Без `except OperationalError` в
        `mi.connect` этот тест падает с `duplicate column name`.
        """
        import sqlite3
        каталог = tempfile.mkdtemp()
        путь = os.path.join(каталог, "contextd.db")
        con = sqlite3.connect(путь)
        con.execute("create table devices(id text primary key, name text, "
                    "token_sha256 text not null, created text, last_seen text, "
                    "revoked_at text)")
        con.commit(); con.close()

        настоящий = sqlite3.connect

        class Соседский(sqlite3.Connection):
            """Соединение, за спиной которого колонку добавляет кто-то другой."""
            подставил = False

            def execute(self, sql, *a):
                r = super().execute(sql, *a)
                if not Соседский.подставил and "table_info(devices)" in sql:
                    Соседский.подставил = True
                    сосед = настоящий(путь)
                    сосед.execute("alter table devices add column scopes text")
                    сосед.commit(); сосед.close()
                return r

        sqlite3.connect = lambda *a, **k: настоящий(*a, factory=Соседский,
                                                    **{k_: v for k_, v in k.items()
                                                       if k_ != "factory"})
        try:
            mi.connect(каталог).close()
        finally:
            sqlite3.connect = настоящий
        self.assertTrue(Соседский.подставил,
                        "гонка не состоялась, тест ничего не проверил")
        con = sqlite3.connect(путь)
        имена = {r[1] for r in con.execute("pragma table_info(devices)")}
        con.close()
        self.assertIn("scopes", имена)

    def test_история_пуста_у_нового_устройства(self):
        dev2, _ = contextd.pair(self.con, "ещё не звонил")
        self.assertEqual(contextd.scopes_from_history(self.con, dev2), [],
                         "устройству без событий список выдавать нечем")

    def test_молчание_не_снимает_уже_заданный_список(self):
        """`@history` у замолчавшего устройства — не команда «открыть всё»."""
        contextd.set_scopes(self.con, self.dev, ["call"])
        буфер = io.StringIO()
        было, sys.stdout = sys.stdout, буфер
        try:
            код = contextd.main(["--root", self.dir, "--allow", self.dev, "@history"])
        finally:
            sys.stdout = было
        self.assertEqual(код, 0)
        self.assertIn("не трогаю", буфер.getvalue())
        row = self.con.execute("select scopes from devices where id=?",
                               (self.dev,)).fetchone()
        self.assertEqual(row["scopes"], "call", "список пережил пустую историю")

    def test_чужое_устройство_не_находится(self):
        found, _ = contextd.set_scopes(self.con, "dev_нет", ["call"])
        self.assertFalse(found)

    def запуск(self, *argv):
        """`main()` целиком, с перехватом вывода: как из командной строки."""
        буфер = io.StringIO()
        было, sys.stdout = sys.stdout, буфер
        try:
            код = contextd.main(["--root", self.dir] + list(argv))
        finally:
            sys.stdout = было
        return код, буфер.getvalue()

    def scopes(self):
        return self.con.execute("select scopes from devices where id=?",
                                (self.dev,)).fetchone()["scopes"]

    def test_allow_из_командной_строки_пишет_список(self):
        код, вывод = self.запуск("--allow", self.dev, "message", "call")
        self.assertEqual(код, 0)
        self.assertEqual(self.scopes(), "call message", "список не отсортирован")
        self.assertIn("call message", вывод)

    def test_allow_history_заполняет_по_фактам(self):
        """С непустой историей `@history` пишет ровно то, что устройство слало."""
        for kind in ("call", "message", "call"):
            mi.put_event(self.con, {"kind": kind, "source": "т", "device_id": self.dev,
                                    "source_id": "%s-%d" % (kind, id(kind))})
        код, _ = self.запуск("--allow", self.dev, "@history")
        self.assertEqual(код, 0)
        self.assertEqual(self.scopes(), "call message")

    def test_пустой_вид_не_снимает_ограничение_молча(self):
        """`--allow ID ""` — не способ открыть всё; для этого есть `--allow ID`."""
        contextd.set_scopes(self.con, self.dev, ["call"])
        код, вывод = self.запуск("--allow", self.dev, "")
        self.assertEqual(код, 1)
        self.assertIn("пустой вид", вывод)
        self.assertEqual(self.scopes(), "call", "список уцелел")

    def test_вид_с_пробелом_не_разваливается_на_два(self):
        код, вывод = self.запуск("--allow", self.dev, "call correction")
        self.assertEqual(код, 1)
        self.assertIn("пробел", вывод)
        self.assertIsNone(self.scopes())

    def test_пробел_это_не_только_пробел(self):
        """`split()` считает пробелом и табуляцию, и NBSP — проверка тоже."""
        for вид in ("call\tcorrection", "call\ncorrection", "call\xa0correction"):
            with self.subTest(вид=repr(вид)):
                код, _ = self.запуск("--allow", self.dev, вид)
                self.assertEqual(код, 1, "вид %r принят" % вид)
                self.assertIsNone(self.scopes())

    def test_снять_ограничение_можно_только_явно(self):
        contextd.set_scopes(self.con, self.dev, ["call"])
        код, _ = self.запуск("--allow", self.dev)
        self.assertEqual(код, 0)
        self.assertIsNone(self.scopes(), "`--allow ID` без видов снимает список")

    def test_дозагрузка_аудио_тоже_под_allowlist(self):
        """ADR-0009 ждёт отлуп на первом `audio` у устройства без разрешения."""
        тело = "как бы аудио".encode("utf-8")
        sha = hashlib.sha256(тело).hexdigest()
        eid = self.звонок("a1", тело)
        contextd.set_scopes(self.con, self.dev, ["message"])
        код, conn = self.залить(eid, тело)
        self.assertEqual(код, 403, "аудио принято при allowlist без `call`")
        # Именно `отлуп`, а не `say`: тело здесь ещё не читали, и сервер
        # обязан его слить и закрыть соединение. С `say` клиент слал бы в
        # закрытое, а маленькое тело успело бы уехать и скрыло бы разницу.
        self.assertEqual(conn, "close", "403 отдан не через `отлуп`")
        self.assertFalse(os.path.exists(mi.blob_path(self.dir, sha, "wav")),
                         "блоб лёг на диск в обход allowlist")

    def test_своё_событие_дозагружается(self):
        """Обратная сторона: разрешённый вид дозагрузке не мешает."""
        тело = "свой звонок".encode("utf-8")
        eid = self.звонок("a3", тело)
        код, _ = self.залить(eid, тело)
        self.assertEqual(код, 200)

    def test_перепаренный_телефон_дочищает_очередь(self):
        """#26. Событие адресуется содержимым, а не устройством.

        Телефон переставили, токен потеряли, владелец сделал `--pair` заново —
        `device_id` стал другим. Недоотправленные звонки телефон шлёт снова, и
        дедуп по `blob:<sha>` возвращает **старое** событие со старым
        владельцем. Если бы дозагрузка сверяла устройство, эти звонки не
        доехали бы никогда: `Core.kt:137` считает 403 терминальным, и
        `retryFailed()` упирался бы в тот же 403.
        """
        тело = "звонок из прошлой жизни".encode("utf-8")
        sha = hashlib.sha256(тело).hexdigest()
        старый = self.звонок("a4", тело)
        новый_dev, новый = contextd.pair(self.con, "телефон переставили")
        self.assertNotEqual(новый_dev, self.dev)
        # source_id другой намеренно: совпадает только содержимое, и если
        # событие вернулось то же — дедуп идёт по блобу, а не по источнику
        self.assertEqual(self.звонок("a4-заново", тело, token=новый), старый)
        код, _ = self.залить(старый, тело, token=новый)
        self.assertEqual(код, 200, "перепаренный телефон не смог долить звонок")
        self.assertTrue(os.path.exists(mi.blob_path(self.dir, sha, "wav")))
        строка = self.con.execute("select state, device_id from events "
                                  "where id=?", (старый,)).fetchone()
        self.assertEqual(строка["state"], "stored", "звонок не доведён")
        # владение не передаётся: событие так и числится за старым устройством.
        # Это осознанно — передача владения при дедупе завела бы воровство
        # ждущих событий. Кривая атрибуция после перепаривания — отдельно.
        self.assertEqual(строка["device_id"], self.dev)

    def отправить(self, путь, событие, token=None):
        """POST события целиком своими руками. Возвращает (код, тело)."""
        req = urllib.request.Request(
            self.base + путь, data=json.dumps(событие).encode("utf-8"),
            method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + (token or self.token))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_блоб_бывает_только_у_звонка(self):
        """Иначе чужой вид забирает будущий звонок себе.

        Ключ дедупа у события с блобом — сам блоб, один на всю базу (делить его
        по виду нельзя: два события на один блоб — две цепочки ASR). Значит
        `message` с чужим sha занимал строку первым, настоящий звонок
        дедуплицировался в неё, и дозагрузка проверяла сохранённый `message`
        против телефонного списка — 403, для клиента терминальный.
        """
        тело = "звонок, на который позарились".encode("utf-8")
        sha = hashlib.sha256(тело).hexdigest()
        _, чужой = contextd.pair(self.con, "ноутбук")
        код, ответ = self.отправить(
            "/v1/ingest/message",
            {"source": "x", "source_id": "z",
             "blob": {"sha256": sha, "bytes": len(тело), "ext": "wav"}},
            token=чужой)
        self.assertEqual(код, 400, "сообщение с блобом принято")
        self.assertIn("только у звонка", ответ["error"])
        contextd.set_scopes(self.con, self.dev, ["call"])
        eid = self.звонок("a6", тело)
        код, _ = self.залить(eid, тело)
        self.assertEqual(код, 200, "звонок не доехал")

    def test_кривой_хеш_и_расширение_не_уводят_запись_из_дерева(self):
        """`sha256` и `ext` приходят из тела и попадают в путь файла."""
        тело = "аудио".encode("utf-8")
        sha = hashlib.sha256(тело).hexdigest()
        код, ответ = self.отправить("/v1/ingest/event", {
            "kind": "call", "source": "phone", "source_id": "b1",
            "blob": {"sha256": "../../../etc/passwd", "bytes": 5}})
        self.assertEqual(код, 400, "хеш не проверен")
        self.assertIn("sha256", ответ["error"])
        # расширение чистится там, где становится путём, — в `blob_path`
        год = os.path.join(self.dir, "calls", "%04d" % datetime.now(mi.TZ).year)
        for ext, ждём in (("../../etc/x", "bin"), (7, "7"), (".wav", "wav"),
                          (None, "bin"), ("w" * 99, "bin"), ("жwav", "bin"),
                          ({}, "bin"), (["wav"], "bin"), ("WAV", "wav")):
            путь = mi.blob_path(self.dir, sha, ext)
            self.assertEqual(os.path.dirname(os.path.dirname(путь)), год,
                             "ext %r увёл запись из дерева" % (ext,))
            self.assertEqual(путь.rsplit(".", 1)[1], ждём,
                             "ext %r дал не то расширение" % (ext,))
        # не-объект в `blob` роняло обработчик до ответа
        код, _ = self.отправить("/v1/ingest/event", {
            "kind": "call", "source": "phone", "source_id": "b3", "blob": 7})
        self.assertEqual(код, 400)
        eid = self.звонок("b2", тело, blob={"sha256": sha, "bytes": len(тело),
                                            "ext": "../../etc/x"})
        код, _ = self.залить(eid, тело)
        self.assertEqual(код, 200)
        путь = self.con.execute("select path from blobs where sha256=?",
                                (sha,)).fetchone()["path"]
        self.assertTrue(путь.startswith(os.path.join(self.dir, "calls")), путь)

    def test_присланный_dedupe_key_не_принимается(self):
        """Ключ дедупа считает сервер: иначе чужое событие можно похоронить.

        Устройство, узнавшее sha ждущего звонка, объявляло ключ `blob:<sha>`
        без самого блоба. Настоящий звонок дедуплицировался в эту строку, и
        дозагрузка отвечала «событие без аудио» (400) — для клиента это
        терминально (`Core.kt`), звонок пропадал молча.
        """
        тело = "звонок, который хотели похоронить".encode("utf-8")
        sha = hashlib.sha256(тело).hexdigest()
        _, чужой = contextd.pair(self.con, "ноутбук")
        req = urllib.request.Request(
            self.base + "/v1/ingest/event",
            data=json.dumps({"kind": "call", "source": "x", "source_id": "z",
                             "dedupe_key": "blob:" + sha}).encode("utf-8"),
            method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + чужой)
        with urllib.request.urlopen(req, timeout=10) as r:
            захват = json.loads(r.read())["event_id"]
        ключ = self.con.execute("select dedupe_key from events where id=?",
                                (захват,)).fetchone()["dedupe_key"]
        self.assertTrue(ключ.startswith("src:"),
                        "присланный ключ принят: %s" % ключ)
        eid = self.звонок("a5", тело)
        self.assertNotEqual(eid, захват, "настоящий звонок съеден захватом")
        код, _ = self.залить(eid, тело)
        self.assertEqual(код, 200)

    def test_неизвестное_устройство_не_пускается(self):
        """Отзыв между `authed()` и проверкой не должен открывать дверь."""
        self.assertFalse(contextd.scope_ok(self.con, "dev_нет", "call"))
        self.assertFalse(contextd.scope_ok(self.con, None, "call"))


if __name__ == "__main__":
    unittest.main()
