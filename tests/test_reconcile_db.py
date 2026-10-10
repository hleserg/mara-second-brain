"""Сверка смотрит на сам реестр: `quick_check`, размер WAL, место на диске.

§5.1 ТЗ требует `integrity_check`/`quick_check` по расписанию и мониторинг
WAL и свободного места (Т2.1б). До этой проверки сверка доверяла базе
безоговорочно: битая страница давала не находку, а исключение где-то в
третьей проверке, которое застава называла «проверка не запустилась».
"""
import contextlib, io, os, sqlite3, sys, shutil, tempfile, unittest, unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import contextd_reconcile as rc


class БазаЦела(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.con = mi.connect(self.root)
        self.addCleanup(self.con.close)

    def виды(self, находки):
        return [f["check"] for f in находки]

    def test_здоровая_база_без_находок(self):
        self.assertEqual(rc.база_цела(self.con, self.root), [])

    def test_битая_страница_это_error(self):
        # заполняем так, чтобы появились страницы с данными, и портим одну
        # из них мимо SQLite: ровно так выглядит сбой диска или обрыв записи
        for i in range(200):
            mi.put_event(self.con, {"kind": "message", "source": "t",
                                    "source_id": str(i), "payload": {"x": "y" * 200}})
        self.con.execute("pragma wal_checkpoint(truncate)")
        self.con.close()
        db = os.path.join(self.root, "contextd.db")
        size = os.path.getsize(db)
        # портим всё, кроме первых двух страниц (заголовок и корень
        # sqlite_master): одна страница посередине после миграции 3 могла
        # оказаться свободной, и quick_check её не видел
        with open(db, "r+b") as fh:
            начало = max(4096 * 2, size // 2 - 2 * 4096)
            fh.seek(начало)
            fh.write(b"\xff" * min(4 * 4096, size - начало - 4096))
        self.con = mi.connect(self.root)
        f = rc.база_цела(self.con, self.root)
        self.assertEqual(self.виды(f), ["база-повреждена"], f)
        self.assertEqual(f[0]["level"], "error")
        self.assertIn("восстанавливать из копии", f[0]["detail"])

    def test_разросшийся_wal_это_warn(self):
        wal = os.path.join(self.root, "contextd.db-wal")
        with open(wal, "ab") as fh:
            fh.truncate(int((rc.WAL_МИБ + 1) * (1 << 20)))
        f = rc.база_цела(self.con, self.root)
        self.assertEqual(self.виды(f), ["wal-разросся"], f)
        self.assertEqual(f[0]["level"], "warn")

    def test_wal_под_порогом_не_находка(self):
        wal = os.path.join(self.root, "contextd.db-wal")
        with open(wal, "ab") as fh:
            fh.truncate(int((rc.WAL_МИБ - 1) * (1 << 20)))
        self.assertEqual(rc.база_цела(self.con, self.root), [])

    def test_мало_места_это_warn(self):
        st = os.statvfs(self.root)
        мало = os.statvfs_result(
            (st.f_bsize, st.f_frsize, st.f_blocks, st.f_bfree,
             int(0.5 * (1 << 30) / st.f_frsize), st.f_files, st.f_ffree,
             st.f_favail, st.f_flag, st.f_namemax))
        with unittest.mock.patch.object(rc.os, "statvfs", return_value=мало):
            f = rc.база_цела(self.con, self.root)
        self.assertEqual(self.виды(f), ["места-мало"], f)
        self.assertLess(f[0]["gib"], rc.МЕСТО_ГИБ)

    def test_схема_отстала_это_находка_а_не_падение(self):
        # выкат без --migrate: раньше крон сверки умирал на connect с трассой
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        mi.migrate(root, mi.ВЕРСИЯ - 1).close()
        con, f = rc.открыть_реестр(root)
        self.assertIsNone(con)
        self.assertEqual([(x["check"], x["level"]) for x in f],
                         [("схема-не-мигрирована", "error")])
        self.assertIn(mi.КОМАНДА, f[0]["detail"])
        self.assertEqual((f[0]["db"], f[0]["code"]), (mi.ВЕРСИЯ - 1, mi.ВЕРСИЯ))

    def test_схема_новее_кода_это_находка(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        mi.connect(root).close()
        c = sqlite3.connect(os.path.join(root, "contextd.db"))
        c.execute("pragma user_version=%d" % (mi.ВЕРСИЯ + 1)); c.close()
        con, f = rc.открыть_реестр(root)
        self.assertIsNone(con)
        self.assertEqual([x["check"] for x in f], ["схема-новее-кода"])
        self.assertEqual(f[0]["level"], "error")

    def test_мусор_вместо_базы_это_находка_а_не_трасса(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        with open(os.path.join(root, "contextd.db"), "wb") as fh:
            fh.write(b"\x00" * 100 + b"not a database" * 50)
        con, f = rc.открыть_реестр(root)
        self.assertIsNone(con)
        self.assertEqual([(x["check"], x["level"]) for x in f],
                         [("база-повреждена", "error")])

    def test_база_только_на_чтение_это_не_повреждение(self):
        # Codex по #141: `OperationalError` — подкласс `DatabaseError`; совет
        # «восстанавливать из копии» здоровой базе с чужими правами был бы вреден.
        # Права в контейнере обходит root, поэтому отказ подменяется у `connect`.
        with unittest.mock.patch.object(mi, "connect", side_effect=sqlite3.OperationalError(
                "attempt to write a readonly database")):
            con, f = rc.открыть_реестр(self.root)
        self.assertIsNone(con)
        self.assertEqual([(x["check"], x["level"]) for x in f],
                         [("база-не-открывается", "error")])
        self.assertIn("права", f[0]["detail"])
        self.assertIn("из копии не восстанавливать", f[0]["detail"])

    def test_каталог_без_прав_это_не_опечатка(self):
        # Codex по #141, круг 2: `isfile` глотает EACCES, и каталог с чужими
        # правами после восстановления выглядел бы как «базы нет».
        # Права в контейнере обходит root — отказ подменяется у `os.stat`.
        with unittest.mock.patch.object(rc.os, "stat",
                                        side_effect=PermissionError(13, "Permission denied")):
            con, f = rc.открыть_реестр(self.root)
        self.assertIsNone(con)
        self.assertEqual([x["check"] for x in f], ["база-не-открывается"])
        self.assertIn("Permission denied", f[0]["detail"])

    def test_нет_базы_это_находка_и_ничего_не_заводится(self):
        # опечатка в --root крона раньше давала пустую базу и «всё сходится»
        root = os.path.join(tempfile.mkdtemp(), "opechatka")
        self.addCleanup(shutil.rmtree, os.path.dirname(root), True)
        con, f = rc.открыть_реестр(root)
        self.assertIsNone(con)
        self.assertEqual([x["check"] for x in f], ["база-нет"])
        self.assertFalse(os.path.exists(os.path.join(root, "contextd.db")), "завёл базу")
        # пустой файл — тоже «нет», `connect` его завёл бы последней версией
        os.makedirs(root)
        open(os.path.join(root, "contextd.db"), "wb").close()
        con, f = rc.открыть_реестр(root)
        self.assertIsNone(con)
        self.assertEqual([x["check"] for x in f], ["база-нет"])

    def test_здоровая_база_открывается_без_находок(self):
        con, f = rc.открыть_реестр(self.root)
        self.addCleanup(con.close)
        self.assertEqual(f, [])

    def test_main_докладывает_о_схеме_и_не_падает(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        mi.migrate(root, mi.ВЕРСИЯ - 1).close()
        self.addCleanup(setattr, mi, "ROOT", mi.ROOT)   # main переставляет mi.ROOT
        поток = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", ["rc", "--root", root, "--json"]), \
                contextlib.redirect_stdout(поток):
            код = rc.main()
        self.assertNotEqual(код, 0)
        self.assertIn("схема-не-мигрирована", поток.getvalue())

    def test_проверка_стоит_в_общем_прогоне_под_заставой(self):
        self.assertEqual([f for f in rc.run(self.con, self.root, vault=None,
                                            targets=[])
                          if f["check"].startswith("база")], [])
        with unittest.mock.patch.object(rc, "база_цела",
                                        side_effect=RuntimeError("бум")):
            f = rc.run(self.con, self.root, vault=None, targets=[])
        self.assertIn("база-упала", self.виды(f))


if __name__ == "__main__":
    unittest.main()
