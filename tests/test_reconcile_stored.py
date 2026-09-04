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
        # ровно то состояние, в котором демона убили: перевод сделан, а
        # дальше нет. Блоб при этом лежит — `finish_stored` иначе бы и не
        # позвался, и строка в `blobs` служит сверке признаком принятой записи
        self.блоб()
        self.con.execute("update events set state='stored' where id=?", (self.eid,))
        mi.write_json(mi.manifest_path(self.root, self.eid), {"id": self.eid})

    def блоб(self, purged=None, файл=True):
        путь = os.path.join(self.root, "b.m4a")
        if файл:
            open(путь, "wb").write(b"a" * 10)
        self.con.execute(
            "insert or replace into blobs"
            "(sha256,path,bytes,mime,created,purged_at) values(?,?,?,?,?,?)",
            ("b" * 64, путь, 10, "audio", mi.now_iso(), purged))

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

    def состояние(self, s):
        self.con.execute("update events set state=? where id=?", (s, self.eid))

    def test_событие_в_new_без_блоба_не_трогаем(self):
        self.состояние("new")
        self.con.execute("delete from blobs where sha256=?", ("b" * 64,))
        self.assertEqual([], rc.запись_без_расшифровки(self.con, self.root),
                         "`new` доедет своим ходом — блоб ещё не лежит")

    def test_событие_в_new_с_принятым_блобом_чинится(self):
        """Обратная сторона того же правила: блоб лёг, а состояние не
        сдвинулось (демон умер между `insert into blobs` и переводом) — эту
        пару до сих пор не видела ни одна проверка."""
        self.состояние("new")
        self.assertIn("расшифровка-поставлена",
                      self.виды(rc.запись_без_расшифровки(self.con, self.root)))

    def test_вычищенную_ретеншеном_запись_не_чиним(self):
        """Аудио удалили намеренно (ТЗ §14) — расшифровывать нечего. Раньше
        такое событие получало работу и уходило в DLQ на пустом месте."""
        self.блоб(purged=mi.now_iso())
        self.assertEqual([], rc.запись_без_расшифровки(self.con, self.root))
        self.assertEqual(0, self.con.execute(
            "select count(*) from jobs").fetchone()[0], "работать не над чем")

    def test_строка_в_blobs_без_файла_работу_не_ставит(self):
        """Ретеншен удаляет файл и только потом ставит `purged_at`
        (`blob_retention.py:82-87`). Смерть между шагами оставляет живую
        строку при пустом диске — работа на ней уйдёт в DLQ и только."""
        os.unlink(os.path.join(self.root, "b.m4a"))
        self.assertEqual([], [f for f in rc.запись_без_расшифровки(
            self.con, self.root) if f["check"] == "расшифровка-поставлена"])
        self.assertEqual(0, self.con.execute(
            "select count(*) from jobs").fetchone()[0], "работать не над чем")

    def test_брошенное_событие_с_принятым_блобом_чинится(self):
        """`stale` (хеш не сошёлся) с реально лежащим блобом бывает после
        отката одного демона: старый `finish_stored` знает лишь `state='new'`,
        молчит и всё равно отвечает телефону 200."""
        self.состояние("stale")
        self.assertIn("расшифровка-поставлена",
                      self.виды(rc.запись_без_расшифровки(self.con, self.root)))

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
