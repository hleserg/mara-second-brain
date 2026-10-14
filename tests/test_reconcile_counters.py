"""#96, Т3.1: отказы демона и занятость базы видны в сверке — снимки
счётчиков `/metrics` за сутки в файле состояния, прирост по окну с учётом
рестартов, пороги за сутки."""
import json, os, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import contextd_reconcile as cr

БЕЗ_ПРОКСИ = {k: "" for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                               "all_proxy", "ALL_PROXY")}


class _Метрики(BaseHTTPRequestHandler):
    текст = ""

    def do_GET(self):
        тело = type(self).текст.encode()
        self.send_response(200 if self.path == "/metrics" else 404)
        self.end_headers()
        self.wfile.write(тело)

    def log_message(self, *a):
        pass


def метрики(**коды):
    строки = ["# HELP что-то", "mara_ingest_queue_depth 0"]
    for к, n in коды.items():
        if к == "locked":
            строки += ['mara_db_locked_total{where="http"} %d' % n,
                       'mara_db_locked_total{where="worker"} 0']
        else:
            строки.append('mara_http_errors_total{code="%s"} %d' % (к[1:], n))
    return "\n".join(строки) + "\n"


class Отказы(unittest.TestCase):
    def setUp(self):
        прокси = mock.patch.dict(os.environ, БЕЗ_ПРОКСИ)
        прокси.start()
        self.addCleanup(прокси.stop)
        self.обработчик = type("М", (_Метрики,), {"текст": ""})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.обработчик)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.адрес = "http://127.0.0.1:%d" % srv.server_address[1]
        self.файл = os.path.join(tempfile.mkdtemp(), "состояние", "счётчики.json")

    def прогон(self, текст, сейчас):
        self.обработчик.текст = текст
        return [(н["check"], н.get("code")) for н in
                cr.отказы_растут(self.адрес, self.файл, сейчас=сейчас)]

    def test_первый_прогон_только_запоминает(self):
        self.assertEqual(self.прогон(метрики(c413=5), 1000), [])
        with open(self.файл) as fh:
            self.assertEqual(len(json.load(fh)), 1)

    def test_413_с_первого_и_прирост_по_окну(self):
        self.прогон(метрики(c401=0, c413=0), 1000)
        self.assertEqual(self.прогон(метрики(c401=5, c413=0), 1000 + 3600), [])
        # ночью один 413 и ещё 401 — в «утреннем» прогоне виден весь день
        self.прогон(метрики(c401=12, c413=1), 1000 + 7200)
        self.assertEqual(sorted(self.прогон(метрики(c401=21, c413=1), 1000 + 30000)),
                         [("отказы-растут", 401), ("отказы-растут", 413)])

    def test_рестарт_демона_не_отрицательный(self):
        """Счётчик после рестарта меньше прошлого: в прирост идёт он сам."""
        self.прогон(метрики(c500=3), 1000)
        self.assertEqual(self.прогон(метрики(c500=0), 2000), [], "рестарт без новых")
        self.assertEqual(self.прогон(метрики(c500=1), 3000), [("отказы-растут", 500)])

    def test_старше_суток_выпадает(self):
        self.прогон(метрики(c413=0), 1000)
        self.прогон(метрики(c413=1), 2000)
        поздно = 2000 + cr.СЧЁТЧИКИ_ОКНО_С + 10
        self.assertEqual(self.прогон(метрики(c413=1), поздно), [], "всплеск вчерашний")

    def test_занятость_базы(self):
        self.прогон(метрики(locked=0), 1000)
        н = self.прогон(метрики(locked=int(cr.ЗАНЯТО_ЗА_СУТКИ)), 5000)
        self.assertEqual(н, [("база-занята", None)])

    def test_не_contextd_и_демона_нет(self):
        self.assertEqual(self.прогон("<html>прокси</html>", 1000), [])
        self.assertFalse(os.path.exists(self.файл), "чужой ответ снимком не стал")
        self.assertEqual(cr.отказы_растут("http://127.0.0.1:9", self.файл), [])
        self.assertEqual(cr.отказы_растут("", self.файл), [])

    def test_кривой_файл_состояния_не_роняет(self):
        os.makedirs(os.path.dirname(self.файл))
        with open(self.файл, "w") as fh:
            fh.write("{мусор")
        self.assertEqual(self.прогон(метрики(c413=1), 1000), [])
        with open(self.файл) as fh:
            self.assertEqual(len(json.load(fh)), 1)


if __name__ == "__main__":
    unittest.main()
