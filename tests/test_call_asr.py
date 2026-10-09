"""Нарезка и склейка транскрипта (ТЗ §8)."""
import os, sys, json, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_asr
import mara_ingest as mi


class Нарезка(unittest.TestCase):
    def test_короткий_звонок_один_кусок(self):
        self.assertEqual(call_asr.slice_plan(10000), [(0, 10000)])

    def test_длинный_режется_с_перекрытием(self):
        p = call_asr.slice_plan(60000)
        self.assertEqual(p[0], (0, 25000))
        self.assertEqual(p[1][0], 23000, "перекрытие две секунды")
        self.assertEqual(p[-1][1], 60000, "хвост не теряется")

    def test_куски_не_длиннее_потолка_сервера(self):
        for a, b in call_asr.slice_plan(600000):
            self.assertLessEqual(b - a, 25000, "сервер отвечает 413 на кусок длиннее 30 с")

    def test_нулевая_длительность_не_ломает(self):
        self.assertEqual(call_asr.slice_plan(0), [])


class Склейка(unittest.TestCase):
    def setUp(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                outer.calls += 1
                body = json.dumps(dict(getattr(outer, "ответ", {}),
                                       text="кусок %d" % outer.calls,
                                       sec=25)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.calls = 0
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()

    def test_движок_берётся_из_ответа_коробки(self):
        движок = {}
        call_asr.transcribe_spans(self.base, [(0, 1000)], lambda a, b: b"wav", движок)
        self.assertEqual(движок, {}, "коробка про движок молчит — не догадываемся")
        self.ответ = {"engine": "whisper.cpp", "model": "large-v3", "language": "ru"}
        движок = {}
        call_asr.transcribe_spans(self.base, [(0, 1000), (1000, 2000)],
                                  lambda a, b: b"wav", движок)
        self.assertEqual(движок, self.ответ, "сказала — записали, ровно как сказала")

    def test_сегменты_получают_спаны_в_координатах_записи(self):
        segs = call_asr.transcribe_spans(self.base, [(0, 25000), (23000, 48000)],
                                         lambda a, b: b"wav")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["start_ms"], 0)
        self.assertEqual(segs[1]["start_ms"], 23000)
        self.assertEqual(segs[1]["segment_id"], "s0002")

    def test_говорящий_не_выдумывается(self):
        segs = call_asr.transcribe_spans(self.base, [(0, 1000)], lambda a, b: b"wav")
        self.assertEqual(segs[0]["speaker"], "unknown-A",
                         "диаризации нет — говорящего не придумываем")
        self.assertIsNone(segs[0]["asr_confidence"])

    def test_пустой_кусок_не_создаёт_сегмент(self):
        class Пусто(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = b'{"text": "  "}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Пусто)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            segs = call_asr.transcribe_spans("http://127.0.0.1:%d" % srv.server_address[1],
                                             [(0, 1000)], lambda a, b: b"wav")
            self.assertEqual(segs, [], "тишина сегментом не становится")
        finally:
            srv.shutdown()


class Реестр(unittest.TestCase):
    """Т5.1, ADR-0004 п.1: расшифровка и сегменты — строки реестра; каждый
    прогон — новая расшифровка, старые строки остаются."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.eid, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "d1",
            "occurred_at": "2026-09-02T14:05:00+03:00", "payload": {}})
        self.segs = [{"segment_id": "s0001", "start_ms": 0, "end_ms": 25000,
                      "speaker": "unknown-A", "text": "раз"},
                     {"segment_id": "s0003", "start_ms": 46000, "end_ms": 60000,
                      "speaker": "unknown-A", "text": "три"}]

    def test_сегменты_ложатся_с_номером_и_границами(self):
        tid = call_asr.записать_сегменты(self.con, self.eid, "ab" * 32, self.segs,
                                         {"engine": "whisper.cpp", "model": "large-v3"})
        t = self.con.execute("select * from transcripts where id=?", (tid,)).fetchone()
        self.assertEqual((t["event_id"], t["engine"], t["model"], t["blob_sha256"]),
                         (self.eid, "whisper.cpp", "large-v3", "ab" * 32))
        тот_же, сег = call_asr.сегменты_события(self.con, self.eid)
        self.assertEqual(тот_же, tid)
        self.assertEqual(sorted(сег), [1, 3], "seq — номер из метки, пропуски сохраняются")
        self.assertEqual((сег[3]["start_ms"], сег[3]["end_ms"], сег[3]["text"]),
                         (46000, 60000, "три"))
        self.assertEqual(len(сег[1]["id"]), 36)

    def test_коробка_без_имени_движка_даёт_unknown(self):
        call_asr.записать_сегменты(self.con, self.eid, None, self.segs)
        t = self.con.execute("select engine, model, language from transcripts").fetchone()
        self.assertEqual(tuple(t), ("unknown", "unknown", None))

    def test_повторный_прогон_заводит_новую_расшифровку(self):
        a = call_asr.записать_сегменты(self.con, self.eid, None, self.segs)
        b = call_asr.записать_сегменты(self.con, self.eid, None, self.segs[:1])
        self.assertNotEqual(a, b)
        self.assertEqual(self.con.execute(
            "select count(*) from transcript_segments").fetchone()[0], 3,
            "старые сегменты остаются — evidence приколочено к ним")
        tid, сег = call_asr.сегменты_события(self.con, self.eid)
        self.assertEqual((tid, sorted(сег)), (b, [1]), "сверка идёт по последней")

    def test_строки_фиксируются_раньше_файла(self):
        """Codex по #121, круг 3: файл без строки `transcripts` — только
        legacy. Значит, `run` обязан фиксировать строки до файла: смерть
        между ними не должна оставлять файл свежего прогона без строк."""
        self.con.execute("insert into blobs(sha256,path,bytes,mime,created) "
                         "values(?,?,?,?,?)", ("ab" * 32, __file__, 1, "audio/x", "t"))
        self.con.execute("update events set blob_sha256=? where id=?", ("ab" * 32, self.eid))
        порядок = []
        было = (call_asr.duration_ms, call_asr.transcribe_spans, call_asr.write_jsonl,
                call_asr.ASR_URL)
        call_asr.duration_ms = lambda path: 1000
        call_asr.transcribe_spans = lambda base, plan, cutter, движок=None: self.segs

        def файл(path, segs):
            порядок.append(("файл", mi.connect(self.dir).execute(
                "select count(*) from transcripts").fetchone()[0]))
            raise OSError("диск кончился до файла")
        call_asr.write_jsonl = файл
        call_asr.ASR_URL = "http://127.0.0.1:1"
        try:
            with self.assertRaises(OSError):
                call_asr.run(self.eid, self.dir)
        finally:
            (call_asr.duration_ms, call_asr.transcribe_spans, call_asr.write_jsonl,
             call_asr.ASR_URL) = было
        self.assertEqual(порядок, [("файл", 1)], "строки зафиксированы до записи файла")
        self.assertEqual(self.con.execute("select state from events").fetchone()[0],
                         "transcribed")

    def test_без_расшифровки_пусто(self):
        self.assertEqual(call_asr.сегменты_события(self.con, self.eid), (None, {}))


if __name__ == "__main__":
    unittest.main()
