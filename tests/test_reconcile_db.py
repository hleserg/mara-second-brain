"""Сверка смотрит на сам реестр: `quick_check`, размер WAL, место на диске.

§5.1 ТЗ требует `integrity_check`/`quick_check` по расписанию и мониторинг
WAL и свободного места (Т2.1б). До этой проверки сверка доверяла базе
безоговорочно: битая страница давала не находку, а исключение где-то в
третьей проверке, которое застава называла «проверка не запустилась».
"""
import os, sys, shutil, tempfile, unittest, unittest.mock

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
        with open(db, "r+b") as fh:
            fh.seek(size // 2)
            fh.write(b"\xff" * 512)
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
