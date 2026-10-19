"""Т7.1 и Т7.4, ADR-0010 решения 1 и 4: объявление работы (`contextd.РАБОТЫ`)
— одно место на шаг, пайплайн, узел, возможность, GPU и порядок;
сервиса нет на узле — работа ждёт с причиной, а не сжигает ретраи до DLQ;
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
import node_agent
import vault_common

БЕЗ_ПРОКСИ = {k: "" for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                               "all_proxy", "ALL_PROXY")}


def сердце(**kw):
    d = {"capabilities": ["asr", "llm"], "checked": ["asr", "llm"],
         "models": [vault_common.МОДЕЛЬ_ИЗВЛЕЧЕНИЯ], "models_complete": True,
         "accept_jobs": True, "minecraft_active": False, "interval_s": 60}
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
        # ADR-0013: извлечение — облачная модель, на узел не ходит
        self.assertEqual(contextd.ТЯЖЁЛЫЕ, {"asr": ("bigpc", "MARA_ASR_URL")})

    def test_объявление_целое(self):
        поля = {"шаг", "следом", "узел", "адрес", "возможность", "gpu", "первой"}
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

    def test_модель_по_умолчанию_из_vault_common(self):
        """Умолчание модели шага — из `vault_common`: двойник-литерал в
        `call_extract` совпадал бы по значению и прятался."""
        окружение = {k: v for k, v in os.environ.items() if k != "MARA_EXTRACT_MODEL"}
        окружение["MARA_ENV_FILE"] = os.path.join(tempfile.mkdtemp(), "нет.env")
        r = subprocess.run([sys.executable, "-c",
                            "import vault_common; vault_common.МОДЕЛЬ_ИЗВЛЕЧЕНИЯ = 'проба:1'; "
                            "import call_extract; print(call_extract.MODEL)"],
                           cwd=СКРИПТЫ, env=окружение, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.stdout.strip(), "проба:1", r.stderr)


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


class Агент(_База):
    """Ревью Т7, P1-1(а) и P2-1: агент говорит, полон ли список моделей, —
    через настоящий `node_agent.модели`."""

    def модели(self, ответ):
        with mock.patch.object(node_agent, "отвечает", lambda *a: ответ):
            return node_agent.модели("http://x")

    def список(self, имена):
        return (True, json.dumps({"models": [{"name": и} for и in имена]}).encode())

    def test_полнота(self):
        длинное = "hf.co/" + "u" * 40 + "/" + "r" * 40 + "-GGUF:Q4_K_M"
        self.assertEqual(self.модели(self.список(["qwen3.5:9b", длинное, "bge-m3:latest"])),
                         (["bge-m3:latest", "qwen3.5:9b"], False), "имя отброшено")
        имена = ["m%02d" % i for i in range(node_agent.МОДЕЛЕЙ_МАКС + 1)]
        self.assertEqual(self.модели(self.список(имена)),
                         (имена[:node_agent.МОДЕЛЕЙ_МАКС], False), "обрезан")
        for n in (node_agent.МОДЕЛЕЙ_МАКС, node_agent.МОДЕЛЕЙ_МАКС - 1, 0):
            self.assertEqual(self.модели(self.список(имена[:n])), (имена[:n], True), n)
        граница = "m" * 80
        self.assertEqual(self.модели(self.список([граница])), ([граница], True), "80 знаков")
        self.assertEqual(self.модели(self.список([граница + "x"])), ([], False), "81 — отброшено")
        for ответ in ((False, b""), (True, b"not json"), (True, b'{"models": null}')):
            self.assertEqual(self.модели(ответ), ([], False), ответ)
        self.assertEqual(node_agent.МОДЕЛЕЙ_МАКС, contextd.МОДЕЛЕЙ_МАКС)

    def test_разбор_полноты_и_предела(self):
        for плохо in ({"models": ["m%02d" % i for i in range(contextd.МОДЕЛЕЙ_МАКС + 1)]},
                      {"models_complete": "да"}):
            with self.subTest(плохо=плохо):
                self.assertIsNone(contextd.разобрать_сердцебиение(плохо)[0])

    def test_испорченная_строка_не_роняет_такт(self):
        """Ревью Т7, P3-6: сохранённое сердцебиение разбирается заново, и
        испорченная строка — «сердцебиения нет», а не падение оборота."""
        self.отчитаться()
        row = self.con.execute("select capabilities_json from compute_nodes "
                               "where id='bigpc'").fetchone()
        d = json.loads(row[0])
        d["models"] = "не список"
        self.con.execute("update compute_nodes set capabilities_json=? where id='bigpc'",
                         (json.dumps(d),))
        self.assertIsNone(contextd.сердцебиение_узла(self.con, "bigpc"))
        self.assertEqual(contextd.недоступные(self.con), {"asr": None})


class Застава(_База):
    """Т7.4: застава игры — по сердцебиению узла, а не по расписанию."""

    def test_узлы_заставы(self):
        self.assertEqual(contextd.под_заставой(), {"gtr"})
        os.environ["MARA_GAME_GUARD"] = ""
        self.assertEqual(contextd.под_заставой(), set())
        os.environ["MARA_GAME_GUARD"] = " bigpc, gtr ,"
        self.assertEqual(contextd.под_заставой(), {"bigpc", "gtr"})
        os.environ["MARA_GAME_GUARD"] = "BigPC; GTR"
        self.assertEqual(contextd.под_заставой(), {"bigpc", "gtr"})
        os.environ["MARA_GAME_GUARD"] = "bigpс"            # «с» кириллицей
        contextd._незнакомые.discard("bigpс")
        вывод = io.StringIO()
        with contextlib.redirect_stdout(вывод):
            self.assertEqual(contextd.под_заставой(), {"bigpс"})
            contextd.под_заставой()
        self.assertEqual(вывод.getvalue().count("MARA_GAME_GUARD"), 1, "сказано один раз")

    def test_игра_держит(self):
        os.environ["MARA_GAME_GUARD"] = "bigpc"
        self.отчитаться(minecraft_active=True)
        self.assertEqual(contextd.недоступные(self.con),
                         {"asr": "node_gaming:bigpc"})
        self.отчитаться(minecraft_active=False)
        self.assertEqual(contextd.недоступные(self.con), {"asr": None})

    def test_без_сердцебиения_узла_нет(self):
        os.environ["MARA_GAME_GUARD"] = "bigpc"
        self.assertEqual(contextd.недоступные(self.con),
                         {"asr": "node_no_heartbeat:bigpc"})
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
        self.assertEqual(contextd.недоступные(self.con), {"asr": None})

    def test_gtr_тяжёлого_не_получает(self):
        """P11 на уровне кода: GTR здоров, не играет и принимает работу — и
        всё равно ничего тяжёлого: его нет в объявлении (нет GPU). Его игра
        тяжёлое на bigpc тоже не держит."""
        self.assertNotIn("gtr", {у for у, _ in contextd.ТЯЖЁЛЫЕ.values()})
        for игра in (False, True):
            self.отчитаться("gtr", minecraft_active=игра)
            self.assertEqual(contextd.недоступные(self.con), {"asr": None})
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
        тексты = {"node_gaming": "MARA_GAME_PROCESSES",
                  "node_no_heartbeat": "включить коробку",
                  "node_paused": "снять паузу на узле",
                  "node_no_capability": "поднять сервис на узле"}
        for причина, слово in тексты.items():
            jid = self.работа("asr", причина)
            когда = (datetime.now(mi.TZ) - timedelta(hours=30)).isoformat(timespec="seconds")
            self.con.execute("update jobs set queued_reason=?, created=?, next_at=0 "
                             "where id=?", (причина + ":bigpc", когда, jid))
        contextd.отметить_узел(self.con, contextd.ВОРКЕР, "core")
        находки = cr.узел_молчит(self.con, hours=24)
        self.assertEqual([н["check"] for н in находки], ["узел-не-берёт"] * 4)
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
