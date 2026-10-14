"""Т9.1, ТЗ §15.1: «compute utilization» и «backup/restore verification» в
`/metrics` — загрузка узла из сердцебиения, возраст сверенной копии на
каждом носителе, последнего учения и частого снимка реестра."""
import contextlib, importlib.util, io, json, os, sys, tempfile, time, unittest
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import mara_ingest as mi
import contextd

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
                          ("ОТМЕТКА_УЧЕНИЯ", "core-drill.json")):
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
        self.assertEqual(м['mara_node_vram_free_mb{node="bigpc"}'], "5120")
        self.assertEqual(м['mara_node_vram_total_mb{node="bigpc"}'], "8192")
        self.assertEqual(м['mara_node_load{node="bigpc"}'], "0.5")
        self.assertEqual(м['mara_node_accepting_jobs{node="bigpc"}'], "0")

    def test_нет_сердцебиения_минус_один(self):
        м = self.метрики()
        for имя in ("mara_node_vram_free_mb", "mara_node_load", "mara_node_accepting_jobs"):
            self.assertEqual(м['%s{node="bigpc"}' % имя], "-1")


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
        os.makedirs(os.path.dirname(mi.ОТМЕТКА_УЧЕНИЯ), exist_ok=True)
        for мусор in ("{мусор", "[1]", '{"at": "вчера"}', '{"at": true}'):
            with open(mi.ОТМЕТКА_УЧЕНИЯ, "w", encoding="utf-8") as fh:
                fh.write(мусор)
            self.assertEqual(self.метрики()["mara_backup_drill_age_seconds"], "-1", мусор)

    def test_сбой_записи_учения_прогон_не_роняет(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            cb.отметить_учение("core-x", "/mnt/a", путь="/proc/нельзя/core-drill.json")
        self.assertIn("отметка учения не записана", err.getvalue())


if __name__ == "__main__":
    unittest.main()
