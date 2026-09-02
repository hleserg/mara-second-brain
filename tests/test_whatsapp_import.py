"""Импортёр экспорта WhatsApp: ключ общий с телефоном, три формата, границы."""
import io
import os
import sys
import json
import unittest
import datetime
import urllib.error

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import whatsapp_import as w  # noqa: E402

МСК = datetime.timezone(datetime.timedelta(hours=3))


class Ключ(unittest.TestCase):
    def test_совпадает_с_фиксом_который_проверяет_kotlin(self):
        with open(os.path.join(ROOT, "tests", "fixtures", "whatsapp-message-id.json"), encoding="utf-8") as fh:
            f = json.load(fh)
        self.assertEqual(f["source_id"],
                         w.message_id(f["package"], f["chat"], f["sender"], f["text"], f["at_ms"]))

    def test_пробелы_схлопываются_nbsp_нет(self):
        a = w.message_id("p", "c", "s", " a \n\t b ", 0)
        self.assertEqual(a, w.message_id("p", "c", "s", "a b", 0))
        self.assertNotEqual(a, w.message_id("p", "c", "s", "a\u00a0b", 0))

    def test_минута_целым_числом_эпохи(self):
        self.assertEqual(w.message_id("p", "c", "s", "x", 1788347100000),
                         w.message_id("p", "c", "s", "x", 1788347100000 + 59_999))
        self.assertNotEqual(w.message_id("p", "c", "s", "x", 1788347100000),
                            w.message_id("p", "c", "s", "x", 1788347100000 + 60_000))


class Разбор(unittest.TestCase):
    def test_android_ru_с_продолжением_и_системной_строкой(self):
        got = w.parse(["02.09.26, 14:05 - Анна: Купи хлеб", "и молоко",
                       "02.09.26, 14:07 - Анна добавил(а) Петю", "хвост системной"], tz=МСК)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "Купи хлеб\nи молоко")
        self.assertEqual(got[0]["iso"], "2026-09-02T14:05:00+03:00")

    def test_android_en_am_pm(self):
        got = w.parse(["9/2/26, 2:05 PM - Anna: hi", "9/2/26, 12:01 AM - Anna: night"], tz=МСК)
        self.assertEqual([m["iso"] for m in got],
                         ["2026-09-02T14:05:00+03:00", "2026-09-02T00:01:00+03:00"])

    def test_ios_с_секундами_и_lrm(self):
        got = w.parse(["\u200e[02.09.26, 14:05:33] Анна: как дела: норм?"], tz=МСК)
        self.assertEqual(got[0]["sender"], "Анна")
        self.assertEqual(got[0]["text"], "как дела: норм?")
        self.assertEqual(got[0]["at_ms"] % 60000, 33000)

    def test_день_больше_двенадцати_не_месяц(self):
        self.assertTrue(w.parse(["13/2/26, 10:00 - A: x"], tz=МСК)[0]["iso"].startswith("2026-02-13"))

    def test_имя_чата_из_файла(self):
        self.assertEqual(w.chat_name("/x/Чат WhatsApp с Анна Петрова.txt"), "Анна Петрова")
        self.assertEqual(w.chat_name("WhatsApp Chat with Anna.zip"), "Anna")
        self.assertIsNone(w.chat_name("_chat.txt"), "iOS не подписывает — нужен --chat")


class События(unittest.TestCase):
    msgs = w.parse(["02.09.26, 14:05 - Анна: привет", "02.09.26, 14:06 - Сергей: ок",
                    "02.09.26, 14:07 - Петя: и я"], tz=МСК)

    def test_контракт_и_свои_исходящие(self):
        ev = list(w.events(self.msgs, "Семья", me="Сергей"))
        self.assertEqual(ev[0]["source"], "whatsapp")
        self.assertEqual(ev[0]["classification"], "personal")
        self.assertEqual(ev[0]["payload"]["chat_type"], "group")
        self.assertEqual(ev[0]["payload"]["via"], "export")
        self.assertTrue(ev[1]["payload"]["outgoing"])
        self.assertEqual(ev[1]["payload"]["sender_name"], "", "своё — без имени, как ответ из шторки на телефоне")
        self.assertEqual(len(ev[0]["source_id"]), 64)

    def test_личный_чат_когда_кроме_меня_один(self):
        ev = list(w.events(self.msgs[:2], "Анна", me="Сергей"))
        self.assertEqual(ev[0]["payload"]["chat_type"], "private")

    def test_отправка_считает_дубли_и_отвергнутое(self):
        seen = []

        def post(ev):
            seen.append(ev["source_id"])
            if ev["payload"]["text"] == "ок":
                raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b""))
            return {"duplicate": seen.count(ev["source_id"]) > 1}
        ev = list(w.events(self.msgs, "Семья"))
        n = w.send_all(post, ev + ev[:1], log=lambda *_: None, sleep=lambda *_: None)
        self.assertEqual(n, {"ok": 2, "dup": 1, "rejected": 1})

    def test_пример_env_без_значений(self):
        with open(os.path.join(ROOT, "install", "whatsapp.env.example"), encoding="utf-8") as fh:
            self.assertIn("MARA_CONTEXT_TOKEN=\n", fh.read())


if __name__ == "__main__":
    unittest.main()
