"""Т2.6, слой 2 (ТЗ §4.8): пересборка карточек из реестра детерминирована.

Проекция рисуется заново из реестра и блобов и совпадает с волтом байт в
байт — после проекции звонка, после правок словами и у карточки, заведённой
словами. Пустой каталог восстанавливается из реестра. В живой волт
пересборка не пишет (Г4/Т2.8).
"""
import os, sys, json, tempfile, unittest, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import ledger_import as li
import call_project as cp
import call_asr
import vault_drift as vd
import vault_rebuild as vr
import contextd_reconcile as rc

EVENT = {"occurred": "2026-09-02T14:05:00+03:00", "ended": "2026-09-02T14:23:11+03:00",
         "payload": {"contact_name": "Анна", "direction": "incoming"}}
СКРИПТ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts",
                      "vault_rebuild.py")


class Пересборка(unittest.TestCase):
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
        self.sid = self.con.execute("select id from transcript_segments").fetchone()[0]
        self.extr = {
            "requests": [{"action": "прислать смету", "requester": "Анна", "explicit": True,
                          "confidence": 0.93, "due_at": "2026-09-04", "deadline_explicit": True,
                          "deadline_phrase": "до пятницы", "disposition": "task",
                          "evidence": [{"segment": "s0011", "segment_id": self.sid,
                                        "start_ms": 252000, "end_ms": 260000}]}],
            "commitments": [{"action": "позвонить в банк", "explicit": True, "confidence": 0.95,
                             "due_at": None, "deadline_explicit": False, "deadline_phrase": "",
                             "disposition": "task",
                             "evidence": [{"segment": "s0011", "segment_id": self.sid}]}],
            "decisions": [], "constraints": [], "open_questions": [],
            "changed_instructions": [], "followups": [], "people_mentioned": ["Анна"],
            "projects_mentioned": [], "event_id": self.eid, "extractor": "m",
            "prompt_version": 2}
        mi.write_json(mi.extraction_path(self.root, self.eid), self.extr)
        self.written = cp.run(self.eid, self.vault, self.root)
        self.card = [w for w in self.written if "prislat" in w][0]
        self.n = 0

    def правка(self, **payload):
        """Как contextd: событие правки сначала в реестр, потом карточка."""
        self.n += 1
        ev = {"kind": "correction", "source": "mara", "source_id": "c%d" % self.n,
              "occurred_at": "2026-09-02T18:%02d:00+03:00" % self.n, "payload": payload}
        eid, _ = mi.put_event(self.con, ev)
        return cp.apply_correction(self.vault, dict(ev, id=eid), self.con)

    def пересборка(self):
        return vr.пересобрать(self.con, self.root, self.vault)

    def читать(self, rel, корень=None):
        with open(os.path.join(корень or self.vault, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_после_проекции_совпадает_байт_в_байт(self):
        итог, карточки = self.пересборка()
        self.assertEqual((итог["проекций"], итог["совпало"]), (3, 3), dict(итог))
        self.assertFalse(vr.расхождение(итог))
        for rel, (состояние, text, _) in карточки.items():
            self.assertEqual(text, self.читать(rel), rel)

    def test_правки_словами_пересобираются_из_corrections(self):
        out = self.правка(item="прислать смету", status="done", note="отправил")
        self.assertTrue(out["applied"])
        self.правка(item="позвонить в банк", due="2026-09-10")
        self.правка(item="позвонить в банк", due="2026-09-12", note="Анна попросила")
        text = self.читать(self.card)
        self.assertIn("\nПравки:\n- ", text)
        self.assertIn(", Мара, correction/", text)
        итог, карточки = self.пересборка()
        self.assertEqual(итог["совпало"], 3, dict(итог))
        self.assertIn("статус proposed → done; отправил", карточки[self.card][1])
        банк = [w for w in self.written if "bank" in w][0]
        self.assertIn("срок 2026-09-10 → 2026-09-12; Анна попросила", карточки[банк][1])
        self.assertIn("\ndue: 2026-09-12\n", карточки[банк][1], "срок — из строки реестра")

    def test_заведённая_словами_карточка_из_события_правки(self):
        out = self.правка(item="покрасить забор", status="open", due="2026-09-20",
                          note="краска в гараже")
        self.правка(item="покрасить забор", status="done")
        итог, карточки = self.пересборка()
        self.assertEqual((итог["проекций"], итог["совпало"]), (4, 4), dict(итог))
        text = карточки[out["created"]][1]
        self.assertIn("- Откуда: сказано Маре, ", text)
        self.assertIn("- Заметка: краска в гараже", text)
        self.assertIn("\nstatus: done\n", text)

    def test_пустой_волт_восстанавливается_из_реестра_и_блобов(self):
        self.правка(item="прислать смету", status="done")
        self.правка(item="покрасить забор", status="open")
        итог, карточки = self.пересборка()
        into = os.path.join(os.path.dirname(self.vault), "восстановленный")
        self.assertEqual(vr.записать(карточки, into, self.vault), 4)
        for rel in карточки:
            self.assertEqual(self.читать(rel, into), self.читать(rel), rel)
        # и реестр считает восстановленный волт своим: дрейфа нет
        for sub in rc.СКЕЛЕТ:
            os.makedirs(os.path.join(into, sub), exist_ok=True)
        self.assertEqual(vd.проверить(self.con, into)[1], [])

    def test_правка_рукой_разошлось_с_диффом(self):
        with open(os.path.join(self.vault, self.card), "a", encoding="utf-8") as fh:
            fh.write("заметка владельца\n")
        итог, карточки = self.пересборка()
        self.assertEqual((итог["совпало"], итог["разошлось"]), (2, 1))
        self.assertTrue(vr.расхождение(итог))
        состояние, _, диф = карточки[self.card]
        self.assertEqual(состояние, "разошлось")
        self.assertIn("-заметка владельца", диф)

    def test_реестр_авторитет_для_шапки_и_evidence(self):
        """Статус в реестре и ссылки `evidence_refs` — источник; извлечение,
        переписанное после проекции, карточку не меняет."""
        self.con.execute("update commitments set status='done' where source_native_id like "
                         "'%/requests/1'")
        extr = json.loads(json.dumps(self.extr))
        extr["requests"][0]["evidence"] = [{"segment": "s0011", "segment_id": self.sid,
                                            "start_ms": 250000, "end_ms": 275000}]
        mi.write_json(mi.extraction_path(self.root, self.eid), extr)
        итог, карточки = self.пересборка()
        text = карточки[self.card][1]
        self.assertIn("\nstatus: done\n", text)
        self.assertIn("  - %s 252000-260000" % self.sid, text, "ссылка из реестра, не из файла")
        self.assertIn("04:12–04:20", text)
        self.assertEqual(карточки[self.card][0], "разошлось")

    def test_без_файла_и_без_источника(self):
        os.remove(os.path.join(self.vault, self.card))
        legacy = os.path.join(self.vault, "kb/commitments/2026-08-01-staroe.md")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: старое\nstatus: open\n---\n\n- Обещание: старое\n")
        li.run(self.con, self.vault)
        итог, карточки = self.пересборка()
        self.assertEqual((итог["без файла"], итог["без источника"], итог["совпало"]),
                         (1, 1, 2), dict(итог))
        self.assertEqual(карточки[self.card][0], "без файла")
        self.assertIsNotNone(карточки[self.card][1], "текст есть — его и запишет --into")
        self.assertIn("перенесена из волта", карточки["kb/commitments/2026-08-01-staroe.md"][2])
        self.assertTrue(vr.расхождение(итог), "без файла — расхождение, без источника — нет")
        os.remove(mi.extraction_path(self.root, self.eid))
        итог, карточки = self.пересборка()
        self.assertEqual(итог["без источника"], 4, "без извлечения звонок не пересобрать")

    def test_в_живой_волт_и_в_непустой_каталог_не_пишет(self):
        итог, карточки = self.пересборка()
        with self.assertRaises(RuntimeError) as e:
            vr.записать(карточки, self.vault, self.vault)
        self.assertIn("Г4", str(e.exception))
        занят = os.path.join(os.path.dirname(self.vault), "занят")
        os.makedirs(os.path.join(занят, "x"))
        with self.assertRaises(RuntimeError):
            vr.записать(карточки, занят, self.vault)
        self.assertEqual(os.listdir(занят), ["x"])

    def test_журнал_рукой_две_строки_в_минуту_и_ранняя_дата_позже(self):
        """Ревью PR #125: строки журнала собираются по соседству записи, не
        по ключу (время, автор, событие) — две строки рукой в одну минуту не
        сливаются, а строка с более ранней датой, дописанная позже, остаётся
        на своём месте (журнал только дописывается)."""
        self.правка(item="прислать смету", status="done", note="отправил")
        with open(os.path.join(self.vault, self.card), "a", encoding="utf-8") as fh:
            fh.write("- 2026-09-21T15:08, владелец: первая заметка\n"
                     "- 2026-09-21T15:08, владелец: вторая заметка\n"
                     "- 2026-09-01T10:00, владелец: срок 2026-09-04 → 2026-09-03; задним числом\n")
        li.run(self.con, self.vault)
        итог, карточки = self.пересборка()
        self.assertEqual(карточки[self.card][0], "совпало", карточки[self.card][2])

    def test_заметка_с_переносом_и_точкой_с_запятой_переживает_круг(self):
        self.правка(item="прислать смету", status="done", note="а ;б;;в\nвторая строка")
        text = self.читать(self.card)
        self.assertIn("статус proposed → done; а; б; в вторая строка\n", text)
        итог, карточки = self.пересборка()
        self.assertEqual(карточки[self.card][0], "совпало", карточки[self.card][2])

    def test_повторная_проекция_не_меняет_created(self):
        """Ревью PR #125: `created` при перерисовке — из реестра, иначе каждая
        повторная проекция давала бы «разошлось» по одной строке."""
        было_created = [l for l in self.читать(self.card).splitlines()
                        if l.startswith("created:")]
        было = mi.now_iso
        mi.now_iso = lambda: "2027-01-01T00:00:00+03:00"
        try:
            cp.run(self.eid, self.vault, self.root)
        finally:
            mi.now_iso = было
        self.assertEqual([l for l in self.читать(self.card).splitlines()
                          if l.startswith("created:")], было_created)
        итог, карточки = self.пересборка()
        self.assertEqual(итог["совпало"], 3, dict(итог))

    def test_into_без_живого_волта(self):
        """Восстановление в пустой каталог не требует волта: сравнивать не с
        чем, карточки — «не сравнивалось», «Люди:» без ссылок до entity-link."""
        with self.assertRaises(vd.ВолтНеПрочитан):
            vr.пересобрать(self.con, self.root, None)
        итог, карточки = vr.пересобрать(self.con, self.root, None, сравнивать=False)
        self.assertEqual((итог["не сравнивалось"], итог["совпало"]), (3, 0), dict(итог))
        self.assertFalse(vr.расхождение(итог))
        into = os.path.join(os.path.dirname(self.vault), "без-волта")
        self.assertEqual(vr.записать(карточки, into, None), 3)
        for rel in карточки:
            self.assertEqual(self.читать(rel, into), self.читать(rel), rel)

    def test_подкаталог_волта_и_файл_вместо_каталога(self):
        итог, карточки = self.пересборка()
        with self.assertRaises(RuntimeError) as e:
            vr.записать(карточки, os.path.join(self.vault, "новое"), self.vault)
        self.assertIn("Г4", str(e.exception))
        self.assertFalse(os.path.exists(os.path.join(self.vault, "новое")))
        файл = os.path.join(os.path.dirname(self.vault), "файл")
        open(файл, "w").close()
        with self.assertRaises(RuntimeError):
            vr.записать(карточки, файл, self.vault)

    def test_неполный_волт_отказ(self):
        with self.assertRaises(vd.ВолтНеПрочитан):
            vr.пересобрать(self.con, self.root, os.path.join(self.root, "нет"))

    def test_командная_строка(self):
        self.con.close()
        env = dict(os.environ, MARA_BLOBS=self.root, MARA_VAULT=self.vault)

        def запуск(*args):
            return subprocess.run([sys.executable, СКРИПТ, *args], env=env,
                                  capture_output=True, text=True)
        r = запуск("--check")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("совпало 3", r.stdout)
        with open(os.path.join(self.vault, self.card), "a", encoding="utf-8") as fh:
            fh.write("рукой\n")
        r = запуск("--check", "--diff")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("разошлось: " + self.card, r.stdout)
        self.assertIn("-рукой", r.stdout)
        into = os.path.join(os.path.dirname(self.vault), "cli")
        r = запуск("--into", into)
        self.assertEqual(r.returncode, 1, r.stdout)       # записано, но расхождение названо
        self.assertIn("записано карточек: 3", r.stdout)
        self.assertNotIn("рукой", self.читать(self.card, into))
        r = запуск("--check", "--root", os.path.join(self.root, "опечатка"))
        self.assertEqual(r.returncode, 2, r.stdout)
        r = запуск("--into", into)                       # каталог уже занят
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("не пустой", r.stderr)
        r = запуск("--into", os.path.join(os.path.dirname(self.vault), "ещё"),
                   "--vault", os.path.join(self.root, "нет"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("не сравнивалось 3", r.stdout)


if __name__ == "__main__":
    unittest.main()
