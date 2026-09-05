"""Уборка аудио и сверка состояния (ТЗ §7, §17).

Аудио живёт девяносто дней и исчезает само. Манифест не исчезает никогда: по
нему потом видно, что запись была и куда делась. Сверка чинит то, что чинится
однозначно, и только докладывает про то, где нужен человек.
"""
import os, sys, json, glob, time, subprocess, tempfile, unittest
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


class СверкаИсточников(unittest.TestCase):
    """Тишина источника с телефона и запись, обещанная, но не долитая (ТЗ §17)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mara-src-")
        self.con = mi.connect(self.root)

    def событие(self, source, days_ago, device="dev_x", blob=None, via=None):
        ev = {"kind": "message", "source": source, "source_id": "%s-%s-%d" % (source, device, days_ago),
              "device_id": device, "payload": {"text": "x", "via": via or "notification"}}
        if blob:
            ev.update(kind="call", blob={"sha256": blob, "ext": "m4a"})
        eid, _ = mi.put_event(self.con, ev)
        когда = (datetime.now(mi.TZ) - timedelta(days=days_ago)).isoformat(timespec="seconds")
        self.con.execute("update events set received=? where id=?", (когда, eid))
        return eid

    def устройство(self, dev="dev_x", hours_ago=1):
        seen = (datetime.now(mi.TZ) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        self.con.execute("insert or replace into devices(id,name,token_sha256,created,last_seen) "
                         "values(?,?,?,?,?)", (dev, "тел", "h", seen, seen))

    def test_телефон_на_связи_а_whatsapp_молчит(self):
        self.событие("whatsapp", 5); self.устройство()
        f = [x for x in rc.run(self.con, self.root, vault=None) if x["check"] == "источник-замолчал"]
        self.assertEqual([x["source"] for x in f], ["whatsapp"])
        self.assertEqual(f[0]["level"], "warn", "эвристика — в дневную сводку, не в код возврата")

    def test_телефон_сам_не_на_связи_не_находка(self):
        self.событие("sms", 5); self.устройство(hours_ago=72)
        self.assertEqual(rc.источник_замолчал(self.con), [])

    def test_никогда_не_слал_или_слал_недавно_не_находка(self):
        self.устройство()
        self.assertEqual(rc.источник_замолчал(self.con), [], "источник не подключали")
        self.событие("whatsapp", 1)
        self.assertEqual(rc.источник_замолчал(self.con), [], "вчера писали")

    def test_свежий_экспорт_не_прикрывает_умерший_слушатель(self):
        self.событие("whatsapp", 5); self.устройство()
        self.событие("whatsapp", 1, device="dev_imp", via="export"); self.устройство("dev_imp", hours_ago=100)
        f = rc.источник_замолчал(self.con)
        self.assertEqual([x["device"] for x in f], ["тел"], "телефон молчит, хоть импортёр и залил вчера")

    def test_старый_экспорт_сам_по_себе_не_находка(self):
        self.событие("whatsapp", 20, device="dev_imp", via="export"); self.устройство("dev_imp")
        self.assertEqual(rc.источник_замолчал(self.con), [], "экспорт — не живой источник")

    def test_обещанная_запись_не_долилась_за_сутки(self):
        свежий = self.событие("phone", 0, blob="b" * 64)
        self.assertEqual(rc.запись_не_долита(self.con), [], "свежий звонок ещё может долиться")
        старый = self.событие("phone", 2, blob="c" * 64)
        f = rc.запись_не_долита(self.con)
        self.assertEqual((f[0]["count"], f[0]["sample"]), (1, [старый]))
        self.assertNotIn(свежий, f[0]["sample"])

    def test_недоставленный_дайджест_видно_в_сверке(self):
        """N11: звонок разобран, а владелец о нём не узнал — это находка."""
        eid = self.событие("phone", 0)
        # failed — это сбой отправки: работа уйдёт в ретрай, а встанет насовсем
        # — скажет dlq(); здесь ждём только настроечную дыру
        for state, did in (("sent", "d1"), ("no-transport", "d2"), ("failed", "d3")):
            self.con.execute("insert into digests(id,event_id,chat_id,text,items_json,"
                             "sent_at,state) values(?,?,?,?,?,?,?)",
                             (did, eid, "@c", "текст", "[]", mi.now_iso(), state))
        f = rc.дайджест_не_доставлен(self.con)
        self.assertEqual((f[0]["count"], f[0]["sample"]), (1, [eid]),
                         "доставленный дайджест — не находка")
        self.con.execute("update digests set state='sent'")
        self.assertEqual(rc.дайджест_не_доставлен(self.con), [])

    def test_сводка_владельцу_только_о_проблемах(self):
        self.assertIsNone(rc.текст([]))
        self.assertIsNone(rc.текст([rc.находка("x", "fixed", "починено")]), "починенное — не проблема")
        t = rc.текст([rc.находка("x", "warn", "беда"), rc.находка("y", "error", "хуже")])
        self.assertIn("проблем 2", t)
        self.assertIn("• беда", t)


class Сырьё(unittest.TestCase):
    def test_raw_старше_срока_убирается_свежее_остаётся(self):
        root = tempfile.mkdtemp(prefix="mara-raw-")
        for name, d in (("tdlib", 40), ("gmail", 40), ("gmail", 3)):
            p = os.path.join(root, name, "raw", день(-d) + ".jsonl")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("x")
        все = lambda: sorted(os.path.basename(p) for p in glob.glob(os.path.join(root, "*", "raw", "*.jsonl")))
        self.assertEqual(br.raw_sweep(root, dry=True)["files"], 2)
        self.assertEqual(len(все()), 3, "холостой прогон удалил")
        self.assertEqual(br.raw_sweep(root), {"files": 2, "bytes": 2})
        self.assertEqual(все(), [день(-3) + ".jsonl"])
        self.assertEqual(br.raw_sweep(root)["files"], 0, "повтор нашёл что убирать")

    def test_плохой_MARA_RAW_DAYS_не_роняет_уборку_в_4_40(self):
        """Крон в 4:40 — отдельная единица со своим отказом.

        Сверка про опечатку доложит, но уборку не сделает: у неё свой процесс.
        Поэтому спрашиваем именно процесс, а не импорт, — так, как его зовёт
        `install/mara.cron`. Раньше он падал на `int("тридцать")` ещё до
        `main`, отдавал `EXIT=1` и не убирал ничего; сырьё копилось, и
        единственным сигналом была ежечасная жалоба соседа.

        Значения два, и второе опаснее первого: `-30` проходит через `int`
        молча и уносит порог в будущее, то есть удаляет всё сырьё разом.
        """
        for значение, кусок in (("тридцать", "не число"),
                                ("-30", "отрицательный срок")):
            with self.subTest(значение=значение):
                root = tempfile.mkdtemp(prefix="mara-raw-")
                старый, свежий = [
                    os.path.join(root, "tdlib", "raw", день(-d) + ".jsonl")
                    for d in (40, 3)]
                for p in (старый, свежий):
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    open(p, "w").write("x")
                r = subprocess.run(
                    [sys.executable,
                     os.path.join(ROOT, "scripts", "blob_retention.py"),
                     "--root", root],
                    env=dict(os.environ, MARA_RAW_DAYS=значение,
                             PYTHONIOENCODING="utf-8"),
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(0, r.returncode, r.stderr)
                # Причина в своём логе: сверка её тоже увидит, но раз в час, а
                # этот файл читают с вопросом «почему тридцать».
                self.assertIn("MARA_RAW_DAYS", r.stdout)
                self.assertIn(значение, r.stdout)
                self.assertIn(кусок, r.stdout)
                self.assertIn("беру 30", r.stdout)
                # И главное: прогон состоялся именно на дефолте, а не только
                # не упал. Свежий файл — половина оракула: откат на ноль
                # вместо тридцати тоже даёт `EXIT=0` и тоже «убирает», только
                # заодно вчерашние логи.
                self.assertFalse(os.path.exists(старый),
                                 "сырьё не убрано: " + r.stdout)
                self.assertTrue(os.path.exists(свежий),
                                "убрано свежее: " + r.stdout)

    def test_ноль_суток_валиден_и_жалобы_не_родит(self):
        """Граница заставы: `< 0`, а не `< 1`.

        Ноль осмыслен — «держать только сегодняшний день», — и жаловаться на
        него не на что. Без этого теста застава тихо съезжает на `<= 0` и
        отбирает у владельца настройку, о которой он не узнает: в логе будет
        «беру 30», а держаться будет месяц.
        """
        root = tempfile.mkdtemp(prefix="mara-raw-")
        вчера, сегодня = [
            os.path.join(root, "tdlib", "raw", день(-d) + ".jsonl")
            for d in (1, 0)]
        for p in (вчера, сегодня):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("x")
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "blob_retention.py"),
             "--root", root],
            env=dict(os.environ, MARA_RAW_DAYS="0", PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertNotIn("беру 30", r.stdout)
        self.assertFalse(os.path.exists(вчера), "вчерашнее не убрано")
        self.assertTrue(os.path.exists(сегодня), "убрано сегодняшнее")


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
