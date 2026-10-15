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

    def test_имя_с_пробелами_режется_целиком(self):
        """Файл, названный рукой, несёт пробелы — хвост названия не течёт
        («kb/commitments/… invoice …» был бы утечкой, Codex по #182)."""
        s = mi.обезличить("спор: 'kb/commitments/Send invoice to Olegu.md' и ещё")
        self.assertNotIn("invoice", s)
        self.assertNotIn("Olegu", s)
        self.assertEqual(s, "спор: 'kb/commitments/…' и ещё")
        s = mi.обезличить("kb/commitments/прислать смету Олегу.md: конфликт")
        self.assertNotIn("смету", s)
        self.assertEqual(s, "kb/commitments/…: конфликт")
        # знаки препинания в имени — тоже имя (Codex, круг 3)
        s = mi.обезличить("kb/commitments/Send invoice, urgent to Olegu.md: спор")
        self.assertNotIn("urgent", s)
        self.assertEqual(s, "kb/commitments/…: спор")
        # «.md » в середине имени: жадность до последнего .md (Codex, круг 4)
        s = mi.обезличить("kb/commitments/Send.md backup to Olegu.md — спор")
        self.assertNotIn("backup", s)
        self.assertEqual(s, "kb/commitments/… — спор")
        # жадность не пересекает начало следующего пути: два пути в одной
        # строке режутся по отдельности, диагностика между ними остаётся
        self.assertEqual(mi.обезличить("спор: kb/commitments/a.md, kb/commitments/b.md"),
                         "спор: kb/commitments/…, kb/commitments/…")
        self.assertEqual(
            mi.обезличить("kb/commitments/a.md — строки нет (ключ vault:kb/commitments/a.md)"),
            "kb/commitments/… — строки нет (ключ vault:kb/commitments/…)")

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
        with unittest.mock.patch.object(contextd, "_запустить", return_value=(1, хвост)):
            ok, err = contextd.run_step("project", "call_x")
        self.assertFalse(ok)
        self.assertNotIn("anna-petrova", err)
        self.assertNotIn(ТОКЕН, err)
        self.assertIn("Read-only file system", err)

    def test_срез_не_оставляет_обрубок_пути(self):
        """Обезличивание до среза: срез `[-500:]` посреди пути оставил бы
        `b/conversations/…имя…` без `kb/`, и шаблон его бы не узнал."""
        хвост = "x" * 1000 + КАРТОЧКА + " " + "y" * (500 - len(КАРТОЧКА) + 3)
        with unittest.mock.patch.object(contextd, "_запустить", return_value=(1, хвост)):
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

    def test_tdlib_contextd_молчит_без_токена(self):
        """`send` повторяет без конца и пишет причину раз в минуту."""
        журнал, раз = [], []

        def post(ev):
            if not раз:
                раз.append(1)
                raise ValueError("Invalid header value b'Bearer %s\\r'" % ("x" * 43))
            return {"duplicate": False}
        демон = types.SimpleNamespace(post=post, log=журнал.append,
                                      sleep=lambda s: None, beat=lambda: None)
        ti.Демон.send(демон, {"source_id": "1/2"})
        self.assertIn("contextd не отвечает (ValueError: Invalid header value", журнал[0])
        self.assertNotIn("x" * 43, журнал[0])

    def плагин(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mara_context_privacy", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "..", "install", "mara-context", "__init__.py"))
        м = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(м)
        return м

    def test_плагин_не_отдаёт_токен_модели_и_в_журнал(self):
        """Ответ инструмента уходит модели (и в облако), предупреждение — в
        журнал Hermes: в обоих ни текста `ValueError` с токеном, а код HTTP
        и причина сокета остаются."""
        import urllib.error
        м = self.плагин()
        секрет = "z" * 43

        def падает(беда):
            def open_url(*a, **kw):
                raise беда
            return open_url
        заголовок = ValueError("Invalid header value b'Bearer %s\\r'" % секрет)
        ответ = м.Брокер(token="t", open_url=падает(заголовок)).правка(
            {"item": "дело", "status": "done"})
        self.assertNotIn(секрет, ответ)
        self.assertIn("ValueError", ответ)
        отказ = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        ответ = м.Брокер(token="t", open_url=падает(отказ)).правка({"item": "дело"})
        self.assertIn("ConnectionRefusedError: Connection refused", ответ)
        with self.assertLogs(м.log, "WARNING") as лог:
            м.Брокер(token="t", open_url=падает(заголовок)).контекст()
        self.assertNotIn(секрет, "\n".join(лог.output))
        self.assertIn("ValueError", "\n".join(лог.output))
        with self.assertLogs(м.log, "WARNING") as лог:
            м.Брокер(token="t", open_url=падает(urllib.error.HTTPError(
                "u", 401, "Unauthorized", {}, None))).контекст()
        self.assertIn("HTTP 401", "\n".join(лог.output))

    def test_сводка_дня_без_ключа(self):
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
        self.assertIn("не вышло: ValueError: Invalid header value", текст,
                      "причина остаётся, без ключа")


class ЧувствительныйРежим(unittest.TestCase):
    """Gated sensitive mode (ТЗ §15.1): журнал несёт содержимое только после
    явного включения на срок, с предупреждением в строке; секреты — никогда."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.строка = "ledger_import: %s несёт id a, в реестре b — верю реестру" % ОБЕЩАНИЕ

    def test_по_умолчанию_обезличено(self):
        s = mi.для_журнала(self.строка, self.root)
        self.assertNotIn("smetu", s)
        self.assertIn("kb/commitments/…", s)
        self.assertNotIn("чувствительный журнал", s)

    def test_включено_путь_цел_токен_нет(self):
        mi.включить_чувствительные(self.root, 30)
        s = mi.для_журнала("%s: спор, адрес /bot%s/x" % (ОБЕЩАНИЕ, ТОКЕН), self.root)
        self.assertIn(ОБЕЩАНИЕ, s, "в режиме путь остаётся")
        self.assertNotIn(ТОКЕН, s, "секреты маскируются всё равно")
        self.assertIn("<TG_TOKEN>", s)
        self.assertIn("[чувствительный журнал до ", s, "явное предупреждение")

    def test_срок_истёк_снова_обезличено(self):
        import datetime as dt
        mi.включить_чувствительные(self.root, 5,
                                   now=dt.datetime.now(mi.TZ) - dt.timedelta(hours=1))
        self.assertIsNone(mi.чувствительно(self.root))
        self.assertNotIn("smetu", mi.для_журнала(self.строка, self.root))

    def test_потолок_сутки(self):
        import datetime as dt
        now = dt.datetime.now(mi.TZ)
        срок = mi.включить_чувствительные(self.root, 999999, now=now)
        self.assertLessEqual((срок - now).total_seconds(), 24 * 3600)

    def test_срок_без_таймзоны_читается(self):
        """Рукой вписанный naive-срок не роняет `_скажи` (aware < naive —
        TypeError), а считается сроком в поясе владельца."""
        with open(os.path.join(self.root, mi.ЧУВСТВ_ФАЙЛ), "w") as fh:
            fh.write("2099-01-01T00:00:00")
        self.assertIsNotNone(mi.чувствительно(self.root))
        self.assertIn(ОБЕЩАНИЕ, mi.для_журнала(self.строка, self.root))

    def test_обезличить_снимает_префикс_режима(self):
        """Хвост шага, родившийся под режимом, в `jobs.last_error` и Telegram
        не притворяется чувствительным: путь там всё равно срезан."""
        mi.включить_чувствительные(self.root, 30)
        s = mi.обезличить(mi.для_журнала(self.строка, self.root))
        self.assertNotIn("чувствительный журнал", s)
        self.assertNotIn("smetu", s)
        self.assertIn("kb/commitments/…", s)

    def test_ноль_выключает(self):
        mi.включить_чувствительные(self.root, 30)
        self.assertIsNotNone(mi.чувствительно(self.root))
        self.assertIsNone(mi.включить_чувствительные(self.root, 0))
        self.assertIsNone(mi.чувствительно(self.root))
        self.assertFalse(os.path.exists(os.path.join(self.root, mi.ЧУВСТВ_ФАЙЛ)))

    def test_кривой_файл_не_роняет_и_не_включает(self):
        with open(os.path.join(self.root, mi.ЧУВСТВ_ФАЙЛ), "w") as fh:
            fh.write("мусор")
        self.assertIsNone(mi.чувствительно(self.root))
        self.assertNotIn("smetu", mi.для_журнала(self.строка, self.root))

    def test_скажи_в_журнал_режет_путь_а_в_режиме_нет(self):
        import ledger_import as li
        прежний = mi.ROOT
        mi.ROOT = self.root
        try:
            err = io.StringIO()               # не tty — как journald и лог крона
            with contextlib.redirect_stderr(err):
                li._скажи(self.строка)
            self.assertNotIn("smetu", err.getvalue())
            self.assertIn("kb/commitments/…", err.getvalue())
            mi.включить_чувствительные(self.root, 30)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                li._скажи(self.строка)
            self.assertIn(ОБЕЩАНИЕ, err.getvalue())
            self.assertIn("[чувствительный журнал до ", err.getvalue())
        finally:
            mi.ROOT = прежний

    def test_скажи_у_терминала_как_есть_только_из_main(self):
        """Владельцу, запустившему перенос руками, нужен путь — без режима.
        Терминал определяет `main`, не сам `_скажи`: у foreground
        `contextd --serve` stderr тоже tty, но правка словами через
        `call_project` — это журнал (Codex по #182)."""
        import ledger_import as li
        import tempfile as tf
        vault = tf.mkdtemp()
        os.makedirs(os.path.join(vault, "kb/commitments"))
        with open(os.path.join(vault, "kb/commitments",
                               "2026-10-15-prislat-smetu-olegu.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("тело без шапки\n")
        поток = io.StringIO()
        поток.isatty = lambda: True
        прежние = mi.ROOT, li._У_ТЕРМИНАЛА
        try:
            with contextlib.redirect_stderr(поток):
                li.main(["--root", self.root, "--vault", vault, "--dry-run"])
            self.assertIn("smetu", поток.getvalue(), "у терминала путь целиком")
        finally:
            mi.ROOT, li._У_ТЕРМИНАЛА = прежние

    def test_скажи_при_tty_библиотечно_всё_равно_журнал(self):
        """Библиотечный зов (правка словами внутри contextd) обезличен и
        при tty-stderr: флаг терминала ставит только `main`."""
        import ledger_import as li
        поток = io.StringIO()
        поток.isatty = lambda: True
        прежний = mi.ROOT
        mi.ROOT = self.root
        try:
            with contextlib.redirect_stderr(поток):
                li._скажи(self.строка)
        finally:
            mi.ROOT = прежний
        self.assertNotIn("smetu", поток.getvalue())
        self.assertIn("kb/commitments/…", поток.getvalue())

    def test_перенос_предупреждает_через_скажи(self):
        """Сквозной: карточка без фронтматтера в прогоне `run` — в журнале
        путь срезан. Мутант «печать мимо _скажи» этот тест роняет."""
        import ledger_import as li
        vault = tempfile.mkdtemp()
        os.makedirs(os.path.join(vault, "kb/commitments"))
        with open(os.path.join(vault, "kb/commitments",
                               "2026-10-15-prislat-smetu-olegu.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("тело без шапки\n")
        con = mi.connect(self.root)
        прежний = mi.ROOT
        mi.ROOT = self.root
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                итог = li.run(con, vault)
        finally:
            mi.ROOT = прежний
        self.assertEqual(итог["спорных"], 1)
        self.assertIn("без фронтматтера", err.getvalue())
        self.assertNotIn("smetu", err.getvalue())

    def test_включатель_contextd(self):
        прежний = mi.ROOT          # contextd.main ставит mi.ROOT = a.root
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                код = contextd.main(["--root", self.root, "--sensitive-logs", "30"])
            self.assertEqual(код, 0)
            self.assertIn("внимание:", buf.getvalue())
            self.assertIsNotNone(mi.чувствительно(self.root))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                код = contextd.main(["--root", self.root, "--sensitive-logs", "0"])
            self.assertEqual(код, 0)
            self.assertIn("выключен", buf.getvalue())
            self.assertIsNone(mi.чувствительно(self.root))
        finally:
            mi.ROOT = прежний

    def test_включатель_на_кривом_root_код_1(self):
        прежний = mi.ROOT
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                код = contextd.main(["--root", os.path.join(self.root, "нет/такого"),
                                     "--sensitive-logs", "30"])
        finally:
            mi.ROOT = прежний
        self.assertEqual(код, 1)
        self.assertIn("не вышло:", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
