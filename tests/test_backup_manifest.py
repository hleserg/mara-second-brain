"""Манифест резервной копии проверяется (ТЗ §5.3, §17.3 п.4; план Т3б.3).

До этой правки опись внутри архива несла sha256 файлов, но сверялась одним
способом — ночным учением, которому нужны носитель, корень и живая база, —
и отвечала на один вопрос: «тот ли хеш у каждого названного файла». Файл,
которого в описи нет, проходил молча; размеров опись не знала; `host`,
класса хранения и версии схемы не было вовсе.

Здесь сверка гоняется как команда `--verify` через `subprocess`: проверяется
дорога от флага до кода возврата, а не функция в обход `main()` (тот же
довод, что в `tests/test_backup_flags.py`). Копию портим на развёрнутом
каталоге — там каждая порча стоит одну строку, — а один сценарий собирает
порченый архив целиком, чтобы дорога через расшифровку и сайдкар тоже была
пройдена. Ночное учение в отдельном сценарии вызывается напрямую: у него
нет флага, по которому его можно было бы прогнать подпроцессом без зеркала.

Имени машины в тестах нет литералом — только `socket.gethostname()`:
репозиторий публичный.
"""
import glob, hashlib, json, os, shutil, socket, sqlite3, subprocess, sys
import tarfile, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import mara_ingest as mi

СКРИПТ = os.path.join(ROOT, "scripts", "core-backup.py")


def load(name):
    import importlib.util
    p = os.path.join(ROOT, "scripts", name)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_")[:-3], p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(shutil.which("gpg"), "нет gpg")
class Манифест(unittest.TestCase):
    def setUp(self):
        self.мод = load("core-backup.py")
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "blobs")
        self.цель = os.path.join(self.tmp, "target")
        self.пароль = os.path.join(self.tmp, "pass")
        open(self.пароль, "w").write("проверочная фраза\n")
        os.chmod(self.пароль, 0o600)
        con = mi.connect(self.root)
        тело = b"audio-for-manifest"
        s = hashlib.sha256(тело).hexdigest()
        p = mi.blob_path(self.root, s, "wav")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(тело)
        con.execute("insert into blobs(sha256,path,bytes,mime,created) "
                    "values(?,?,?,?,?)",
                    (s, p, len(тело), "audio", mi.now_iso()))
        con.commit()
        con.close()
        # Мелочь по одному файлу на каталог: вместе с базой в описи четыре
        # строки, и каждая порча ниже целит в одну из них.
        for каталог, имя_ф, текст in (("manifests", "e1.json", "{}"),
                                      ("transcripts", "e1.jsonl", "{}\n"),
                                      ("extractions", "e1.json", "{}")):
            os.makedirs(os.path.join(self.root, каталог), exist_ok=True)
            open(os.path.join(self.root, каталог, имя_ф), "w").write(текст)
        self.env = {**os.environ, "MARA_BACKUP_ALLOW_SAME_DEV": "1",
                    "MARA_STATE": os.path.join(self.tmp, "state")}
        r = subprocess.run(
            [sys.executable, СКРИПТ, "--root", self.root,
             "--targets", self.цель, "--pass-file", self.пароль,
             "--work", os.path.join(self.tmp, "work"),
             "--no-audio", "--no-drill"],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.архив = glob.glob(os.path.join(self.цель, "core-*.tar.gz.gpg"))[0]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def verify(self, путь):
        r = subprocess.run(
            [sys.executable, СКРИПТ, "--verify", путь,
             "--pass-file", self.пароль],
            capture_output=True, text=True, env=self.env)
        # Отчёт обязан быть напечатан при любом коде: падение без отчёта —
        # отдельная беда, и её ловит каждый сценарий одной строкой.
        self.assertNotIn("Traceback", r.stderr, r.stderr)
        return r.returncode, json.loads(r.stdout), r.stderr

    def развернуть(self):
        """Копия, какой её оставляет шаг 2 рунбука: расшифровано и распаковано
        в каталог, `manifest.json` на месте."""
        tar = os.path.join(self.tmp, "core.tar.gz")
        self.мод.дешифр(self.архив, tar, self.пароль)
        копия = os.path.join(self.tmp, "копия")
        os.makedirs(копия)
        with tarfile.open(tar) as tf:
            tf.extractall(копия, filter="data")
        os.unlink(tar)
        return копия

    def запаковать(self, копия, куда):
        """Обратно в зашифрованный архив — тем же `шифр`, что и бэкап."""
        tar = os.path.join(self.tmp, "re.tar.gz")
        with tarfile.open(tar, "w:gz") as tf:
            for каталог, _, имена in os.walk(копия):
                for f in sorted(имена):
                    полный = os.path.join(каталог, f)
                    tf.add(полный, arcname=os.path.relpath(полный, копия))
        self.мод.шифр(tar, куда, self.пароль)
        os.unlink(tar)

    def манифест(self, копия):
        with open(os.path.join(копия, "manifest.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def записать_манифест(self, копия, м):
        with open(os.path.join(копия, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(м, fh, ensure_ascii=False)

    def test_честная_копия_проходит(self):
        """Положительный вход — иначе отрицательные ниже доказывают не порчу,
        а то, что сверка не сходится ни на чём."""
        код, r, _ = self.verify(self.архив)
        self.assertEqual(код, 0, r)
        self.assertEqual(r["итог"], "ок", r)
        self.assertEqual(r["архив"], os.path.basename(self.архив))
        self.assertEqual(r["сайдкар"], "сошёлся", r)
        # База и три файла мелочи, сам манифест в опись не входит.
        self.assertEqual(r["файлов"], 4, r)
        self.assertEqual(r["в_копии"], 4, r)
        self.assertEqual(r["сверено_хешей"], 4, r)
        self.assertEqual(r["сверено_размеров"], 4, r)
        self.assertEqual(r["расхождения"], [], r)
        # Поля §5.3, которых раньше не было. Хост — вызовом, не литералом.
        self.assertEqual(r["версия"], 2, r)
        self.assertEqual(r["host"], socket.gethostname(), r)
        # Поколения (Т3б.5): политика целиком, не только суточный счёт.
        self.assertEqual(r["retention"],
                         {"class": "daily", "keep": 8, "weekly": 5, "monthly": 6}, r)
        con = sqlite3.connect(os.path.join(self.root, "contextd.db"))
        версия_схемы = con.execute("pragma user_version").fetchone()[0]
        con.close()
        self.assertEqual(r["schema_version"], версия_схемы, r)
        # Версия схемы ненулевая с Т2.1: иначе эта застава сверяла бы ноль с
        # нулём и не заметила бы манифест, в котором поля нет вовсе.
        self.assertGreater(версия_схемы, 0)
        # Та же копия каталогом и каталог носителя: оба входа `--verify`.
        код, r2, _ = self.verify(self.развернуть())
        self.assertEqual((код, r2["итог"], r2["сверено_хешей"]), (0, "ок", 4), r2)
        код, r3, _ = self.verify(self.цель)
        self.assertEqual((код, r3["итог"]), (0, "ок"), r3)
        self.assertEqual(r3["архив"], os.path.basename(self.архив), r3)

    def test_подменённый_байт_ловится(self):
        """Байт на байт, длина та же: размер сходится, хеш — нет. Так
        различаются два сторожа, и в расхождении назван только тот, что
        сработал."""
        копия = self.развернуть()
        файл = os.path.join(копия, "manifests", "e1.json")
        self.assertEqual(open(файл, "rb").read(), b"{}")
        open(файл, "wb").write(b"[]")
        код, r, _ = self.verify(копия)
        self.assertEqual(код, 1, r)
        self.assertEqual(r["итог"], "расхождения", r)
        self.assertEqual(len(r["расхождения"]), 1, r)
        self.assertIn("manifests/e1.json", r["расхождения"][0])
        self.assertIn("sha256", r["расхождения"][0])
        self.assertNotIn("размер", r["расхождения"][0])
        self.assertEqual(r["сверено_размеров"], 4, r)
        self.assertEqual(r["сверено_хешей"], 3, r)

    def test_подменённый_байт_в_архиве_ловится(self):
        """Та же порча, но через архив: дорога с расшифровкой. Перешифрованный
        архив заодно расходится с сайдкаром — и это обязано быть названо
        первым: сайдкар единственное, что сверяется без пароля."""
        копия = self.развернуть()
        open(os.path.join(копия, "extractions", "e1.json"), "wb").write(b"[]")
        self.запаковать(копия, self.архив)
        код, r, _ = self.verify(self.архив)
        self.assertEqual(код, 1, r)
        self.assertTrue(r["сайдкар"].startswith("не сошёлся"), r)
        self.assertIn("сайдкар", r["расхождения"][0])
        self.assertTrue(any("extractions/e1.json" in x and "sha256" in x
                            for x in r["расхождения"]), r)

    def test_лишний_файл_ловится(self):
        """Файл, которого в описи нет, — главная дыра старой сверки: цикл шёл
        по описи, и чужое в копии не замечал никто."""
        копия = self.развернуть()
        open(os.path.join(копия, "manifests", "чужой.json"), "w").write("{}")
        код, r, _ = self.verify(копия)
        self.assertEqual(код, 1, r)
        self.assertEqual(r["расхождения"], ["лишний файл: manifests/чужой.json"], r)
        self.assertEqual(r["в_копии"], 5, r)
        # Хеши остальных при этом сверены все: лишний файл не отменяет
        # сверки названных.
        self.assertEqual(r["сверено_хешей"], 4, r)

    def test_пропавший_файл_ловится(self):
        копия = self.развернуть()
        os.unlink(os.path.join(копия, "transcripts", "e1.jsonl"))
        код, r, _ = self.verify(копия)
        self.assertEqual(код, 1, r)
        self.assertEqual(r["расхождения"], ["нет файла: transcripts/e1.jsonl"], r)
        self.assertEqual(r["сверено_хешей"], 3, r)

    def test_размер_ловится_и_называется(self):
        """Дописанный байт меняет и размер, и хеш; в расхождении названы оба —
        по размеру видно, что файл дописали, а не переписали."""
        копия = self.развернуть()
        open(os.path.join(копия, "manifests", "e1.json"), "ab").write(b"\n")
        код, r, _ = self.verify(копия)
        self.assertEqual(код, 1, r)
        self.assertEqual(len(r["расхождения"]), 1, r)
        self.assertIn("размер 3 вместо 2", r["расхождения"][0])
        self.assertIn("sha256", r["расхождения"][0])

    def test_schema_version_сверяется_с_базой(self):
        """Копия до миграции с описью после неё — единственное, что отличит
        их без счётчиков, это версия схемы."""
        копия = self.развернуть()
        м = self.манифест(копия)
        м["schema_version"] += 1
        self.записать_манифест(копия, м)
        код, r, _ = self.verify(копия)
        self.assertEqual(код, 1, r)
        self.assertEqual(len(r["расхождения"]), 1, r)
        self.assertIn("schema_version", r["расхождения"][0])

    def test_старый_манифест_без_хешей_не_падает(self):
        """Опись без `files`: проверить нечем, и сказать об этом надо
        словами и своим кодом возврата — не нулём (копия не проверена) и не
        единицей (расхождений нет), и уж точно не трейсбеком."""
        копия = self.развернуть()
        м = self.манифест(копия)
        del м["files"], м["bytes"]
        self.записать_манифест(копия, м)
        код, r, шум = self.verify(копия)
        self.assertEqual(код, 2, (r, шум))
        self.assertEqual(r["итог"], "хешей нет", r)
        self.assertEqual(r["расхождения"], [], r)
        self.assertEqual(r["файлов"], 0, r)
        self.assertEqual(r["сверено_хешей"], 0, r)
        # Что в копии лежит — посчитано: пустая опись не делает копию пустой.
        self.assertEqual(r["в_копии"], 4, r)

    def test_манифест_версии_1_сверяется_по_хешам(self):
        """Архивы до Т3б.3 лежат на носителях и обязаны читаться: хеши там
        есть, размеров нет (кроме `db_bytes`), новых полей нет. Сверка идёт
        по тому, что есть, а про остальное говорит «нет», а не падает."""
        копия = self.развернуть()
        м = self.манифест(копия)
        for ключ in ("bytes", "host", "retention", "schema_version",
                     "manifest_version"):
            del м[ключ]
        self.записать_манифест(копия, м)
        код, r, _ = self.verify(копия)
        self.assertEqual(код, 0, r)
        self.assertEqual(r["итог"], "ок", r)
        self.assertEqual(r["версия"], 1, r)
        self.assertIsNone(r["host"], r)
        self.assertEqual(r["сверено_хешей"], 4, r)
        # Размер базы версия 1 знала одной строкой `db_bytes` — он сверен.
        self.assertEqual(r["размеров"], "есть", r)
        self.assertEqual(r["сверено_размеров"], 1, r)
        del м["db_bytes"]
        self.записать_манифест(копия, м)
        код, r, _ = self.verify(копия)
        self.assertEqual((код, r["итог"]), (0, "ок"), r)
        self.assertEqual(r["размеров"], "нет в манифесте", r)
        self.assertEqual(r["сверено_размеров"], 0, r)

    def test_ночное_учение_сверяет_опись(self):
        """Та же сверка стоит внутри `проверка`: архив с лишним файлом ночью
        разворачивать нельзя. Прежний цикл по описи его не видел."""
        копия = self.развернуть()
        open(os.path.join(копия, "manifests", "чужой.json"), "w").write("{}")
        имя = "core-1999-01-01.tar.gz.gpg"
        self.запаковать(копия, os.path.join(self.цель, имя))
        with self.assertRaises(RuntimeError) as ctx:
            self.мод.проверка(self.цель, self.пароль, self.root, имя=имя,
                              без_зеркала="зеркала в тесте нет")
        self.assertIn("лишний файл: manifests/чужой.json", str(ctx.exception))
        # Контроль: нетронутый архив тем же путём проходит, и в его итоге
        # названы поля описи — ночной JSON теперь говорит, с чем сверяли.
        r = self.мод.проверка(self.цель, self.пароль, self.root,
                              имя=os.path.basename(self.архив),
                              без_зеркала="зеркала в тесте нет")
        self.assertEqual(r["файлов"], 4, r)
        self.assertEqual(r["манифест"]["host"], socket.gethostname(), r)
        self.assertEqual(r["манифест"]["сверено_размеров"], 4, r)


    def test_файл_между_описью_и_таром_не_роняет_учение(self):
        """Ревью PR #117, P2-4: опись и тар обходили мелочь по отдельности;
        файл, положенный воркером в зазор, попадал в тар, но не в опись, и
        ночное учение падало на «лишнем файле» — а за ним не шла ротация.
        Теперь обход один; подмена `мелочь` подкладывает файл на втором
        вызове, и при одном обходе второго вызова просто нет."""
        import unittest.mock
        исходная = self.мод.мелочь
        вызовов = []

        def подмена(root):
            вызовов.append(1)
            if len(вызовов) >= 2:
                open(os.path.join(root, "transcripts", "e2.jsonl"), "w").write("{}\n")
            return исходная(root)

        with unittest.mock.patch.object(self.мод, "мелочь", подмена), \
                unittest.mock.patch.dict(os.environ, {"MARA_BACKUP_ALLOW_SAME_DEV": "1"}), \
                unittest.mock.patch.object(self.мод.mi, "ОТМЕТКА_НОСИТЕЛЕЙ",
                                           os.path.join(self.tmp, "state", "core-targets.json")):
            os.makedirs(os.path.join(self.tmp, "state"), exist_ok=True)
            r = self.мод.прогон(self.root, [self.цель], self.пароль, 7,
                                os.path.join(self.tmp, "work"), аудио=False, drill=True)
        self.assertEqual(len(вызовов), 1, "мелочь обошли дважды")
        self.assertFalse(os.path.exists(os.path.join(self.root, "transcripts", "e2.jsonl")))
        self.assertTrue(r, r)

    def test_verify_не_открыл_это_код_3_без_трейсбека(self):
        """Ревью P3-3: «не смог открыть» и «разошлось» не сливаются в единицу."""
        for args in (["--verify", os.path.join(self.tmp, "нет.tar.gz.gpg")],
                     ["--verify", self.архив, "--pass-file", os.path.join(self.tmp, "нет")]):
            r = subprocess.run([sys.executable, СКРИПТ] + args,
                               capture_output=True, text=True, env=self.env)
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("не смог открыть", r.stderr)


    def test_подмена_файла_между_описью_и_таром_не_ломает_копию(self):
        """Codex по #117, P2: воркер подменил расшифровку после хеша, но до
        tar.add — архив не сходился с собственной описью. Теперь и опись, и
        тар читают одну копию из стейджа."""
        import unittest.mock
        исходный = self.мод.архив

        def подмена(root, снимок_db, манифест, dst, файлы_мелочи=None):
            with open(os.path.join(self.root, "transcripts", "e1.jsonl"), "w") as fh:
                fh.write("подменили после хеша\n")
            return исходный(root, снимок_db, манифест, dst, файлы_мелочи)

        with unittest.mock.patch.object(self.мод, "архив", подмена), \
                unittest.mock.patch.dict(os.environ, {"MARA_BACKUP_ALLOW_SAME_DEV": "1"}), \
                unittest.mock.patch.object(self.мод.mi, "ОТМЕТКА_НОСИТЕЛЕЙ",
                                           os.path.join(self.tmp, "state", "core-targets.json")):
            os.makedirs(os.path.join(self.tmp, "state"), exist_ok=True)
            r = self.мод.прогон(self.root, [self.цель], self.пароль, 7,
                                os.path.join(self.tmp, "work"), аудио=False, drill=True)
        self.assertTrue(r, r)


if __name__ == "__main__":
    unittest.main()
