"""Уборка аудио и сверка состояния (ТЗ §7, §17).

Аудио живёт девяносто дней и исчезает само. Манифест не исчезает никогда: по
нему потом видно, что запись была и куда делась. Сверка чинит то, что чинится
однозначно, и только докладывает про то, где нужен человек.
"""
import os, sys, json, time, tempfile, unittest
from datetime import datetime, timedelta

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import mara_ingest as mi
import blob_retention as br
import contextd_reconcile as rc


def день(сдвиг):
    return (datetime.now(mi.TZ) + timedelta(days=сдвиг)).date().isoformat()


def транскрипт(root, eid):
    p = mi.transcript_path(root, eid)
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    open(p, "w").close()


def стенд(audio_until=None, pin=0, аудио=True):
    """База с одним звонком: блоб, манифест, событие."""
    root = tempfile.mkdtemp(prefix="mara-ret-")
    con = mi.connect(root)
    sha = "a" * 64
    eid, _ = mi.put_event(con, {"kind": "call", "source": "test", "source_id": "1",
                                "occurred_at": "2026-06-01T10:00:00+03:00",
                                "payload": {"contact_name": "Анна", "ext": "m4a"},
                                "blob": {"sha256": sha, "ext": "m4a"}})
    path = mi.blob_path(root, sha, "m4a")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if аудио:
        open(path, "wb").close()
    con.execute("insert into blobs(sha256,path,bytes,mime,created,pin,audio_until) "
                "values(?,?,?,?,?,?,?)",
                (sha, path, 0, "audio", mi.now_iso(), pin, audio_until or день(-1)))
    mi.write_json(mi.manifest_path(root, eid),
                  {"id": eid, "recording": {"audio_sha256": sha}, "purged": None})
    return root, con, eid, sha, path


class Ретеншен(unittest.TestCase):
    def test_просроченное_аудио_удаляется_а_манифест_остаётся(self):
        root, con, eid, sha, path = стенд()
        отчёт = br.sweep(con, root)
        self.assertEqual(отчёт["purged"], 1)
        self.assertFalse(os.path.exists(path), "файл записи должен быть удалён")
        with open(mi.manifest_path(root, eid), encoding="utf-8") as fh:
            man = json.load(fh)
        self.assertEqual(man["purged"]["reason"], "retention")
        self.assertTrue(man["purged"]["at"], "время уборки не записано")
        self.assertIsNotNone(con.execute("select purged_at from blobs where sha256=?",
                                         (sha,)).fetchone()["purged_at"])

    def test_повтор_уборки_ничего_не_ломает(self):
        root, con, eid, sha, path = стенд()
        br.sweep(con, root)
        второй = br.sweep(con, root)
        self.assertEqual(второй["purged"], 0, "убранное второй раз не убирается")
        self.assertEqual(второй["errors"], [])

    def test_pin_отменяет_удаление(self):
        root, con, eid, sha, path = стенд(pin=1)
        отчёт = br.sweep(con, root)
        self.assertEqual(отчёт["purged"], 0)
        self.assertTrue(os.path.exists(path), "закреплённая запись остаётся навсегда")

    def test_срок_ещё_не_вышел(self):
        root, con, eid, sha, path = стенд(audio_until=день(30))
        self.assertEqual(br.sweep(con, root)["purged"], 0)
        self.assertTrue(os.path.exists(path))

    def test_файла_уже_нет_а_запись_закрывается(self):
        root, con, eid, sha, path = стенд(аудио=False)
        отчёт = br.sweep(con, root)
        self.assertEqual(отчёт["errors"], [], "пропавший файл — не авария уборки")
        self.assertIsNotNone(con.execute("select purged_at from blobs where sha256=?",
                                         (sha,)).fetchone()["purged_at"])


class Сверка(unittest.TestCase):
    def test_манифест_без_блоба_уходит_в_отчёт(self):
        root, con, eid, sha, path = стенд(аудио=False)
        mi.add_job(con, eid, "asr")
        находки = rc.run(con, root, vault=None)
        своё = [f for f in находки if f["check"] == "манифест-без-блоба"]
        self.assertEqual(len(своё), 1)
        self.assertEqual(своё[0]["level"], "error")
        self.assertEqual(con.execute("select state from jobs where event_id=?",
                                     (eid,)).fetchone()["state"], "dlq",
                         "работа не должна вечно ретраиться на пропавшем файле")

    def test_убранное_по_ретеншену_не_считается_поломкой(self):
        root, con, eid, sha, path = стенд()
        br.sweep(con, root)
        находки = rc.run(con, root, vault=None)
        self.assertEqual([f for f in находки if f["check"] == "манифест-без-блоба"], [])

    def test_транскрипт_без_работы_извлечения_ставит_работу(self):
        root, con, eid, sha, path = стенд(audio_until=день(30))
        транскрипт(root, eid)
        rc.run(con, root, vault=None)
        kinds = [r["kind"] for r in con.execute("select kind from jobs where event_id=?",
                                                (eid,)).fetchall()]
        self.assertIn("extract", kinds, "расшифровка есть, а извлечения никто не ставил")

    def test_повторная_сверка_не_плодит_работы(self):
        root, con, eid, sha, path = стенд(audio_until=день(30))
        транскрипт(root, eid)
        rc.run(con, root, vault=None)
        rc.run(con, root, vault=None)
        n = con.execute("select count(*) from jobs where event_id=? and kind='extract'",
                        (eid,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_осиротевший_блоб_не_удаляется_молча(self):
        root, con, eid, sha, path = стенд(audio_until=день(30))
        чужой = mi.blob_path(root, "b" * 64, "m4a")
        open(чужой, "wb").close()
        находки = rc.run(con, root, vault=None)
        своё = [f for f in находки if f["check"] == "блоб-без-манифеста"]
        self.assertEqual(len(своё), 1)
        self.assertTrue(os.path.exists(чужой), "сверка не удаляет ничего сама")

    def test_просроченный_ретеншен_виден(self):
        root, con, eid, sha, path = стенд()
        находки = rc.run(con, root, vault=None)
        self.assertTrue([f for f in находки if f["check"] == "ретеншен-просрочен"])

    def test_лаг_индекса_считается_по_базе_basic_memory(self):
        root, con, eid, sha, path = стенд(audio_until=день(30))
        vault = tempfile.mkdtemp(prefix="mara-vault-")
        os.makedirs(os.path.join(vault, "kb/conversations"))
        for name in ("a.md", "b.md"):
            open(os.path.join(vault, "kb/conversations", name), "w").close()
        bm = os.path.join(root, "memory.db")
        import sqlite3
        c = sqlite3.connect(bm)
        c.execute("create table entity(file_path text)")
        c.execute("insert into entity values('kb/conversations/a.md')")
        c.commit()
        c.close()
        находки = rc.run(con, root, vault=vault, bm_db=bm)
        своё = [f for f in находки if f["check"] == "лаг-индекса"][0]
        self.assertEqual(своё["count"], 1, "одна карточка из двух не проиндексирована")

    def test_чистое_состояние_не_даёт_ошибок(self):
        root, con, eid, sha, path = стенд(audio_until=день(30))
        находки = rc.run(con, root, vault=None)
        self.assertEqual([f for f in находки if f["level"] == "error"], [])
        self.assertEqual(rc.код(находки), 0, "на здоровой системе крон молчит")


if __name__ == "__main__":
    unittest.main()
