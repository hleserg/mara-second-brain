# -*- coding: utf-8 -*-
"""Сверка замечает, что пакет контекста перестал пересобираться.

Дыра ADR-0008 (решение 5): возраст пакета меряется только метрикой
`mara_context_pack_age_seconds`, а `/metrics` открыт с петли. Молчащий крон в
4:25 выглядел из сверки ровно как исправная система, и Мара всё это время
подавала вчерашний список обязательств как текущий.
"""
import os, sys, shutil, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import context_pack
import contextd_reconcile as rc


class ВозрастПакета(unittest.TestCase):
    def setUp(self):
        self.vault = tempfile.mkdtemp(prefix="mara-vault-")
        os.makedirs(os.path.join(self.vault, ".git"))
        os.makedirs(os.path.join(self.vault, "kb/commitments"))
        # Пакет кладёт настоящий писатель: проверка обязана смотреть на тот
        # файл, который появляется в бою, а не на выдуманный тестом путь.
        context_pack.build_now(self.vault)

    def tearDown(self):
        shutil.rmtree(self.vault, ignore_errors=True)

    def состарить(self, имя, часов):
        p = os.path.join(self.vault, context_pack.DIR, имя)
        когда = time.time() - часов * 3600
        os.utime(p, (когда, когда))

    def виды(self, находки):
        return [f["check"] for f in находки]

    def test_свежий_пакет_не_находка(self):
        self.assertEqual([], rc.пакет_устарел(self.vault))

    def test_сутки_с_небольшим_ещё_не_отказ(self):
        """28 часов — это ночь, пересобранная с опозданием, а не пропущенная.
        Вместе со следующим тестом порог зажат между 28 и 30 часами."""
        self.состарить("manifest.json", 28)
        self.assertEqual([], rc.пакет_устарел(self.vault))

    def test_пропущенная_ночь_даёт_отказ(self):
        self.состарить("manifest.json", 30)
        f = rc.пакет_устарел(self.vault)
        self.assertEqual(["пакет-устарел"], self.виды(f))
        self.assertEqual("error", f[0]["level"])
        # 30 ч — это 1.25 суток, и прошедшие с `utime` мгновения
        # округляют её вверх; ровно 1.25 не бывает.
        self.assertEqual(1.3, f[0]["days"])
        self.assertEqual(1, rc.код(f), "отказ обязан давать ненулевой код")

    def test_синк_basic_memory_не_гасит_находку(self):
        """`now.md` дописывает своим фронтматтером Basic Memory (ADR-0008,
        решение 7), и его синк двигает `mtime` в ночь, когда пересборки не
        было. Признак живости — манифест: у него писатель один."""
        self.состарить("manifest.json", 30)
        now = os.path.join(self.vault, context_pack.DIR, "now.md")
        os.utime(now, None)
        self.assertEqual(["пакет-устарел"],
                         self.виды(rc.пакет_устарел(self.vault)))

    def test_пакета_нет_это_ненастроенность(self):
        os.unlink(os.path.join(self.vault, context_pack.DIR, "manifest.json"))
        f = rc.пакет_устарел(self.vault)
        self.assertEqual(["пакет-не-собран"], self.виды(f))
        self.assertEqual("warn", f[0]["level"])
        self.assertEqual(0, rc.код(f), "ненастроенность крон криком не будит")

    def test_без_волта_проверка_молчит(self):
        """Самопроверка зовёт `run(con, root, vault=None)`, а на машине без
        волта каталога нет вовсе: ни то, ни другое не поломка пакета."""
        self.assertEqual([], rc.пакет_устарел(None))
        self.assertEqual([], rc.пакет_устарел(os.path.join(self.vault, "нет")))

    def test_проверка_проведена_в_сверку(self):
        """Находка, которую `run` не собирает, не доедет ни до крона, ни до
        сводки владельцу."""
        root = tempfile.mkdtemp(prefix="mara-root-")
        self.addCleanup(shutil.rmtree, root, True)
        con = mi.connect(root)
        self.addCleanup(con.close)
        self.состарить("manifest.json", 30)
        f = rc.run(con, root, vault=self.vault, targets=[])
        self.assertIn("пакет-устарел", self.виды(f))


if __name__ == "__main__":
    unittest.main()
