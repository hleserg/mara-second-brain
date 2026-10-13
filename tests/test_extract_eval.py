"""Регрессионный корпус извлечения (Т5.5): `scripts/extract_eval.py`."""
import contextlib, io, json, os, re, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import call_extract as ce
import extract_eval as ee


class Корпус(unittest.TestCase):
    def test_записанные_ответы_дают_записанный_счёт(self):
        сумма, расхождения = ee.replay()
        self.assertEqual(расхождения, [])
        self.assertGreaterEqual(len(ee.случаи()), 10)
        # каждый счётчик хоть раз срабатывает — иначе часть правил счёта
        # корпусом не проверяется
        for k in ee.СЧЁТ:
            self.assertGreater(сумма[k], 0, k)

    def test_случаи_устроены_одинаково(self):
        for путь, с in ee.случаи():
            with self.subTest(путь=os.path.basename(путь)):
                self.assertEqual(os.path.basename(путь), с["id"] + ".json")
                for s in с["segments"]:
                    self.assertRegex(s["segment_id"], r"^s\d{4}$")
                    self.assertLessEqual(s["start_ms"], s["end_ms"])
                for k in ("model", "prompt_version", "rules_version", "response", "ожидаемо"):
                    self.assertIn(k, с["recorded"])

    def test_золото_из_расшифровки(self):
        """Ключ золота звучит в разговоре: разметка о сказанном, а не о
        том, что хотелось бы услышать."""
        for _, с in ee.случаи():
            текст = " ".join(s["text"] for s in с["segments"]).lower()
            золото = с["gold"]
            ключи = ([з["ключ"] for k in ee.СПИСКИ for з in золото.get(k) or []]
                     + list(золото.get("не_задача") or []))
            for ключ in ключи:
                for к in ключ:
                    self.assertIn(к.lower(), текст, (с["id"], к))

    def test_без_контактов(self):
        """Г7 и §7.3: в корпусе нет номеров и адресов."""
        for путь, _ in ee.случаи():
            with open(путь, encoding="utf-8") as fh:
                текст = fh.read()
            self.assertIsNone(re.search(r"\+?\d[\d\s()-]{9,}\d", текст), путь)
            self.assertNotIn("@", текст, путь)


class Регресс(unittest.TestCase):
    """Правило, которое меняет исход, видно по корпусу."""

    def test_порог_задачи_ниже_виден(self):
        with mock.patch.object(ce, "TASK_MIN", 0.6):
            _, расхождения = ee.replay()
        self.assertIn("10-nizkaya-uverennost", [r[0] for r in расхождения])

    def test_срок_без_разбора_виден(self):
        with mock.patch.object(ce, "parse_deadline", lambda phrase, occ: (None, False)):
            _, расхождения = ee.replay()
        self.assertTrue({"01-prosba-so-srokom", "04-prosba-i-obeshchanie"}
                        <= {r[0] for r in расхождения})


class Счёт(unittest.TestCase):
    ОКН = "2026-09-02T14:05:00+03:00"

    def случай(self, **золото):
        return {"id": "t", "occurred": self.ОКН,
                "segments": [{"segment_id": "s0001", "start_ms": 0, "end_ms": 1000,
                              "text": "x"}],
                "gold": золото}

    def пункт(self, action, **kw):
        d = {"action": action, "explicit": True, "deadline_phrase": "",
             "confidence": 0.95, "evidence": [{"segment": "s0001"}]}
        d.update(kw)
        return d

    def test_точность_по_задачам(self):
        с = self.случай(requests=[{"ключ": ["смет"]}], commitments=[{"ключ": ["звон"]}])
        счёт = ee.оценить(с, {"requests": [self.пункт("прислать смету"),
                                           self.пункт("перезвонить"),
                                           self.пункт("покрасить")]})
        self.assertEqual((счёт["верно"], счёт["перепутано"], счёт["лишнее"],
                          счёт["пропущено"]), (1, 1, 1, 1))
        self.assertEqual(ee.итог([счёт])["точность"], 0.333)
        self.assertIsNone(ee.итог([dict.fromkeys(ee.СЧЁТ, 0)])["точность"])

    def test_кто_по_началу_строки(self):
        self.assertTrue(ee.тот_же("Анна", "анна петрова"))
        self.assertFalse(ee.тот_же("Анна", "Олег"))
        self.assertTrue(ee.тот_же("", "кто угодно"))


class Живой(unittest.TestCase):
    """`--live` без модели: подмена `спросить` отвечает записанным, и
    `--record` переписывает корпус тем же счётом и именем модели."""

    def test_запись_корпуса(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        каталог = os.path.join(tmp, "corpus")
        shutil.copytree(ee.КОРПУС, каталог)
        ответы = {ce.transcript_text(с["segments"]): с["recorded"]["response"]
                  for _, с in ee.случаи(каталог)}
        with contextlib.redirect_stdout(io.StringIO()):
            сумма = ee.live(каталог, записать=True, спросить=ответы.__getitem__)
        было, _ = ee.replay()
        self.assertEqual(сумма, было)
        for _, с in ee.случаи(каталог):
            self.assertEqual(с["recorded"]["model"], ce.MODEL)
            self.assertEqual(с["recorded"]["prompt_version"], ce.PROMPT_VERSION)
        self.assertEqual(ee.replay(каталог)[1], [])


class Команда(unittest.TestCase):
    def пуск(self, *args):
        return subprocess.run([sys.executable, os.path.join(СКРИПТЫ, "extract_eval.py")]
                              + list(args), capture_output=True, text=True)

    def test_replay_код_0_и_итог(self):
        r = self.пуск("--replay")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"точность"', r.stdout)

    def test_расхождение_код_1(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        каталог = os.path.join(tmp, "corpus")
        shutil.copytree(ee.КОРПУС, каталог)
        путь = os.path.join(каталог, "01-prosba-so-srokom.json")
        with open(путь, encoding="utf-8") as fh:
            с = json.load(fh)
        с["recorded"]["ожидаемо"]["верно"] = 0
        with open(путь, "w", encoding="utf-8") as fh:
            json.dump(с, fh, ensure_ascii=False)
        r = self.пуск("--replay", "--corpus", каталог)
        self.assertEqual(r.returncode, 1)
        self.assertIn("01-prosba-so-srokom: ждали", r.stderr)

    def test_record_без_live_отказ(self):
        self.assertEqual(self.пуск("--record").returncode, 2)


if __name__ == "__main__":
    unittest.main()
