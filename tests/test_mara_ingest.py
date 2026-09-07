"""Приём: дедуп, аренда работ, расписание ретраев (ТЗ §17, §20)."""
import os, sys, sqlite3, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi

EV = {"kind": "call", "source": "phone", "source_id": "call-1",
      "occurred_at": "2026-09-02T14:05:00+03:00",
      "blob": {"sha256": "a" * 64, "bytes": 10, "mime": "audio/m4a", "ext": "m4a"}}


class Ingest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)

    def test_повтор_того_же_аудио_не_создаёт_второй_звонок(self):
        first, dup1 = mi.put_event(self.con, dict(EV))
        second, dup2 = mi.put_event(self.con, dict(EV))
        self.assertEqual(first, second)
        self.assertFalse(dup1)
        self.assertTrue(dup2)

    def test_дедуп_по_хешу_а_не_по_имени_источника(self):
        mi.put_event(self.con, dict(EV))
        _, dup = mi.put_event(self.con, dict(EV, source_id="другое-имя-того-же-файла"))
        self.assertTrue(dup, "аудио с тем же sha256 — тот же звонок")

    def test_событие_без_аудио_дедупится_по_source_id(self):
        m = {"kind": "message", "source": "telegram", "source_id": "msg-7"}
        mi.put_event(self.con, dict(m))
        _, dup = mi.put_event(self.con, dict(m))
        self.assertTrue(dup)

    def test_разные_источники_не_склеиваются(self):
        a = {"kind": "message", "source": "telegram", "source_id": "7"}
        b = {"kind": "message", "source": "sms", "source_id": "7"}
        mi.put_event(self.con, dict(a))
        _, dup = mi.put_event(self.con, dict(b))
        self.assertFalse(dup, "одинаковый id у разных источников — разные события")

    def test_аренда_работы_не_отдаёт_её_дважды(self):
        eid, _ = mi.put_event(self.con, dict(EV))
        mi.add_job(self.con, eid, "asr")
        self.assertIsNotNone(mi.claim_job(self.con))
        self.assertIsNone(mi.claim_job(self.con), "работа под арендой")

    def test_расписание_ретраев_из_тз(self):
        for attempts, want in enumerate(mi.RETRY):
            got = mi.next_delay(attempts)
            self.assertLessEqual(abs(got - want), want * 0.2 + 1,
                                 "попытка %d: %d вместо ~%d" % (attempts, got, want))

    def test_после_шестой_попытки_dlq(self):
        eid, _ = mi.put_event(self.con, dict(EV))
        jid = mi.add_job(self.con, eid, "asr")
        for _ in range(7):
            job = mi.claim_job(self.con, now=2 ** 31)
            if job is None:
                break
            mi.finish_job(self.con, job["id"], False, "тестовая ошибка")
        state = self.con.execute("select state from jobs where id=?", (jid,)).fetchone()[0]
        self.assertEqual(state, "dlq")

    def test_успех_закрывает_работу(self):
        eid, _ = mi.put_event(self.con, dict(EV))
        jid = mi.add_job(self.con, eid, "asr")
        mi.finish_job(self.con, jid, True)
        state = self.con.execute("select state from jobs where id=?", (jid,)).fetchone()[0]
        self.assertEqual(state, "done")

    def test_просроченная_аренда_возвращает_работу(self):
        eid, _ = mi.put_event(self.con, dict(EV))
        mi.add_job(self.con, eid, "asr")
        job = mi.claim_job(self.con)
        self.assertIsNotNone(job)
        # воркер упал, не закрыв работу: через LEASE_SEC она снова свободна
        again = mi.claim_job(self.con, now=2 ** 31)
        self.assertIsNotNone(again, "аренда протухла — работу надо отдать другому")
        self.assertEqual(again["id"], job["id"])

    def test_путь_блоба_раскладывает_по_годам(self):
        p = mi.blob_path(self.dir, "b" * 64, "m4a")
        self.assertIn("/calls/", p)
        self.assertTrue(p.endswith("b" * 64 + ".m4a"))

    def test_гонка_на_вставке_события_даёт_дубль_а_не_поломку(self):
        """N7. put_event делает select, потом insert. Телефон, проснувшись,
        досылает очередь разом: между этими двумя запросами успевает вставить
        другой запрос. Это дубль, а не 500 в ответ телефону."""
        второй = mi.connect(self.dir)

        class Опережающий:
            """Соединение, которое перед insert пускает вперёд конкурента."""

            def __init__(self, con):
                self.con, self.сработал = con, False

            def execute(self, sql, args=()):
                if sql.lstrip().startswith("insert into events") and not self.сработал:
                    self.сработал = True
                    mi.put_event(второй, dict(EV))
                return self.con.execute(sql, args)

        обгон = Опережающий(self.con)
        eid, dup = mi.put_event(обгон, dict(EV))
        self.assertTrue(обгон.сработал, "конкурент обязан был вклиниться")
        self.assertTrue(dup, "чужая вставка того же ключа — дубль")
        self.assertEqual(self.con.execute("select count(*) from events").fetchone()[0], 1)
        self.assertEqual(self.con.execute("select id from events").fetchone()["id"], eid)


class ГонкаЗаАренду:
    """Соединение, пускающее второго воркера ровно между `select` и `update`.

    Иначе гонку не поймать: она живёт в микросекундах между двумя запросами, и
    тест на потоках зеленел бы через раз. Здесь окно открывается руками.
    """

    def __init__(self, con, второй=None, в_щели=None):
        self.con, self.второй, self.влез = con, второй, False
        self.в_щели = в_щели

    def execute(self, sql, args=()):
        if sql.startswith("update jobs set lease_until") and not self.влез:
            self.влез = True
            if self.в_щели:
                self.в_щели(self.con)
            else:
                mi.claim_job(self.второй)
        return self.con.execute(sql, args)


class Работы(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.eid, _ = mi.put_event(self.con, dict(EV))

    def test_аренду_не_получают_двое_в_одну_щель(self):
        mi.add_job(self.con, self.eid, "asr")
        второй = mi.connect(self.dir)
        гонка = ГонкаЗаАренду(self.con, второй)
        self.assertIsNone(mi.claim_job(гонка),
                          "работа выдана обоим: аренда взята без проверки")
        self.assertTrue(гонка.влез, "щель не открылась — тест ничего не проверил")

    def test_доигравшему_не_выдают_уже_отработавшую(self):
        # аренда истекла на середине транскрипта, второй воркер работу выбрал,
        # а первый в этот момент доиграл: `finish_job` ставит `done` и сбрасывает
        # `lease_until` в ноль, не глядя на аренду. По одному `lease_until<?`
        # второй увидел бы `0 < now` и час GPU ушёл бы на уже сделанное
        jid = mi.add_job(self.con, self.eid, "asr")
        гонка = ГонкаЗаАренду(self.con, в_щели=lambda c: c.execute(
            "update jobs set state='done', lease_until=0 where id=?", (jid,)))
        self.assertIsNone(mi.claim_job(гонка),
                          "выдана отработавшая работа: в аренде нет проверки state")
        self.assertTrue(гонка.влез, "щель не открылась — тест ничего не проверил")

    def test_вторая_работа_того_же_вида_не_заводится(self):
        a = mi.add_job(self.con, self.eid, "asr")
        b = mi.add_job(self.con, self.eid, "asr")
        self.assertEqual(a, b, "повтор должен вернуть ту же работу")
        self.assertEqual(1, self.con.execute(
            "select count(*) from jobs where event_id=? and kind='asr'",
            (self.eid,)).fetchone()[0], "вторая расшифровка — второй час GPU")

    def test_работы_разных_видов_не_мешают_друг_другу(self):
        mi.add_job(self.con, self.eid, "asr")
        mi.add_job(self.con, self.eid, "extract")
        self.assertEqual(2, self.con.execute(
            "select count(*) from jobs where event_id=?", (self.eid,)).fetchone()[0])

    def test_поверх_отработавшей_работа_ставится_заново(self):
        jid = mi.add_job(self.con, self.eid, "asr")
        self.con.execute("update jobs set state='done' where id=?", (jid,))
        новая = mi.add_job(self.con, self.eid, "asr")
        self.assertNotEqual(jid, новая,
                            "починка руками поверх done не должна блокироваться")

    def test_поверх_брошенной_в_dlq_тоже(self):
        jid = mi.add_job(self.con, self.eid, "asr")
        self.con.execute("update jobs set state='dlq' where id=?", (jid,))
        self.assertNotEqual(jid, mi.add_job(self.con, self.eid, "asr"),
                            "сверка чинит именно dlq — ей нельзя мешать")


# Ровно то, что стоит на doctor: ключи ledger без `not null` (НБ12 из #39).
# Списано с mara_ingest.py на 31de30e — миграцию проверяем на настоящей
# старой форме, а не на её пересказе.
СТАРАЯ_СХЕМА = """
create table if not exists commitments(
  id text primary key, title text, status text, owner text, promised_to text,
  due text, due_explicit text, origin_event text, source_native_id text unique,
  created text, occurred text, valid_from text, confidence real,
  supersedes text, classification text);
create table if not exists conversations(
  id text primary key, title text, occurred text, valid_from text,
  origin_event text, source_native_id text unique, created text,
  classification text);
create table if not exists projections(
  path text primary key, object_kind text, object_id text,
  content_sha256 text, written text);
create index if not exists projections_object on projections(object_id);
"""

КЛЮЧИ = (("commitments", "id"), ("commitments", "source_native_id"),
         ("conversations", "id"), ("conversations", "source_native_id"),
         ("projections", "path"))


class СхемаЛеджера(unittest.TestCase):
    """НБ12: ключи ledger обязаны быть `not null`.

    `text primary key` в SQLite null не запрещает — наследие, которое там
    признали ошибкой и не чинят ради совместимости. Без явного `not null`
    объект без ключа ложится в базу и всплывает уже дублем.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def старая_база(self):
        """База в форме, которая сейчас лежит на doctor, с данными внутри."""
        os.makedirs(self.dir, exist_ok=True)
        con = sqlite3.connect(os.path.join(self.dir, "contextd.db"),
                              isolation_level=None)
        con.row_factory = sqlite3.Row
        con.executescript(СТАРАЯ_СХЕМА)
        con.execute("insert into commitments(id,title,source_native_id) "
                    "values('c1','смета','vault:kb/commitments/a.md')")
        con.execute("insert into conversations(id,title,source_native_id) "
                    "values('v1','звонок','call/2026-09-01')")
        con.execute("insert into projections(path,object_kind,object_id) "
                    "values('kb/commitments/a.md','commitment','c1')")
        con.close()

    def test_пустой_ключ_в_ledger_не_принимается(self):
        con = mi.connect(self.dir)
        for таблица, поле in КЛЮЧИ:
            with self.subTest(таблица=таблица, поле=поле):
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute("insert into %s(%s) values(null)"
                                % (таблица, поле))

    def test_старая_база_доезжает_сама(self):
        """Аддитивная миграция такое не умеет: `add column` уже созданную
        колонку не меняет. Значит перестройка — и она обязана случиться при
        первом же открытии, как и добавление `scopes`."""
        self.старая_база()
        con = mi.connect(self.dir)
        for таблица, поле in КЛЮЧИ:
            флаги = {r["name"]: r["notnull"] for r in
                     con.execute("pragma table_info(%s)" % таблица)}
            self.assertEqual(флаги[поле], 1,
                             "%s.%s осталась необязательной" % (таблица, поле))

    def test_перестройка_не_теряет_строки(self):
        self.старая_база()
        con = mi.connect(self.dir)

        def одно(sql):
            return con.execute(sql).fetchone()[0]

        self.assertEqual(одно("select title from commitments"), "смета")
        self.assertEqual(одно("select title from conversations"), "звонок")
        self.assertEqual(одно("select object_id from projections"), "c1")

    def test_перестройка_возвращает_индекс(self):
        """`alter table rename` уводит индекс за таблицей, а `create index if
        not exists` потом видит занятое имя и молча ничего не делает. Тогда
        сверка проекций теряет свой индекс и никто об этом не узнаёт."""
        self.старая_база()
        con = mi.connect(self.dir)
        имена = {r["name"] for r in
                 con.execute("pragma index_list(projections)")}
        self.assertIn("projections_object", имена)

    def test_срыв_посреди_перестройки_откатывает_всё(self):
        """Перестройка идёт одной транзакцией. Оборвись она на середине —
        база обязана остаться в прежней форме и со строками, а не с новой
        таблицей, хвостом `_old` и данными в двух местах сразу.

        Рвём на последней таблице `ЛЕДЖЕР`, а не на первой: срыв на первой
        оставляет тест зелёным и тогда, когда транзакция разбита на три — по
        коммиту на таблицу. Проверять надо, что откат уносит и уже
        перестроенных предшественников, а утверждения ниже про
        `commitments` — как раз про такого предшественника."""
        self.старая_база()
        con = sqlite3.connect(os.path.join(self.dir, "contextd.db"),
                              isolation_level=None)
        con.row_factory = sqlite3.Row

        class Срыв:
            """Соединение, роняющее последнюю таблицу."""

            def __init__(self, con):
                self.con = con

            def __getattr__(self, имя):
                return getattr(self.con, имя)   # чтобы подмена ловилась

            def execute(self, sql, args=()):
                if sql.startswith("drop table projections_old"):
                    raise sqlite3.OperationalError("место на диске кончилось")
                return self.con.execute(sql, args)

        with self.assertRaises(sqlite3.OperationalError):
            mi._ужать_ledger(Срыв(con))
        флаги = {r["name"]: r["notnull"] for r in
                 con.execute("pragma table_info(commitments)")}
        self.assertEqual(флаги["id"], 0, "форма не вернулась к прежней")
        self.assertEqual(
            con.execute("select count(*) from commitments").fetchone()[0], 1)
        self.assertEqual([r["name"] for r in con.execute(
            "select name from sqlite_master where name like '%_old'")], [])

    def test_второе_открытие_не_лезет_в_запись(self):
        """Ранний выход — не украшение. Без него каждое открытие базы брало
        бы `begin immediate`, то есть блокировку на запись, ради трёх
        `pragma`; а открывают базу демон на каждый HTTP-поток, бэкап, ретеншн
        и весь крон."""
        self.старая_база()
        mi.connect(self.dir).close()
        con = mi.connect(self.dir)

        class Счётчик:
            """Соединение, запоминающее, о чём его просили."""

            def __init__(self, con):
                self.con, self.было = con, []

            def __getattr__(self, имя):
                return getattr(self.con, имя)

            def execute(self, sql, args=()):
                self.было.append(sql)
                return self.con.execute(sql, args)

        счёт = Счётчик(con)
        mi._ужать_ledger(счёт)
        self.assertEqual(
            [с for с in счёт.было
             if not с.startswith("pragma table_info(")], [],
            "перестройка на уже перестроенной базе полезла в запись")

    def test_второе_открытие_ничего_не_перестраивает(self):
        self.старая_база()
        mi.connect(self.dir).close()
        con = mi.connect(self.dir)
        остатки = [r["name"] for r in con.execute(
            "select name from sqlite_master where name like '%_old'")]
        self.assertEqual(остатки, [], "хвосты перестройки остались в базе")
        self.assertEqual(
            con.execute("select count(*) from commitments").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
