"""Приватное не уезжает в облако (ТЗ §18, §20).

Тест ломает сборку, если карточка с `cloud_allowed: false` дошла до облачного
адаптера. Это тот самый тест, которого требует ТЗ §18. Фикстура намеренно
помечена `sensitive: false`: старая защита её пропускает, новая обязана
удержать — иначе поля из ТЗ §10 так и остались бы декларацией.
"""
import os, sys, unittest, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))

def load_queue_worker():
    """queue-worker.py с дефисом в имени, обычным import не берётся."""
    p = os.path.join(HERE, "..", "scripts", "queue-worker.py")
    spec = importlib.util.spec_from_file_location("queue_worker", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class CloudBoundary(unittest.TestCase):
    def setUp(self):
        self.qw = load_queue_worker()
        self.head = open(os.path.join(HERE, "fixtures", "private-call.md"),
                         encoding="utf-8").read()

    def test_cloud_allowed_false_держит_карточку(self):
        self.assertIsNotNone(self.qw.holds_from_cloud(self.head))

    def test_sensitive_true_держит_карточку(self):
        self.assertIsNotNone(self.qw.holds_from_cloud("---\nsensitive: true\n---\n"))

    def test_model_scope_local_only_держит_карточку(self):
        self.assertIsNotNone(self.qw.holds_from_cloud("---\nmodel_scope: local-only\n---\n"))

    def test_обычная_карточка_проходит(self):
        self.assertIsNone(self.qw.holds_from_cloud("---\ntitle: заметка\n---\nтекст"))

if __name__ == "__main__":
    unittest.main()
