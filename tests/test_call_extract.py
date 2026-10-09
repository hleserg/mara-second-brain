"""Правила извлечения (ТЗ §9, §20).

Модель тут не зовётся: она недетерминирована, а проверяем мы правила, а не её
настроение. На вход подаётся то, что модель могла бы вернуть.
"""
import os, sys, json, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_extract as ce

OCC = "2026-09-02T14:05:00+03:00"          # среда
SPAN = [{"segment": "s0001"}]
СЕГМЕНТЫ = {1: {"start_ms": 0, "end_ms": 25000, "id": "seg-1"},
            2: {"start_ms": 23000, "end_ms": 48000, "id": "seg-2"}}


class Правила(unittest.TestCase):
    def test_явная_просьба_становится_задачей(self):
        raw = {"requests": [{"action": "прислать смету", "requester": "Анна",
                             "owner": "sergey", "explicit": True, "confidence": 0.93,
                             "evidence": SPAN}]}
        out = ce.normalize(raw, OCC, СЕГМЕНТЫ)
        self.assertEqual(out["requests"][0]["disposition"], "task")

    def test_предположение_не_становится_обязательством(self):
        raw = {"requests": [{"action": "может, покрасить стены", "explicit": False,
                             "confidence": 0.92, "evidence": SPAN}]}
        out = ce.normalize(raw, OCC, СЕГМЕНТЫ)
        self.assertEqual(out["requests"][0]["disposition"], "needs-review",
                         "неявное не становится задачей даже при высокой уверенности")

    def test_середина_шкалы_идёт_на_проверку(self):
        raw = {"requests": [{"action": "что-то", "explicit": True, "confidence": 0.7,
                             "evidence": SPAN}]}
        self.assertEqual(ce.normalize(raw, OCC, СЕГМЕНТЫ)["requests"][0]["disposition"],
                         "needs-review")

    def test_ниже_порога_не_создаётся(self):
        raw = {"requests": [{"action": "что-то", "confidence": 0.4, "evidence": SPAN}]}
        self.assertEqual(ce.normalize(raw, OCC, СЕГМЕНТЫ)["requests"], [])

    def test_явный_дедлайн_парсится(self):
        raw = {"commitments": [{"action": "смета", "owner": "sergey", "confidence": 0.9,
                                "deadline_phrase": "до пятницы", "explicit": True,
                                "evidence": SPAN}]}
        out = ce.normalize(raw, OCC, СЕГМЕНТЫ)
        self.assertEqual(out["commitments"][0]["due_at"], "2026-09-04")
        self.assertTrue(out["commitments"][0]["deadline_explicit"])

    def test_завтра_считается_от_времени_разговора(self):
        raw = {"commitments": [{"action": "перезвонить", "confidence": 0.9,
                                "explicit": True, "deadline_phrase": "завтра",
                                "evidence": SPAN}]}
        self.assertEqual(ce.normalize(raw, OCC, СЕГМЕНТЫ)["commitments"][0]["due_at"], "2026-09-03")

    def test_размытый_дедлайн_не_выдумывается(self):
        raw = {"commitments": [{"action": "смета", "confidence": 0.9, "explicit": True,
                                "deadline_phrase": "побыстрее", "evidence": SPAN}]}
        out = ce.normalize(raw, OCC, СЕГМЕНТЫ)
        self.assertIsNone(out["commitments"][0]["due_at"])
        self.assertFalse(out["commitments"][0]["deadline_explicit"])
        self.assertEqual(out["commitments"][0]["deadline_phrase"], "побыстрее",
                         "исходная фраза сохраняется, ТЗ §9")

    def test_пункт_без_спана_выбрасывается(self):
        raw = {"commitments": [{"action": "нечто", "confidence": 0.99, "evidence": []}]}
        self.assertEqual(ce.normalize(raw, OCC, СЕГМЕНТЫ)["commitments"], [])

    def test_спан_без_начала_не_считается_спаном(self):
        raw = {"commitments": [{"action": "нечто", "confidence": 0.99,
                                "evidence": [{"end_ms": 10}]}]}
        self.assertEqual(ce.normalize(raw, OCC, СЕГМЕНТЫ)["commitments"], [])

    def test_новое_поручение_вытесняет_старое_через_supersedes(self):
        raw = {"changed_instructions": [{"supersedes": "смета до пятницы",
                                         "new_state": "смету не надо, нужен счёт",
                                         "confidence": 0.9, "explicit": True,
                                         "evidence": SPAN}]}
        out = ce.normalize(raw, OCC, СЕГМЕНТЫ)
        self.assertEqual(out["changed_instructions"][0]["supersedes"], "смета до пятницы")
        self.assertEqual(out["changed_instructions"][0]["new_state"],
                         "смету не надо, нужен счёт")

    def test_упомянутые_люди_и_проекты_переносятся(self):
        raw = {"people_mentioned": ["Анна"], "projects_mentioned": ["ремонт"]}
        out = ce.normalize(raw, OCC, СЕГМЕНТЫ)
        self.assertEqual(out["people_mentioned"], ["Анна"])
        self.assertEqual(out["projects_mentioned"], ["ремонт"])

    def test_пустой_ответ_модели_не_ломает(self):
        out = ce.normalize({}, OCC, СЕГМЕНТЫ)
        self.assertEqual(out["requests"], [])
        self.assertEqual(out["people_mentioned"], [])


class Evidence(unittest.TestCase):
    """Т2.4, ADR-0004 п.2–3 (P12 §16.2): модель называет сегмент; ссылка в
    несуществующий сегмент или за его границы не сохраняется; пункт без
    валидной ссылки не создаётся, с частично отклонёнными — идёт в ревью."""

    def пункт(self, evidence, **kw):
        it = {"action": "прислать смету", "explicit": True, "confidence": 0.95,
              "deadline_phrase": "", "evidence": evidence}
        it.update(kw)
        return ce.normalize({"requests": [it]}, OCC, СЕГМЕНТЫ)

    def test_ссылка_по_метке_получает_границы_сегмента_и_id(self):
        out = self.пункт([{"segment": "s0002"}])
        self.assertEqual(out["requests"][0]["evidence"],
                         [{"segment": "s0002", "segment_id": "seg-2",
                           "start_ms": 23000, "end_ms": 48000}])
        self.assertEqual(out["requests"][0]["disposition"], "task")
        self.assertEqual(out["evidence_rejected"], [])

    def test_несуществующий_сегмент_отклоняется_и_пункт_не_создаётся(self):
        out = self.пункт([{"segment": "s0009"}])
        self.assertEqual(out["requests"], [])
        о, = out["evidence_rejected"]
        self.assertEqual((о["list"], о["why"], о["evidence"]),
                         ("requests", "нет такого сегмента", {"segment": "s0009"}))

    def test_частично_отклонённый_пункт_идёт_в_ревью(self):
        out = self.пункт([{"segment": "s0001"}, {"segment": "s0042"}])
        it, = out["requests"]
        self.assertEqual(it["disposition"], "needs-review")
        self.assertEqual([e["segment"] for e in it["evidence"]], ["s0001"])
        self.assertEqual(len(out["evidence_rejected"]), 1)

    def test_подынтервал_внутри_сегмента_сохраняется_за_границами_нет(self):
        out = self.пункт([{"segment": "s0002", "start_ms": 25000, "end_ms": 30000}])
        self.assertEqual(out["requests"][0]["evidence"][0]["start_ms"], 25000)
        out = self.пункт([{"segment": "s0002", "start_ms": 10000, "end_ms": 30000}])
        self.assertEqual(out["requests"], [])
        self.assertEqual(out["evidence_rejected"][0]["why"], "подынтервал за границами сегмента")
        out = self.пункт([{"segment": "s0002", "start_ms": 30000, "end_ms": 25000}])
        self.assertEqual(out["requests"], [], "конец раньше начала")

    def test_миллисекунды_без_метки_не_evidence(self):
        """Старая форма ответа — просто спан — теперь ничего не подтверждает."""
        out = self.пункт([{"start_ms": 0, "end_ms": 1000}])
        self.assertEqual(out["requests"], [])
        self.assertEqual(out["evidence_rejected"][0]["why"], "нет такого сегмента")

    def test_сегменты_из_файла_без_id(self):
        сег = ce.сегменты_из([{"segment_id": "s0001", "start_ms": 0, "end_ms": 5},
                              {"segment_id": "мусор", "start_ms": 0, "end_ms": 5}])
        self.assertEqual(сег, {1: {"start_ms": 0, "end_ms": 5, "id": None}})

    def test_схема_просит_сегмент_а_не_миллисекунды(self):
        ev = ce.ITEM["properties"]["evidence"]["items"]
        self.assertEqual(ev["required"], ["segment"])
        self.assertIn("segment", ce.PROMPT)
        self.assertNotIn("start_ms", ce.PROMPT)
        self.assertGreaterEqual(ce.PROMPT_VERSION, 2)


class Шаг(unittest.TestCase):
    """`run`: сегменты для сверки берутся из реестра, отказ по evidence —
    строка `audit_events` одной транзакцией с переходом события."""

    def setUp(self):
        import tempfile
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "scripts"))
        import mara_ingest as mi, call_asr
        self.mi, self.asr = mi, call_asr
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.eid, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "d1",
            "occurred_at": OCC, "payload": {}})
        self.segs = [{"segment_id": "s0001", "start_ms": 0, "end_ms": 25000,
                      "speaker": "unknown-A", "text": "пришлю смету"}]
        call_asr.write_jsonl(mi.transcript_path(self.dir, self.eid), self.segs)
        self.tid = call_asr.записать_сегменты(self.con, self.eid, None, self.segs)

    def прогон(self, ответ):
        было = ce.ask_model
        ce.ask_model = lambda text, base_url=None, model=None: ответ
        try:
            ce.run(self.eid, self.dir)
        finally:
            ce.ask_model = было
        with open(self.mi.extraction_path(self.dir, self.eid), encoding="utf-8") as fh:
            return json.load(fh)

    def test_отказ_по_evidence_ложится_в_аудит(self):
        extr = self.прогон({"requests": [
            {"action": "прислать смету", "explicit": True, "confidence": 0.95,
             "deadline_phrase": "", "evidence": [{"segment": "s0001"}, {"segment": "s0007"}]},
            {"action": "выдумка", "explicit": True, "confidence": 0.95,
             "deadline_phrase": "", "evidence": [{"segment": "s0009"}]}]})
        self.assertEqual([it["action"] for it in extr["requests"]], ["прислать смету"])
        self.assertEqual(extr["requests"][0]["disposition"], "needs-review")
        self.assertEqual(extr["requests"][0]["evidence"][0]["segment_id"],
                         self.con.execute("select id from transcript_segments").fetchone()[0])
        self.assertEqual(extr["transcript_id"], self.tid)
        self.assertNotIn("evidence_rejected", extr)
        рows = [dict(r) for r in self.con.execute(
            "select * from audit_events where action='evidence_rejected'")]
        self.assertEqual(len(рows), 2)
        д = json.loads(рows[0]["detail_json"])
        self.assertEqual((рows[0]["actor_type"], рows[0]["actor_id"], рows[0]["object_id"]),
                         ("model", ce.MODEL, self.eid))
        self.assertEqual((д["why"], д["evidence"], д["transcript_id"]),
                         ("нет такого сегмента", {"segment": "s0007"}, self.tid))
        self.assertNotIn("смету", рows[0]["detail_json"].replace("прислать смету", ""),
                         "текста расшифровки в аудите нет")
        self.assertEqual(self.con.execute("select state from events").fetchone()[0],
                         "extracted")

    def test_расшифровка_без_строк_сверяется_по_файлу(self):
        """Звонок, расшифрованный до Т5.1: строк нет, сверка — по файлу,
        `segment_id` пустой, `transcript_id` пустой."""
        self.con.execute("delete from transcript_segments")
        self.con.execute("delete from transcripts")
        extr = self.прогон({"requests": [
            {"action": "прислать смету", "explicit": True, "confidence": 0.95,
             "deadline_phrase": "", "evidence": [{"segment": "s0001"}]}]})
        self.assertEqual(extr["requests"][0]["evidence"],
                         [{"segment": "s0001", "segment_id": None, "start_ms": 0,
                           "end_ms": 25000}])
        self.assertIsNone(extr["transcript_id"])


class Промпт(unittest.TestCase):
    def test_транскрипт_печатается_со_спанами(self):
        segs = [{"segment_id": "s0001", "start_ms": 0, "end_ms": 25000,
                 "speaker": "unknown-A", "text": "привет"}]
        t = ce.transcript_text(segs)
        self.assertIn("s0001", t)
        self.assertIn("00:00", t)
        self.assertIn("привет", t)

    def test_схема_покрывает_поля_тз(self):
        props = ce.SCHEMA["properties"]
        for key in ("requests", "commitments", "decisions", "constraints",
                    "open_questions", "changed_instructions", "people_mentioned",
                    "projects_mentioned", "followups"):
            self.assertIn(key, props)


if __name__ == "__main__":
    unittest.main()
