"""Т2.1б: время вне `events` — по времени, а не по строке.

Метки `now_iso()` несут сдвиг `MARA_TZ_HOURS`. После смены пояса в одной
колонке лежат строки с разными сдвигами, и лексический порядок перестаёт
быть временным: `…T23:00:00+14:00` (09:00 UTC) лексически позже
`…T20:00:00+00:00` (20:00 UTC). У `events` для этого есть вычисляемые
`*_utc` (миграция 7); здесь — остальные таблицы, которые читаются «последний»
или «по порядку». В каждом тесте две строки: более поздняя во времени
записана в +00:00, более ранняя — в +14:00, так что по строке выиграла бы
ранняя."""
import importlib.util, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import mara_ingest as mi
import call_asr
import call_extract
import contextd
import contextd_reconcile as cr


def _модуль(name):
    p = os.path.join(ROOT, "scripts", name)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_")[:-3], p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def метки(часов_назад_рано=3, часов_назад_поздно=1):
    """`(рано, поздно)`: ранняя в +14:00, поздняя в +00:00 — по строке
    ранняя больше."""
    сейчас = datetime.now(timezone.utc)
    рано = (сейчас - timedelta(hours=часов_назад_рано)).astimezone(
        timezone(timedelta(hours=14))).isoformat(timespec="seconds")
    поздно = (сейчас - timedelta(hours=часов_назад_поздно)).astimezone(
        timezone(timedelta(hours=0))).isoformat(timespec="seconds")
    assert рано > поздно, "стенд: по строке ранняя должна быть больше"
    return рано, поздно


class _База(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)

    def tearDown(self):
        self.con.close()

    def событие(self, sid="x"):
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": sid, "payload": {}})
        return eid


class ПоследняяРевизия(_База):
    """«Последняя расшифровка» и «последнее извлечение» события — по времени.
    Id у ранней нарочно больше: порядок не должен держаться на id."""

    def test_последняя_расшифровка(self):
        eid = self.событие()
        рано, поздно = метки()
        for tid, когда in (("t_b", рано), ("t_a", поздно)):
            self.con.execute("insert into transcripts(id,event_id,created) values(?,?,?)",
                             (tid, eid, когда))
        self.assertEqual(call_asr.сегменты_события(self.con, eid)[0], "t_a")

    def test_последнее_извлечение(self):
        eid = self.событие()
        рано, поздно = метки()
        for xid, когда, n in (("x_b", рано, 1), ("x_a", поздно, 2)):
            self.con.execute("insert into extractions(id,event_id,data_json,created) "
                             "values(?,?,?,?)", (xid, eid, '{"n": %d}' % n, когда))
        self.assertEqual(call_extract.извлечение_события(self.con, eid),
                         ("x_a", {"n": 2}))


class Устройства(_База):
    """Свежесть устройства в `/metrics` — самая новая по времени, и у имени, и
    у `mara_mobile_last_seen_seconds`."""

    def test_свежесть_по_времени(self):
        рано, поздно = метки(3, 1)
        for did, когда in (("d_b", рано), ("d_a", поздно)):
            self.con.execute("insert into devices(id,name,token_sha256,created,last_seen) "
                             "values(?,?,?,?,?)", (did, "телефон", did, рано, когда))
        строки = contextd.metrics(self.con, self.dir, None).splitlines()
        for имя in ('mara_device_last_seen_seconds{name="телефон"} ',
                    "mara_mobile_last_seen_seconds "):
            v = float([l for l in строки if l.startswith(имя)][0].split()[1])
            self.assertTrue(3500 <= v <= 3700, (имя, v))


class Дайджесты(_База):
    def дайджест(self, did, eid, state, когда):
        self.con.execute("insert into digests(id,event_id,text,sent_at,state) "
                         "values(?,?,?,?,?)", (did, eid, "текст " + did, когда, state))

    def test_последний_дайджест_в_bootstrap(self):
        рано, поздно = метки()
        self.дайджест("g_b", "e_b", "sent", рано)
        self.дайджест("g_a", "e_a", "sent", поздно)
        self.assertEqual(contextd.bootstrap(self.con)["last_digest"]["id"], "g_a")

    def test_догрузка_и_недоставка_по_порядку_времени(self):
        рано, поздно = метки()
        # вставляем позднюю первой: порядок вставки тоже не должен решать
        for state in ("stale", "no-transport"):
            self.дайджест("g_a_" + state, "e_a_" + state, state, поздно)
            self.дайджест("g_b_" + state, "e_b_" + state, state, рано)
        н, = cr.дайджест_догрузка(self.con)
        self.assertEqual(н["sample"], ["e_b_stale", "e_a_stale"])
        н, = cr.дайджест_не_доставлен(self.con)
        self.assertEqual(н["sample"], ["e_b_no-transport", "e_a_no-transport"])


class Очередь(_База):
    def test_dlq_называет_последнюю_по_времени(self):
        рано, поздно = метки()
        for jid, kind, когда in (("j_a", "asr", поздно), ("j_b", "extract", рано)):
            self.con.execute("insert into jobs(id,event_id,kind,state,last_error,"
                             "created,updated) values(?,?,?,?,?,?,?)",
                             (jid, "e", kind, "dlq", "ValueError: " + kind, рано, когда))
        н, = cr.dlq(self.con)
        self.assertIn("последняя asr", н["detail"])

    def test_outbox_по_порядку_создания(self):
        рано, поздно = метки()
        for oid, когда in (("o_a", поздно), ("o_b", рано)):
            self.con.execute("insert into outbox(id,kind,payload_json,created) "
                             "values(?,?,?,?)", (oid, "telegram", '{"id": "%s"}' % oid, когда))
        отправлено = []
        mi.из_outbox(self.con, lambda kind, p: отправлено.append(p["id"]) or "sent")
        self.assertEqual(отправлено, ["o_b", "o_a"])


class БлобыДляУчения(_База):
    """`core-backup.строки_блобов` — «свежие первыми»: свежие сверяются
    учением каждую ночь, остальные — по кругу."""

    def test_свежие_первыми_по_времени(self):
        рано, поздно = метки()
        for sha, когда in (("b" * 64, рано), ("a" * 64, поздно)):
            self.con.execute("insert into blobs(sha256,path,bytes,created) values(?,?,?,?)",
                             (sha, "/нет/" + sha, 1, когда))
        self.con.commit()
        cb = _модуль("core-backup.py")
        строки = cb.строки_блобов(os.path.join(self.dir, "contextd.db"))
        self.assertEqual([r["sha256"][0] for r in строки], ["a", "b"])


class ВитокПеречитки(unittest.TestCase):
    """Курсор перечитки старых копий (`проверить_аудио`) идёт по времени:
    после смены пояса строка со старым сдвигом не встаёт перед курсором."""

    def test_виток_по_времени(self):
        import json
        cb = _модуль("core-backup.py")
        tmp = tempfile.mkdtemp()
        root, цель = os.path.join(tmp, "blobs"), os.path.join(tmp, "target")
        def строка(имя, когда):
            sha = (имя * 64)[:64]
            p = os.path.join(root, "calls", имя + ".wav")
            for путь in (p, os.path.join(цель, "calls", имя + ".wav.gpg")):
                os.makedirs(os.path.dirname(путь), exist_ok=True)
                open(путь, "wb").close()
            return {"sha256": sha, "path": p, "created": когда}
        свежие = [строка(x, "2026-10-13T20:00:00+00:00") for x in "xyz"][:cb.ПРОБА]
        # по времени: a (09:00Z) < b (10:00Z) < c (11:00Z) < d (12:30Z);
        # по строке a (+03:00) стоит третьей
        старые = [строка("a", "2026-10-12T12:00:00+03:00"),
                  строка("b", "2026-10-12T10:00:00+00:00"),
                  строка("c", "2026-10-12T11:00:00+00:00"),
                  строка("d", "2026-10-12T12:30:00+00:00")]
        хеши = {os.path.join(цель, os.path.relpath(r["path"], root) + ".gpg"): r["sha256"]
                for r in свежие + старые}
        курсор = os.path.join(tmp, "курсор.json")
        было = cb.sha_шифра
        cb.sha_шифра = lambda путь, пароль: хеши[путь]
        self.addCleanup(setattr, cb, "sha_шифра", было)
        def ночь():
            итог = cb.проверить_аудио(None, root, цель, "/нет-фразы",
                                      строки=свежие + старые, курсор=курсор)
            with open(курсор, encoding="utf-8") as fh:
                return итог, json.load(fh)[цель]
        итог, сост = ночь()
        self.assertEqual(итог["аудио_перечитано"], cb.ПРОБА_СТАРЫХ)
        self.assertEqual(сост["cursor"]["created"],
                         старые[cb.ПРОБА_СТАРЫХ - 1]["created"])
        self.assertEqual(сост["end"]["created"], старые[-1]["created"])
        # следующая ночь читает курсор из файла и доходит виток, а не
        # начинает его заново
        итог, сост = ночь()
        self.assertEqual(итог["аудио_перечитано"], len(старые) - cb.ПРОБА_СТАРЫХ)
        self.assertEqual(сост["cursor"]["created"], старые[-1]["created"])
        self.assertEqual(cb._секунды("2026-10-13T23:00:00+03:00"), 1791921600)
        self.assertEqual(cb._секунды("мусор"), 0)


class Выражение(unittest.TestCase):
    def test_сдвиг_учтён_и_мусор_это_null(self):
        con = mi.connect(tempfile.mkdtemp())
        выр = mi.секунды("?")
        self.assertEqual(con.execute("select %s, %s" % (выр, выр),
                                     ("2026-10-13T23:00:00+03:00",
                                      "2026-10-13T20:00:00Z")).fetchone()[:],
                         (1791921600, 1791921600))
        self.assertIsNone(con.execute("select " + выр, ("не дата",)).fetchone()[0])
        con.close()


if __name__ == "__main__":
    unittest.main()
