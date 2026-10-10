"""Т2.6, ТЗ §4.8 «manifest с hash проекций», §5.2 «checkpoint после проекций».

Манифест `_system/projections.json` пишет каждый прогон, меняющий
`projections`: проектор звонка, правка словами, перенос из волта; после него
контрольная точка — `projections.manifest_hash`. Пересборка в пустой каталог
кладёт манифест рядом с карточками. Сверка выносит расхождение в находку.
"""
import os, sys, json, hashlib, tempfile, unittest, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import ledger_import as li
import call_project as cp
import call_asr
import vault_manifest as vm
import vault_rebuild as vr
import contextd_reconcile as rc

EVENT = {"occurred": "2026-09-02T14:05:00+03:00", "ended": "2026-09-02T14:23:11+03:00",
         "payload": {"contact_name": "Анна", "direction": "incoming"}}


class Манифест(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.root, self.vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(self.root)
        for sub in rc.СКЕЛЕТ + rc.КАРТОЧКИ:
            os.makedirs(os.path.join(self.vault, sub), exist_ok=True)
        self.con = mi.connect(self.root)
        self.eid, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "d1",
            "occurred_at": EVENT["occurred"], "ended_at": EVENT["ended"],
            "payload": EVENT["payload"]})
        call_asr.записать_сегменты(self.con, self.eid, None, [
            {"segment_id": "s0011", "start_ms": 250000, "end_ms": 275000, "text": "смета"}])
        sid = self.con.execute("select id from transcript_segments").fetchone()[0]
        mi.write_json(mi.extraction_path(self.root, self.eid), {
            "requests": [{"action": "прислать смету", "requester": "Анна", "explicit": True,
                          "confidence": 0.93, "due_at": "2026-09-04", "deadline_explicit": True,
                          "deadline_phrase": "до пятницы", "disposition": "task",
                          "evidence": [{"segment": "s0011", "segment_id": sid,
                                        "start_ms": 252000, "end_ms": 260000}]}],
            "commitments": [], "decisions": [], "constraints": [], "open_questions": [],
            "changed_instructions": [], "followups": [], "people_mentioned": [],
            "projects_mentioned": [], "event_id": self.eid})
        self.written = cp.run(self.eid, self.vault, self.root)
        self.card = [w for w in self.written if "prislat" in w][0]
        self.n = 0

    def манифест(self):
        with open(os.path.join(self.vault, vm.ПУТЬ), encoding="utf-8") as fh:
            return json.load(fh)

    def проверка(self):
        return vm.проверить(self.con, self.vault)

    def правка(self, **payload):
        self.n += 1
        ev = {"kind": "correction", "source": "mara", "source_id": "c%d" % self.n,
              "occurred_at": "2026-09-02T18:%02d:00+03:00" % self.n, "payload": payload}
        eid, _ = mi.put_event(self.con, ev)
        return cp.apply_correction(self.vault, dict(ev, id=eid), self.con)

    def test_проектор_пишет_манифест_и_контрольную_точку(self):
        док = self.манифест()
        self.assertEqual(set(док["projections"]), set(self.written))
        self.assertEqual(док["hash"], vm.хеш(док["projections"]))
        self.assertEqual(док["pipeline_version"], mi.PIPELINE_VERSION)
        for rel, з in док["projections"].items():
            with open(os.path.join(self.vault, rel), "rb") as fh:
                self.assertEqual(з["sha256"], hashlib.sha256(fh.read()).hexdigest(), rel)
            self.assertEqual(з["projector_version"], mi.PIPELINE_VERSION)
        хеши = {r[0] for r in self.con.execute("select manifest_hash from projections")}
        self.assertEqual(хеши, {док["hash"]}, "§5.2: у всех строк контрольная точка манифеста")
        итог, зам = self.проверка()
        self.assertEqual(зам, [])
        self.assertFalse(vm.расхождение(итог, strict=True))
        self.assertEqual((итог["проекций"], итог["в манифесте"]), (2, 2))

    def test_хеш_от_содержимого_а_не_от_времени(self):
        h1 = self.манифест()["hash"]
        h2 = vm.записать(self.con, self.vault, когда="2030-01-01T00:00:00+00:00")
        self.assertEqual(h1, h2)
        self.assertEqual(self.манифест()["generated"], "2030-01-01T00:00:00+00:00")

    def test_правка_рукой_видна_только_строго(self):
        with open(os.path.join(self.vault, self.card), "a", encoding="utf-8") as fh:
            fh.write("\nзаметка рукой\n")
        итог, зам = self.проверка()
        self.assertEqual(итог["файлов не как в манифесте"], 1)
        self.assertFalse(vm.расхождение(итог))
        self.assertTrue(vm.расхождение(итог, strict=True))
        self.assertEqual(rc.манифест_проекций(self.con, self.vault), [], "норма дня до Т2.8")

    def test_перенос_переписывает_манифест_после_правки_рукой(self):
        with open(os.path.join(self.vault, self.card), "a", encoding="utf-8") as fh:
            fh.write("\nзаметка рукой\n")
        старый = self.манифест()["hash"]
        li.run(self.con, self.vault)
        итог, зам = self.проверка()
        self.assertEqual(зам, [], "после переноса манифест и файлы сходятся")
        self.assertNotEqual(self.манифест()["hash"], старый)
        после = self.манифест()["generated"]
        li.run(self.con, self.vault, dry_run=True)
        self.assertEqual(self.манифест()["generated"], после, "проба ничего не пишет")

    def test_правка_словами_обновляет_манифест(self):
        out = self.правка(item="прислать смету", status="done")
        self.assertTrue(out.get("applied"), out)
        итог, зам = self.проверка()
        self.assertEqual(зам, [])
        self.assertFalse(vm.расхождение(итог, strict=True))

    def test_реестр_ушёл_вперёд_без_контрольной_точки(self):
        # прогон прерван между записью проекции и точкой: строка новая, манифест старый
        self.con.execute("update projections set content_sha256='x' where path=?", (self.card,))
        итог, зам = self.проверка()
        self.assertEqual((итог["манифест устарел"], итог["строк без контрольной точки"]), (1, 1))
        self.assertTrue(vm.расхождение(итог))
        н = rc.манифест_проекций(self.con, self.vault)
        self.assertEqual(len(н), 1)
        self.assertEqual((н[0]["check"], н[0]["level"], н[0]["count"]), ("манифест-проекций", "warn", 2))
        vm.записать(self.con, self.vault)
        итог, _ = self.проверка()
        self.assertFalse(vm.расхождение(итог))

    def test_удалённый_файл_и_повреждённый_манифест(self):
        os.remove(os.path.join(self.vault, self.card))
        итог, _ = self.проверка()
        self.assertEqual(итог["файлов нет"], 1)
        self.assertTrue(vm.расхождение(итог))
        путь = os.path.join(self.vault, vm.ПУТЬ)
        док = self.манифест()
        док["count"] = 99
        док["projections"][self.card]["sha256"] = "подделка"
        with open(путь, "w", encoding="utf-8") as fh:
            json.dump(док, fh)
        итог, зам = self.проверка()
        self.assertEqual(итог["манифест повреждён"], 1)
        self.assertEqual(len(зам), 1, "за повреждённым дальше не сверяется")
        with open(путь, "w") as fh:
            fh.write('{"hash": 1}')
        итог, _ = self.проверка()
        self.assertEqual(итог["манифест не читается"], 1)
        self.assertFalse(vm.расхождение(итог))
        self.assertTrue(vm.расхождение(итог, strict=True))

    def test_без_манифеста_не_находка(self):
        os.remove(os.path.join(self.vault, vm.ПУТЬ))
        итог, _ = self.проверка()
        self.assertEqual(итог["манифеста нет"], 1)
        self.assertFalse(vm.расхождение(итог))
        self.assertEqual(rc.манифест_проекций(self.con, self.vault), [])

    def test_пересборка_в_пустой_каталог_кладёт_манифест(self):
        into = os.path.join(os.path.dirname(self.vault), "rebuilt")
        итог, карточки = vr.пересобрать(self.con, self.root, self.vault)
        vr.записать(карточки, into, self.vault, self.con)
        итог, зам = vm.проверить(self.con, into)
        self.assertEqual(зам, [], "пересобранный каталог сходится с манифестом реестра")
        self.assertEqual(self.манифест()["hash"], vm.хеш(vm.собрать(self.con)),
                         "живой волт и его контрольная точка не тронуты")

    def test_командная_строка(self):
        env = dict(os.environ, MARA_BLOBS=self.root, MARA_VAULT=self.vault)
        def запуск(*args):
            return subprocess.run([sys.executable, os.path.join(os.path.dirname(cp.__file__),
                                   "vault_manifest.py"), *args], env=env,
                                  capture_output=True, text=True)
        r = запуск("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("манифест проекций: в реестре 2, в манифесте 2", r.stdout)
        self.con.execute("update projections set content_sha256='x' where path=?", (self.card,))
        self.con.commit()
        r = запуск("--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("манифест устарел", r.stdout)
        r = запуск("--write")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(запуск("--check").returncode, 0)
        r = запуск("--check", "--root", os.path.join(self.root, "нет"))
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
