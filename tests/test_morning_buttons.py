# -*- coding: utf-8 -*-
"""Т-У.5: кнопки под пунктами утра — сборка, отправка, нажатия.

Отправка — настоящий `morning_send.run` в фейковый телеграм (адресом, как в
сквозном тесте конвейера). Нажатия — настоящий `contextd` за настоящим
HTTP: callback из фейкового Bot API превращается в правку словами, и карточка
в волте меняется, как от правки Мары.
"""
import contextlib, io, json, os, sys, tempfile, threading, unittest, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import mara_ingest as mi                                     # noqa: E402
import morning_send as ms                                    # noqa: E402
import telegram_buttons as tb                                # noqa: E402
import call_digest as cd                                     # noqa: E402
import contextd                                              # noqa: E402

ИД = "01999999-0000-7000-8000-00000000abcd"
ИД2 = "01999999-0000-7000-8000-00000000beef"
ИД3 = "01999999-0000-7000-8000-0000c0ffee11"
ИД4 = "01999999-0000-7000-8000-0000deadbe11"
ИД5 = "01999999-0000-7000-8000-0000feedca11"
ДЕНЬ = "2026-10-15"
import datetime                                              # noqa: E402
СЕЙЧАС = datetime.datetime(2026, 10, 15, 9, 0, tzinfo=mi.TZ)


class _Телеграм(BaseHTTPRequestHandler):
    """Фейковый Bot API: пишет зовы, `getUpdates` отдаёт заготовленные пачки."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        тело = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8"))
        метод = self.path.rsplit("/", 1)[-1]
        поля = {k: v[0] for k, v in тело.items()}
        # чей бот звал: кнопки обязаны жить на своём, не на боте Hermes
        поля["_бот"] = self.path.split("/bot", 1)[-1].split("/", 1)[0]
        self.server.зовы.append((метод, поля))
        if метод in self.server.отбитые:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if метод == "getUpdates":
            ответ = {"ok": True,
                     "result": self.server.пачки.pop(0) if self.server.пачки else []}
        else:
            ответ = {"ok": not self.server.отказ, "result": True}
        raw = json.dumps(ответ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def _телеграм():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Телеграм)
    srv.зовы, srv.пачки, srv.отказ, srv.отбитые = [], [], False, set()
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1] + "/bot%s/%s"


def _волт(*карточки):
    v = tempfile.mkdtemp(prefix="mara-vault-")
    os.makedirs(os.path.join(v, "kb/commitments"))
    os.makedirs(os.path.join(v, ".git"))
    for имя, шапка in карточки:
        with open(os.path.join(v, "kb/commitments", имя), "w", encoding="utf-8") as fh:
            fh.write("---\n%s\n---\n" % "\n".join("%s: %s" % kv for kv in шапка))
    return v


КАРТОЧКА = ("a.md", (("title", "прислать смету"), ("id", ИД), ("status", "open"),
                     ("due", ДЕНЬ), ("promised_to", "Анна"),
                     ("origin", "call/x"), ("occurred", "2026-10-14 14:00")))


class Кнопки(unittest.TestCase):
    """Сборка рядов: номера пунктов, версии из реестра, пункт без id."""

    def test_ряды_повторяют_номера_утра(self):
        v = _волт(КАРТОЧКА,
                  ("b.md", (("title", "без id"), ("status", "proposed"),
                            ("occurred", ДЕНЬ))))
        root = tempfile.mkdtemp()
        con = mi.connect(root)
        con.execute("insert into commitments(id, title, status, due, version, "
                    "source_native_id) values(?,?,?,?,?,?)",
                    (ИД, "прислать смету", "open", ДЕНЬ, 7, "k-1"))
        con.commit()
        текст, пункты = ms._сводка().утро(v, ДЕНЬ)
        self.assertEqual([n for n, _ in пункты], [1, 2])
        ряды = ms.кнопки(con, пункты)
        self.assertEqual(len(ряды), 1, "пункт без id остаётся без кнопок")
        self.assertEqual([к["callback_data"] for к in ряды[0]],
                         ["d:0000abcd:7", "n:0000abcd:7", "t:0000abcd:7"])
        self.assertEqual([к["text"] for к in ряды[0]],
                         ["1 сделал", "1 не моё", "1 завтра"])
        # данные кнопки разбираются приёмником — формат один на двоих
        for к in ряды[0]:
            self.assertIsNotNone(tb.разобрать(к["callback_data"]), к)

    def test_ручная_правка_обезоруживает_кнопки(self):
        """Шапка разошлась с реестром (правка рукой, перенос не догнал) —
        версия реестра несвежая, и кнопки не рисуются (Codex, P2)."""
        v = _волт(КАРТОЧКА)
        con = mi.connect(tempfile.mkdtemp())
        con.execute("insert into commitments(id, title, status, due, version, "
                    "source_native_id) values(?,?,?,?,?,?)",
                    (ИД, "прислать смету", "open", "2026-10-12", 7, "r-1"))
        con.commit()
        self.assertEqual(ms.кнопки(con, ms._сводка().утро(v, ДЕНЬ)[1]), [],
                         "срок в файле другой — клик переписал бы ручную правку")
        con.execute("update commitments set due=?, status='done'", (ДЕНЬ,))
        con.commit()
        self.assertEqual(ms.кнопки(con, ms._сводка().утро(v, ДЕНЬ)[1]), [],
                         "статус в файле другой — тоже без кнопок")
        con.execute("update commitments set status='open'")
        con.commit()
        self.assertEqual(len(ms.кнопки(con, ms._сводка().утро(v, ДЕНЬ)[1])), 1,
                         "шапка сошлась со строкой — кнопки на месте")

    def test_без_строки_в_реестре_версия_не_едет(self):
        v = _волт(КАРТОЧКА)
        con = mi.connect(tempfile.mkdtemp())
        ряды = ms.кнопки(con, ms._сводка().утро(v, ДЕНЬ)[1])
        self.assertEqual(ряды[0][0]["callback_data"], "d:0000abcd:-")
        self.assertEqual(tb.разобрать("d:0000abcd:-"), ("d", "0000abcd", None))


class Сводка(unittest.TestCase):
    def test_no_morning_убирает_блок_утра(self):
        """Контракт для крона Мары: с `--no-morning` утра нет, сводка есть."""
        import contextlib, io
        v = _волт(КАРТОЧКА)
        os.makedirs(os.path.join(v, "kb/notes"))
        with open(os.path.join(v, "kb/notes/n.md"), "w", encoding="utf-8") as fh:
            fh.write("---\noccurred: 2026-10-14\nproject: x\n---\n\n# Починил синк\n")
        ds = ms._сводка()
        def прогон(*флаги):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ds.main(["--vault", v, "--date", "2026-10-14", "--raw", *флаги])
            return out.getvalue()
        с_утром = прогон()
        self.assertIn("Сегодня с датой:", с_утром)
        без = прогон("--no-morning")
        self.assertNotIn("Сегодня с датой:", без)
        self.assertIn("Починил синк", без, "сводка за вчера осталась")


class Отправка(unittest.TestCase):
    """`morning_send.run`: клавиатура доезжает, дедуп на день, пустое утро."""

    def setUp(self):
        self.tg, self.api = _телеграм()
        self.addCleanup(self.tg.shutdown)
        self.root = tempfile.mkdtemp(prefix="mara-root-")
        self.env = os.path.join(self.root, "env")
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_HOME_CHANNEL=424242\n"
                     "MARA_BUTTONS_TOKEN=x\nMARA_BUTTONS_BOT_TOKEN=btn-token\n")
        патч = mock.patch.object(cd, "API", self.api % ("%s", "sendMessage"))
        патч.start()
        self.addCleanup(патч.stop)

    def test_утро_уходит_с_клавиатурой_один_раз_в_день(self):
        v = _волт(КАРТОЧКА)
        self.assertEqual(ms.run(self.root, v, self.env, now=СЕЙЧАС), "sent")
        метод, поля = self.tg.зовы[0]
        self.assertEqual(метод, "sendMessage")
        self.assertIn("Сегодня с датой:", поля["text"])
        клава = json.loads(поля["reply_markup"])["inline_keyboard"]
        self.assertEqual(клава[0][0]["callback_data"], "d:0000abcd:-")
        self.assertEqual(поля["_бот"], "btn-token",
                         "утро с кнопками шлёт бот кнопок, не бот Hermes")
        # повтор крона тем же днём — дубль не уходит
        self.assertEqual(ms.run(self.root, v, self.env, now=СЕЙЧАС), "already")
        self.assertEqual(len(self.tg.зовы), 1)

    def test_dry_run_работает_до_настройки(self):
        """Превью печатает текст и кнопки без токенов и без похода в
        телеграм (Codex, круг 10)."""
        import contextlib, io
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_HOME_CHANNEL=424242\n")
        v = _волт(КАРТОЧКА)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ms.run(self.root, v, self.env, now=СЕЙЧАС,
                                    dry_run=True), "dry-run")
        self.assertIn("Сегодня с датой:", out.getvalue())
        self.assertIn("d:0000abcd:-", out.getvalue())
        self.assertEqual(self.tg.зовы, [])

    def test_без_токенов_кнопок_doctor_молчит(self):
        """Настройка кнопок неполная: утро от doctor не уходит вовсе — оно
        остаётся за кроном Мары, и двух утр не бывает ни при вводе кнопок,
        ни при откате (Codex, круг 9)."""
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_HOME_CHANNEL=424242\n"
                     "MARA_BUTTONS_BOT_TOKEN=btn-token\n")
        v = _волт(КАРТОЧКА)
        self.assertEqual(ms.run(self.root, v, self.env, now=СЕЙЧАС), "quiet")
        self.assertEqual(self.tg.зовы, [], "двух утр не бывает")
        con = mi.connect(self.root)
        self.assertIsNone(con.execute("select 1 from outbox where kind=?",
                                      (ms.ВИД,)).fetchone(),
                          "и строки outbox тоже нет")

    def test_пустое_утро_уходит_без_кнопок(self):
        v = _волт()
        self.assertEqual(ms.run(self.root, v, self.env, now=СЕЙЧАС), "sent")
        _, поля = self.tg.зовы[0]
        self.assertIn("ничего", поля["text"])
        self.assertNotIn("reply_markup", поля)

    def test_отказ_телеграма_не_копится(self):
        self.tg.отказ = True
        v = _волт(КАРТОЧКА)
        self.assertEqual(ms.run(self.root, v, self.env, now=СЕЙЧАС), "failed")
        con = mi.connect(self.root)
        строка = con.execute("select state from outbox where kind=?",
                             (ms.ВИД,)).fetchone()
        self.assertIn(строка["state"], ("skipped", "failed"),
                      "утро, доехавшее к обеду, хуже никакого — не ретраится")


class Нажатия(unittest.TestCase):
    """Сквозной путь: callback → настоящий `contextd` → карточка в волте."""

    @classmethod
    def setUpClass(cls):
        cls.tg, cls.api = _телеграм()
        cls.root = tempfile.mkdtemp(prefix="mara-root-")
        cls.vault = _волт(КАРТОЧКА,
                          ("c.md", (("title", "перезвонить"), ("id", ИД2),
                                    ("status", "open"), ("due", ДЕНЬ))),
                          ("e.md", (("title", "вернуть самокат"), ("id", ИД3),
                                    ("status", "open"), ("due", ДЕНЬ))),
                          ("g.md", (("title", "полить цветы"), ("id", ИД4),
                                    ("status", "open"), ("due", ДЕНЬ))),
                          ("k.md", (("title", "сдать отчёт"), ("id", ИД5),
                                    ("status", "open"), ("due", ДЕНЬ))))
        cls.srv = contextd.make_server(cls.root, 0, vault=cls.vault)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        con = mi.connect(cls.root)
        dev, cls.token = contextd.pair(con, "кнопки")
        contextd.set_scopes(con, dev, ["correction"])
        cls.env = os.path.join(cls.root, "env")
        with open(cls.env, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_HOME_CHANNEL=424242\n"
                     "MARA_BUTTONS_BOT_TOKEN=btn-token\n"
                     "MARA_BUTTONS_TOKEN=%s\n" % cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tg.shutdown()

    def setUp(self):
        self.tg.зовы.clear()
        self.tg.пачки.clear()
        self.tg.отбитые.clear()

    def _нажатие(self, data, ид_кб="cb-1", от=424242, клава=None):
        return {"update_id": 5, "callback_query": {
            "id": ид_кб, "from": {"id": от}, "data": data,
            "message": {"message_id": 10, "chat": {"id": 424242},
                        "reply_markup": {"inline_keyboard": клава or []}}}}

    def _статус(self, имя):
        with open(os.path.join(self.vault, "kb/commitments", имя),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_сделал_правит_карточку_и_снимает_ряд(self):
        мой = [{"text": "1 сделал", "callback_data": "d:0000abcd:-"}]
        чужой = [{"text": "2 сделал", "callback_data": "d:0000beef:-"}]
        self.tg.пачки.append([self._нажатие("d:0000abcd:-", клава=[мой, чужой])])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            n = tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base)
        self.assertEqual(n, 1)
        self.assertIn("status: done", self._статус("a.md"))
        методы = [м for м, _ in self.tg.зовы]
        self.assertIn("answerCallbackQuery", методы)
        ответ = dict(self.tg.зовы)["answerCallbackQuery"]
        self.assertTrue(ответ["text"], "владельцу отвечают словами")
        self.assertEqual(ответ["_бот"], "btn-token",
                         "поллер ходит ботом кнопок, не ботом Hermes")
        правка = dict(self.tg.зовы)["editMessageReplyMarkup"]
        self.assertEqual(json.loads(правка["reply_markup"])["inline_keyboard"],
                         [чужой], "ряд применённого пункта снят, чужой остался")
        with open(os.path.join(self.root, "tg-buttons.offset")) as fh:
            self.assertGreaterEqual(int(fh.read()), 6,
                                    "смещение записано после обработки")
        # ключ дедупа — ровно id нажатия: повторная доставка того же update
        # (ниже) узнаётся по нему, а не по случайности
        con = mi.connect(self.root)
        self.assertTrue(con.execute(
            "select 1 from events where kind='correction' and source_id=?",
            ("tgbtn/cb-1",)).fetchone())
        # телеграм доставил то же нажатие снова: квитанция идемпотентности
        # (Т2.9) отдаёт сохранённый итог — ответ словами первого применения,
        # ряд снимается и здесь, второй правки и второго события нет
        событий = con.execute("select count(*) n from events").fetchone()["n"]
        self.tg.зовы.clear()
        self.tg.пачки.append([self._нажатие("d:0000abcd:-", клава=[мой])])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base)
        self.assertEqual(dict(self.tg.зовы)["answerCallbackQuery"]["text"], ответ["text"])
        self.assertIn("editMessageReplyMarkup", dict(self.tg.зовы))
        self.assertEqual(con.execute("select count(*) n from events").fetchone()["n"],
                         событий, "повтор не рождает второго события")

    def test_два_нажатия_одного_утра_не_воскрешают_ряд(self):
        """Оба callback'а несут один снимок клавиатуры: второй edit не
        возвращает ряд, снятый первым (Codex, круг 10)."""
        ряд1 = [{"text": "1 сделал", "callback_data": "d:0000abcd:-"}]
        ряд2 = [{"text": "2 сделал", "callback_data": "d:0000beef:-"}]
        клава = [ряд1, ряд2]
        self.tg.пачки.append([
            dict(self._нажатие("d:0000abcd:-", ид_кб="cb-40", клава=клава),
                 update_id=40),
            dict(self._нажатие("d:0000beef:-", ид_кб="cb-41", клава=клава),
                 update_id=41)])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base)
        правки = [поля for м, поля in self.tg.зовы if м == "editMessageReplyMarkup"]
        self.assertEqual(len(правки), 2)
        self.assertEqual(json.loads(правки[-1]["reply_markup"])["inline_keyboard"],
                         [], "второй edit не воскресил ряд первого")

    def test_завтра_переносит_срок(self):
        self.tg.пачки.append([self._нажатие("t:0000beef:-", ид_кб="cb-2")])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base,
                    now=СЕЙЧАС)
        self.assertIn("due: 2026-10-16", self._статус("c.md"))

    def test_вчерашняя_версия_кнопки_не_правит_слепо(self):
        """Карточка изменилась после отправки утра — нажатие со старой
        версией даёт конфликт, а не слепую правку (ADR-0003 §4.5)."""
        self.tg.пачки.append([self._нажатие("t:c0ffee11:-", ид_кб="cb-5")])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base,
                    now=СЕЙЧАС)
        con = mi.connect(self.root)
        версия = con.execute("select version from commitments where id=?",
                             (ИД3,)).fetchone()["version"]
        self.tg.зовы.clear()
        self.tg.пачки.append([self._нажатие("n:c0ffee11:%d" % (версия + 5),
                                            ид_кб="cb-6")])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base)
        self.assertIn("status: open", self._статус("e.md"),
                      "конфликт версии — карточка не тронута")
        self.assertTrue(dict(self.tg.зовы)["answerCallbackQuery"]["text"],
                        "владельцу отвечают словами конфликта")
        self.assertNotIn("editMessageReplyMarkup", dict(self.tg.зовы),
                         "непримёнённый пункт кнопок не теряет")

    def test_правка_рукой_после_утра_даёт_конфликт(self):
        """Файл правлен рукой после отправки утра: версия реестра совпадает,
        но она не об этом файле — конфликт, а не слепая правка (Codex, P2;
        обещание docs/morning-brief.md)."""
        # первая правка заводит строку реестра, шапка и строка сходятся
        self.tg.пачки.append([self._нажатие("t:deadbe11:-", ид_кб="cb-10")])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base,
                    now=СЕЙЧАС)
        self.assertIn("due: 2026-10-16", self._статус("g.md"))
        con = mi.connect(self.root)
        версия = con.execute("select version from commitments where id=?",
                             (ИД4,)).fetchone()["version"]
        # правка рукой в Obsidian: файл ушёл вперёд, перенос его не догнал
        путь = os.path.join(self.vault, "kb/commitments", "g.md")
        with open(путь, encoding="utf-8") as fh:
            текст = fh.read()
        with open(путь, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("status: open", "status: cancelled"))
        # кнопка со свежей версией реестра — всё равно конфликт
        self.tg.зовы.clear()
        ряд = [[{"text": "x", "callback_data": "d:deadbe11:%d" % версия}]]
        self.tg.пачки.append([self._нажатие("d:deadbe11:%d" % версия, ид_кб="cb-11",
                                            клава=ряд)])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base)
        self.assertIn("status: cancelled", self._статус("g.md"),
                      "ручная отмена не переписана кликом")
        ответ = dict(self.tg.зовы)["answerCallbackQuery"]["text"]
        self.assertIn("рукой", ответ)
        self.assertNotIn("editMessageReplyMarkup", dict(self.tg.зовы))

    def test_смена_времени_срока_рукой_тоже_дрейф(self):
        """Рукой поправлено только время срока: сравнение целиком это видит —
        старая кнопка даёт конфликт, а тревога дрейфа при повторном нажатии
        несёт свежее состояние файла (Codex, круг 8)."""
        self.tg.пачки.append([self._нажатие("t:feedca11:-", ид_кб="cb-20")])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base,
                    now=СЕЙЧАС)
        путь = os.path.join(self.vault, "kb/commitments", "k.md")
        con = mi.connect(self.root)
        версия = con.execute("select version from commitments where id=?",
                             (ИД5,)).fetchone()["version"]
        with open(путь, encoding="utf-8") as fh:
            текст = fh.read()
        with open(путь, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("due: 2026-10-16", "due: 2026-10-16T09:00"))
        self.tg.зовы.clear()
        self.tg.пачки.append([self._нажатие("t:feedca11:%d" % версия, ид_кб="cb-21")])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base,
                    now=СЕЙЧАС)
        self.assertIn("due: 2026-10-16T09:00", self._статус("k.md"),
                      "время, поставленное рукой, не перетёрто")
        self.assertIn("рукой", dict(self.tg.зовы)["answerCallbackQuery"]["text"])
        # файл уехал ещё раз — открытая тревога дрейфа несёт свежий current
        with open(путь, encoding="utf-8") as fh:
            текст = fh.read()
        with open(путь, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("status: open", "status: cancelled"))
        self.tg.пачки.append([self._нажатие("d:feedca11:%d" % версия, ид_кб="cb-22")])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base,
                    now=СЕЙЧАС)
        тревога = con.execute(
            "select detail_json from alerts where kind='version_conflict' and "
            "state='open' and object_id=?", (ИД5,)).fetchone()
        self.assertEqual(json.loads(тревога["detail_json"])["current"]["status"],
                         "cancelled", "ревью-очередь видит свежее состояние файла")

    def test_рассинхрон_не_душит_уже_так_и_заметку(self):
        """ADR-0003 п.5 при рассинхроне жив: кнопка в статус, который рукой
        уже поставлен, — «уже так»; чистая заметка — сливается, не конфликт
        (Codex, круг 5)."""
        путь = os.path.join(self.vault, "kb/commitments", "e.md")
        with open(путь, encoding="utf-8") as fh:
            текст = fh.read()
        if "status: done" not in текст:
            with open(путь, "w", encoding="utf-8") as fh:
                fh.write(текст.replace("status: open", "status: done"))
        con = mi.connect(self.root)
        e = {"MARA_BUTTONS_TOKEN": self.token}
        общее = {"source": "mara", "kind": "correction",
                 "occurred_at": mi.now_iso()}
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            # кнопка «сделал» по файлу, где рукой уже done: «уже так»
            код, тело = tb._в_доктор(e, dict(общее, source_id="ds-1", payload={
                "id": "c0ffee11", "status": "done", "expected_version": 1}),
                url=self.base)
            self.assertEqual(код, 200, тело)
            self.assertNotEqual(тело["applied"].get("error"), "version_conflict",
                                "правка без изменения — не конфликт")
            self.assertFalse(тело["applied"].get("applied"), тело["applied"])
            # чистая заметка коммутативна и при рассинхроне
            код, тело = tb._в_доктор(e, dict(общее, source_id="ds-2", payload={
                "id": "c0ffee11", "note": "созвон в среду",
                "expected_version": 1}), url=self.base)
            self.assertEqual(код, 200, тело)
            self.assertNotEqual(тело["applied"].get("error"), "version_conflict",
                                "заметка не трогает шапку — не конфликт")
        self.assertIn("созвон в среду", self._статус("e.md"))

    def test_адрес_демона_из_env_файла(self):
        """Крон окружения не несёт: MARA_CONTEXT_URL читается из того же
        env-файла, что токены, — нажатие доходит без параметра url
        (Codex, круг 11)."""
        env2 = os.path.join(self.root, "с-адресом.env")
        with open(self.env, encoding="utf-8") as fh:
            строки = fh.read()
        with open(env2, "w", encoding="utf-8") as fh:
            fh.write(строки + "MARA_CONTEXT_URL=%s\n" % self.base)
        con = mi.connect(self.root)
        было = con.execute("select count(*) n from events").fetchone()["n"]
        self.tg.пачки.append([dict(self._нажатие("d:0000abcd:-", ид_кб="cb-60"),
                                   update_id=60)])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            tb.цикл(tempfile.mkdtemp(), env2, секунд=0, api=self.api)
        self.assertEqual(con.execute("select count(*) n from events").fetchone()["n"],
                         было + 1, "нажатие дошло по адресу из env-файла")

    def test_смещение_ровно_за_обработанным(self):
        """Свой offset-файл, один update: смещение — ровно update_id+1
        (общий файл класса для точного замера не годится — его двигают
        соседние тесты)."""
        root2 = tempfile.mkdtemp(prefix="mara-offset-")
        self.tg.пачки.append([dict(self._нажатие("x", ид_кб="cb-50"),
                                   update_id=7)])
        tb.цикл(root2, self.env, секунд=0, api=self.api, url=self.base)
        with open(os.path.join(root2, "tg-buttons.offset")) as fh:
            self.assertEqual(fh.read(), "8")

    def test_чужое_нажатие_не_доезжает_до_doctor(self):
        con = mi.connect(self.root)
        было = con.execute("select count(*) n from events").fetchone()["n"]
        self.tg.пачки.append([self._нажатие("d:0000abcd:-", ид_кб="cb-3", от=777)])
        tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base)
        self.assertEqual(con.execute("select count(*) n from events").fetchone()["n"],
                         было, "чужая кнопка — не команда")
        self.assertIn("владельца", dict(self.tg.зовы)["answerCallbackQuery"]["text"])

    def test_кривые_данные_не_команда(self):
        con = mi.connect(self.root)
        было = con.execute("select count(*) n from events").fetchone()["n"]
        self.tg.пачки.append([self._нажатие("rm -rf /", ид_кб="cb-4")])
        tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base)
        self.assertEqual(con.execute("select count(*) n from events").fetchone()["n"],
                         было)
        self.assertIn("словами", dict(self.tg.зовы)["answerCallbackQuery"]["text"])

    def test_дубль_без_квитанции_не_врёт_успехом(self):
        """Итога первой доставки нет (`duplicate`, `applied: null`) —
        не «уже записала», и ряд остаётся: итог неизвестен (Codex, P2)."""
        e = {"TELEGRAM_BOT_TOKEN": "btn-token", "TELEGRAM_HOME_CHANNEL": "424242",
             "MARA_BUTTONS_TOKEN": "x"}
        зовы = []
        with mock.patch.object(tb, "_бот",
                               lambda e, метод, поля, api=None, timeout=30:
                               зовы.append((метод, поля))), \
             mock.patch.object(tb, "_в_доктор",
                               lambda e, событие, url=None:
                               (200, {"duplicate": True, "applied": None})):
            исход = tb.обработать(
                {"callback_query": {"id": "cb-9", "from": {"id": 424242},
                                    "data": "d:0000abcd:-",
                                    "message": {"message_id": 1,
                                                "chat": {"id": 424242},
                                                "reply_markup": {"inline_keyboard": [
                                                    [{"text": "x",
                                                      "callback_data": "d:0000abcd:-"}]]}}}},
                e)
        self.assertEqual(исход, "duplicate")
        методы = [м for м, _ in зовы]
        self.assertEqual(методы, ["answerCallbackQuery"], "ряд не тронут")
        self.assertNotIn("записала,", зовы[0][1]["text"])
        self.assertIn("итог", зовы[0][1]["text"])

    def test_уже_так_тоже_снимает_ряд(self):
        """Кнопка по карточке, где уже так: ряд снимается — иначе после
        сорвавшегося editMessageReplyMarkup каждое повторное нажатие рожало
        бы событие-пустышку и ело лимит часа (Codex, круг 7)."""
        e = {"TELEGRAM_BOT_TOKEN": "btn-token", "TELEGRAM_HOME_CHANNEL": "424242",
             "MARA_BUTTONS_TOKEN": "x"}
        зовы = []
        клава = [[{"text": "x", "callback_data": "d:0000abcd:-"}]]
        with mock.patch.object(tb, "_бот",
                               lambda e, метод, поля, api=None, timeout=30:
                               зовы.append((метод, поля))), \
             mock.patch.object(tb, "_в_доктор",
                               lambda e, событие, url=None:
                               (200, {"applied": {"found": True, "applied": False,
                                                  "text": "уже так"}})):
            исход = tb.обработать(
                {"callback_query": {"id": "cb-12", "from": {"id": 424242},
                                    "data": "d:0000abcd:-",
                                    "message": {"message_id": 1,
                                                "chat": {"id": 424242},
                                                "reply_markup": {
                                                    "inline_keyboard": клава}}}},
                e)
        self.assertEqual(исход, "applied")
        self.assertIn("editMessageReplyMarkup", [м for м, _ in зовы])

    def test_протухшее_нажатие_не_клинит_поллер(self):
        """Телеграм отвечает 400 «query is too old» на старый callback:
        всплывашка — best-effort, правка и смещение не страдают (ревью, P2)."""
        self.tg.отбитые.add("answerCallbackQuery")
        self.tg.пачки.append([self._нажатие("n:0000beef:-", ид_кб="cb-7",
                                            клава=[[{"text": "x",
                                                     "callback_data": "n:0000beef:-"}]])])
        with mock.patch.dict(os.environ, {"MARA_CORRECTIONS_PER_HOUR": "100"}):
            n = tb.цикл(self.root, self.env, секунд=0, api=self.api, url=self.base)
        self.assertEqual(n, 1, "цикл не упал на всплывашке")
        self.assertIn("status: cancelled", self._статус("c.md"))
        self.assertIn("editMessageReplyMarkup", dict(self.tg.зовы),
                      "ряд снят и без всплывашки")
        with open(os.path.join(self.root, "tg-buttons.offset")) as fh:
            self.assertGreaterEqual(int(fh.read()), 6, "смещение двигается")

    def test_нечитаемый_env_не_тихий(self):
        """«Файл есть, а крону не по правам» — не «не настроено»: строка в
        лог и ненулевой код, а не вечная тишина (Codex, круг 3)."""
        закрытый = os.path.join(self.root, "закрытый.env")
        with open(закрытый, "w", encoding="utf-8") as fh:
            fh.write("MARA_BUTTONS_TOKEN=x\n")
        os.chmod(закрытый, 0)
        self.addCleanup(os.chmod, закрытый, 0o600)
        if os.access(закрытый, os.R_OK):
            self.skipTest("под root права не держат")
        err = io.StringIO()
        с_переменными = {k: v for k, v in os.environ.items()
                         if not k.startswith("TELEGRAM") and k != "MARA_BUTTONS_TOKEN"}
        with mock.patch.dict(os.environ, с_переменными, clear=True), \
             contextlib.redirect_stderr(err):
            self.assertIsNone(tb.цикл(self.root, закрытый, секунд=0, api=self.api),
                              "поломка настройки — не «нечего делать»")
            # крон и сторож видят беду кодом выхода, не только строкой в логе
            self.assertEqual(tb.main(["--root", self.root, "--env-file", закрытый,
                                      "--seconds", "0"]), 1)
        self.assertIn("не читается", err.getvalue())
        self.assertEqual(self.tg.зовы, [])

    def test_нечитаемый_env_код_выхода(self):
        """То же, что выше, но без прав файла (под root они не держат):
        `цикл` — None, `main` — 1, видимые крону и сторожу."""
        есть = os.path.join(self.root, "есть.env")
        open(есть, "w").close()
        err = io.StringIO()
        with mock.patch.object(tb.os, "access", return_value=False), \
             contextlib.redirect_stderr(err):
            self.assertIsNone(tb.цикл(self.root, есть, секунд=0, api=self.api))
            self.assertEqual(tb.main(["--root", self.root, "--env-file", есть,
                                      "--seconds", "0"]), 1)
        self.assertIn("не читается", err.getvalue())

    def test_не_настроено_тихий_выход(self):
        пусто = os.path.join(self.root, "пусто.env")
        open(пусто, "w").close()
        с_переменными = {k: v for k, v in os.environ.items()
                         if not k.startswith("TELEGRAM")
                         and not k.startswith("MARA_BUTTONS")}
        with mock.patch.dict(os.environ, с_переменными, clear=True):
            self.assertEqual(tb.цикл(self.root, пусто, секунд=0, api=self.api), 0)
            # бот дайджестов без бота кнопок — тоже «не настроено»: его
            # очередь обновлений принадлежит Hermes
            свой = os.path.join(self.root, "digest-only.env")
            with open(свой, "w", encoding="utf-8") as fh:
                fh.write("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_HOME_CHANNEL=424242\n"
                         "MARA_BUTTONS_TOKEN=x\n")
            self.assertEqual(tb.цикл(self.root, свой, секунд=0, api=self.api), 0)
        self.assertEqual(self.tg.зовы, [], "без настройки в телеграм не ходим")


if __name__ == "__main__":
    unittest.main()
