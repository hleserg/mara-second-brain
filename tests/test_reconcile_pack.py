# -*- coding: utf-8 -*-
"""Сверка замечает, что пакет контекста перестал пересобираться.

Дыра ADR-0008 (решение 5): возраст пакета меряется только метрикой
`mara_context_pack_age_seconds`, а `/metrics` открыт с петли. Молчащий крон в
4:25 выглядел из сверки ровно как исправная система, и Мара всё это время
подавала вчерашний список обязательств как текущий.
"""
import importlib, io, os, sys, shutil, tempfile, time, unittest
import unittest.mock

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
        # atime — сегодняшний, и это не украшение: с `os.utime(p, (когда,
        # когда))` оба поля равны, и подмена `st_mtime` на `st_atime` в
        # проверке проходит мимо всех тестов. А на doctor эти поля не равны —
        # `now_pack` открывает манифест на каждом `/v1/context/bootstrap`,
        # и при `relatime` возраст по atime не выйдет за сутки никогда.
        p = os.path.join(self.vault, context_pack.DIR, имя)
        когда = time.time() - часов * 3600
        os.utime(p, (time.time(), когда))

    def виды(self, находки):
        return [f["check"] for f in находки]

    def test_свежий_пакет_не_находка(self):
        self.assertEqual([], rc.пакет_устарел(self.vault))

    def test_здоровый_максимум_молчит(self):
        """Здоровая система доходит до 23.7 ч: это прогон сверки в 4:07,
        последний перед пересборкой в 4:25. 26 часов — уже с запасом за ним,
        и всё ещё молчание: порог обязан терпеть пересборку, опоздавшую
        на пару часов."""
        self.состарить("manifest.json", 26)
        self.assertEqual([], rc.пакет_устарел(self.vault))

    def test_к_сводке_в_8_00_после_пропущенной_ночи_находка_есть(self):
        """Вся наблюдаемая владельцем поверхность — сводка в 8:00; лог
        часовых прогонов не читает никто. От пересборки в 4:25 до сводки
        3 ч 35 мин, значит после пропущенной ночи к сводке возрасту 27.58 ч.
        Не звенит здесь — владелец узнаёт вторыми сутками, и порог тогда
        не лучше бэкапного."""
        self.состарить("manifest.json", 27.58)
        self.assertEqual(["пакет-устарел"],
                         self.виды(rc.пакет_устарел(self.vault)))

    def test_пропущенная_ночь_даёт_отказ(self):
        self.состарить("manifest.json", 30)
        f = rc.пакет_устарел(self.vault)
        self.assertEqual(["пакет-устарел"], self.виды(f))
        self.assertEqual("error", f[0]["level"])
        # 30 ч — это 1.25 суток, и прошедшие с `utime` мгновения
        # округляют её вверх; ровно 1.25 не бывает.
        self.assertEqual(1.3, f[0]["days"])
        # Владелец прочитает не поле, а строку: без этого «%.1f» спокойно
        # мутирует в «%.0f», и в сводке будет «не пересобирался 1 сут.».
        self.assertIn("1.3 сут", f[0]["detail"])
        self.assertEqual(1, rc.код(f), "отказ обязан давать ненулевой код")

    def test_чужая_правка_now_md_не_гасит_находку(self):
        """У `now.md` второй писатель — Basic Memory возвращает туда свой
        фронтматтер (ADR-0008, решение 7), и когда он тронет файл, нам знать
        неоткуда. Признак живости берём с манифеста: у него писатель один,
        и чужая правка `now.md` находку не гасит."""
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

    def test_нечитаемый_каталог_не_врёт_про_сборку(self):
        """Права сняты — файл есть, но не читается. Совет «запустить
        сборку» был бы враньём, а падение унесло бы всю сверку."""
        if os.geteuid() == 0:
            self.skipTest("под root права не запрещают ничего")
        d = os.path.join(self.vault, context_pack.DIR)
        os.chmod(d, 0)
        try:
            f = rc.пакет_устарел(self.vault)
        finally:
            # Права — до `tearDown`, а не через `addCleanup`: тот бежит
            # после, и `shutil.rmtree` уже не снёс бы каталог с правами 0.
            os.chmod(d, 0o755)
        self.assertEqual(["пакет-не-прочитан"], self.виды(f))
        self.assertEqual("warn", f[0]["level"])
        self.assertNotIn("запустить", f[0]["detail"])

    def test_сломанный_импорт_не_роняет_сверку(self):
        """`run` зовёт проверки подряд и без try: исключение отсюда унесло
        бы семь проверок ниже — вместе с §12 и сводкой в 8:00. Сломать импорт
        можно не только правкой нашего файла: `context_pack` на импорте читает
        с диска `mara-brief.py`, а сверка в кроне — отдельный процесс, где
        импорт каждый час первый."""
        root = tempfile.mkdtemp(prefix="mara-root-")
        self.addCleanup(shutil.rmtree, root, True)
        con = mi.connect(root)
        self.addCleanup(con.close)
        # Ломаем так, как ломается в бою. `sys.modules[...] = None` дало бы
        # `ImportError` — единственный класс, которого живой отказ как раз и
        # не даёт: `context_pack` исполняет `mara-brief.py` с диска, и оттуда
        # прилетает `SyntaxError`, `FileNotFoundError` или что угодно из тела
        # модуля. Оракул на `ImportError` закреплял бы не ту заставу: сужение
        # `except Exception` до `except ImportError` он бы пропустил.
        подмена = tempfile.mkdtemp(prefix="mara-подмена-")
        self.addCleanup(shutil.rmtree, подмена, True)
        io.open(os.path.join(подмена, "context_pack.py"), "w",
                encoding="utf-8").write(
                    'raise RuntimeError("mara-brief.py битый")\n')
        self.addCleanup(sys.modules.__setitem__, "context_pack",
                        sys.modules["context_pack"])
        del sys.modules["context_pack"]
        sys.path.insert(0, подмена)
        self.addCleanup(sys.path.remove, подмена)
        importlib.invalidate_caches()
        f = rc.run(con, root, vault=self.vault, targets=[])
        находки = {x["check"]: x for x in f}
        self.assertIn("пакет-не-проверен", находки)
        # Уровень — решение, а не мелочь: `код()` даёт 0, то есть крон молчит,
        # и находка живёт только сводкой. Тем же ходом идёт `лаг_индекса` на
        # `sqlite3.Error`.
        self.assertEqual("warn", находки["пакет-не-проверен"]["level"])
        # Текст отказа — единственное, что от причины остаётся: `except
        # Exception` трассу уже проглотил, в `reconcile.log` её нет. Без
        # него опечатка в env, пропавший файл и синтаксическая ошибка
        # выглядят в сводке одинаково. Выброшенный ``%s` с `% e`` полный гейт
        # проходил зелёным — проверено.
        self.assertIn("битый", находки["пакет-не-проверен"]["detail"])
        # Префикс — единственная атрибуция, которая доезжает до владельца:
        # `текст()` кладёт в сводку только `detail`, имя проверки остаётся в
        # логе. Без префикса в 8:00 приходит голое сообщение исключения, и
        # какая из тринадцати проверок это сказала — прочесть негде. Снятие
        # префикса полный гейт проходило зелёным; нашёл ревьюер, круг 5.
        self.assertIn("не запустилась", находки["пакет-не-проверен"]["detail"])
        # Свидетель — последняя проверка в `run`: не дошли бы до неё, если бы
        # исключение улетело наверх.
        self.assertIn("бэкап-ядра-конфиг", находки)

    def test_сломанный_blob_retention_не_роняет_сверку(self):
        """Тот же отказ у второго ленивого импорта — `blob_retention`.

        Застава стояла у пакета и не стояла у соседа тридцатью строками ниже:
        ровно та половинчатая правка, за которую PR #48 и ругал ADR-0008.
        Раньше здесь ломала живая опечатка в `MARA_RAW_DAYS`; теперь её ловит
        сам `blob_retention`, и заставе нужен отказ, который не лечится в
        источнике. Подменный модуль его и даёт: файл читается с диска, и
        `RuntimeError` из тела — такой же честный отказ, как `SyntaxError` в
        `mara-brief.py` у соседа выше. Ширину `except` это держит по-прежнему:
        мимо `except ImportError` `RuntimeError` пролетел бы."""
        root = tempfile.mkdtemp(prefix="mara-root-")
        self.addCleanup(shutil.rmtree, root, True)
        con = mi.connect(root)
        self.addCleanup(con.close)
        подмена = tempfile.mkdtemp(prefix="mara-подмена-")
        self.addCleanup(shutil.rmtree, подмена, True)
        io.open(os.path.join(подмена, "blob_retention.py"), "w",
                encoding="utf-8").write(
                    'raise RuntimeError("blob_retention битый")\n')
        import blob_retention
        self.addCleanup(sys.modules.__setitem__, "blob_retention",
                        blob_retention)
        del sys.modules["blob_retention"]
        sys.path.insert(0, подмена)
        self.addCleanup(sys.path.remove, подмена)
        importlib.invalidate_caches()
        f = rc.run(con, root, vault=self.vault, targets=[])
        находки = {x["check"]: x for x in f}
        self.assertIn("ретеншен-не-проверен", находки)
        self.assertEqual("warn", находки["ретеншен-не-проверен"]["level"])
        # Токен причины живёт только в теле исключения. Прежний оракул брал
        # `"тридцать"`, а тот лежал ещё и в `os.environ`, и подстановка
        # `% os.environ[...]` вместо `% e` оставила бы тест зелёным при
        # потерянной причине. Нашёл ревьюер, круг 5 ревью #48.
        self.assertIn("битый", находки["ретеншен-не-проверен"]["detail"])
        # Префикс — см. соседа выше: в сводку уходит только `detail`.
        self.assertIn("не запустилась",
                      находки["ретеншен-не-проверен"]["detail"])
        # Свидетель — последняя проверка в `run`: до неё не добрались бы, если
        # бы исключение улетело наверх.
        self.assertIn("бэкап-ядра-конфиг", находки)

    def test_опечатка_в_MARA_RAW_DAYS_докладывается_отдельной_находкой(self):
        """Опечатка теперь не отказ, а находка: уборка идёт на дефолте.

        До этой правки цепочка была короткой и неверной: `int("тридцать")` на
        импорте — `EXIT=1` у крона в 4:40, ноль убранного сырья, а сверке
        только «проверка не запустилась», из которой не прочесть, что сломан
        конфиг, а не код. Теперь модуль берёт тридцать, кладёт причину на себя
        и отдаёт её сверке — единственному читателю, который доносит до
        владельца."""
        root = tempfile.mkdtemp(prefix="mara-root-")
        self.addCleanup(shutil.rmtree, root, True)
        con = mi.connect(root)
        self.addCleanup(con.close)
        import blob_retention
        self.addCleanup(sys.modules.__setitem__, "blob_retention",
                        blob_retention)
        # Значений четыре, и второе здесь не про минус, а про оракул: сверка,
        # собирающая текст жалобы сама из `os.environ` по шаблону «не число»,
        # на «тридцать» неотличима от честной. Отличает её только значение с
        # другой причиной. Мутант «причину сверка берёт из окружения» на одной
        # «тридцать» выживал. Третье закрывает последнюю ветку модуля: до него
        # кламп на потолок до сверки не доезжал вовсе, и находка на нём могла
        # не собираться годами — за руку взял бы только владелец, поставивший
        # шесть знаков. Нашёл ревьюер, круг 2. Четвёртое — дробная запись:
        # до правки она сюда доезжала под чужой причиной («не число»), и
        # владелец, поправив кавычки, получал бы ту же жалобу снова.
        for значение, кусок in (("тридцать", "не число"),
                                ("-1", "отрицательный срок"),
                                ("1000000", "больше ста лет"),
                                ("44.5", "дробный срок")):
            with self.subTest(значение=значение):
                sys.modules.pop("blob_retention", None)
                заплата = unittest.mock.patch.dict(
                    os.environ, {"MARA_RAW_DAYS": значение})
                заплата.start()
                try:
                    importlib.invalidate_caches()
                    f = rc.run(con, root, vault=self.vault, targets=[])
                    находки = {x["check"]: x for x in f}
                    свой = sys.modules["blob_retention"].ОШИБКА_КОНФИГА
                finally:
                    заплата.stop()
                # Отказа больше нет: заставу это не отменяет, но по этому пути
                # она молчит.
                self.assertNotIn("ретеншен-не-проверен", находки)
                self.assertIn("ретеншен-конфиг", находки)
                # `error`, а не `warn`: `код()` даёт 1, крон ругается
                # ежечасно, пока владелец не поправит. Прецедент —
                # `бэкап-ядра-конфиг` (`5d8bc67`).
                self.assertEqual("error", находки["ретеншен-конфиг"]["level"])
                # Тождество, а не вхождение подстроки: и значение, и имя
                # переменной достижимы из `os.environ`, так что сверка,
                # собравшая текст сама, прошла бы любой `assertIn` зелёной. А
                # тезис правки ровно обратный — причину кладёт тот, кто её
                # знает. Нашёл ревьюер, круг 1.
                self.assertEqual(свой, находки["ретеншен-конфиг"]["detail"])
                self.assertIn(кусок, находки["ретеншен-конфиг"]["detail"])
                self.assertIn(значение, находки["ретеншен-конфиг"]["detail"])
                self.assertIn("бэкап-ядра-конфиг", находки)

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
