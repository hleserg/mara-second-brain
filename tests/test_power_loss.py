"""Убитый процесс и повтор вокруг критических транзакций (ТЗ §5.1, Т2.1б).

`tests/test_transactions.py` роняет шаг исключением — это проверка границ
внутри живого процесса. Здесь процесс умирает по-настоящему: `SIGKILL`
посреди транзакции, между файлом и переходом, после коммита до выхода, с
замком в руках, с недописанным стейджингом. §5.1: «восстановление не
должно зависеть от clean shutdown», «тесты power-loss/kill/retry вокруг
критических транзакций». Что проверяется после смерти: полусостояния нет,
база цела (`quick_check`), замок отпущен, повтор той же операции доводит
дело до конца и ничего не плодит.

Ребёнок — отдельный интерпретатор с тем же кодом; он доходит до нужной
точки, пишет в метку-файл то, что видит своим соединением, и засыпает;
родитель по метке убивает его. `SIGKILL` — смерть процесса, не обрыв
питания: страничный кэш ОС доезжает до диска, и долговечность коммита при
настоящем обрыве держится на `synchronous` (FULL по умолчанию, ADR-0005),
а не на этом стенде. Что стенд проверяет: полукоммита нет, замок мертвеца
отпущен, повтор доводит, коммит без checkpoint и без закрытия соединения
читается.
"""
import io, os, sys, time, glob, signal, shutil, hashlib, sqlite3, tempfile, subprocess, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)
import mara_ingest as mi
import contextd

ЗВУК = b"\x00\x00\x00\x18ftypM4A " + b"a" * 64

# Сценарии ребёнка: до метки — работа, после метки — сон до SIGKILL.
РЕБЁНОК = r'''
import os, sys, time, tempfile
sys.path.insert(0, %(scripts)r)
import mara_ingest as mi
root, eid, метка, сценарий = %(root)r, %(eid)r, %(метка)r, %(сценарий)r
con = mi.connect(root)
def стоп():
    # что ребёнок видит сам перед смертью — родитель сверит с тем, что
    # пережило смерть (мутант «метка до записей» иначе проходил бы)
    вижу = "%%s %%d" %% (con.execute("select state from events where id=?", (eid,)).fetchone()[0],
                      con.execute("select count(*) from jobs where event_id=?",
                                  (eid,)).fetchone()[0])
    with open(метка + ".tmp", "w") as fh:
        fh.write(вижу)
    os.replace(метка + ".tmp", метка)
    time.sleep(60)
if сценарий == "посреди-транзакции":
    with mi.транзакция(con):
        con.execute("update events set state='stored' where id=?", (eid,))
        mi.add_job(con, eid, "asr")
        стоп()
elif сценарий == "после-манифеста":
    mi.write_json(mi.manifest_path(root, eid), {"event_id": eid, "частичный": True})
    стоп()
elif сценарий == "после-коммита":
    with mi.транзакция(con):
        con.execute("update events set state='stored' where id=?", (eid,))
        mi.add_job(con, eid, "asr")
    стоп()                       # соединение не закрыто, checkpoint не было
elif сценарий == "с-замком":
    con.execute("begin immediate")
    стоп()
elif сценарий == "стейджинг":
    sha = %(sha)r
    path = mi.blob_path(root, sha, "m4a")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".part")
    os.write(fd, b"x" * 40)      # половина байт, без fsync и без rename
    стоп()
'''


class УбитыйПроцесс(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        mi.ROOT = self.root
        self.con = mi.connect(self.root)
        self.addCleanup(self.con.close)
        self.sha = hashlib.sha256(ЗВУК).hexdigest()
        self.eid, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "s",
            "blob": {"sha256": self.sha, "bytes": len(ЗВУК), "ext": "m4a"}})
        self.дети = []
        self.addCleanup(self._прибрать)

    def _прибрать(self):
        for p in self.дети:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stderr.close()

    def запустить(self, сценарий):
        """Ребёнок до метки. Возвращает (процесс, что он видел своим соединением)."""
        метка = os.path.join(self.root, "метка-" + сценарий)
        код = РЕБЁНОК % {"scripts": SCRIPTS, "root": self.root, "eid": self.eid,
                        "метка": метка, "сценарий": сценарий, "sha": self.sha}
        p = subprocess.Popen([sys.executable, "-c", код], stderr=subprocess.PIPE)
        self.дети.append(p)
        for _ in range(600):
            if os.path.exists(метка):
                break
            if p.poll() is not None:
                self.fail("ребёнок умер сам, не дойдя до метки: %s"
                          % p.stderr.read().decode("utf-8", "replace"))
            time.sleep(0.05)
        else:
            self.fail("ребёнок не дошёл до метки")
        with open(метка, encoding="utf-8") as fh:
            return p, fh.read()

    def убить(self, p):
        """SIGKILL и подтверждение, что умер именно от него, а не вышел сам."""
        p.send_signal(signal.SIGKILL)
        p.wait()
        self.assertEqual(p.returncode, -signal.SIGKILL, "ребёнок вышел сам — стенд ничего не проверил")

    def убить_на(self, сценарий):
        """Запустить ребёнка, дождаться метки, SIGKILL. Возвращает что он видел."""
        p, вижу = self.запустить(сценарий)
        self.убить(p)
        return вижу

    def замок_занят(self):
        """Чужой `begin immediate` держит базу: короткий таймаут упирается."""
        быстрое = sqlite3.connect(os.path.join(self.root, "contextd.db"), timeout=0.2,
                                  isolation_level=None)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                быстрое.execute("begin immediate")
        finally:
            быстрое.close()

    def wal(self):
        путь = os.path.join(self.root, "contextd.db-wal")
        return os.path.getsize(путь) if os.path.exists(путь) else 0

    def состояние(self):
        return self.con.execute("select state from events where id=?",
                                (self.eid,)).fetchone()[0]

    def работ(self):
        return self.con.execute("select count(*) from jobs where event_id=?",
                                (self.eid,)).fetchone()[0]

    def цела(self):
        return self.con.execute("pragma quick_check").fetchone()[0]

    def test_смерть_посреди_транзакции_не_оставляет_полусостояния(self):
        p, вижу = self.запустить("посреди-транзакции")
        self.assertEqual(вижу, "stored 1", "ребёнок внутри транзакции видел свои записи")
        self.замок_занят()                       # транзакция и правда открыта
        self.убить(p)
        self.assertEqual((self.состояние(), self.работ()), ("new", 0),
                         "незакоммиченное не пережило смерть")
        self.assertEqual(self.цела(), "ok")
        # замок умершего отпущен: запись проходит без ожидания
        с = mi.connect(self.root)
        self.addCleanup(с.close)
        t = time.monotonic()
        with mi.транзакция(с):
            с.execute("insert into compute_nodes(id,name) values('n','x')")
        self.assertLess(time.monotonic() - t, 5, "замок мертвеца держит базу")
        # повтор той же операции доводит дело до конца, и ровно один раз
        contextd.finish_stored(self.con, self.root, self.eid)
        contextd.finish_stored(self.con, self.root, self.eid)
        self.assertEqual((self.состояние(), self.работ()), ("stored", 1))

    def test_смерть_между_манифестом_и_переходом_чинится_повтором(self):
        self.убить_на("после-манифеста")
        self.assertTrue(os.path.exists(mi.manifest_path(self.root, self.eid)),
                        "файл лёг раньше базы — так и задумано (§5.2)")
        self.assertEqual((self.состояние(), self.работ()), ("new", 0),
                         "база не говорит stored раньше, чем доведена до конца")
        self.assertFalse(glob.glob(os.path.join(self.root, "manifests", "*.tmp")),
                         "write_json атомарна: временных нет")
        contextd.finish_stored(self.con, self.root, self.eid)
        self.assertEqual((self.состояние(), self.работ()), ("stored", 1))
        import json
        with open(mi.manifest_path(self.root, self.eid), encoding="utf-8") as fh:
            self.assertNotIn("частичный", json.load(fh), "повтор переписал манифест целиком")

    def test_коммит_переживает_смерть_до_закрытия_соединения(self):
        """WAL без checkpoint и без clean shutdown — коммит на месте
        (§5.1: восстановление не зависит от чистого завершения).

        Ребёнок — единственное соединение с базой: родитель закрывает своё
        до его запуска (последнее закрытие делает checkpoint и убирает WAL)
        и открывает новое только после убийства. Иначе выживший `-shm`
        родителя подсказывал бы новому соединению состояние WAL, и холодный
        старт — восстановление индекса WAL с нуля, как у демона после
        смерти, — не проверялся бы (Codex по #133, P2)."""
        self.con.close()
        self.assertEqual(self.wal(), 0, "последнее закрытие убрало WAL — чистый старт ребёнка")
        p, вижу = self.запустить("после-коммита")
        self.assertEqual(вижу, "stored 1")
        self.assertGreater(self.wal(), 0, "коммит ребёнка лежит в WAL, checkpoint не было")
        self.убить(p)
        wal = os.path.join(self.root, "contextd.db-wal")
        self.assertTrue(os.path.exists(wal), "WAL пережил смерть — восстанавливать есть что")
        # первое соединение после смерти: индекс WAL строится с нуля
        self.con = mi.connect(self.root)
        self.addCleanup(self.con.close)
        self.assertEqual((self.состояние(), self.работ()), ("stored", 1))
        self.assertEqual(self.цела(), "ok")
        self.con.execute("pragma wal_checkpoint(truncate)")
        self.assertEqual(self.цела(), "ok")
        contextd.finish_stored(self.con, self.root, self.eid)
        self.assertEqual(self.работ(), 1, "повтор после пережившего коммита не плодит работ")

    def test_замок_мертвеца_отпускается(self):
        """`begin immediate` в убитом процессе: пока он жив — `database is
        locked`, умер — запись проходит, ждать `busy_timeout` до конца не
        приходится."""
        p, _ = self.запустить("с-замком")
        self.замок_занят()
        self.убить(p)
        t = time.monotonic()
        with mi.транзакция(self.con):
            self.con.execute("insert into compute_nodes(id,name) values('n','x')")
        self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(self.цела(), "ok")

    def test_недописанный_стейджинг_не_становится_блобом(self):
        """Смерть между записью `.part` и `rename`: базе о файле ничего не
        известно, повтор с телефона принимает блоб целиком (§5.2: метаданные
        после fsync/rename)."""
        self.убить_на("стейджинг")
        путь = mi.blob_path(self.root, self.sha, "m4a")
        self.assertFalse(os.path.exists(путь), "половина байт не опубликована")
        self.assertTrue(glob.glob(os.path.join(os.path.dirname(путь), "*.part")))
        self.assertIsNone(self.con.execute("select 1 from blobs where sha256=?",
                                           (self.sha,)).fetchone())
        self.assertEqual(self.состояние(), "new")
        код, _ = contextd.ingest_audio(self.con, self.root, self.eid, io.BytesIO(ЗВУК),
                                       len(ЗВУК))
        self.assertEqual(код, 200)
        with open(путь, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), self.sha)
        self.assertEqual((self.состояние(), self.работ()), ("stored", 1))


if __name__ == "__main__":
    unittest.main()
