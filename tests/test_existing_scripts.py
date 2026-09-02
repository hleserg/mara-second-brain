"""Новые типы видны старым скриптам — и невидимы там, где нельзя.

Дневная страница собирается локально, в неё звонки попасть обязаны. Дневная
сводка уезжает в OpenRouter, и приватный разговор туда попасть не должен ни
при каких обстоятельствах: `sensitive: true` на карточке разговора для того и
стоит. Это не недоделка, а граница (ТЗ §18).
"""
import os, sys, re, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def load(name):
    """Скрипты с дефисом в имени обычным import не берутся."""
    import importlib.util
    p = os.path.join(ROOT, "scripts", name)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_")[:-3], p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CARD = ("---\ntitle: Звонок · Анна · 14:05\ntype: conversation\nsource: phone\n"
        "created: 2026-09-02T14:31:00+03:00\noccurred: 2026-09-02\n"
        "sensitive: true\ncloud_allowed: false\n---\n\n"
        "## Попросили\n- прислать смету · 04:12\n\nЛюди: Анна\n")


def vault_with_call():
    v = tempfile.mkdtemp()
    for sub in ("kb/notes", "kb/sessions", "kb/conversations", "kb/commitments",
                "daily", ".git"):
        os.makedirs(os.path.join(v, sub))
    open(os.path.join(v, "kb/conversations/2026-09-02-1405-anna.md"), "w",
         encoding="utf-8").write(CARD)
    return v


class ДневнаяСтраница(unittest.TestCase):
    def test_разговор_попадает_в_день(self):
        dp = load("daily-page.py")
        days = dp.scan(vault_with_call())
        self.assertIn("2026-09-02", days)
        self.assertTrue(any("anna" in row[1] for row in days["2026-09-02"]),
                        "звонок должен появиться в оглавлении дня")

    def test_разговор_идёт_в_раздел_разговоры(self):
        dp = load("daily-page.py")
        rows = load("daily-page.py").scan(vault_with_call())["2026-09-02"]
        self.assertEqual(rows[0][0], dp.SECTIONS.index("Разговоры"))


class ДневнаяСводка(unittest.TestCase):
    def test_приватный_разговор_не_уезжает_в_облако(self):
        ds = load("daily-summary.py")
        v = vault_with_call()
        got = ds.cards(os.path.join(v, "kb/conversations"), "2026-09-02")
        self.assertEqual(got, [], "sensitive: true — материал для облака не берём")


class ПорядокПолей(unittest.TestCase):
    def test_новые_ключи_известны_миграции(self):
        fm = load("frontmatter-migrate.py")
        for key in ("domain", "classification", "storage_scope", "model_scope",
                    "cloud_allowed", "audience", "content_sha256",
                    "pipeline_version", "valid_from", "supersedes"):
            self.assertIn(key, fm.ORDER, "ключ %s уедет в хвост при миграции" % key)


class Линкер(unittest.TestCase):
    def test_карточки_разговоров_попадают_в_обход(self):
        el = load("entity-link.py")
        found = list(el.cards(vault_with_call()))
        self.assertTrue(any("conversations" in p for p, _, _ in found),
                        "линкер обходит kb рекурсивно, разговоры входят")


if __name__ == "__main__":
    unittest.main()
