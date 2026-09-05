"""Уборка аудио и сверка состояния (ТЗ §7, §17).

Аудио живёт девяносто дней и исчезает само. Манифест не исчезает никогда: по
нему потом видно, что запись была и куда делась. Сверка чинит то, что чинится
однозначно, и только докладывает про то, где нужен человек.
"""
import os, sys, io, json, glob, time, subprocess, tempfile, unittest
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
        # Сутки выбраны по границе, а не «одно старое, одно свежее»: условие
        # отбора — `имя >= порог`, значит при тридцати днях `-31` уходит, а
        # `-30` остаётся, и мимо проходит ровно тридцать. На паре −40/−3
        # зелёным был любой срок от трёх до тридцати девяти, то есть
        # дефолт «тридцать» не был закреплён ничем. Нашёл ревьюер, круг 1.
        for name, d in (("tdlib", 31), ("gmail", 31), ("gmail", 30)):
            p = os.path.join(root, name, "raw", день(-d) + ".jsonl")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("x")
        все = lambda: sorted(os.path.basename(p) for p in glob.glob(os.path.join(root, "*", "raw", "*.jsonl")))
        self.assertEqual(br.raw_sweep(root, dry=True)["files"], 2)
        self.assertEqual(len(все()), 3, "холостой прогон удалил")
        self.assertEqual(br.raw_sweep(root), {"files": 2, "bytes": 2})
        self.assertEqual(все(), [день(-30) + ".jsonl"])
        self.assertEqual(br.raw_sweep(root)["files"], 0, "повтор нашёл что убирать")

    def test_плохой_MARA_RAW_DAYS_не_роняет_уборку_в_4_40(self):
        """Крон в 4:40 — отдельная единица со своим отказом.

        Сверка про опечатку доложит, но уборку не сделает: у неё свой процесс.
        Поэтому спрашиваем именно процесс, а не импорт, — так, как его зовёт
        `install/mara.cron`. Раньше он падал на `int("тридцать")` ещё до
        `main`, отдавал `EXIT=1` и не убирал ничего; сырьё копилось, и
        единственным сигналом была ежечасная жалоба соседа.

        Минус опаснее нечислового: он проходит разбор молча и уносит порог
        в будущее, то есть удаляет всё сырьё разом. Берём `-1`, а не `-30`:
        застава стоит на `< 0`, и сужение до `< -1` на тридцатке было бы
        зелёным, а `-1` тогда прошёл бы молча и снёс всё сырьё, включая
        сегодняшнее. Нашёл ревьюер, круг 1.

        Две дробные записи здесь не про дробь, а про порядок застав. `-0.5`
        обязан считаться минусом до округления: округляем вверх — и `-0.5`
        даёт ноль, «держать только сегодняшний день», то есть ровно ту
        потерю, против которой застава стоит, только молча. `nan` же
        проходит обе заставы насквозь: он не меньше нуля и не больше
        потолка, а `ceil(nan)` бросает уже на импорте — крон в 4:40 снова
        отдаёт `EXIT=1` и не убирает ничего.

        `-0.0` — пятый класс, найденный ревьюером в круге 1, и он опаснее
        всех: `-0.0 < 0` ложно, `ceil(-0.0)` даёт ноль, жалобы нет, и в
        ближайшие 4:40 уходит всё сырьё, кроме сегодняшнего. Застава теперь
        смотрит на знак, а не на сравнение; без этого значения мутант
        `elif СРОК < -0.4` проходил полный гейт зелёным.

        Пустая строка — самая частая правка crontab: владелец стирает
        значение, оставляя присвоение. Молча взять тридцать здесь нельзя по
        той же причине, что и на «тридцать»: если стояло девяносто, потеря
        шестидесяти суток пройдёт без единой строки. Мутант, берущий дефолт
        молча, до этого значения полный гейт проходил зелёным. Нашёл
        ревьюер, круг 1.
        """
        for значение, кусок in (("тридцать", "не число"),
                                ("nan", "не число"),
                                ("", "не число"),
                                ("-1", "отрицательный срок"),
                                ("-0.5", "отрицательный срок"),
                                ("-0.0", "отрицательный срок")):
            with self.subTest(значение=значение):
                root = tempfile.mkdtemp(prefix="mara-raw-")
                # −31/−30, а не −40/−3: закрепляет, что откат идёт именно
                # на тридцать. С парой пошире мутант «беру 29» (в тексте
                # по-прежнему «30») проходил гейт зелёным.
                старый, свежий = [
                    os.path.join(root, "tdlib", "raw", день(-d) + ".jsonl")
                    for d in (31, 30)]
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
                # Целиком «MARA_RAW_DAYS='-1'», а не два вхождения по
                # отдельности: голое `-1` есть в любом таймстампе через
                # `2026-11-05`, и тест был бы зелёным от одной даты. Кавычки
                # из `%r` в дате не встречаются никогда.
                self.assertIn("MARA_RAW_DAYS=%r" % значение, r.stdout)
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

    def test_огромный_MARA_RAW_DAYS_не_роняет_уборку(self):
        """Третий класс плохого значения, и он ронял крон ровно так же.

        `timedelta(days=1000000)` бросает `OverflowError` внутри `raw_sweep`,
        то есть уже после чистого импорта: сверка про это не узнаёт вовсе,
        `код()` даёт ноль, и сводка в 8:00 говорит «всё сходится», пока сырьё
        копится. Хуже, чем было до правки, где сверка хотя бы ругалась.

        Оракул перевёрнут относительно соседей: старый файл обязан
        **выжить**. Откат на тридцать тоже дал бы `EXIT=0` и тоже «убрал бы»,
        только заодно снёс бы всё, что владелец этим числом просил сохранить.
        Нашёл ревьюер, круг 1.

        Пара стоит ровно на потолке, а не «где-то за месяцем»: сорокадневный
        файл переживал любой потолок от сорока и выше, и три мутанта проходили
        полный гейт зелёным — `ПОТОЛОК = 365000`, откат на `730000` и текст
        «беру 30» при клампе на потолок. Обе даты представимы: 1926 год, а
        `date.min` — примерно 740 тысяч суток назад, запас двадцатикратный.
        Нашёл ревьюер, круг 2.
        """
        root = tempfile.mkdtemp(prefix="mara-raw-")
        старый, свежий = [
            os.path.join(root, "tdlib", "raw", день(-d) + ".jsonl")
            for d in (36501, 36500)]
        for f in (старый, свежий):
            os.makedirs(os.path.dirname(f), exist_ok=True)
            open(f, "w").write("x")
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "blob_retention.py"),
             "--root", root],
            env=dict(os.environ, MARA_RAW_DAYS="1000000",
                     PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("MARA_RAW_DAYS=%r" % "1000000", r.stdout)
        self.assertIn("больше ста лет", r.stdout)
        # Перевод строки в конце — не украшение: голое «беру 36500» есть
        # внутри «беру 365000», и текстовый оракул на потолок в 365 тысяч
        # был бы зелёным. Число здесь литерал, а не `br.ПОТОЛОК`:
        # константа, взятая у самого модуля, совпадёт с любой мутацией.
        self.assertIn("беру 36500\n", r.stdout)
        self.assertFalse(os.path.exists(старый),
                         "потолок не убирает даже за своей границей: "
                         + r.stdout)
        self.assertTrue(os.path.exists(свежий),
                        "срок урезали до дефолта и снесли сохраняемое: "
                        + r.stdout)

    def test_ровно_потолок_валиден_и_жалобы_не_родит(self):
        """Вторая граница той же заставы: `> ПОТОЛОК`, а не `>=`.

        Число в жалобе — совет владельцу: «беру 36500» читается как «столько
        можно, ставь». Мутант `elif RAW_DAYS >= ПОТОЛОК:` полный гейт проходил
        зелёным, и владелец, послушавшийся совета, получал на здоровом
        конфиге ежечасный `error` — навсегда, потому что чинить нечего.
        Прогон при этом не меняется вовсе: кламп даёт то же самое число, и
        отличить ветку можно только по жалобе. Нашёл ревьюер, круг 3 в #49.
        """
        root = tempfile.mkdtemp(prefix="mara-raw-")
        старый, свежий = [
            os.path.join(root, "tdlib", "raw", день(-d) + ".jsonl")
            for d in (36501, 36500)]
        for p in (старый, свежий):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("x")
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "blob_retention.py"),
             "--root", root],
            env=dict(os.environ, MARA_RAW_DAYS="36500",
                     PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr)
        # Имя переменной целиком, а не «беру»: любая из трёх жалоб печатает
        # `MARA_RAW_DAYS=`, а штатный прогон — никогда.
        self.assertNotIn("MARA_RAW_DAYS", r.stdout)
        self.assertFalse(os.path.exists(старый), "за потолком не убрано")
        self.assertTrue(os.path.exists(свежий), "убрано на самом потолке")

    def test_дробный_срок_округляется_вверх_и_жалуется(self):
        """Четвёртый класс, и до правки он уезжал в «не число, беру 30».

        `int("44.5")` бросает `ValueError`, то есть дробная запись попадала
        не в свою ветку, а в первую: владелец, поставивший полтора месяца,
        получал месяц, и ближайший прогон в 4:40 сносил пятнадцать суток
        сырья, которые он просил сохранить. Вероятность не умозрительная —
        двумя строками выше в `install/mara.cron` стоит
        `MARA_CORE_BACKUP_MAX_DAYS=2.2`, дробное значение в соседней
        переменной того же блока.

        Округление вверх, а не усечение: у двух подмен разная цена. `44.5 →
        44` удаляет сутки сырья, которые владелец просил держать, `44.5 →
        45` держит лишние сутки диска. Модуль различает ветки именно ценой
        подмены, и обратимая ошибка дешевле. Оракул −46/−45 это и проверяет:
        при усечении порог встаёт на сутки ближе и сорокапятидневный файл
        умирает.

        Значений два, и второе — весь смысл первого. `44.5` стоит ровно в
        точке, где округление вверх и округление до ближайшего совпадают
        (`int(44.5 + 0.5)` — те же сорок пять), поэтому на нём решение PR не
        проверяется вовсе: мутант `int(СРОК + 0.5)` проходил полный гейт
        зелёным, хотя `44.2` у него уезжает в сорок четыре. `44.2` — точка,
        где вверх, вниз и к ближайшему расходятся все трое. Нашёл ревьюер,
        круг 1.
        """
        for значение in ("44.5", "44.2"):
            with self.subTest(значение=значение):
                root = tempfile.mkdtemp(prefix="mara-raw-")
                старый, свежий = [
                    os.path.join(root, "tdlib", "raw", день(-d) + ".jsonl")
                    for d in (46, 45)]
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
                self.assertIn("MARA_RAW_DAYS=%r" % значение, r.stdout)
                self.assertIn("дробный срок", r.stdout)
                # Перевод строки, как у потолка: голое «беру 45» есть внутри
                # «беру 450», и оракул на усечение до сорока пяти сотен был бы
                # зелёным.
                self.assertIn("беру 45\n", r.stdout)
                self.assertFalse(os.path.exists(старый),
                                 "сырьё не убрано: " + r.stdout)
                self.assertTrue(os.path.exists(свежий),
                                "срок урезали и снесли сохраняемое: "
                                + r.stdout)

    def test_целое_с_точкой_срок_не_меняет_и_молчит(self):
        """`45.0` — те же сорок пять суток, и жаловаться тут не на что.

        Жалоба уходит владельцу в сводку 8:00 каждое утро, пока конфиг не
        поправят, а поправлять нечего: `45.0` и `45` дают один порог.
        Прецедент — `test_ровно_потолок_валиден_и_жалобы_не_родит`, где
        владелец, послушавшийся совета из жалобы, получал на здоровом
        конфиге ежечасный `error` навсегда. Здесь это ещё вероятнее:
        дробную запись владельцу подсказывает соседняя строка крона.
        """
        root = tempfile.mkdtemp(prefix="mara-raw-")
        старый, свежий = [
            os.path.join(root, "tdlib", "raw", день(-d) + ".jsonl")
            for d in (46, 45)]
        for p in (старый, свежий):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("x")
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "blob_retention.py"),
             "--root", root],
            env=dict(os.environ, MARA_RAW_DAYS="45.0",
                     PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr)
        # Имя переменной целиком, а не «беру»: любая из четырёх жалоб печатает
        # `MARA_RAW_DAYS=`, а штатный прогон — никогда.
        self.assertNotIn("MARA_RAW_DAYS", r.stdout)
        self.assertFalse(os.path.exists(старый),
                         "сырьё не убрано: " + r.stdout)
        self.assertTrue(os.path.exists(свежий),
                        "точка в записи урезала срок: " + r.stdout)

    def test_бесконечность_читается_как_потолок_а_не_как_мусор(self):
        """`inf` значит «не убирать», и место ему в ветке потолка.

        До правки разбор начинался с `int`, а `int("inf")` бросает
        `ValueError`: бесконечность уезжала в «не число, беру 30» — ровно в
        ту потерю, против которой ветка потолка и построена. Владелец,
        написавший `inf` вместо шести знаков, получал месяц и лишался всего
        остального в ближайшие 4:40.

        Оракул перевёрнут так же, как у шести знаков: старый файл обязан
        выжить. `float` бесконечность берёт молча, бросает на ней уже
        округление — значит застава на потолок обязана стоять **до** него.
        """
        root = tempfile.mkdtemp(prefix="mara-raw-")
        старый, свежий = [
            os.path.join(root, "tdlib", "raw", день(-d) + ".jsonl")
            for d in (36501, 36500)]
        for p in (старый, свежий):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("x")
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "blob_retention.py"),
             "--root", root],
            env=dict(os.environ, MARA_RAW_DAYS="inf",
                     PYTHONIOENCODING="utf-8"),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("MARA_RAW_DAYS=%r" % "inf", r.stdout)
        self.assertIn("больше ста лет", r.stdout)
        self.assertIn("беру 36500\n", r.stdout)
        self.assertFalse(os.path.exists(старый),
                         "потолок не убирает даже за своей границей: "
                         + r.stdout)
        self.assertTrue(os.path.exists(свежий),
                        "бесконечность прочли как мусор и урезали до "
                        "дефолта: " + r.stdout)

    def test_значение_MARA_RAW_DAYS_из_крона_совпадает_с_дефолтом_кода(self):
        """Зеркало `test_значение_из_крона_совпадает_с_дефолтом_кода`.

        Разъехавшись, эти двое молчат: в бою побеждает crontab. Заодно это
        единственное место, где закреплён сам дефолт «тридцать» на штатном
        пути — без переменной вовсе. В ТЗ этого числа нет: месяц выбран
        докстрингом модуля, и crontab обязан повторять именно его.
        """
        with io.open(os.path.join(ROOT, "install", "mara.cron"),
                     encoding="utf-8") as ф:
            строки = ф.readlines()
        # Имя до `=` со снятыми пробелами, а не `startswith`: по
        # `crontab(5)` пробелы вокруг знака необязательны, и законную строку
        # `MARA_RAW_DAYS = 30` зеркало не узнавало вовсе — падало на
        # `assertEqual(len(присвоения), 1)`, обвиняя конфиг в отсутствии
        # строки, которая там есть. Нашёл ревьюер, круг 3.
        присвоения = [i for i, l in enumerate(строки)
                      if l.partition("=")[0].strip() == "MARA_RAW_DAYS"]
        работы = [i for i, l in enumerate(строки) if "blob_retention.py" in l]
        self.assertEqual(len(присвоения), 1, присвоения)
        self.assertEqual(len(работы), 1, работы)
        # Половина смысла — порядок: `VAR=` в crontab действует только на
        # работы ниже себя, и сказано это в самом файле. Присвоение, уехавшее
        # под работу, по-прежнему совпадает с дефолтом кода и по-прежнему
        # мертво: сверять значение, не сверяя место, — сверять половину.
        # Нашёл ревьюер, круг 2.
        self.assertLess(присвоения[0], работы[0],
                        "присвоение ниже работы — работа его не увидит")
        # Печатаем и жалобу, а не один срок. Сравнивая числа, зеркало
        # видело бы только «в кроне другое годное число»: у опечатки, чей
        # откат равен тридцати (`тридцать`, `-0.0`, `-1`, пустое значение),
        # оба прогона дают тридцать — один потому, что переменной нет,
        # другой потому, что модуль откатился после жалобы. Такая опечатка
        # проезжала гейт зелёной и жила бы до тех пор, пока владелец сам не
        # прочтёт ночную жалобу. Нашёл ревьюер, круг 2 — в правке, которую
        # сам же и предложил кругом раньше.
        код = ("import sys; sys.path.insert(0, %r); import blob_retention"
               " as br; print(br.RAW_DAYS, br.ОШИБКА_КОНФИГА)"
               % os.path.join(ROOT, "scripts"))
        окр = {k: v for k, v in os.environ.items() if k != "MARA_RAW_DAYS"}
        r = subprocess.run([sys.executable, "-c", код], env=окр,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        # Значение из crontab отдаём тому же модулю, а не разбираем здесь
        # сами. Свой разбор был бы вторым экземпляром правила округления —
        # его пришлось бы чинить каждый раз вместе с первым, а `int("30.0")`
        # уронил бы зеркало трейсбеком на конфиге, который модуль принимает.
        # Так правило живёт в одном месте по построению, а не по
        # договорённости. Предложил ревьюер, круг 1.
        # Парные кавычки снимаем: `crontab(5)` разрешает их, чтобы сохранить
        # краевые пробелы, и модулю крон отдаёт значение уже без них. Отдав
        # кавычки внутрь, зеркало краснело на `MARA_RAW_DAYS="30"` с
        # сообщением про разъехавшийся дефолт — то есть обвиняло модуль в
        # том, чего он не делал. Голый `.strip("\"'")` тут неверен: он снял
        # бы и непарную кавычку, которую крон оставляет на месте, и зеркало
        # стало бы снисходительнее крона. Нашёл ревьюер, круг 3.
        значение = строки[присвоения[0]].partition("=")[2].strip()
        if len(значение) > 1 and значение[0] == значение[-1] \
                and значение[0] in "\"'":
            значение = значение[1:-1]
        rк = subprocess.run([sys.executable, "-c", код],
                            env=dict(окр, MARA_RAW_DAYS=значение),
                            capture_output=True, text=True)
        self.assertEqual(rк.returncode, 0, rк.stderr)
        # Жалоба проверяется первой, и порядок тут — весь смысл. Эталон
        # печатает `30 None` всегда: жалующийся дефолт упал бы `KeyError`-ом
        # и был бы пойман возвратом выше. Значит на любой опечатке в кроне
        # первым падал `assertEqual` — с текстом про разъехавшийся дефолт,
        # хотя разъехаться нечему, в кроне опечатка. Точная формулировка
        # лежала строкой ниже и не показывалась никогда. Нашёл ревьюер,
        # круг 3.
        self.assertNotIn("MARA_RAW_DAYS", rк.stdout,
                         "значение из crontab рождает жалобу: " + значение)
        self.assertEqual(rк.stdout, r.stdout,
                         "crontab и дефолт модуля разъехались: " + значение)


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

    def test_жалоба_конфига_и_просроченные_доезжают_вместе(self):
        """Две находки из одной проверки, и вход у них разный.

        Порознь каждая покрыта, вместе — не была: мутант, где сбор жалобы
        конфига затирается списком просроченных (`ф.append(...)` →
        `ф = [...]`), полный гейт проходил зелёным. А в бою это самое частое
        сочетание: опечатка в сроке ровно и означает, что уборка не идёт и
        просроченное копится. Нашёл ревьюер, круг 1.
        """
        root, con, eid, sha, path = стенд()
        было = br.ОШИБКА_КОНФИГА
        br.ОШИБКА_КОНФИГА = "MARA_RAW_DAYS='пять' — не число, беру 30"
        self.addCleanup(setattr, br, "ОШИБКА_КОНФИГА", было)
        виды = [f["check"] for f in rc.run(con, root, vault=None)]
        self.assertIn("ретеншен-конфиг", виды)
        self.assertIn("ретеншен-просрочен", виды)

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
