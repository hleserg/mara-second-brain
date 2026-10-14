"""Т7.1 и Т7.4, ADR-0010 решения 1 и 4: объявление работы (`contextd.РАБОТЫ`)
— одно место на шаг, пайплайн, узел, возможность, модель, GPU и порядок;
модели нет на узле — работа ждёт с причиной, а не сжигает ретраи до DLQ;
застава игры не отдаёт работу узлу, пока там играют или пока от него нет
сердцебиения, а GTR тяжёлого не получает вовсе."""
import contextlib, io, json, os, subprocess, sys, tempfile, unittest
from datetime import datetime, timedelta
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import mara_ingest as mi
import contextd
import contextd_reconcile as cr
import vault_common

БЕЗ_ПРОКСИ = {k: "" for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                               "all_proxy", "ALL_PROXY")}


def сердце(**kw):
    d = {"capabilities": ["asr", "llm"], "checked": ["asr", "llm"],
         "models": [vault_common.МОДЕЛЬ_ИЗВЛЕЧЕНИЯ], "accept_jobs": True,
         "minecraft_active": False, "interval_s": 60}
    d.update(kw)
    return d


class Объявление(unittest.TestCase):
    def test_пайплайн_и_узлы_как_были(self):
        self.assertEqual(contextd.STEP, {"asr": "call_asr.py", "extract": "call_extract.py",
                                         "project": "call_project.py",
                                         "digest": "call_digest.py",
                                         "nudge": "message_nudge.py"})
        self.assertEqual(contextd.NEXT, {"asr": "extract", "extract": "project",
                                         "project": "digest"})
        self.assertEqual(contextd.ТЯЖЁЛЫЕ, {"asr": ("bigpc", "MARA_ASR_URL"),
                                            "extract": ("bigpc", "MARA_LLM_URL")})

    def test_объявление_целое(self):
        поля = {"шаг", "следом", "узел", "адрес", "возможность", "модель", "gpu", "первой"}
        for kind, р in contextd.РАБОТЫ.items():
            with self.subTest(kind=kind):
                # опечатка в ключе молча выключила бы правило
                self.assertLessEqual(set(р), поля)
                self.assertTrue(os.path.isfile(os.path.join(СКРИПТЫ, р["шаг"])))
                if р.get("следом"):
                    self.assertIn(р["следом"], contextd.РАБОТЫ)
                # узел — вместе с адресом и возможностью, иначе тяжёлое шло
                # бы мимо пробы и сердцебиения
                self.assertEqual(bool(р.get("узел")), bool(р.get("адрес")))
                self.assertEqual(bool(р.get("узел")), bool(р.get("возможность")))
                # под семафором GPU — только работа узла: лёгкая держала бы
                # его на doctor, пока ждёт расшифровка
                if р.get("gpu"):
                    self.assertTrue(р.get("узел"))
                if р.get("модель"):
                    self.assertEqual(р.get("возможность"), "llm")
        self.assertEqual(tuple(k for k, р in contextd.РАБОТЫ.items() if р.get("первой")),
                         ("nudge",))

    def test_gpu_по_объявлению(self):
        with mock.patch.object(contextd, "GPU") as семафор, \
                mock.patch.object(contextd, "_запустить", return_value=(0, "")):
            for kind in list(contextd.РАБОТЫ) + ["неизвестный"]:
                семафор.reset_mock()
                contextd.run_step(kind, "e1")
                with self.subTest(kind=kind):
                    self.assertEqual(семафор.acquire.called,
                                     bool(contextd.РАБОТЫ.get(kind, {}).get("gpu")))
                    self.assertEqual(семафор.release.called, семафор.acquire.called)

    def test_модель_по_умолчанию_одна(self):
        """Шаг и воркер берут умолчание из одного места: разъехавшись, воркер
        ждал бы на узле не ту модель, которой пойдёт шаг."""
        окружение = {k: v for k, v in os.environ.items() if k != "MARA_EXTRACT_MODEL"}
        окружение["MARA_ENV_FILE"] = os.path.join(tempfile.mkdtemp(), "нет.env")
        # подменённое умолчание должно дойти до шага: двойник-литерал в
        # `call_extract` совпадал бы по значению и прятался
        r = subprocess.run([sys.executable, "-c",
                            "import vault_common; vault_common.МОДЕЛЬ_ИЗВЛЕЧЕНИЯ = 'проба:1'; "
                            "import call_extract; print(call_extract.MODEL)"],
                           cwd=СКРИПТЫ, env=окружение, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.stdout.strip(), "проба:1", r.stderr)
        with mock.patch.dict(os.environ, {"MARA_ENV_FILE": окружение["MARA_ENV_FILE"]}):
            os.environ.pop("MARA_EXTRACT_MODEL", None)
            self.assertEqual(contextd.нужная_модель("MARA_EXTRACT_MODEL"),
                             vault_common.МОДЕЛЬ_ИЗВЛЕЧЕНИЯ)


class _База(unittest.TestCase):
    def setUp(self):
        contextd._пробы.clear()
        self.addCleanup(contextd._пробы.clear)
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.addCleanup(self.con.close)
        self.env_file = os.path.join(self.dir, "env")
        окружение = mock.patch.dict(os.environ, dict(БЕЗ_ПРОКСИ, MARA_ENV_FILE=self.env_file))
        окружение.start()
        self.addCleanup(окружение.stop)
        for k in ("MARA_ASR_URL", "MARA_LLM_URL", "MARA_EXTRACT_MODEL", "MARA_GAME_GUARD"):
            os.environ.pop(k, None)

    def отчитаться(self, узел="bigpc", **kw):
        с, беда = contextd.разобрать_сердцебиение(сердце(**kw))
        assert беда is None, беда
        contextd.принять_сердцебиение(self.con, узел, с)

    def состарить(self, узел, секунд):
        row = self.con.execute("select capabilities_json from compute_nodes where id=?",
                               (узел,)).fetchone()
        d = json.loads(row[0])
        d["heartbeat_at"] = (datetime.now(mi.TZ) - timedelta(seconds=секунд)).isoformat(
            timespec="seconds")
        self.con.execute("update compute_nodes set capabilities_json=? where id=?",
                         (json.dumps(d), узел))

    def работа(self, kind, метка):
        eid, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                         "source_id": метка, "payload": {}})
        return mi.add_job(self.con, eid, kind)

    def такт(self):
        with mock.patch.object(contextd, "run_step", return_value=(True, "")) as шаг, \
                contextlib.redirect_stdout(io.StringIO()):
            job = contextd.такт(self.con)
        return job, шаг

    def строка(self, jid):
        return tuple(self.con.execute("select state, attempts, queued_reason from jobs "
                                      "where id=?", (jid,)).fetchone())


class Модель(_База):
    """Т7.1: `модель` объявления против `models` сердцебиения."""

    def test_модели_нет_ждём(self):
        self.отчитаться(models=["llama3:8b", "bge-m3:latest"])
        self.assertEqual(contextd.недоступные(self.con),
                         {"asr": None, "extract": "node_no_model:bigpc"})

    def test_модель_есть(self):
        self.отчитаться(models=["bge-m3:latest", vault_common.МОДЕЛЬ_ИЗВЛЕЧЕНИЯ])
        self.assertEqual(contextd.недоступные(self.con), {"asr": None, "extract": None})

    def test_без_тега_это_latest(self):
        os.environ["MARA_EXTRACT_MODEL"] = "bge-m3"
        self.отчитаться(models=["bge-m3:latest"])
        self.assertIsNone(contextd.недоступные(self.con)["extract"])
        os.environ["MARA_EXTRACT_MODEL"] = "llama3:latest"
        self.отчитаться(models=["llama3"])
        self.assertIsNone(contextd.недоступные(self.con)["extract"])
        self.отчитаться(models=["llama3:8b"])
        self.assertEqual(contextd.недоступные(self.con)["extract"], "node_no_model:bigpc")

    def test_не_знаю_не_держит(self):
        """Пустой список (сервис не отдал), обрезанный (агент шлёт не больше
        `МОДЕЛЕЙ_МАКС`), непроверенный сервис — «не знаю», работа идёт."""
        for kw in ({"models": []},
                   {"models": ["m%02d" % i for i in range(contextd.МОДЕЛЕЙ_МАКС)]},
                   {"models": ["llama3:8b"], "checked": ["asr"]}):
            with self.subTest(kw=kw):
                self.отчитаться(**kw)
                self.assertIsNone(contextd.недоступные(self.con)["extract"])
        # сервис не ответил — это «сервиса нет», а не «модели нет»
        self.отчитаться(models=["llama3:8b"], capabilities=["asr"])
        self.assertEqual(contextd.недоступные(self.con)["extract"], "node_no_capability:bigpc")

    def test_модель_из_файла_как_у_шага(self):
        with open(self.env_file, "w") as fh:
            fh.write("MARA_EXTRACT_MODEL=llama3:8b\n")
        self.отчитаться(models=["llama3:8b"])
        self.assertIsNone(contextd.недоступные(self.con)["extract"])
        self.отчитаться(models=[vault_common.МОДЕЛЬ_ИЗВЛЕЧЕНИЯ])
        self.assertEqual(contextd.недоступные(self.con)["extract"], "node_no_model:bigpc")
        self.assertNotIn("MARA_EXTRACT_MODEL", os.environ, "файл в окружение демона не впитан")

    def test_такт_берёт_asr_а_извлечение_ждёт(self):
        извлечение = self.работа("extract", "m1")
        расшифровка = self.работа("asr", "m2")
        self.отчитаться(models=["llama3:8b"])
        job, шаг = self.такт()
        self.assertEqual(job["kind"], "asr")
        self.assertEqual(шаг.call_args[0][0], "asr")
        self.assertEqual(self.строка(извлечение), ("ready", 0, "node_no_model:bigpc"))
        self.assertEqual(self.строка(расшифровка)[0], "done")


class Застава(_База):
    """Т7.4: застава игры — по сердцебиению узла, а не по расписанию."""

    def test_узлы_заставы(self):
        self.assertEqual(contextd.под_заставой(), {"gtr"})
        os.environ["MARA_GAME_GUARD"] = ""
        self.assertEqual(contextd.под_заставой(), set())
        os.environ["MARA_GAME_GUARD"] = " bigpc, gtr ,"
        self.assertEqual(contextd.под_заставой(), {"bigpc", "gtr"})

    def test_игра_держит(self):
        os.environ["MARA_GAME_GUARD"] = "bigpc"
        self.отчитаться(minecraft_active=True)
        self.assertEqual(contextd.недоступные(self.con),
                         {"asr": "node_gaming:bigpc", "extract": "node_gaming:bigpc"})
        self.отчитаться(minecraft_active=False)
        self.assertEqual(contextd.недоступные(self.con), {"asr": None, "extract": None})

    def test_без_сердцебиения_узла_нет(self):
        os.environ["MARA_GAME_GUARD"] = "bigpc"
        self.assertEqual(contextd.недоступные(self.con),
                         {"asr": "node_no_heartbeat:bigpc",
                          "extract": "node_no_heartbeat:bigpc"})
        self.отчитаться()
        self.состарить("bigpc", 3600)
        self.assertEqual(contextd.недоступные(self.con)["asr"], "node_no_heartbeat:bigpc")

    def test_пауза_раньше_игры(self):
        os.environ["MARA_GAME_GUARD"] = "bigpc"
        self.отчитаться(minecraft_active=True, accept_jobs=False)
        self.assertEqual(contextd.недоступные(self.con)["asr"], "node_paused:bigpc")

    def test_вне_заставы_игра_не_держит(self):
        """По умолчанию застава только у GTR: игра на bigpc тяжёлое не держит."""
        self.отчитаться(minecraft_active=True)
        self.assertEqual(contextd.недоступные(self.con), {"asr": None, "extract": None})

    def test_gtr_тяжёлого_не_получает(self):
        """P11 на уровне кода: GTR здоров, не играет и принимает работу — и
        всё равно ничего тяжёлого: его нет в объявлении (нет GPU). Его игра
        тяжёлое на bigpc тоже не держит."""
        self.assertNotIn("gtr", {у for у, _ in contextd.ТЯЖЁЛЫЕ.values()})
        for игра in (False, True):
            self.отчитаться("gtr", minecraft_active=игра)
            self.assertEqual(contextd.недоступные(self.con), {"asr": None, "extract": None})
        jid = self.работа("asr", "g1")
        job, шаг = self.такт()
        self.assertEqual((job["id"], шаг.call_args[0][0]), (jid, "asr"))

    def test_застава_на_пути_любого_допуска(self):
        """Работу, допущенную на узел под заставой, застава держит так же."""
        работа = {"шаг": "call_digest.py", "узел": "gtr", "адрес": "MARA_GTR_URL",
                  "возможность": "cpu"}
        with mock.patch.dict(contextd.РАБОТЫ, {"лёгкое": работа}), \
                mock.patch.dict(contextd.ТЯЖЁЛЫЕ, {"лёгкое": ("gtr", "MARA_GTR_URL")}):
            self.assertEqual(contextd.недоступные(self.con)["лёгкое"], "node_no_heartbeat:gtr")
            self.отчитаться("gtr", capabilities=["cpu"], checked=["cpu"],
                            minecraft_active=True)
            self.assertEqual(contextd.недоступные(self.con)["лёгкое"], "node_gaming:gtr")
            self.отчитаться("gtr", capabilities=["cpu"], checked=["cpu"])
            self.assertIsNone(contextd.недоступные(self.con)["лёгкое"])

    def test_такт_не_берёт_при_игре(self):
        os.environ["MARA_GAME_GUARD"] = "bigpc"
        jid = self.работа("asr", "t1")
        self.отчитаться(minecraft_active=True)
        with mock.patch.object(contextd, "run_step", side_effect=AssertionError("взял")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(contextd.такт(self.con))
        self.assertEqual(self.строка(jid), ("ready", 0, "node_gaming:bigpc"))


class Сверка(_База):
    def test_находка_называет_причину(self):
        тексты = {"node_no_model": "модели", "node_gaming": "игра идёт",
                  "node_no_heartbeat": "сердцебиения от него нет"}
        for причина, слово in тексты.items():
            jid = self.работа("extract", причина)
            когда = (datetime.now(mi.TZ) - timedelta(hours=30)).isoformat(timespec="seconds")
            self.con.execute("update jobs set queued_reason=?, created=?, next_at=0 "
                             "where id=?", (причина + ":bigpc", когда, jid))
        contextd.отметить_узел(self.con, contextd.ВОРКЕР, "core")
        находки = cr.узел_молчит(self.con, hours=24)
        self.assertEqual([н["check"] for н in находки], ["узел-не-берёт"] * 3)
        for н, слово in zip(находки, [тексты[k] for k in sorted(тексты)]):
            self.assertIn(слово, н["detail"])
        self.assertEqual(cr.очередь_стоит(self.con, hours=1), [])

    def test_метрика_ждущих(self):
        jid = self.работа("asr", "w1")
        self.con.execute("update jobs set queued_reason='node_gaming:bigpc' where id=?", (jid,))
        строки = contextd.metrics(self.con, self.dir, None).splitlines()
        self.assertIn('mara_jobs_waiting_node{node="bigpc"} 1', строки)


if __name__ == "__main__":
    unittest.main()
