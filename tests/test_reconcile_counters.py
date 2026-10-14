"""#96, Т3.1: отказы демона, падения обработчика и занятость базы видны в
сверке — снимки счётчиков `/metrics` в файле состояния, прирост по цепочке от
новейшего снимка не моложе суток, рестарт — по времени старта, пороги за
сутки."""
import contextlib, io, json, os, sqlite3, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import contextd_reconcile as cr
import contextd
import mara_ingest as mi

БЕЗ_ПРОКСИ = {k: "" for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                               "all_proxy", "ALL_PROXY")}
СУТКИ = cr.СЧЁТЧИКИ_ОКНО_С


class _Метрики(BaseHTTPRequestHandler):
    текст = ""
    код = 200

    def do_GET(self):
        тело = type(self).текст.encode()
        self.send_response(type(self).код if self.path == "/metrics" else 404)
        self.end_headers()
        self.wfile.write(тело)

    def log_message(self, *a):
        pass


def метрики(старт=None, упало=None, http=0, worker=0, **коды):
    """Текст `/metrics` в формате демона: занятость с обеими метками есть
    всегда, как у `contextd.metrics`."""
    строки = ["# HELP что-то", "mara_ingest_queue_depth 0",
              'mara_db_locked_total{where="http"} %d' % http,
              'mara_db_locked_total{where="worker"} %d' % worker]
    for к, n in коды.items():
        строки.append('mara_http_errors_total{code="%s"} %d' % (к[1:], n))
    if упало is not None:
        строки.append("mara_http_crashes_total %d" % упало)
    if старт is not None:
        строки.append("mara_process_start_time_seconds %d" % старт)
    return "\n".join(строки) + "\n"


class Отказы(unittest.TestCase):
    def setUp(self):
        прокси = mock.patch.dict(os.environ, БЕЗ_ПРОКСИ)
        прокси.start()
        self.addCleanup(прокси.stop)
        self.обработчик = type("М", (_Метрики,), {"текст": "", "код": 200})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.обработчик)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.адрес = "http://127.0.0.1:%d" % srv.server_address[1]
        self.файл = os.path.join(tempfile.mkdtemp(), "состояние", "счётчики.json")

    def прогон(self, текст, сейчас, адрес=None):
        self.обработчик.текст = текст
        return [(н["check"], н.get("code", н.get("count"))) for н in
                cr.отказы_растут(адрес or self.адрес, self.файл, сейчас=сейчас)]

    def цепочка(self, адрес=None):
        with open(self.файл) as fh:
            return json.load(fh)[адрес or self.адрес]

    def test_первый_прогон_только_запоминает(self):
        self.assertEqual(self.прогон(метрики(c413=5), 1000), [])
        self.assertEqual(len(self.цепочка()), 1)

    def test_400_403_413_с_первого_и_прирост_по_окну(self):
        self.прогон(метрики(c401=0, c413=0), 1000)
        self.assertEqual(self.прогон(метрики(c401=4, c413=0), 1000 + 3600), [])
        # ночью один 413, 400 и 403 и ещё 401 — в «утреннем» прогоне виден весь день
        self.прогон(метрики(c401=4, c413=1, c400=1, c403=1), 1000 + 7200)
        self.assertEqual(sorted(self.прогон(метрики(c401=5, c413=1, c400=1, c403=1),
                                            1000 + 30000)),
                         [("отказы-растут", 400), ("отказы-растут", 401),
                          ("отказы-растут", 403), ("отказы-растут", 413)])

    def test_рестарт_без_времени_старта(self):
        """Демон старше #96: счётчик меньше прошлого — с нуля, в прирост
        идёт он сам, а не отрицательная разность."""
        self.прогон(метрики(c413=3), 1000)
        self.assertEqual(self.прогон(метрики(c413=0), 2000), [], "рестарт без новых")
        self.assertEqual(self.прогон(метрики(c413=1), 3000), [("отказы-растут", 413)])

    def test_рестарт_по_времени_старта_догнавший_счётчик(self):
        """413 в 06:30, выкат в 10:20, второй 413 в 10:40: значение то же,
        что до рестарта, — по разности прирост ноль, по старту — один (ревью)."""
        self.прогон(метрики(старт=100, c413=0), 1000)
        self.прогон(метрики(старт=100, c413=1), 2000)
        # вчерашний 413 ушёл за окно: опорный — снимок 2000, прироста нет
        self.assertEqual(self.прогон(метрики(старт=100, c413=1), 2000 + СУТКИ + 10), [])
        self.assertEqual(self.прогон(метрики(старт=500, c413=1), 2000 + СУТКИ + 20),
                         [("отказы-растут", 413)])

    def test_щели_между_сутками_нет(self):
        """Вчера 8:00 — снимок, 8:03 — 413, 8:07 — снимок. Сегодня прогон в
        8:00 ушёл на 15 с позже: опорный — вчерашний 8:00, отказ виден."""
        утро = 8 * 3600
        self.прогон(метрики(старт=1, c413=0), утро)
        self.прогон(метрики(старт=1, c413=1), утро + 7 * 60)
        self.assertEqual(self.прогон(метрики(старт=1, c413=1), утро + СУТКИ + 15),
                         [("отказы-растут", 413)])
        self.assertEqual(len(self.цепочка()), 3, "опорный не моложе суток остался")
        # через сутки после того — вчерашний всплеск уже не наш
        self.assertEqual(self.прогон(метрики(старт=1, c413=1), утро + 2 * СУТКИ + 30), [])

    def test_старше_суток_выпадает(self):
        self.прогон(метрики(c413=0), 1000)
        self.прогон(метрики(c413=1), 2000)
        self.прогон(метрики(c413=1), 2000 + СУТКИ + 5)
        self.assertEqual(self.прогон(метрики(c413=1), 2000 + СУТКИ + 10), [],
                         "всплеск вчерашний")

    def test_занятость_по_меткам(self):
        """Сброс одной метки не прячется за ростом другой: http 40→0 и
        worker 0→60 — прирост 60, а не 20 (ревью)."""
        self.прогон(метрики(http=40), 1000)
        self.assertEqual(self.прогон(метрики(http=0, worker=60), 5000), [("база-занята", 60)])

    def test_падения_обработчика(self):
        self.прогон(метрики(упало=0), 1000)
        self.assertEqual(self.прогон(метрики(упало=1), 2000), [("демон-падает", 1)])

    def test_метрики_не_читаются_это_находка(self):
        """Демон жив (это проверил `main`), а `/metrics` не прочитан: чужой
        ответ, разъехавшийся формат, отказ — находка, а не тишина (ревью)."""
        self.assertEqual(self.прогон("<html>прокси</html>", 1000),
                         [("счётчики-не-читаются", None)])
        self.assertFalse(os.path.exists(self.файл), "чужой ответ снимком не стал")
        self.обработчик.код = 401
        н = cr.отказы_растут(self.адрес, self.файл, сейчас=1000)
        self.assertIn("ответ 401", н[0]["detail"])
        н = cr.отказы_растут("http://127.0.0.1:9", self.файл, сейчас=1000)
        self.assertEqual([x["check"] for x in н], ["счётчики-не-читаются"])
        self.assertEqual(cr.отказы_растут("", self.файл), [])

    def test_не_петлю_не_спрашиваем(self):
        """`/metrics` на чужом адресе отдаёт 401 и каждым прогоном накручивал
        бы демону этот счётчик — запроса нет вовсе."""
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("спросил")):
            н = cr.отказы_растут("http://203.0.113.7:8788", self.файл, сейчас=1000)
        self.assertIn("не на петле", н[0]["detail"])

    def test_цепочка_своя_у_каждого_адреса(self):
        self.прогон(метрики(c413=0), 1000)
        чужой = "http://localhost:%s" % self.адрес.rsplit(":", 1)[1]
        self.прогон(метрики(c413=7), 1500, адрес=чужой)
        self.assertEqual(self.прогон(метрики(c413=0), 2000), [], "чужие числа не вклеены")
        self.assertEqual(len(self.цепочка()), 2)
        self.assertEqual(len(self.цепочка(чужой)), 1)

    def test_кривой_файл_состояния_не_роняет(self):
        os.makedirs(os.path.dirname(self.файл))
        for мусор in ("{мусор", "[1, 2]"):
            with open(self.файл, "w") as fh:
                fh.write(мусор)
            self.assertEqual(self.прогон(метрики(c413=1), 1000), [])
            self.assertEqual(len(self.цепочка()), 1)
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.файл))
                          if f != os.path.basename(self.файл)], [], "временных не осталось")


class ФорматДемона(unittest.TestCase):
    """Разбор держится на выводе самого `contextd.metrics`, а не на
    синтетике: разъедется формат — тест красный, а не сверка тихая."""

    def test_разбор_понимает_contextd(self):
        d = tempfile.mkdtemp()
        con = mi.connect(d)
        self.addCleanup(con.close)
        with mock.patch.dict(contextd._отказы, {403: 2, 413: 1}, clear=True), \
                mock.patch.dict(contextd._заперто, {"http": 3, "worker": 4}), \
                mock.patch.dict(contextd._упало, {"http": 5}):
            текст = contextd.metrics(con, d, None)
        счётчики, старт = cr.разобрать_метрики(текст)
        self.assertEqual(счётчики, {"403": 2, "413": 1, "db_locked:http": 3,
                                    "db_locked:worker": 4, "crashes": 5})
        self.assertEqual(старт, contextd._СТАРТ)

    def test_падение_обработчика_считается(self):
        """«500» демон не отдаёт: упавший обработчик рвёт соединение, и
        считает его `handle_error` — кроме обрыва клиентом и занятой базы."""
        srv = contextd.make_server(tempfile.mkdtemp(), 0)
        self.addCleanup(srv.server_close)
        with mock.patch.dict(contextd._упало, {"http": 0}), \
                mock.patch.dict(contextd._заперто, {"http": 0, "worker": 0}):
            for беда in (RuntimeError("поломка"), ConnectionResetError("ушёл"),
                         sqlite3.OperationalError("database is locked")):
                try:
                    raise беда
                except Exception:
                    with contextlib.redirect_stderr(io.StringIO()), \
                            contextlib.redirect_stdout(io.StringIO()):
                        srv.handle_error(None, ("127.0.0.1", 1))
            self.assertEqual((contextd._упало["http"], contextd._заперто["http"]), (1, 1))


class Крон(unittest.TestCase):
    def main(self, демон):
        находка = cr.находка("отказы-растут", "warn", "ответов 413", code=413)
        argv = ["contextd_reconcile.py", "--json", "--daemon-url", "http://127.0.0.1:9"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(cr, "открыть_реестр", return_value=(None, [
                    cr.находка("бэкап", "error", "бэкап встал")])), \
                mock.patch.object(cr, "демон_не_отвечает", return_value=демон), \
                mock.patch.object(cr, "отказы_растут", return_value=[находка]) as отказы, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cr.main()
        return [f["check"] for f in json.loads(out.getvalue())], отказы

    def test_крон_зовёт_счётчики_у_живого_демона(self):
        проверки, отказы = self.main([])
        self.assertEqual(проверки, ["бэкап", "отказы-растут"], "счётчики — после run()")
        отказы.assert_called_once_with("http://127.0.0.1:9")

    def test_у_лежащего_демона_счётчики_не_спрашиваются(self):
        проверки, отказы = self.main([cr.находка("демон-не-отвечает", "error", "лежит")])
        self.assertEqual(проверки, ["демон-не-отвечает", "бэкап"])
        отказы.assert_not_called()

    def test_сводка_не_режет_ошибки_предупреждениями(self):
        находки = [cr.находка("w%d" % i, "warn", "предупреждение %d" % i) for i in range(12)]
        находки.append(cr.находка("бэкап", "error", "бэкап встал"))
        текст = cr.текст(находки, limit=12)
        self.assertIn("бэкап встал", текст)
        self.assertIn("… и ещё 1", текст)


if __name__ == "__main__":
    unittest.main()
