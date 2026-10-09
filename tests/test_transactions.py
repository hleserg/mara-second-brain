"""Транзакционные границы приёма и воркера (ТЗ §5.2, Т2.1в).

До этого каждый оператор был сам себе транзакцией: смерть демона между
переходом события и постановкой работы, между строкой блоба и переходом,
между итогом шага и следующей работой оставляла полусостояние, которое
чинила только сверка. Здесь — что полусостояний больше нет, и что файл
попадает на диск раньше, чем база о нём узнаёт.
"""
import io, os, sys, hashlib, shutil, tempfile, unittest, unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import contextd

ЗВУК = b"\x00\x00\x00\x18ftypM4A " + b"a" * 64


class Границы(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        mi.ROOT = self.root
        self.con = mi.connect(self.root)
        self.addCleanup(self.con.close)

    def событие(self, sid="s"):
        sha = hashlib.sha256(ЗВУК).hexdigest()
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone", "source_id": sid,
                                         "blob": {"sha256": sha, "bytes": len(ЗВУК),
                                                  "ext": "m4a"}})
        return eid, sha

    def состояние(self, eid):
        return self.con.execute("select state from events where id=?", (eid,)).fetchone()[0]

    def работ(self, eid=None):
        return self.con.execute("select count(*) from jobs" +
                                (" where event_id=?" if eid else ""),
                                (eid,) if eid else ()).fetchone()[0]

    def test_finish_stored_переход_и_работа_вместе(self):
        eid, _ = self.событие()
        with unittest.mock.patch.object(mi, "add_job", side_effect=RuntimeError("умер")):
            with self.assertRaises(RuntimeError):
                contextd.finish_stored(self.con, self.root, eid)
        self.assertEqual(self.состояние(eid), "new", "переход без работы откатился")
        self.assertFalse(self.con.in_transaction)
        self.assertTrue(os.path.exists(mi.manifest_path(self.root, eid)),
                        "манифест на диске раньше перехода — это нормально")
        contextd.finish_stored(self.con, self.root, eid)
        self.assertEqual((self.состояние(eid), self.работ(eid)), ("stored", 1))
        contextd.finish_stored(self.con, self.root, eid)
        self.assertEqual(self.работ(eid), 1, "повтор не плодит работ")

    def test_манифест_не_записался_база_не_тронута(self):
        eid, _ = self.событие()
        with unittest.mock.patch.object(mi, "write_json", side_effect=OSError("диск")):
            with self.assertRaises(OSError):
                contextd.finish_stored(self.con, self.root, eid)
        self.assertEqual((self.состояние(eid), self.работ(eid)), ("new", 0))

    def test_приём_блоба_строка_переход_работа_вместе(self):
        eid, sha = self.событие()
        with unittest.mock.patch.object(mi, "add_job", side_effect=RuntimeError("умер")):
            with self.assertRaises(RuntimeError):
                contextd.ingest_audio(self.con, self.root, eid, io.BytesIO(ЗВУК), len(ЗВУК))
        self.assertIsNone(self.con.execute("select 1 from blobs where sha256=?",
                                           (sha,)).fetchone(), "строка блоба откатилась")
        self.assertEqual(self.состояние(eid), "new")
        self.assertFalse(self.con.in_transaction)
        # файл при этом лежит: его подберёт повтор с телефона по дедупу
        путь = mi.blob_path(self.root, sha, "m4a")
        self.assertTrue(os.path.exists(путь))
        код, _ = contextd.ingest_audio(self.con, self.root, eid, io.BytesIO(ЗВУК), len(ЗВУК))
        self.assertEqual(код, 200)
        self.assertEqual((self.состояние(eid), self.работ(eid)), ("stored", 1))

    def test_fsync_до_rename_у_блоба_и_манифеста(self):
        eid, sha = self.событие()
        порядок = []
        настоящий_fsync, настоящий_replace = os.fsync, os.replace

        def fsync(fd):
            порядок.append("fsync")
            return настоящий_fsync(fd)

        def replace(a, b):
            порядок.append("replace:" + os.path.basename(b))
            return настоящий_replace(a, b)

        with unittest.mock.patch.object(os, "fsync", fsync), \
                unittest.mock.patch.object(os, "replace", replace):
            contextd.ingest_audio(self.con, self.root, eid, io.BytesIO(ЗВУК), len(ЗВУК))
        имена = [x for x in порядок if x.startswith("replace")]
        self.assertEqual(len(имена), 2, порядок)            # блоб и манифест
        for i, x in enumerate(порядок):
            if x.startswith("replace"):
                self.assertEqual(порядок[i - 1], "fsync", "rename без fsync: %s" % порядок)

    def test_итог_шага_и_следующая_работа_вместе(self):
        eid, _ = self.событие()
        jid = mi.add_job(self.con, eid, "asr")
        job = mi.claim_job(self.con)
        with unittest.mock.patch.object(mi, "add_job", side_effect=RuntimeError("умер")):
            with self.assertRaises(RuntimeError):
                contextd.закрыть_работу(self.con, job, True, "")
        self.assertEqual(self.con.execute("select state from jobs where id=?",
                                          (jid,)).fetchone()[0], "ready",
                         "итог без следующей работы откатился")
        contextd.закрыть_работу(self.con, job, True, "")
        виды = sorted(r[0] for r in self.con.execute(
            "select kind from jobs where event_id=? order by kind", (eid,)))
        self.assertEqual(виды, ["asr", "extract"])
        self.assertEqual(self.con.execute("select state from jobs where id=?",
                                          (jid,)).fetchone()[0], "done")

    def test_сбой_шага_не_ставит_следующую(self):
        eid, _ = self.событие()
        mi.add_job(self.con, eid, "asr")
        job = mi.claim_job(self.con)
        contextd.закрыть_работу(self.con, job, False, "ffmpeg упал")
        self.assertEqual(self.работ(eid), 1)
        self.assertEqual(self.con.execute("select attempts from jobs").fetchone()[0], 1)

    def test_транзакция_внутри_чужой_это_savepoint(self):
        self.con.execute("begin immediate")
        with contextd.транзакция(self.con):
            self.con.execute("insert into compute_nodes(id,name) values('n','x')")
        self.assertTrue(self.con.in_transaction, "чужую транзакцию не закрыли")
        # исключение внутри вложенного блока откатывает ровно его (ревью P2)
        with self.assertRaises(RuntimeError):
            with contextd.транзакция(self.con):
                self.con.execute("insert into compute_nodes(id,name) values('m','y')")
                raise RuntimeError("бум")
        self.assertTrue(self.con.in_transaction)
        self.assertEqual([r[0] for r in self.con.execute("select id from compute_nodes")],
                         ["n"], "вложенный шаг откатился, внешний цел")
        self.con.execute("rollback")
        self.assertIsNone(self.con.execute("select 1 from compute_nodes").fetchone())

    def test_упавший_commit_откатывает_а_не_оставляет_транзакцию(self):
        con = self.con

        class Кривой:
            """Соединение, у которого commit падает — диск, I/O."""

            def execute(self, sql, *a):
                if sql == "commit":
                    raise RuntimeError("диск")
                return con.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(con, name)

        with self.assertRaises(RuntimeError):
            with contextd.транзакция(Кривой()):
                con.execute("insert into compute_nodes(id,name) values('n','x')")
        self.assertFalse(con.in_transaction, "соединение зависло в транзакции")
        self.assertIsNone(self.con.execute("select 1 from compute_nodes").fetchone())

    def test_исключение_в_переносе_под_приёмом_не_коммитит_полусостояние(self):
        """Ревью P2: внутри транзакции приёма упавший шаг переноса коммитился
        вместе с событием — объект без проекции."""
        import unittest.mock
        import ledger_import as li
        vault = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, vault, True)
        os.makedirs(os.path.join(vault, ".git"))
        os.makedirs(os.path.join(vault, "kb/commitments"))
        with open(os.path.join(vault, "kb/commitments/a.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: забор\ntype: commitment\nstatus: open\n---\n")
        with unittest.mock.patch.object(li, "_проекция_и_история",
                                        side_effect=RuntimeError("бум")):
            with contextd.транзакция(self.con):
                self.con.execute("insert into compute_nodes(id,name) values('n','x')")
                with self.assertRaises(RuntimeError):
                    li.перенести_карточку(self.con, vault, "kb/commitments/a.md")
        self.assertEqual(self.con.execute("select count(*) from commitments").fetchone()[0], 0)
        self.assertEqual(self.con.execute("select count(*) from compute_nodes").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
