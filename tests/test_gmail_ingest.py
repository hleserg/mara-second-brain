"""Разбор письма и контракт события Gmail (спека 7, ТЗ §12) — без Google."""
import os, sys, json, base64, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import gmail_ingest as g


def b64(s, cs="utf-8"):
    return base64.urlsafe_b64encode(s.encode(cs)).rstrip(b"=").decode()


def письмо(**kw):
    return g._msg("m1", **kw)


class Разбор(unittest.TestCase):

    def test_новое_письмо_контракт(self):
        ev = g.email_event(письмо(text="приду в шесть", html="<p>приду в шесть</p>", att="счёт.pdf"))
        self.assertEqual((ev["source"], ev["source_id"], ev["classification"]), ("gmail", "m1", "personal"))
        p = ev["payload"]
        self.assertEqual(p["thread_id"], "tm1")
        self.assertEqual(p["subject"], "Тема m1")
        self.assertEqual(p["text"], "приду в шесть")
        self.assertTrue(p["has_html"])
        self.assertFalse(p["outgoing"])
        self.assertEqual(p["rfc_message_id"], "<m1@example.com>")
        self.assertEqual(ev["occurred_at"], g.iso(1756800000))

    def test_вложение_метаданные_без_тела(self):
        p = g.email_event(письмо(att="скан.pdf"))["payload"]
        self.assertEqual(p["attachments"], [{"name": "скан.pdf", "mime": "application/pdf",
                                            "size": 12345, "attachment_id": "att1"}])
        self.assertNotIn("data", json.dumps(p["attachments"]))

    def test_html_когда_нет_plain(self):
        m = письмо()
        m["payload"]["parts"] = [{"mimeType": "text/html",
                                  "body": {"data": b64("<div>Привет,<br>до <b>пятницы</b></div><style>p{}</style>")}}]
        self.assertEqual(g.email_event(m)["payload"]["text"], "Привет,\nдо пятницы")

    def test_кодировка_из_заголовка_части(self):
        m = письмо()
        m["payload"]["parts"] = [{"mimeType": "text/plain",
                                  "headers": [{"name": "Content-Type", "value": "text/plain; charset=windows-1251"}],
                                  "body": {"data": b64("Договорились", "cp1251")}}]
        self.assertEqual(g.email_event(m)["payload"]["text"], "Договорились")

    def test_исходящее_по_ярлыку(self):
        self.assertTrue(g.email_event(письмо(labels=("SENT",)))["payload"]["outgoing"])

    def test_спам_корзина_черновик_пропускаются(self):
        for l in ("SPAM", "TRASH", "DRAFT"):
            self.assertIsNone(g.email_event(письмо(labels=("INBOX", l))), l)

    def test_корзина_это_ревизия_а_не_потеря(self):
        ev = g.revision_event(письмо(labels=("TRASH",)), "555")
        self.assertEqual(ev["source_id"], "m1/labels/555")
        self.assertEqual(ev["payload"]["revision_of"], "m1")
        self.assertTrue(ev["payload"]["trashed"])
        self.assertEqual(ev["payload"]["subject"], "Тема m1", "ревизия несёт полное письмо")

    def test_надгробие(self):
        ev = g.tombstone("m1", "556", now=lambda: 0)
        self.assertEqual(ev["source_id"], "m1/deleted")
        self.assertEqual(ev["payload"]["tombstone_of"], "m1")

    def test_тело_режется_полное_в_raw(self):
        p = g.email_event(письмо(text="я" * (g.ТЕКСТ + 10)))["payload"]
        self.assertEqual(len(p["text"]), g.ТЕКСТ)


class Граница(unittest.TestCase):

    def test_только_личный_ящик(self):
        self.assertTrue(g.личный("me@gmail.com"))
        self.assertTrue(g.личный("Me@GoogleMail.com"))
        self.assertFalse(g.личный("me@example.com"), "домен компании — рабочая почта, ТЗ §12")
        self.assertFalse(g.личный(None))

    def test_состояние_вне_волта_и_репо(self):
        home = g.state_dir()
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        for корень in ("/srv/vault", repo):
            self.assertNotEqual(os.path.commonpath([home, корень]), корень, home)

    def test_в_примере_env_нет_секретов(self):
        p = os.path.join(os.path.dirname(__file__), "..", "install", "gmail.env.example")
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if "=" in line and not line.startswith("#"):
                    self.assertEqual(line.split("=", 1)[1].strip(), "", line)

    def test_scope_только_чтение(self):
        self.assertTrue(g.SCOPE.endswith("gmail.readonly"))


if __name__ == "__main__":
    unittest.main()
