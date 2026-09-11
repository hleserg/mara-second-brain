# -*- coding: utf-8 -*-
"""Сверка замечает пропавший волт и не глохнет на упавшей проверке.

Две дыры из #39, у которых один корень: молчание выглядит как исправность.

Первая — волт. `лаг_индекса` и `пакет_устарел` на отсутствующем каталоге
возвращают пустой список: размонтированный том из сверки неотличим от
здоровой системы. А проектор в это время создаёт каталог заново на системном
диске и пишет карточки туда, и владелец открывает в Obsidian вчерашний слепок.

Вторая — сама сверка. Тринадцать проверок звались подряд без застав, и
исключение в третьей уносило десять оставшихся вместе с собой: цикл выходил
ненулевым кодом, но что именно не проверено, в сводке не было.
"""
import os, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import contextd_reconcile as rc


class ВолтПропал(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mara-root-")
        self.con = mi.connect(self.root)
        self.vault = tempfile.mkdtemp(prefix="mara-vault-")
        os.makedirs(os.path.join(self.vault, ".git"))
        for sub in rc.КАРТОЧКИ:
            os.makedirs(os.path.join(self.vault, sub))

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.vault, ignore_errors=True)

    def находки(self, vault):
        return {f["check"]: f for f in rc.run(self.con, self.root, vault=vault,
                                              bm_db=None, targets=[])}

    def test_целый_волт_молчит(self):
        self.assertNotIn("волт-пропал", self.находки(self.vault))

    def test_волта_нет_совсем(self):
        """Том размонтировали. Раньше сверка отвечала «проблем нет»."""
        shutil.rmtree(self.vault)
        f = self.находки(self.vault)
        self.assertIn("волт-пропал", f)
        self.assertEqual("error", f["волт-пропал"]["level"],
                         "пропавший волт — поломка, а не наблюдение: крон обязан "
                         "выйти ненулевым кодом")
        self.assertIn(self.vault, f["волт-пропал"]["detail"])

    def test_волт_есть_а_каталогов_карточек_нет(self):
        """Монтирование промахнулось: каталог создан, но пустой и не тот."""
        shutil.rmtree(os.path.join(self.vault, rc.КАРТОЧКИ[0]))
        f = self.находки(self.vault)
        self.assertIn("волт-пропал", f)
        self.assertEqual("error", f["волт-пропал"]["level"])
        self.assertIn(rc.КАРТОЧКИ[0], f["волт-пропал"]["detail"])

    def test_волт_не_задан_не_повод_кричать(self):
        """`vault=None` — законный режим сверки без волта вообще."""
        self.assertNotIn("волт-пропал", self.находки(None))


class ЗаставаНаКаждойПроверке(unittest.TestCase):
    """Упавшая проверка называет себя и не уносит остальные."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mara-root-")
        self.con = mi.connect(self.root)
        self.vault = tempfile.mkdtemp(prefix="mara-vault-")
        os.makedirs(os.path.join(self.vault, ".git"))
        for sub in rc.КАРТОЧКИ:
            os.makedirs(os.path.join(self.vault, sub))

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.vault, ignore_errors=True)

    def test_падение_одной_проверки_не_уносит_остальные(self):
        def взорваться(*a, **kw):
            raise RuntimeError("диск отвалился")

        было = rc.манифест_без_блоба
        rc.манифест_без_блоба = взорваться
        self.addCleanup(setattr, rc, "манифест_без_блоба", было)
        находки = rc.run(self.con, self.root, vault=self.vault, bm_db=None,
                         targets=[])
        имена = {f["check"] for f in находки}
        сломанная = [f for f in находки if f["check"].endswith("-не-проверена")]
        self.assertEqual(len(сломанная), 1, находки)
        self.assertIn("диск отвалился", сломанная[0]["detail"],
                      "текст отказа — единственное, что остаётся от причины: "
                      "трассы в reconcile.log нет")
        # Свидетель — последняя проверка цепочки: до неё не дошли бы, если
        # бы исключение улетело наверх. `сердцебиение` на свежем корне
        # находок не даёт вовсе, поэтому свидетелем быть не может.
        self.assertIn("бэкап-ядра-конфиг", имена,
                      "проверки после упавшей обязаны отработать")

    def test_падение_последней_проверки_тоже_названо(self):
        """Последняя в цепочке — единственный свидетель, что дошли до конца."""
        было = rc.бэкап_ядра
        rc.бэкап_ядра = lambda *a, **kw: (_ for _ in ()).throw(OSError("нет носителя"))
        self.addCleanup(setattr, rc, "бэкап_ядра", было)
        находки = rc.run(self.con, self.root, vault=self.vault, bm_db=None,
                         targets=[])
        сломанная = [f for f in находки if f["check"].endswith("-не-проверена")]
        self.assertEqual(len(сломанная), 1, находки)
        self.assertIn("нет носителя", сломанная[0]["detail"])


if __name__ == "__main__":
    unittest.main()
