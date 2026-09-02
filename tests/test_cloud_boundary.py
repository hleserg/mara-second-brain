"""Приватное не уезжает в облако (ТЗ §18, §20).

Тест ломает сборку, если карточка с `cloud_allowed: false` дошла до облачного
адаптера. Это тот самый тест, которого требует ТЗ §18. Фикстура намеренно
помечена `sensitive: false`: старая защита её пропускает, новая обязана
удержать — иначе поля из ТЗ §10 так и остались бы декларацией.
"""
import os, sys, unittest, importlib.util

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

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

class Пакет(unittest.TestCase):
    """Контекст-брокер — единственное место, где `cloud_allowed: false` частично
    ослаблен: критерий приёмки 6 требует, чтобы Мара знала обязательства без
    вызова инструмента. Наружу уезжает whitelist из пяти полей, и проверять это
    надо здесь, где границу будут искать (ТЗ §15)."""

    def test_из_карточки_наружу_едет_только_дистиллят(self):
        import tempfile, context_pack
        v = tempfile.mkdtemp()
        os.makedirs(os.path.join(v, "kb/commitments"))
        with open(os.path.join(v, "kb/commitments", "a.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("---\ntitle: прислать смету\nstatus: proposed\n"
                     "due: 2026-09-04\npromised_to: +79990000000\n"
                     "sensitive: true\ncloud_allowed: false\n"
                     "deadline_phrase: 'до пятницы, как договорились'\n---\n\n"
                     "- Обещание: прислать смету\n- Откуда: [[звонок]] · 04:12\n")
        text, _ = context_pack.собрать(v)
        self.assertIn("прислать смету", text, "иначе пакет бесполезен")
        self.assertIn("2026-09-04", text, "срок — часть дистиллята")
        for запрет in ("Обещание:", "Откуда:", "04:12", "как договорились",
                       "79990000000"):
            self.assertNotIn(запрет, text,
                             "%r уехал бы провайдеру модели" % запрет)


if __name__ == "__main__":
    unittest.main()
