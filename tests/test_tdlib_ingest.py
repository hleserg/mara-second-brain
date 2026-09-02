"""Разбор апдейтов TDLib в события contextd (ТЗ §11) — без библиотеки и без сети."""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import tdlib_ingest as ti

ЧАТ = {"id": 7, "title": "Анна", "type": {"@type": "chatTypePrivate", "user_id": 42}}


def msg(mid=1, text="привет", **kw):
    d = {"@type": "message", "id": mid, "chat_id": 7, "date": 1756800000, "is_outgoing": False,
         "sender_id": {"@type": "messageSenderUser", "user_id": 42},
         "content": {"@type": "messageText", "text": {"@type": "formattedText", "text": text}}}
    d.update(kw)
    return d


class Разбор(unittest.TestCase):
    def test_новое_сообщение(self):
        ev = ti.message_event(msg(), ЧАТ, "private", "Анна Петрова")
        self.assertEqual(ev["source"], "telegram")
        self.assertEqual(ev["source_id"], "7/1", "идемпотентность по (chat_id, message_id)")
        self.assertEqual(ev["classification"], "personal", "личное и группы — local-only по умолчанию")
        self.assertEqual(ev["payload"]["text"], "привет")
        self.assertEqual(ev["payload"]["media"], [])
        self.assertIsNone(ev["payload"]["reply_to"])

    def test_ответ_и_тред_сохраняются(self):
        ev = ti.message_event(msg(reply_to={"@type": "messageReplyToMessage", "chat_id": 7, "message_id": 5},
                                  message_thread_id=5), ЧАТ, "private", "Анна")
        self.assertEqual(ev["payload"]["reply_to"], 5)
        self.assertEqual(ev["payload"]["thread_id"], 5)

    def test_правка_отдельное_событие(self):
        ev = ti.revision_event(msg(text="в шесть", edit_date=1756800600), ЧАТ, "private", "Анна")
        self.assertEqual(ev["source_id"], "7/1/edit/1756800600", "тот же ключ дедуп бы выбросил")
        self.assertEqual(ev["payload"]["revision_of"], "7/1")
        self.assertEqual(ev["payload"]["text"], "в шесть")

    def test_удаление_из_кэша_не_удаление(self):
        self.assertEqual(ti.tombstones({"chat_id": 7, "message_ids": [1], "is_permanent": True,
                                        "from_cache": True}), [])
        self.assertEqual(ti.tombstones({"chat_id": 7, "message_ids": [1], "is_permanent": False,
                                        "from_cache": False}), [])
        t = ti.tombstones({"chat_id": 7, "message_ids": [1, 2], "is_permanent": True, "from_cache": False})
        self.assertEqual([x["source_id"] for x in t], ["7/1/deleted", "7/2/deleted"])
        self.assertEqual(t[0]["payload"]["tombstone_of"], "7/1")

    def test_бот_и_канал_пропускаются(self):
        бот = {"type": {"@type": "userTypeBot"}}
        self.assertEqual(ti.chat_kind(ЧАТ, бот), "bot")
        self.assertEqual(ti.chat_kind(ЧАТ, {"type": {"@type": "userTypeRegular"}}), "private")
        self.assertEqual(ti.chat_kind({"type": {"@type": "chatTypeSupergroup", "is_channel": True}}), "channel")
        self.assertEqual(ti.chat_kind({"type": {"@type": "chatTypeSupergroup", "is_channel": False}}), "group")
        self.assertEqual(ti.chat_kind({"type": {"@type": "chatTypeBasicGroup"}}), "group")
        self.assertIsNone(ti.message_event(msg(), ЧАТ, "bot", "Мара"))
        self.assertIsNone(ti.message_event(msg(), ЧАТ, "channel", ""))

    def test_вложение_метаданные_без_тела(self):
        doc = {"@type": "messageDocument", "caption": {"@type": "formattedText", "text": "смета"},
               "document": {"@type": "document", "file_name": "smeta.pdf", "mime_type": "application/pdf",
                            "document": {"@type": "file", "id": 3, "size": 12345,
                                         "local": {"path": "/секретный/путь"}}}}
        ev = ti.message_event(msg(content=doc), ЧАТ, "private", "Анна")
        self.assertEqual(ev["payload"]["text"], "смета")
        self.assertEqual(ev["payload"]["media"],
                         [{"type": "document", "mime": "application/pdf", "name": "smeta.pdf", "size": 12345}])
        voice = {"@type": "messageVoiceNote",
                 "voice_note": {"@type": "voiceNote", "duration": 7, "mime_type": "audio/ogg",
                                "voice": {"@type": "file", "id": 4, "size": 900}}}
        ev = ti.message_event(msg(content=voice), ЧАТ, "private", "Анна")
        self.assertEqual(ev["payload"]["media"][0]["type"], "voice_note")
        self.assertEqual(ev["payload"]["media"][0]["duration"], 7)

    def test_каталог_состояния_вне_волта(self):
        """ТЗ §11, §18: база TDLib не в волте и не в репо — иначе уедет в R2 или git."""
        home = ti.state_dir("/srv/mara-blobs")
        репо = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        for чужое in ("/srv/vault", репо):
            self.assertNotEqual(os.path.commonpath([home, чужое]), чужое, home)


if __name__ == "__main__":
    unittest.main()
