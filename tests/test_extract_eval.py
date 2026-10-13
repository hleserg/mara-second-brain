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
        """Ключ золота и имя «кто» звучат в разговоре: разметка о сказанном,
        а модели, кроме расшифровки, ничего не дают."""
        for _, с in ee.случаи():
            текст = ee._текст(" ".join(s["text"] for s in с["segments"]))
            золото = с["gold"]
            ключи = ([з["ключ"] for k in ee.СПИСКИ for з in золото.get(k) or []]
                     + list(золото.get("не_задача") or []))
            for ключ in ключи:
                for к in ключ:
                    self.assertTrue(ee._есть(к, текст), (с["id"], к))
            for k in ee.СПИСКИ:
                for з in золото.get(k) or []:
                    кто = з.get(ee.КТО[k])
                    if кто:
                        self.assertIn(ee._текст(кто), текст, (с["id"], кто))

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
        с = self.случай(requests=[{"ключ": ["смет"]}], commitments=[{"ключ": ["перезвон"]}])
        счёт = ee.оценить(с, {"requests": [self.пункт("прислать смету"),
                                           self.пункт("перезвонить"),
                                           self.пункт("покрасить")]})
        self.assertEqual((счёт["верно"], счёт["перепутано"], счёт["лишнее"],
                          счёт["пропущено"]), (1, 1, 1, 1))
        self.assertEqual(ee.итог([счёт])["точность"], 0.333)
        self.assertIsNone(ee.итог([dict.fromkeys(ee.СЧЁТ, 0)])["точность"])

    def test_кто_по_началу_строки(self):
        self.assertTrue(ee.тот_же("Анна", "анна петрова"))
        self.assertTrue(ee.тот_же("Алёна", "Алена"))
        self.assertFalse(ee.тот_же("Анна", "Олег"))
        self.assertTrue(ee.тот_же("", "кто угодно"))

    def test_ключ_с_начала_слова_ё_и_варианты(self):
        узнаёт = lambda ключ, action: ee.узнаёт({"ключ": ключ}, {"action": action})
        self.assertTrue(узнаёт(["счёт"], "оплатить счет"))
        self.assertTrue(узнаёт(["чертеж"], "посмотреть чертёж"))
        self.assertFalse(узнаёт(["счёт"], "сделать расчёт"))
        self.assertFalse(узнаёт(["олег", "ключ"], "попросить Олега"))
        self.assertTrue(узнаёт([["перезвон", "позвон"], "плитк"], "позвонить по плитке"))

    def test_дубль_не_верно(self):
        с = self.случай(requests=[{"ключ": ["смет"]}])
        счёт = ee.оценить(с, {"requests": [self.пункт("прислать смету"),
                                           self.пункт("скинуть смету")]})
        self.assertEqual((счёт["верно"], счёт["лишнее"]), (1, 1))

    def test_сопоставление_один_к_одному_а_не_жадное(self):
        """Широкое и узкое золото, пункты в «неудобном» порядке: правильный
        ответ — два верных, а не верно, лишнее и пропущено."""
        с = self.случай(requests=[{"ключ": ["смет"]}, {"ключ": ["смет", "ремонт"]}])
        счёт = ee.оценить(с, {"requests": [self.пункт("прислать смету по ремонту"),
                                           self.пункт("прислать смету")]})
        self.assertEqual((счёт["верно"], счёт["лишнее"], счёт["пропущено"]), (2, 0, 0))
        # первый пункт годится обоим золотам, второй — только первому:
        # без перекладки (дополняющего пути) второй остался бы без пары
        с = self.случай(requests=[{"ключ": ["смет"]}, {"ключ": ["договор"]}])
        счёт = ee.оценить(с, {"requests": [self.пункт("прислать смету и договор"),
                                           self.пункт("прислать смету")]})
        self.assertEqual((счёт["верно"], счёт["лишнее"], счёт["пропущено"]), (2, 0, 0))

    def test_при_равенстве_пар_узкое_золото(self):
        """Один пункт годится широкому и узкому золоту: пара — с узким, и
        срок сверяется с ним, а не с чужим."""
        с = self.случай(requests=[{"ключ": ["смет"], "due_at": "2026-09-04"},
                                  {"ключ": ["смет", "ремонт"], "due_at": None}])
        счёт = ee.оценить(с, {"requests": [self.пункт("прислать смету по ремонту")]})
        self.assertEqual((счёт["верно"], счёт["срок_не_тот"], счёт["пропущено"]), (1, 0, 1))

    def test_при_равенстве_числа_пар_самая_узкая_раскладка(self):
        """Две раскладки по две пары: широкое+узкое (1+3) и среднее+узкое
        (3+2) — берётся вторая, и срок сверяется с её золотом."""
        с = self.случай(requests=[{"ключ": ["x"], "due_at": "2026-09-04"},
                                  {"ключ": ["y", "z"], "due_at": None},
                                  {"ключ": ["c", "d", "e"], "due_at": None}])
        пары = ee.сопоставить([self.пункт("x c d e"), self.пункт("y z c d e")],
                              с["gold"]["requests"])
        self.assertEqual(пары, {0: 2, 1: 1})

    def test_на_ревью_только_пунктом_ревью(self):
        с = self.случай(requests=[{"ключ": ["смет"]}, {"ключ": ["смет", "ремонт"]}])
        счёт = ee.оценить(с, {"requests": [self.пункт("прислать смету по ремонту")]})
        self.assertEqual((счёт["верно"], счёт["на_ревью"], счёт["пропущено"]), (1, 0, 1))
        # один пункт ревью закрывает одно золото, не два
        счёт = ee.оценить(с, {"requests": [self.пункт("прислать смету по ремонту",
                                                      confidence=0.7)]})
        self.assertEqual((счёт["на_ревью"], счёт["пропущено"]), (1, 1))

    def test_срок_null_сверяется_а_без_срока_нет(self):
        пункт = self.пункт("прислать смету", deadline_phrase="до пятницы")
        счёт = ee.оценить(self.случай(requests=[{"ключ": ["смет"], "due_at": None}]),
                          {"requests": [пункт]})
        self.assertEqual(счёт["срок_не_тот"], 1)
        счёт = ee.оценить(self.случай(requests=[{"ключ": ["смет"]}]), {"requests": [пункт]})
        self.assertEqual(счёт["срок_не_тот"], 0)

    def test_ответ_не_объект(self):
        with self.assertRaises(ValueError):
            ee.оценить(self.случай(), [])


class Живой(unittest.TestCase):
    """`--live` без модели: подмена `спросить` отвечает записанным. `--record`
    кладёт ответ в `живой`, синтетику не трогает; при ошибке хоть на одном
    случае не пишет ничего."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.каталог = os.path.join(tmp, "corpus")
        shutil.copytree(ee.КОРПУС, self.каталог)
        self.ответы = {ce.transcript_text(с["segments"]): с["recorded"]["response"]
                       for _, с in ee.случаи(self.каталог)}

    def снимок(self):
        return {п: open(п, encoding="utf-8").read() for п, _ in ee.случаи(self.каталог)}

    def test_запись_рядом_с_синтетикой(self):
        было = self.снимок()
        with contextlib.redirect_stdout(io.StringIO()):
            сумма = ee.live(self.каталог, записать=True, спросить=self.ответы.__getitem__)
        синтетика, _ = ee.replay()
        self.assertEqual(сумма, dict(синтетика, ошибка=0))
        for путь, с in ee.случаи(self.каталог):
            self.assertEqual(с["живой"]["model"], ce.MODEL)
            self.assertEqual(с["живой"]["prompt_version"], ce.PROMPT_VERSION)
            исходный = json.loads(было[путь])
            self.assertEqual(с["recorded"], исходный["recorded"], "синтетика тронута")
        self.assertEqual(ee.replay(self.каталог)[1], [])
        self.assertEqual(ee.replay(self.каталог, "живой")[1], [])

    def test_без_record_не_пишет(self):
        было = self.снимок()
        with contextlib.redirect_stdout(io.StringIO()):
            ee.live(self.каталог, записать=False, спросить=self.ответы.__getitem__)
        self.assertEqual(self.снимок(), было)

    def test_ошибка_на_случае_не_пишет_ничего(self):
        было = self.снимок()
        четвёртый = ce.transcript_text(ee.случаи(self.каталог)[3][1]["segments"])

        def спросить(текст):
            if текст == четвёртый:
                raise OSError("модель недоступна")
            return self.ответы[текст]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            сумма = ee.live(self.каталог, записать=True, спросить=спросить)
        self.assertEqual(сумма["ошибка"], 1)
        self.assertIn("ошибка: OSError", out.getvalue())
        self.assertIn("корпус не записан", err.getvalue())
        self.assertEqual(self.снимок(), было)


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
