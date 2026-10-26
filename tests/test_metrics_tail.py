"""Т9.1, ТЗ §15.1: «compute utilization» и «backup/restore verification» в
`/metrics` — загрузка узла из сердцебиения, возраст сверенной копии на
каждом носителе, последнего учения и частого снимка реестра."""
import contextlib, importlib.util, io, json, os, sys, tempfile, time, unittest
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import mara_ingest as mi
import contextd
import contextd_reconcile as cr
import node_agent

_spec = importlib.util.spec_from_file_location("core_backup", os.path.join(СКРИПТЫ, "core-backup.py"))
cb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cb)


class _База(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.addCleanup(self.con.close)
        self.состояние = tempfile.mkdtemp()
        for имя, файл in (("ОТМЕТКА_НОСИТЕЛЕЙ", "core-targets.json"),
                          ("ОТМЕТКА_УЧЕНИЯ", "core-drill.json"),
                          ("ОТМЕТКА_СЕКРЕТОВ", "core-secrets.json")):
            п = mock.patch.object(mi, имя, os.path.join(self.состояние, файл))
            п.start()
            self.addCleanup(п.stop)
        снимки = mock.patch.dict(os.environ, {"MARA_CORE_SNAPSHOTS": os.path.join(self.dir, "снимки")})
        снимки.start()
        self.addCleanup(снимки.stop)

    def метрики(self):
        return dict(l.rsplit(" ", 1) for l in
                    contextd.metrics(self.con, self.dir, None).splitlines() if l)


class Загрузка(_База):
    def test_из_свежего_сердцебиения(self):
        с, беда = contextd.разобрать_сердцебиение({"capabilities": ["asr"], "vram_free_mb": 5120,
                                                   "vram_total_mb": 8192, "load": 0.5,
                                                   "accept_jobs": False})
        self.assertIsNone(беда)
        contextd.принять_сердцебиение(self.con, "bigpc", с)
        м = self.метрики()
        self.assertEqual(м['mara_node_vram_free_bytes{node="bigpc"}'], str(5120 << 20))
        self.assertEqual(м['mara_node_vram_total_bytes{node="bigpc"}'], str(8192 << 20))
        self.assertEqual(м['mara_node_load{node="bigpc"}'], "0.5")
        self.assertEqual(м['mara_node_accepting_jobs{node="bigpc"}'], "0")

    def test_нет_сердцебиения_минус_один(self):
        м = self.метрики()
        for имя in ("mara_node_vram_free_bytes", "mara_node_load", "mara_node_accepting_jobs"):
            self.assertEqual(м['%s{node="bigpc"}' % имя], "-1")

    def test_от_агента_узла_до_metrics(self):
        """Сквозь настоящий `node_agent.собрать`: рукописное сердцебиение
        прятало, что агент `load` не шлёт вовсе (ревью Т9.1, P2-3)."""
        with mock.patch.object(node_agent, "видеокарта", return_value=(1200, 16303, 37)):
            тело = node_agent.собрать(env={}, имена=set())
        с, беда = contextd.разобрать_сердцебиение(тело)
        self.assertIsNone(беда)
        contextd.принять_сердцебиение(self.con, "bigpc", с)
        м = self.метрики()
        self.assertEqual(м['mara_node_load{node="bigpc"}'], "37")
        self.assertEqual(м['mara_node_vram_free_bytes{node="bigpc"}'], str(1200 << 20))
        self.assertEqual(м['mara_node_accepting_jobs{node="bigpc"}'], "1")


class Бэкап(_База):
    def test_ни_разу_минус_один(self):
        м = self.метрики()
        self.assertEqual(м["mara_backup_drill_age_seconds"], "-1")
        self.assertEqual(м["mara_registry_snapshot_age_seconds"], "-1")
        self.assertFalse([k for k in м if k.startswith("mara_backup_target_age_seconds")])

    def test_носители_учение_снимок(self):
        cb.отметка(mi.ОТМЕТКА_НОСИТЕЛЕЙ, ["/mnt/a", "/mnt/b"], когда=time.time() - 3600)
        cb.отметить_учение("core-2026-10-14.tar.gz.gpg", "/mnt/a", когда=time.time() - 7200)
        os.makedirs(mi.снимки(self.dir))
        снимок = os.path.join(mi.снимки(self.dir), "contextd-2026-10-14T08-00.db")
        open(снимок, "w").close()
        os.utime(снимок, (time.time() - 60, time.time() - 60))
        м = self.метрики()
        self.assertAlmostEqual(int(м['mara_backup_target_age_seconds{target="/mnt/a"}']), 3600, delta=5)
        self.assertIn('mara_backup_target_age_seconds{target="/mnt/b"}', м)
        self.assertAlmostEqual(int(м["mara_backup_drill_age_seconds"]), 7200, delta=5)
        self.assertAlmostEqual(int(м["mara_registry_snapshot_age_seconds"]), 60, delta=5)
        with open(mi.ОТМЕТКА_УЧЕНИЯ, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["target"], "/mnt/a")

    def test_кривая_отметка_не_роняет(self):
        """`/metrics` скребут раз в минуту: отметка, испорченная руками,
        роняла бы обработчик на каждом скрейпе (ревью Т9.1, P3-1)."""
        os.makedirs(os.path.dirname(mi.ОТМЕТКА_УЧЕНИЯ), exist_ok=True)
        for мусор in ("{мусор", "[1]", '{"at": "вчера"}', '{"at": true}', '{"at": NaN}',
                      '{"at": Infinity}', '{"at": 1e400}', '{"at": 1%s}' % ("0" * 400),
                      "[" * 100000 + "]" * 100000):
            with open(mi.ОТМЕТКА_УЧЕНИЯ, "w", encoding="utf-8") as fh:
                fh.write(мусор)
            self.assertEqual(self.метрики()["mara_backup_drill_age_seconds"], "-1", мусор[:20])
        with open(mi.ОТМЕТКА_НОСИТЕЛЕЙ, "w", encoding="utf-8") as fh:
            fh.write('{"/mnt/a": NaN, "\\ud800": 1, "/mnt/b": %d}' % time.time())
        м = self.метрики()
        self.assertEqual(м['mara_backup_target_age_seconds{target="/mnt/a"}'], "-1")
        self.assertIn('mara_backup_target_age_seconds{target="/mnt/b"}', м)
        self.assertEqual(len([k for k in м if k.startswith("mara_backup_target_age")]), 2,
                         "ключ с одиночным суррогатом в экспозицию не идёт")
        contextd.metrics(self.con, self.dir, None).encode("utf-8")

    def test_висячий_снимок_не_роняет(self):
        os.makedirs(mi.снимки(self.dir))
        os.symlink(os.path.join(self.dir, "нет-такого"),
                   os.path.join(mi.снимки(self.dir), "contextd-2026-10-14T08-00.db"))
        self.assertEqual(self.метрики()["mara_registry_snapshot_age_seconds"], "-1")

    def test_сбой_записи_учения_прогон_не_роняет(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            cb.отметить_учение("core-x", "/mnt/a", путь="/proc/нельзя/core-drill.json")
        self.assertIn("отметка учения не записана", err.getvalue())


class Секреты(_База):
    """Т3б.5 п.5: бандл секретов идёт после ядра и ночь не роняет — о его
    отсутствии говорит только отметка `core-secrets.json`."""

    def test_архивы_ложатся_а_бандла_нет(self):
        cb.отметка(mi.ОТМЕТКА_НОСИТЕЛЕЙ, ["/mnt/a"], когда=time.time() - 3600)
        self.assertEqual([(x["check"], x["level"]) for x in cr.бэкап_секретов()],
                         [("бэкап-секретов", "warn")], "отметки нет вовсе")
        cb.отметить_секреты(["/mnt/a"], 0, "secrets-x",
                            когда=time.time() - (cr.БЭКАП_СУТКИ + 1) * 86400)
        self.assertEqual([(x["check"], x["level"]) for x in cr.бэкап_секретов()],
                         [("бэкап-секретов", "error")])
        cb.отметить_секреты(["/mnt/a"], 1, "secrets-x", когда=time.time() - 3600)
        н = cr.бэкап_секретов()
        self.assertEqual([(x["check"], x["level"], x["unreadable"]) for x in н],
                         [("бэкап-секретов", "warn", 1)], "нечитаемое — предупреждение")
        cb.отметить_секреты(["/mnt/a"], 0, "secrets-x", когда=time.time() - 3600)
        self.assertEqual(cr.бэкап_секретов(), [])

    def test_архивы_не_ложатся_судит_бэкап(self):
        self.assertEqual(cr.бэкап_секретов(), [])
        cb.отметка(mi.ОТМЕТКА_НОСИТЕЛЕЙ, ["/mnt/a"],
                   когда=time.time() - (cr.БЭКАП_СУТКИ + 1) * 86400)
        self.assertEqual(cr.бэкап_секретов(), [], "носители молчат — об этом бэкап_ядра")

    def test_в_общей_сверке(self):
        cb.отметка(mi.ОТМЕТКА_НОСИТЕЛЕЙ, ["/mnt/a"], когда=time.time() - 3600)
        self.assertIn("бэкап-секретов",
                      [x["check"] for x in cr.run(self.con, self.dir, vault=None, targets=[])])


class Учение(_База):
    """Ревью Т9.1, P2-2: учение падает после того, как архив лёг и отметка
    носителей обновлена, — находки бэкапа молчат, а копия не доказана."""

    def test_архивы_ложатся_а_учение_нет(self):
        cb.отметка(mi.ОТМЕТКА_НОСИТЕЛЕЙ, ["/mnt/a"], когда=time.time() - 3600)
        н = cr.учение_бэкапа()
        self.assertEqual([(x["check"], x["level"]) for x in н],
                         [("учение-не-проходит", "warn")], "отметки нет вовсе")
        cb.отметить_учение("core-x", "/mnt/a", когда=time.time() - (cr.БЭКАП_СУТКИ + 1) * 86400)
        н = cr.учение_бэкапа()
        self.assertEqual([(x["check"], x["level"]) for x in н],
                         [("учение-не-проходит", "error")])
        cb.отметить_учение("core-x", "/mnt/a", когда=time.time() - 3600)
        self.assertEqual(cr.учение_бэкапа(), [])

    def test_архивы_не_ложатся_судит_бэкап(self):
        self.assertEqual(cr.учение_бэкапа(), [], "отметки носителей нет — бэкап не настроен")
        cb.отметка(mi.ОТМЕТКА_НОСИТЕЛЕЙ, ["/mnt/a"],
                   когда=time.time() - (cr.БЭКАП_СУТКИ + 1) * 86400)
        self.assertEqual(cr.учение_бэкапа(), [], "носители молчат — об этом бэкап_ядра")
        with open(mi.ОТМЕТКА_НОСИТЕЛЕЙ, "w", encoding="utf-8") as fh:
            fh.write('{"/mnt/a": NaN}')
        self.assertEqual(cr.учение_бэкапа(), [])

    def test_в_общей_сверке(self):
        cb.отметка(mi.ОТМЕТКА_НОСИТЕЛЕЙ, ["/mnt/a"], когда=time.time() - 3600)
        self.assertIn("учение-не-проходит",
                      [x["check"] for x in cr.run(self.con, self.dir, vault=None, targets=[])])


    def test_самопроверка_не_читает_отметки_хоста(self):
        """Гейт 2026-10-19 на doctor: носители отмечены, учение ещё ни разу
        не проходило — и `--self-check` падал на настоящей находке
        `учение-не-проходит`, хотя `targets` он подменяет. Отметки
        самопроверка обязана уводить во временный каталог сама."""
        cb.отметка(mi.ОТМЕТКА_НОСИТЕЛЕЙ, ["/mnt/a"], когда=time.time() - 3600)
        for имя in ("ROOT", "STATE"):
            п = mock.patch.object(mi, имя, getattr(mi, имя))
            п.start()
            self.addCleanup(п.stop)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cr.self_check(), 0)
        self.assertNotEqual(mi.STATE, self.состояние, "самопроверка увела отметки к себе")


if __name__ == "__main__":
    unittest.main()
