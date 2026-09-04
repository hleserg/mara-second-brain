"""Сверка видит, что бэкап ядра встал (P0-2, P0-5).

Молчащий крон выглядит ровно как отсутствующий, поэтому пустые носители и
протухший архив обязаны отличаться друг от друга и от нормы.

Считаем по каждому носителю: живой носитель закрывал вопрос за всех, и
отвалившийся диск не было видно, пока цела вторая копия, — а третьей копии по
§12 при этом уже нет."""
import os, sys, json, time, shutil, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import contextd_reconcile as rc


class БэкапЯдра(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.цель = os.path.join(self.tmp, "backup")
        os.makedirs(self.цель)
        # Здесь проверяется свежесть архива, а не носитель: временный каталог
        # лежит на той же ФС, что и корень, и без этого всё упиралось бы в
        # проверку монтирования. Она — предмет tests/test_backup_mount.py.
        os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)

    def отметка(self, **когда):
        """Файл, который в бою пишет core-backup: когда на носитель в
        последний раз лёг архив. Без него отвалившийся носитель неотличим от
        никогда не подключавшегося."""
        p = os.path.join(self.tmp, "core-targets.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({k: time.time() - v * 86400
                       for k, v in когда.items()}, fh)
        return p

    def архив(self, дней_назад):
        p = os.path.join(self.цель, "core-2026-09-%02d.tar.gz.gpg"
                         % (int(дней_назад) + 1))
        open(p, "wb").write(b"gpg")
        t = time.time() - дней_назад * 86400
        os.utime(p, (t, t))
        return p

    def test_свежий_архив_не_находка(self):
        self.архив(0)
        self.assertEqual(rc.бэкап_ядра([self.цель]), [])

    def test_протухший_архив_это_ошибка(self):
        self.архив(5)
        f = rc.бэкап_ядра([self.цель])
        self.assertEqual((f[0]["check"], f[0]["level"]), ("бэкап-ядра-устарел", "error"))
        self.assertEqual(f[0]["days"], 5.0)

    def test_отвалившаяся_на_вечер_шара_не_находка(self):
        отпал = os.path.join(self.tmp, "нет-такого")
        self.архив(0)
        о = self.отметка(**{отпал: 0.2})
        self.assertEqual(rc.бэкап_ядра([self.цель, отпал], отметка=о), [],
                         "шару выключили на вечер — это не поломка")

    def test_отвалившийся_надолго_носитель_это_ошибка(self):
        отпал = os.path.join(self.tmp, "нет-такого")
        self.архив(0)
        о = self.отметка(**{отпал: 5})
        f = rc.бэкап_ядра([self.цель, отпал], отметка=о)
        self.assertEqual([(x["check"], x["level"]) for x in f],
                         [("бэкап-ядра-отвалился", "error")],
                         "неделя без третьей копии — отказ, даже когда\n"
                         "вторая копия жива")
        self.assertEqual(f[0]["target"], отпал)

    def test_живой_но_отставший_носитель_виден_за_свежим(self):
        второй = os.path.join(self.tmp, "backup2")
        os.makedirs(второй)
        self.архив(0)
        старый = os.path.join(второй, "core-2026-08-01.tar.gz.gpg")
        open(старый, "wb").write(b"gpg")
        t = time.time() - 5 * 86400
        os.utime(старый, (t, t))
        f = rc.бэкап_ядра([self.цель, второй])
        self.assertEqual([(x["check"], x["target"]) for x in f],
                         [("бэкап-ядра-устарел", второй)],
                         "запись на носитель встала, а общий максимум\n"
                         "это прятал")

    def test_пустые_носители_это_ненастроенность_а_не_отказ(self):
        f = rc.бэкап_ядра([self.цель])
        self.assertEqual((f[0]["check"], f[0]["level"]), ("бэкап-ядра-нет", "warn"))

    def test_одна_пропущенная_ночь_не_отказ_а_две_отказ(self):
        # Порог меряется в пропущенных ночах: запись раз в сутки, поэтому
        # простой в полтора дня — это одна пропущенная ночь (норма), а в
        # два с половиной — две подряд (отказ). Обе точки рядом с порогом:
        # без них ослабление порога на сутки невидимо.
        отпал = os.path.join(self.tmp, "нет-такого")
        self.архив(0)
        о = self.отметка(**{отпал: 1.5})
        self.assertEqual(rc.бэкап_ядра([self.цель, отпал], отметка=о), [])
        о = self.отметка(**{отпал: 2.5})
        f = rc.бэкап_ядра([self.цель, отпал], отметка=о)
        self.assertEqual([x["check"] for x in f], ["бэкап-ядра-отвалился"])

    def test_возраст_архива_меряется_теми_же_ночами(self):
        self.архив(1.5)
        self.assertEqual(rc.бэкап_ядра([self.цель]), [],
                         "одна пропущенная ночь — ещё не отказ")
        shutil.rmtree(self.цель)
        os.makedirs(self.цель)
        self.архив(2.5)
        f = rc.бэкап_ядра([self.цель])
        self.assertEqual([x["check"] for x in f], ["бэкап-ядра-устарел"])

    def test_все_носители_выдернуты_это_находка(self):
        первый = os.path.join(self.tmp, "нет-раз")
        второй = os.path.join(self.tmp, "нет-два")
        о = self.отметка(**{первый: 0.3, второй: 0.3})
        f = rc.бэкап_ядра([первый, второй], отметка=о)
        self.assertEqual([(x["check"], x["level"]) for x in f],
                         [("бэкап-ядра-копий-ноль", "warn")],
                         "внешних копий нет ни одной, а отметки свежие")

    def test_копий_ноль_говорится_и_поверх_других_находок(self):
        # Иначе за факт «копий ноль» отвечает первая попавшаяся строка,
        # из которой читается обратное: один не настроили, второй цел.
        первый = os.path.join(self.tmp, "нет-раз")
        второй = os.path.join(self.tmp, "нет-два")
        о = self.отметка(**{второй: 0.3})
        f = rc.бэкап_ядра([первый, второй], отметка=о)
        self.assertEqual(sorted(x["check"] for x in f),
                         ["бэкап-ядра-копий-ноль", "бэкап-ядра-носители"])

    def test_пустой_список_носителей_это_находка(self):
        f = rc.бэкап_ядра([], отметка=self.отметка())
        self.assertEqual((f[0]["check"], f[0]["level"]),
                         ("бэкап-ядра-конфиг", "error"),
                         "третью копию по §12 класть некуда, а сверка молчит")

    def test_порог_читается_из_окружения(self):
        # Иначе `_порог` может просто всегда возвращать дефолт: сверка зелёная,
        # а ручка в crontab ничего не делает.
        os.environ["MARA_CORE_BACKUP_MAX_DAYS"] = "5"
        try:
            self.assertEqual(rc._порог("MARA_CORE_BACKUP_MAX_DAYS", 2), 5.0)
            os.environ["MARA_CORE_BACKUP_MAX_DAYS"] = "пять"
            было = len(rc.ОШИБКИ_КОНФИГА)
            self.assertEqual(rc._порог("MARA_CORE_BACKUP_MAX_DAYS", 2), 2)
            self.assertEqual(len(rc.ОШИБКИ_КОНФИГА), было + 1)
            rc.ОШИБКИ_КОНФИГА.pop()
        finally:
            os.environ.pop("MARA_CORE_BACKUP_MAX_DAYS", None)

    def test_нечисловой_порог_не_роняет_сверку(self):
        # Пороги теперь живут строками в crontab: опечатка там не имеет права
        # уносить всю сверку целиком при импорте.
        self.архив(0)
        f = rc.бэкап_ядра([self.цель], ошибки=["MARA_CORE_BACKUP_MAX_DAYS"])
        self.assertEqual([x["check"] for x in f], ["бэкап-ядра-конфиг"])

    def test_исчезнувшие_архивы_это_не_ненастроенность(self):
        # Тот же пустой носитель, разница только в отметке: без неё это
        # ненастроенная машина, с ней — кто-то унёс копию. Иначе проверялось
        # бы существование двух веток, а не то, что отметка их переключает.
        пусто = os.path.join(self.tmp, "нет-файла.json")
        f = rc.бэкап_ядра([self.цель], отметка=пусто)
        self.assertEqual((f[0]["check"], f[0]["level"]),
                         ("бэкап-ядра-нет", "warn"))
        о = self.отметка(**{self.цель: 0.3})
        f = rc.бэкап_ядра([self.цель], отметка=о)
        self.assertEqual((f[0]["check"], f[0]["level"]),
                         ("бэкап-ядра-пропали", "error"),
                         "носитель принимал архивы и опустел — их убрали")

    def test_битая_отметка_не_роняет_сверку(self):
        # Файл машинный, но лежит там, куда дотянется кто угодно: сверка,
        # упавшая на нём, унесёт заодно DLQ, сердцебиения и ретеншен.
        битая = os.path.join(self.tmp, "битая.json")
        open(битая, "w").write('["не словарь"]')
        # Носитель именно отвалившийся: на живом отметку не спрашивают вовсе,
        # и тест был бы зелен независимо от того, разобрали её или нет.
        отпал = os.path.join(self.tmp, "нет-такого")
        f = rc.бэкап_ядра([отпал], отметка=битая)
        self.assertEqual(sorted(x["check"] for x in f),
                         ["бэкап-ядра-копий-ноль", "бэкап-ядра-носители"])

    def test_битый_конфиг_носителей_это_находка_а_не_падение(self):
        f = rc.бэкап_ядра([], ошибки=["носители: диск — путь не абсолютный"])
        self.assertEqual([(x["check"], x["level"]) for x in f],
                         [("бэкап-ядра-конфиг", "error"),
                          ("бэкап-ядра-конфиг", "error")],
                         "разбор не удался и класть некуда — это две беды")
        self.assertIn("не абсолютный", f[0]["detail"])

    def test_носитель_без_отметки_это_ненастроенность(self):
        f = rc.бэкап_ядра([os.path.join(self.tmp, "нет-такого")],
                          отметка=os.path.join(self.tmp, "нет-файла.json"))
        self.assertEqual([(x["check"], x["level"]) for x in f],
                         [("бэкап-ядра-носители", "warn"),
                          ("бэкап-ядра-копий-ноль", "warn")],
                         "носитель, на который ни разу не писали, — не отказ")


if __name__ == "__main__":
    unittest.main()
