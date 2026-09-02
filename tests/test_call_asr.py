"""Нарезка и склейка транскрипта (ТЗ §8)."""
import os, sys, json, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_asr


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
                body = json.dumps({"text": "кусок %d" % outer.calls,
                                   "sec": 25}).encode("utf-8")
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


if __name__ == "__main__":
    unittest.main()
