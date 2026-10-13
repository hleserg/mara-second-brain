"""ТЗ §15.1, Т9.1: в журналах нет содержимого, номеров и токенов.

Пути карточек несут имя или номер собеседника (`slug(contact)`) и пересказ
обязательства (`slug(action)`), токен бота едет в адресе запроса. Каждый
тест — один канал, по которому это уходило в журнал, `jobs.last_error`,
сверку или Telegram. Значения вымышленные.
"""
import contextlib, http.client, io, os, subprocess, sys, tempfile, types
import unittest, unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import redact
import call_digest as cd
import contextd
import contextd_reconcile as rc
import tdlib_ingest as ti

ТОКЕН = "123456789:AAFakeFakeFakeFakeFakeFakeFakeFake12"
КАРТОЧКА = "kb/conversations/2026-10-11-1405-anna-petrova.md"
ОБЕЩАНИЕ = "kb/commitments/2026-10-11-prislat-smetu-olegu.md"


class Обезличить(unittest.TestCase):
    def test_токен_бота_ловится_и_в_адресе(self):
        """`\\b` тут не годится: в `/bot123:` между `t` и `1` нет границы."""
        s = "InvalidURL: '/bot%s/sendMessage'" % ТОКЕН
        for rx, repl in redact.SECRETS:
            s = rx.sub(repl, s)
        self.assertNotIn(ТОКЕН, s)
        self.assertIn("<TG_TOKEN>", s)

    def test_путь_карточки_до_каталога(self):
        s = mi.обезличить("PermissionError: [Errno 13] Permission denied: '%s'\n"
                          "спор: %s, %s" % (КАРТОЧКА, ОБЕЩАНИЕ, КАРТОЧКА))
        self.assertNotIn("anna-petrova", s)
        self.assertNotIn("smetu", s)
        self.assertIn("kb/conversations/…", s)
        self.assertIn("kb/commitments/…", s)
        self.assertIn("Permission denied", s, "причина остаётся")

    def test_карточка_человека_тоже(self):
        """`call_project.person_card` пишет `entities/people/<slug(имени)>.md`
        тем же шагом `project`."""
        s = mi.обезличить("/srv/vault/entities/people/anna-petrova.md: Permission denied")
        self.assertNotIn("anna-petrova", s)
        self.assertEqual(s, "/srv/vault/entities/people/…: Permission denied")

    def test_после_пути_диагностика_остаётся(self):
        self.assertEqual(mi.обезличить("%s: конфликт" % КАРТОЧКА),
                         "kb/conversations/…: конфликт")
        self.assertEqual(mi.обезличить("%s;потом" % ОБЕЩАНИЕ),
                         "kb/commitments/…;потом")

    def test_обычный_текст_не_трогается(self):
        for s in ("telegram 400: chat not found", "таймаут часа на шаге asr", ""):
            self.assertEqual(mi.обезличить(s), s)
        self.assertEqual(mi.обезличить(None), "")


class ХвостШага(unittest.TestCase):
    def test_last_error_без_пути_и_токена(self):
        """`run_step` — единственное место, откуда хвост stderr шага уходит в
        `jobs.last_error`, а оттуда — в сверку и Telegram."""
        хвост = "Traceback …\nOSError: [Errno 30] Read-only file system: '%s' %s" % (
            КАРТОЧКА, ТОКЕН)
        готово = subprocess.CompletedProcess([], 1, "", хвост)
        with unittest.mock.patch.object(contextd.subprocess, "run", return_value=готово):
            ok, err = contextd.run_step("project", "call_x")
        self.assertFalse(ok)
        self.assertNotIn("anna-petrova", err)
        self.assertNotIn(ТОКЕН, err)
        self.assertIn("Read-only file system", err)

    def test_срез_не_оставляет_обрубок_пути(self):
        """Обезличивание до среза: срез `[-500:]` посреди пути оставил бы
        `b/conversations/…имя…` без `kb/`, и шаблон его бы не узнал."""
        хвост = "x" * 1000 + КАРТОЧКА + " " + "y" * (500 - len(КАРТОЧКА) + 3)
        готово = subprocess.CompletedProcess([], 1, "", хвост)
        with unittest.mock.patch.object(contextd.subprocess, "run", return_value=готово):
            _, err = contextd.run_step("project", "call_x")
        self.assertLessEqual(len(err), 500)
        self.assertNotIn("anna-petrova", err)


class Сверка(unittest.TestCase):
    def setUp(self):
        self.con = mi.connect(tempfile.mkdtemp())

    def test_dlq_называет_причину_без_пути(self):
        """Строки, легшие в `last_error` до обезличивания в `run_step`, ещё
        несут путь; в сводку уходит последняя строка, обезличенная."""
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": "d", "payload": {}})
        self.con.execute(
            "insert into jobs(id,event_id,kind,state,attempts,last_error,created,"
            "updated) values('j',?,'project','dlq',6,?,?,?)",
            (eid, "Traceback (most recent call last):\n  File …\n"
                  "PermissionError: [Errno 13] Permission denied: '%s'\n" % ОБЕЩАНИЕ,
             mi.now_iso(), mi.now_iso()))
        f = rc.dlq(self.con)
        self.assertEqual(len(f), 1)
        self.assertNotIn("smetu", f[0]["detail"])
        self.assertIn("PermissionError", f[0]["detail"], "причина — последняя строка")
        self.assertNotIn("Traceback", f[0]["detail"])

    def test_доложить_не_печатает_адрес_с_токеном(self):
        def deliver(*a):
            raise http.client.InvalidURL(
                "URL can't contain control characters. '/bot%s  # бот'" % ТОКЕН)
        with unittest.mock.patch.object(cd, "deliver", deliver), \
                unittest.mock.patch.object(cd, "env", lambda: {}):
            исход = rc.доложить([rc.находка("x", "warn", "что-то")])
        self.assertEqual(исход, "failed: InvalidURL")

    def test_доложить_и_на_кривом_заголовке(self):
        """urllib бросает `ValueError` с заголовком целиком, если в значении
        перевод строки."""
        def deliver(*a):
            raise ValueError("Invalid header value b'Bearer %s\\r'" % ТОКЕН)
        with unittest.mock.patch.object(cd, "deliver", deliver), \
                unittest.mock.patch.object(cd, "env", lambda: {}):
            self.assertEqual(rc.доложить([rc.находка("x", "warn", "что-то")]),
                             "failed: ValueError")


class Дайджест(unittest.TestCase):
    def test_кривой_токен_не_уходит_в_адрес(self):
        """Хвостовой комментарий в env: раньше `urlopen` бросал `InvalidURL`
        с токеном внутри, теперь до сети не доходит."""
        err = io.StringIO()
        with unittest.mock.patch.object(cd.urllib.request, "urlopen",
                                        side_effect=AssertionError("сеть")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cd.deliver("текст", ТОКЕН + "  # бот", "123456789"),
                             "no-transport")
        self.assertNotIn(ТОКЕН, err.getvalue())
        self.assertIn("не похож на токен", err.getvalue())


class Правка(unittest.TestCase):
    def test_ошибка_правки_без_пути_карточки(self):
        """`OSError` из `apply_correction` несёт путь карточки обязательства."""
        import json, threading, urllib.request
        import call_project
        каталог = tempfile.mkdtemp()
        mi.ROOT = каталог
        vault = tempfile.mkdtemp()
        for под in (".git", "kb/commitments"):
            os.makedirs(os.path.join(vault, под))
        srv = contextd.make_server(каталог, port=0, vault=vault)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        con = mi.connect(каталог)
        _, токен = contextd.pair(con, "телефон")

        def apply_correction(*a, **kw):
            raise PermissionError(13, "Permission denied", os.path.join(vault, ОБЕЩАНИЕ))
        out = io.StringIO()
        запрос = urllib.request.Request(
            "http://127.0.0.1:%d/v1/ingest/event" % srv.server_address[1],
            data=json.dumps({"kind": "correction", "source": "mara", "source_id": "п",
                             "payload": {"item": "смета", "status": "done"}}).encode(),
            headers={"Authorization": "Bearer " + токен,
                     "Content-Type": "application/json"})
        with unittest.mock.patch.object(call_project, "apply_correction", apply_correction), \
                contextlib.redirect_stdout(out):
            with urllib.request.urlopen(запрос, timeout=10) as r:
                self.assertEqual(r.status, 200)
        строки = [l for l in out.getvalue().splitlines() if l.startswith("correction ")]
        self.assertEqual(len(строки), 1, out.getvalue())
        self.assertIn("PermissionError errno=13", строки[0])
        self.assertNotIn("smetu", out.getvalue())


class Телеграм(unittest.TestCase):
    def test_журнал_приёма_без_id_собеседника(self):
        """`chat_id` личного чата — id собеседника; строка — на каждое сообщение."""
        журнал = []
        демон = types.SimpleNamespace(post=lambda ev: {"duplicate": False},
                                      log=журнал.append, sleep=lambda s: None,
                                      beat=lambda: None)
        ti.Демон.send(демон, {"source_id": "424242/17"})
        self.assertEqual(len(журнал), 1)
        self.assertNotIn("424242", журнал[0])
        self.assertTrue(журнал[0].endswith(" ok"), журнал[0])

    def test_дамп_апдейтов_не_включается(self):
        """python-telegram на DEBUG пишет апдейты целиком, с текстами;
        `serve` закрепляет его логгер на INFO до подключения."""
        import logging
        логгер = logging.getLogger("telegram.tdjson")
        self.addCleanup(логгер.setLevel, логгер.level)
        логгер.setLevel(logging.DEBUG)

        class Стоп(Exception):
            pass

        def _connect(home):
            raise Стоп
        with unittest.mock.patch.dict(os.environ, {"MARA_CONTEXT_TOKEN": "t"}), \
                unittest.mock.patch.object(ti, "_connect", _connect), \
                self.assertRaises(Стоп):
            ti.serve(tempfile.mkdtemp())
        self.assertEqual(логгер.level, logging.INFO)



class Хвосты(unittest.TestCase):
    """Каналы, которые #152 оставил: `outbox.error`, догон TDLib, работа
    `queue-worker` в волте, сводка дня в Telegram. Чужой адресат дайджеста
    печатается целиком сознательно (#61, `test_call_digest`): это id канала
    из env владельца, и без него отказ не узнать."""

    def test_outbox_error_обезличен(self):
        con = mi.connect(tempfile.mkdtemp())
        self.addCleanup(con.close)
        mi.в_outbox(con, "digest", {"text": "x"})

        def отправить(kind, payload):
            raise http.client.InvalidURL("'/bot%s/sendMessage' %s" % (ТОКЕН, ОБЕЩАНИЕ))
        mi.из_outbox(con, отправить, продолжать=True)
        err = con.execute("select error from outbox").fetchone()[0]
        self.assertNotIn(ТОКЕН, err)
        self.assertNotIn("smetu", err)
        self.assertIn("InvalidURL", err)

    def test_догон_tdlib_без_текста_исключения(self):
        журнал = []

        def catch_up():
            raise RuntimeError("чат «Анна Петрова»: привет")

        class Раз:
            n = 0

            def is_set(self):
                self.n += 1
                return self.n > 1

            def wait(self, t):
                pass
        демон = types.SimpleNamespace(catch_up=catch_up, log=журнал.append,
                                      connected=False, beat=lambda: None)
        ti.Демон.run(демон, Раз())
        self.assertEqual(журнал, ["догон не удался: RuntimeError"])

    def test_ошибка_работы_в_волте_без_ключа(self):
        import importlib.util
        путь = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "scripts", "queue-worker.py")
        spec = importlib.util.spec_from_file_location("queue_worker", путь)
        qw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qw)
        s = qw.причина(ValueError("Invalid header value b'Bearer sk-or-v1-%s\\r'"
                                  % ("f" * 40)))
        self.assertNotIn("sk-or", s)
        self.assertTrue(s.startswith("ValueError: "), s)

    def test_сводка_дня_без_текста_исключения(self):
        """Сводка уходит в Telegram; `ValueError` заголовка несёт ключ."""
        import importlib.util
        путь = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "scripts", "daily-summary.py")
        spec = importlib.util.spec_from_file_location("daily_summary", путь)
        ds = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ds)

        def ask(*a, **kw):
            raise ValueError("Invalid header value b'Bearer sk-or-v1-%s\\r'" % ("f" * 40))
        with unittest.mock.patch.object(ds, "cards", lambda *a: [("заметка", "текст")]), \
                unittest.mock.patch.object(ds, "diary", lambda *a: ""), \
                unittest.mock.patch.object(ds, "plain", lambda *a: "механика"), \
                unittest.mock.patch.object(ds, "material", lambda *a: "м"), \
                unittest.mock.patch.object(ds, "ask", ask), \
                unittest.mock.patch.object(redact, "require_chain", lambda: None), \
                unittest.mock.patch.object(redact, "redact", lambda t: (t, {})):
            текст = ds.summary("/нет", "2026-10-13", key="k")
        self.assertNotIn("sk-or", текст)
        self.assertIn("не вышло: ValueError", текст)


if __name__ == "__main__":
    unittest.main()
