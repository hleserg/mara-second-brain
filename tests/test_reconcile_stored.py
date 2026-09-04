"""Сверка видит запись, доведённую до `stored` без работы расшифровки.

Дыра §5.2: `finish_stored` переводит состояние, пишет манифест и ставит работу
тремя коммитами без транзакции (ADR-0005). Смерть демона посередине оставляет
звонок, который никто никогда не расшифрует, — и сам себя этот случай не чинит:
повтор с телефона увидит дубль по `dedupe_key` и не сделает ничего.
"""
import os, sys, shutil, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import contextd_reconcile as rc

EV = {"kind": "call", "source": "phone", "source_id": "call-9",
      "blob": {"sha256": "b" * 64, "bytes": 10, "mime": "audio/m4a", "ext": "m4a"}}


class ЗаписьБезРасшифровки(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.con = mi.connect(self.root)
        self.eid, _ = mi.put_event(self.con, dict(EV))
        # ровно то состояние, в котором демона убили: перевод сделан, дальше нет
        self.con.execute("update events set state='stored' where id=?", (self.eid,))
        mi.write_json(mi.manifest_path(self.root, self.eid), {"id": self.eid})

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def виды(self, находки):
        return [f["check"] for f in находки]

    def test_работа_расшифровки_ставится_заново(self):
        f = rc.запись_без_расшифровки(self.con, self.root)
        self.assertIn("расшифровка-поставлена", self.виды(f))
        self.assertEqual(1, self.con.execute(
            "select count(*) from jobs where event_id=? and kind='asr'",
            (self.eid,)).fetchone()[0], "работа не поставлена")

    def test_повторная_сверка_не_плодит_работы(self):
        rc.запись_без_расшифровки(self.con, self.root)
        self.assertEqual([], rc.запись_без_расшифровки(self.con, self.root),
                         "вторая сверка нашла то, что уже починила")
        self.assertEqual(1, self.con.execute(
            "select count(*) from jobs").fetchone()[0], "работа задвоилась")

    def test_живая_работа_не_повод_для_находки(self):
        mi.add_job(self.con, self.eid, "asr")
        self.assertEqual([], rc.запись_без_расшифровки(self.con, self.root))

    def test_отработавшая_расшифровка_не_повод(self):
        jid = mi.add_job(self.con, self.eid, "asr")
        self.con.execute("update jobs set state='done' where id=?", (jid,))
        self.assertEqual([], rc.запись_без_расшифровки(self.con, self.root),
                         "звонок уже расшифрован, чинить нечего")

    def test_событие_в_new_не_трогаем(self):
        self.con.execute("update events set state='new' where id=?", (self.eid,))
        self.assertEqual([], rc.запись_без_расшифровки(self.con, self.root),
                         "`new` доедет своим ходом — блоб ещё не лежит")

    def test_недописанный_манифест_докладывается(self):
        os.unlink(mi.manifest_path(self.root, self.eid))
        f = rc.запись_без_расшифровки(self.con, self.root)
        self.assertIn("манифест-не-дописан", self.виды(f))
        self.assertEqual("warn", [x for x in f
                                  if x["check"] == "манифест-не-дописан"][0]["level"],
                         "манифест собирает демон, сверка только докладывает")

    def test_находка_попадает_в_общий_прогон(self):
        f = rc.run(self.con, self.root, vault=None, targets=[])
        self.assertIn("расшифровка-поставлена", self.виды(f),
                      "проверка не подключена к run()")


if __name__ == "__main__":
    unittest.main()
