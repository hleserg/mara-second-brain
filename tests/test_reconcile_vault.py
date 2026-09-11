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
import os, shutil, sqlite3, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import contextd_reconcile as rc


class ВолтПропал(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mara-root-")
        self.con = mi.connect(self.root)
        self.vault = tempfile.mkdtemp(prefix="mara-vault-")
        for sub in rc.СКЕЛЕТ:
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

    def test_волт_есть_а_скелета_нет(self):
        """Монтирование промахнулось: каталог создан, но это не наш волт."""
        shutil.rmtree(os.path.join(self.vault, rc.СКЕЛЕТ[1]))
        f = self.находки(self.vault)
        self.assertIn("волт-неполон", f)
        self.assertEqual("error", f["волт-неполон"]["level"])
        self.assertIn(rc.СКЕЛЕТ[1], f["волт-неполон"]["detail"])

    def test_свежая_установка_без_карточек_молчит(self):
        """Свидетелем нельзя брать то, что создаёт проектор.

        Круг 1 ревью PR #90: первая редакция проверки искала
        `kb/conversations` и `kb/commitments`. Их заводит лениво
        `call_project.py` по первой карточке, а установщик
        (`install/stage0-doctor.sh:17-18`) не заводит вовсе — на живом волте
        doctor 2026-09-11 обоих нет, звонков ещё не было. Проверка на них
        сыпала бы `error` каждый час на здоровой системе, и владелец
        научился бы не читать `error`.
        """
        for sub in ("kb/conversations", "kb/commitments"):
            self.assertFalse(os.path.isdir(os.path.join(self.vault, sub)),
                             "скелет установщика карточек не содержит")
        f = self.находки(self.vault)
        self.assertNotIn("волт-неполон", f)
        self.assertNotIn("волт-пропал", f)

    def test_промах_монтирования_не_гаснет_от_карточек(self):
        """Обратная сторона того же: детект не смеет гаситься сам.

        На промахнувшемся монтировании проектор создаёт карточки заново. Если
        свидетель — они, окно детекта равно одной проекции: появился
        `kb/conversations` — находка сузилась, появился `kb/commitments` —
        замолчала совсем. Скелет установщика проектор не создаёт никогда.
        """
        чужой = tempfile.mkdtemp(prefix="mara-not-vault-")
        self.addCleanup(shutil.rmtree, чужой, True)
        for sub in ("kb/conversations", "kb/commitments"):
            os.makedirs(os.path.join(чужой, sub))
        f = self.находки(чужой)
        self.assertIn("волт-неполон", f)
        self.assertEqual(sorted(f["волт-неполон"]["missing"]),
                         sorted(rc.СКЕЛЕТ), "карточки скелет не заменяют")

    def test_волт_не_задан_не_повод_кричать(self):
        """`vault=None` — законный режим сверки без волта вообще."""
        self.assertNotIn("волт-пропал", self.находки(None))


class ЗаставаНаКаждойПроверке(unittest.TestCase):
    """Упавшая проверка называет себя и не уносит остальные."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mara-root-")
        self.con = mi.connect(self.root)
        self.vault = tempfile.mkdtemp(prefix="mara-vault-")
        for sub in rc.СКЕЛЕТ:
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
        # последняя в цепочке докладывает, что до неё дошли
        хвост = rc.бэкап_ядра
        rc.бэкап_ядра = lambda *a, **kw: [rc.находка("дошли-до-конца", "warn", "сторож")]
        self.addCleanup(setattr, rc, "бэкап_ядра", хвост)
        находки = rc.run(self.con, self.root, vault=self.vault, bm_db=None,
                         targets=[])
        имена = {f["check"] for f in находки}
        сломанная = [f for f in находки if f["check"].endswith("-упала")]
        self.assertEqual(len(сломанная), 1, находки)
        self.assertIn("диск отвалился", сломанная[0]["detail"],
                      "текст отказа — единственное, что остаётся от причины: "
                      "трассы в reconcile.log нет")
        # Свидетель — подмена последней проверки, а не её побочный эффект:
        # `бэкап-ядра-конфиг` появлялся лишь потому, что `targets=[]` — решат
        # однажды, что пустой список носителей не `error`, и тест позеленел бы
        # при снятых заставах (круг 1 ревью PR #90, п.11).
        self.assertIn("дошли-до-конца", имена,
                      "проверки после упавшей обязаны отработать")

    def test_падение_последней_проверки_тоже_названо(self):
        """Последняя в цепочке — единственный свидетель, что дошли до конца."""
        было = rc.бэкап_ядра
        rc.бэкап_ядра = lambda *a, **kw: (_ for _ in ()).throw(OSError("нет носителя"))
        self.addCleanup(setattr, rc, "бэкап_ядра", было)
        находки = rc.run(self.con, self.root, vault=self.vault, bm_db=None,
                         targets=[])
        сломанная = [f for f in находки if f["check"].endswith("-упала")]
        self.assertEqual(len(сломанная), 1, находки)
        self.assertIn("нет носителя", сломанная[0]["detail"])


class КарточкиПропали(unittest.TestCase):
    """Волт смонтирован, скелет на месте, а карточек в нём больше нет.

    `лаг_индекса` сравнивал только в одну сторону (`свои - видит`), и волт, из
    которого карточки исчезли — промах rsync, откат git, переименованный
    каталог, — из сверки был неотличим от здорового: `if not свои: return []`.
    То же молчание, от которого написан `волт_пропал`, только уровнем ниже
    (круг 1 ревью PR #90, п.3). Свидетель был под рукой: база Basic Memory
    всё ещё помнит файлы, которых нет.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mara-root-")
        self.con = mi.connect(self.root)
        self.vault = tempfile.mkdtemp(prefix="mara-vault-")
        for sub in rc.СКЕЛЕТ:
            os.makedirs(os.path.join(self.vault, sub))
        os.makedirs(os.path.join(self.vault, "kb/conversations"))
        self.bm = os.path.join(self.root, "memory.db")
        c = sqlite3.connect(self.bm)
        c.execute("create table entity(file_path text)")
        c.executemany("insert into entity values(?)",
                      [("kb/conversations/a.md",), ("kb/conversations/b.md",)])
        c.commit()
        c.close()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.vault, ignore_errors=True)

    def находки(self):
        return {f["check"]: f for f in rc.run(self.con, self.root,
                                              vault=self.vault, bm_db=self.bm,
                                              targets=[])}

    def карточка(self, имя):
        open(os.path.join(self.vault, "kb/conversations", имя), "w").close()

    def test_обе_карточки_на_месте_молчит(self):
        self.карточка("a.md")
        self.карточка("b.md")
        self.assertNotIn("карточки-пропали", self.находки())

    def test_исчезли_все_карточки(self):
        """Раньше это был самый тихий отказ: `свои` пусто — выход без находок."""
        f = self.находки()
        self.assertIn("карточки-пропали", f)
        self.assertEqual(f["карточки-пропали"]["count"], 2)

    def test_исчезла_одна(self):
        self.карточка("a.md")
        f = self.находки()
        self.assertEqual(f["карточки-пропали"]["count"], 1)
        self.assertIn("kb/conversations/b.md", f["карточки-пропали"]["sample"])

    def test_чужие_записи_базы_не_считаются(self):
        """Basic Memory индексирует весь волт. Пропажу считаем только по
        карточкам: `kb/notes/…` и `daily/…` живут своей жизнью и удаляются
        владельцем без всякой поломки."""
        self.карточка("a.md")
        self.карточка("b.md")
        c = sqlite3.connect(self.bm)
        c.execute("insert into entity values('kb/notes/что-то-удалённое.md')")
        c.commit()
        c.close()
        self.assertNotIn("карточки-пропали", self.находки())

    def база(self, *строки):
        c = sqlite3.connect(self.bm)
        c.execute("delete from entity")
        c.executemany("insert into entity values(?)", [(x,) for x in строки])
        c.commit()
        c.close()

    def test_карточка_в_подкаталоге_не_числится_пропавшей(self):
        """Проектор кладёт плоско, но обход волта всё равно рекурсивный:
        пока сравнение шло в одну сторону, нерекурсивный `glob` прятал
        находку, а с обратной стороной он бы её выдумывал — и вечно."""
        os.makedirs(os.path.join(self.vault, "kb/conversations/2026/09"))
        путь = "kb/conversations/2026/09/анна.md"
        open(os.path.join(self.vault, путь), "w").close()
        self.база(путь)
        self.assertNotIn("карточки-пропали", self.находки())

    def test_не_markdown_в_каталоге_карточек_не_находка(self):
        """Перечисляем `*.md` — значит и спрашиваем только про них.
        Иначе любой индексируемый `.canvas` или вложение станет вечной
        находкой, которую нечем закрыть."""
        self.карточка("a.md")
        self.база("kb/conversations/a.md", "kb/conversations/схема.canvas")
        self.assertNotIn("карточки-пропали", self.находки())

    def test_пустой_путь_в_базе_не_роняет_проверку(self):
        """`file_path is null` до появления обратной стороны был безвреден:
        `None` только вычитался. Теперь он идёт в `startswith` и роняет
        `лаг_индекса` целиком — вместе с уже посчитанной находкой
        `лаг-индекса`, которую застава не спасает."""
        self.база(None, "kb/conversations/b.md")
        self.карточка("живая.md")
        f = self.находки()
        self.assertNotIn("индекс-упала", f)
        self.assertEqual(f["карточки-пропали"]["count"], 1)
        self.assertEqual(f["лаг-индекса"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
