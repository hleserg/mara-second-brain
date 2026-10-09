"""Т2.6, слой 1 (ТЗ §4.8): детектор расхождений проекции и реестра.

Ручные изменения выявляются, не затираются молча: каждое расхождение между
волтом и реестром названо строкой, а сверка (`contextd_reconcile`) выносит
его в находку. Ничего не пишется.
"""
import os, sys, json, tempfile, unittest, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import ledger_import as li
import call_project as cp
import call_asr
import vault_drift as vd
import contextd_reconcile as rc

EVENT = {"occurred": "2026-09-02T14:05:00+03:00", "ended": "2026-09-02T14:23:11+03:00",
         "payload": {"contact_name": "Анна", "direction": "incoming"}}


class Дрейф(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.root, self.vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(self.root)
        for sub in rc.СКЕЛЕТ:
            os.makedirs(os.path.join(self.vault, sub), exist_ok=True)
        os.makedirs(os.path.join(self.vault, ".git"), exist_ok=True)
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
        self.card = os.path.join(self.vault, [w for w in self.written if "prislat" in w][0])

    def дрейф(self):
        return vd.проверить(self.con, self.vault)

    def test_после_проекции_расхождений_нет(self):
        итог, зам = self.дрейф()
        self.assertEqual(зам, [])
        self.assertFalse(vd.расхождение(итог, strict=True))
        self.assertEqual((итог["карточек"], итог["проекций"]), (2, 2))
        p = self.con.execute("select ledger_version, projector_version from projections "
                             "where path like 'kb/commitments/%'").fetchone()
        self.assertEqual(tuple(p), (1, mi.PIPELINE_VERSION), "§4.8: версии в проекции")

    def test_правка_рукой_видна_только_строго(self):
        with open(self.card, "a", encoding="utf-8") as fh:
            fh.write("\nзаметка владельца\n")
        итог, зам = self.дрейф()
        self.assertEqual(итог["изменены после переноса"], 1)
        self.assertFalse(vd.расхождение(итог), "до переноса это норма дня")
        self.assertTrue(vd.расхождение(итог, strict=True))
        # перенос забрал правку — дрейфа нет
        li.run(self.con, self.vault)
        self.assertFalse(vd.расхождение(self.дрейф()[0], strict=True))

    def test_правка_статуса_рукой_не_расхождение_шапки(self):
        """Ревью: самый частый случай — статус закрыт рукой в Obsidian до
        переноса. Это «изменены после переноса», а не «шапка разошлась»:
        расхождение объяснено правкой файла, и сверка о нём не кричит."""
        with open(self.card, encoding="utf-8") as fh:
            текст = fh.read()
        with open(self.card, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("status: proposed", "status: done"))
        итог, зам = self.дрейф()
        self.assertEqual((итог["изменены после переноса"], итог["шапка разошлась"],
                          итог["evidence разошлось"]), (1, 0, 0))
        self.assertFalse(vd.расхождение(итог))
        self.assertEqual([f["check"] for f in rc.run(self.con, self.root, vault=self.vault,
                                                     bm_db=None, targets=[])
                          if f["check"] == "проекция-разошлась"], [])

    def test_переименованная_рукой_не_удалена(self):
        новый = self.card.replace(".md", "-x.md")
        os.rename(self.card, новый)
        итог, зам = self.дрейф()
        self.assertEqual((итог["переименованы"], итог["проекций без файла"],
                          итог["карточек без проекции"]), (1, 0, 1))
        self.assertFalse(vd.расхождение(итог))
        self.assertTrue(vd.расхождение(итог, strict=True))
        li.run(self.con, self.vault)
        self.assertFalse(vd.расхождение(self.дрейф()[0], strict=True),
                         "перенос перевесил проекцию")

    def test_неполный_волт_отказ_а_не_удалено_руками(self):
        with self.assertRaises(vd.ВолтНеПрочитан):
            vd.проверить(self.con, os.path.join(self.vault, "нет"))
        import shutil
        shutil.rmtree(os.path.join(self.vault, "kb"))
        with self.assertRaises(vd.ВолтНеПрочитан):
            vd.проверить(self.con, self.vault)
        находки = rc.run(self.con, self.root, vault=self.vault, bm_db=None, targets=[])
        self.assertNotIn("проекция-разошлась", [f["check"] for f in находки],
                         "о волте говорит волт_пропал, не детектор")

    def test_evidence_сравнивается_только_с_model(self):
        """Строка `human` в реестре (когда появится) карточку не ломает:
        карточка перечисляет только ссылки из извлечения."""
        sid = self.con.execute("select id from transcript_segments").fetchone()[0]
        oid = self.con.execute("select id from commitments").fetchone()[0]
        self.con.execute("insert into evidence_refs(id,object_kind,object_id,kind,segment_id,"
                         "start_ms,end_ms,producer,created) values(?,?,?,?,?,?,?,?,?)",
                         (mi.uuid7(), "commitment", oid, "audio", sid, 250000, 275000,
                          "human", mi.now_iso()))
        self.assertEqual(self.дрейф()[0]["evidence разошлось"], 0)

    def test_база_только_для_чтения(self):
        con = vd.только_чтение(self.root)
        self.assertEqual(vd.проверить(con, self.vault)[1], [])
        with self.assertRaises(__import__("sqlite3").OperationalError):
            vd.только_чтение(os.path.join(self.root, "нет-такого"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "нет-такого")),
                         "опечатка в --root не заводит базу")

    def test_удалённая_руками_карточка_при_живом_объекте(self):
        os.remove(self.card)
        итог, зам = self.дрейф()
        self.assertEqual(итог["проекций без файла"], 1)
        self.assertTrue(vd.расхождение(итог))
        self.assertEqual(зам[0][0], "проекций без файла")
        self.assertIn("удалена руками", зам[0][1])

    def test_шапка_разошлась_с_реестром(self):
        self.con.execute("update commitments set status='done', due='2026-09-10'")
        итог, зам = self.дрейф()
        self.assertEqual(итог["шапка разошлась"], 2)
        self.assertTrue(any("status в шапке 'proposed', в реестре 'done'" in з
                            for _, з in зам), зам)

    def test_evidence_разошлось(self):
        self.con.execute("delete from evidence_refs")
        итог, зам = self.дрейф()
        self.assertEqual(итог["evidence разошлось"], 1)
        self.assertIn("evidence в шапке 1, в реестре 0", зам[0][1])

    def test_чужая_карточка_без_проекции(self):
        with open(os.path.join(self.vault, "kb/commitments/ruka.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: рукой\nstatus: open\n---\n")
        итог, зам = self.дрейф()
        self.assertEqual(итог["карточек без проекции"], 1)
        self.assertFalse(vd.расхождение(итог))

    def test_проекция_без_объекта(self):
        self.con.execute("pragma foreign_keys=off")
        self.con.execute("delete from evidence_refs")
        self.con.execute("delete from revisions")
        self.con.execute("delete from commitments")
        итог, зам = self.дрейф()
        self.assertEqual(итог["проекций без объекта"], 1)

    def test_сверка_выносит_находку(self):
        self.assertEqual([f for f in rc.run(self.con, self.root, vault=self.vault, bm_db=None,
                                            targets=[]) if f["check"] == "проекция-разошлась"],
                         [])
        # правка рукой другой карточки в образец находки не попадает (ревью)
        conv = os.path.join(self.vault, [w for w in self.written
                                         if w.startswith("kb/conversations/")][0])
        with open(conv, "a", encoding="utf-8") as fh:
            fh.write("\nзаметка\n")
        os.remove(self.card)
        f, = [f for f in rc.run(self.con, self.root, vault=self.vault, bm_db=None, targets=[])
              if f["check"] == "проекция-разошлась"]
        self.assertEqual((f["level"], f["count"]), ("warn", 1))
        self.assertIn("vault_drift.py --check", f["detail"])
        self.assertEqual(len(f["sample"]), 1)
        self.assertIn("удалена руками", f["sample"][0])

    def test_командная_строка(self):
        скрипт = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts",
                              "vault_drift.py")
        r = subprocess.run([sys.executable, скрипт, "--check", "--vault", self.vault,
                            "--root", self.root], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("дрейф проекции: карточек 2, проекций 2", r.stdout)
        os.remove(self.card)
        r = subprocess.run([sys.executable, скрипт, "--check", "--vault", self.vault,
                            "--root", self.root], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("удалена руками", r.stdout)
        r = subprocess.run([sys.executable, скрипт, "--check", "--vault",
                            os.path.join(self.vault, "нет"), "--root", self.root],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("волт не прочитан", r.stderr)


if __name__ == "__main__":
    unittest.main()
