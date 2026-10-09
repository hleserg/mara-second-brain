"""Регрессии на коллизии (ТЗ §17.1, Т2.7).

Три сценария, которые §17.1 называет поимённо, — одним файлом, чтобы чекбокс
держался на тестах с этими именами, а не на разборе соседних модулей:
два одинаково названных обязательства, два звонка в одну минуту, повтор
после потерянного ответа. Механизмы — Т2.2 (различитель пути из id,
`_свободный`), Т2.3 (код карточки в правке) и Т2.9 (квитанция).
"""
import io, os, sys, json, uuid, hashlib, tempfile, threading, unittest, unittest.mock
import urllib.request, urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import call_project as cp
import contextd

ИЗВЛЕЧЕНИЕ = {"requests": [{"action": "прислать смету", "requester": "Анна",
                            "owner": "sergey", "explicit": True, "confidence": 0.9,
                            "due_at": "2026-09-04", "deadline_explicit": True,
                            "disposition": "task",
                            "evidence": [{"start_ms": 1000, "end_ms": 2000}]}],
              "commitments": [], "decisions": [], "constraints": [], "open_questions": [],
              "changed_instructions": [], "followups": [],
              "people_mentioned": ["Анна"], "projects_mentioned": []}


def _текст(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _id(текст):
    return [l for l in текст.split("---", 2)[1].splitlines() if l.startswith("id: ")][0][4:]


class Коллизии(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.root, self.vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(self.root)
        os.makedirs(os.path.join(self.vault, ".git"))
        self.con = mi.connect(self.root)
        self.addCleanup(self.con.close)

    def звонок(self, sid, occurred, contact="Анна"):
        eid, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": sid,
            "occurred_at": occurred, "ended_at": occurred,
            "payload": {"contact_name": contact, "direction": "incoming"}})
        mi.write_json(mi.extraction_path(self.root, eid), ИЗВЛЕЧЕНИЕ)
        return eid

    def test_два_одинаково_названных_обязательства_из_разных_звонков(self):
        """Два звонка в один день, в каждом — «прислать смету»: путь из даты и
        slug один и тот же. До Т2.2 второй файл молча ложился на первый."""
        a = self.звонок("a", "2026-09-02T10:00:00+03:00")
        b = self.звонок("b", "2026-09-02T16:30:00+03:00", contact="Борис")
        первые = [r for r in cp.run(a, self.vault, self.root) if r.startswith("kb/commitments/")]
        вторые = [r for r in cp.run(b, self.vault, self.root) if r.startswith("kb/commitments/")]
        self.assertEqual(len(первые), 1)
        self.assertEqual(len(вторые), 1)
        self.assertNotEqual(первые, вторые, "второе обязательство легло на первое")
        self.assertTrue(вторые[0].endswith("--%s.md" % _id(_текст(
            os.path.join(self.vault, вторые[0])))[-8:]), вторые)
        self.assertEqual(self.con.execute("select count(*) from commitments").fetchone()[0], 2)
        # правка словами — «подходят несколько»; по коду — точно в своё
        ид_b = _id(_текст(os.path.join(self.vault, вторые[0])))
        событие = {"id": "c1", "occurred_at": "2026-09-02T18:00:00+03:00",
                   "payload": {"item": "прислать смету", "status": "done"}}
        out = cp.apply_correction(self.vault, событие, self.con)
        self.assertIn("ambiguous", out)
        self.assertEqual(sorted(out["ambiguous_ids"]), sorted(
            r[0] for r in self.con.execute("select id from commitments")))
        out = cp.apply_correction(self.vault, dict(событие, id="c2", payload=dict(
            событие["payload"], id="#" + ид_b[-8:])), self.con)
        self.assertEqual(out["id"], ид_b)
        статусы = dict(self.con.execute("select id, status from commitments"))
        self.assertEqual(статусы[ид_b], "done")
        self.assertEqual([s for i, s in статусы.items() if i != ид_b], ["proposed"],
                         "правка по коду тронула соседа")

    def test_два_звонка_в_одну_минуту_одному_контакту(self):
        """R12 §16: та же минута, тот же контакт — два разговора, два набора
        обязательств, ни одной перезаписи, повтор проекции — те же пути."""
        a = self.звонок("a", "2026-09-02T14:05:00+03:00")
        b = self.звонок("b", "2026-09-02T14:05:40+03:00")
        первые, вторые = cp.run(a, self.vault, self.root), cp.run(b, self.vault, self.root)
        self.assertFalse(set(первые) & set(вторые), "второй звонок лёг на файлы первого")
        self.assertEqual(cp.run(b, self.vault, self.root), вторые)
        self.assertEqual(self.con.execute("select count(*) from conversations").fetchone()[0], 2)
        self.assertEqual(self.con.execute("select count(*) from projections").fetchone()[0], 4)
        for rel in первые + вторые:
            self.assertTrue(os.path.exists(os.path.join(self.vault, rel)), rel)


class ПовторПослеПотерянногоОтвета(unittest.TestCase):
    """Телефон не получил ответа и шлёт то же событие ещё раз: один объект,
    одна работа, и с ключом идемпотентности — тот же самый ответ."""

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

    def post(self, path, тело):
        сырое = isinstance(тело, bytes)
        req = urllib.request.Request(self.base + path, method="POST",
                                     data=тело if сырое else json.dumps(тело).encode("utf-8"))
        req.add_header("Content-Type", "application/octet-stream" if сырое
                       else "application/json")
        req.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)

    def test_повтор_события_и_заливки_не_плодит_ни_объектов_ни_работ(self):
        тело = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"a" * 40
        sha = hashlib.sha256(тело).hexdigest()
        ev = {"kind": "call", "source": "phone", "source_id": sha,
              "blob": {"sha256": sha, "bytes": len(тело), "ext": "wav"},
              "idempotency_key": "lost-1"}
        _, первый, _ = self.post("/v1/ingest/event", ev)
        _, повтор, заголовки = self.post("/v1/ingest/event", ev)
        self.assertEqual(повтор, первый, "ответ после потерянного — тот же")
        self.assertEqual(заголовки.get("Idempotent-Replay"), "true")
        eid = первый["event_id"]
        код, _, _ = self.post("/v1/ingest/audio?event=" + eid, тело)
        self.assertEqual(код, 200)
        код, ответ, _ = self.post("/v1/ingest/audio?event=" + eid, тело)
        self.assertEqual((код, ответ.get("duplicate")), (200, True), "повтор заливки — дубль")
        # без ключа — событие то же, работа одна
        _, без_ключа, _ = self.post("/v1/ingest/event", {k: v for k, v in ev.items()
                                                       if k != "idempotency_key"})
        self.assertEqual((без_ключа["event_id"], без_ключа["duplicate"]), (eid, True))
        self.assertEqual(self.con.execute("select count(*) from events").fetchone()[0], 1)
        self.assertEqual(self.con.execute(
            "select count(*) from jobs where kind='asr'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
