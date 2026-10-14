"""Т4.3, ТЗ §8.4 п.5: телефон присылает здоровье захвата — сервер видит
«звонок был, записи нет» (тревога `recording_missing_after_call` в
`alerts`, одна на звонок, закрытие с историей) и состояние §8.5
(`provider_health`), сверка говорит о них владельцу."""
import contextlib, io, json, os, sys, tempfile, threading, unittest
import urllib.error, urllib.request
from datetime import datetime, timedelta
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import mara_ingest as mi
import contextd
import contextd_reconcile as cr

БЕЗ_ПРОКСИ = {k: "" for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                               "all_proxy", "ALL_PROXY")}
ЗВОНОК = 1_760_000_000_000          # начало звонка, мс эпохи
ДРУГОЙ = ЗВОНОК + 3_600_000


def отчёт(**kw):
    d = {"state": "healthy", "reason": "после перезагрузки звонок записался",
         "alert_call_ms": 0, "alerts_total": 0, "recorders": ["acr_phone_aph"],
         "checked_at_ms": ЗВОНОК + 600_000}
    d.update(kw)
    return d


class Разбор(unittest.TestCase):
    def test_закрытый_список(self):
        з, беда = contextd.разобрать_здоровье(отчёт(лишнее="x" * 10 ** 5,
                                                    recorders=["b", "a", "a"]))
        self.assertIsNone(беда)
        self.assertEqual(set(з), {"state", "reason", "alert_call_ms", "closed_call_ms",
                                  "checked_at_ms", "closed_as", "alerts_total", "recorders"})
        self.assertEqual(з["recorders"], ["a", "b"])

    def test_кривые_поля_отказ(self):
        for плохо in ({"state": "зелёный"}, {"reason": "x" * 301}, {"reason": "а\nб"},
                      {"alert_call_ms": -1}, {"alert_call_ms": True}, {"alert_call_ms": "1"},
                      {"closed_as": "fixed"}, {"alerts_total": -1},
                      {"recorders": "acr"}, {"recorders": ["ACR Phone"]},
                      {"recorders": ["a"] * 9}):
            with self.subTest(плохо=плохо):
                з, беда = contextd.разобрать_здоровье(отчёт(**плохо))
                self.assertIsNone(з)
                self.assertTrue(беда)
        self.assertTrue(contextd.разобрать_здоровье({})[1], "state обязателен")

    def test_контракт_с_телефоном(self):
        """Тело отчёта закреплено `tests/fixtures/device-health.json`, и с ним
        сверяются обе стороны — этот разбор и `HealthJson.build` телефона
        (`CoreTest.kt`): поле, которое телефон шлёт, а сервер выбрасывает,
        разошлось бы молча."""
        путь = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                            "device-health.json")
        with open(путь, encoding="utf-8") as fh:
            тело = json.load(fh)
        з, беда = contextd.разобрать_здоровье(тело)
        self.assertIsNone(беда)
        self.assertEqual(set(з), set(тело), "закрытый список совпадает с контрактом")
        self.assertEqual(з["alert_call_ms"], тело["alert_call_ms"])

    def test_причина_обезличена(self):
        з, _ = contextd.разобрать_здоровье(отчёт(reason="kb/commitments/2026-10-14-смета.md"))
        self.assertNotIn("смета", з["reason"])


class _База(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.addCleanup(self.con.close)
        self.dev, _ = contextd.pair(self.con, "pura70")

    def прислать(self, **kw):
        з, беда = contextd.разобрать_здоровье(отчёт(**kw))
        assert беда is None, беда
        contextd.принять_здоровье(self.con, self.dev, з)

    def тревоги(self):
        return [(r[0], r[1], r[2]) for r in self.con.execute(
            "select json_extract(detail_json, '$.call_started'), state, "
            "json_extract(detail_json, '$.resolution') from alerts where kind=? "
            "order by opened, id", (mi.ЗАПИСИ_НЕТ,))]


class Тревога(_База):
    def test_одна_на_звонок_и_закрытие_с_историей(self):
        self.прислать(state="unhealthy", reason="звонок был, записи нет",
                      alert_call_ms=ЗВОНОК, alerts_total=1)
        self.прислать(state="unhealthy", reason="звонок был, записи нет",
                      alert_call_ms=ЗВОНОК, alerts_total=1)
        звонок = contextd._из_мс(ЗВОНОК)
        self.assertEqual(self.тревоги(), [(звонок, "open", None)], "повтор не множит (§8.4 п.4)")
        # запись появилась — телефон закрыл тревогу как восстановленную (п.7)
        self.прислать(alerts_total=1, closed_call_ms=ЗВОНОК, closed_as="recovered")
        self.assertEqual(self.тревоги(), [(звонок, "resolved", "recovered")])
        self.assertEqual(self.con.execute(
            "select count(*) from alerts where resolved is not null").fetchone()[0], 1)

    def test_новый_звонок_без_записи_закрывает_прежнюю(self):
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        # прежний ушёл из недельного журнала, следом — тревога по другому
        self.прислать(state="unhealthy", alert_call_ms=ДРУГОЙ, alerts_total=2,
                      closed_call_ms=ЗВОНОК, closed_as="expired")
        self.assertEqual(self.тревоги(), [(contextd._из_мс(ЗВОНОК), "resolved", "expired"),
                                          (contextd._из_мс(ДРУГОЙ), "open", None)])
        # телефон не сказал, чем закрылась, — `closed`, а не догадка
        self.прислать(alerts_total=2)
        self.assertEqual(self.тревоги()[1][1:], ("resolved", "closed"))

    def test_закрытая_и_названная_снова_переоткрывается(self):
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        self.прислать(alerts_total=1)
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        self.assertEqual(self.тревоги(), [(contextd._из_мс(ЗВОНОК), "open", None)])

    def test_состояние_и_since(self):
        self.прислать(state="unknown", reason="после перезагрузки звонка ещё не было")
        row = self.con.execute("select state, since, detail_json from provider_health "
                               "where provider=?", ("phone:" + self.dev,)).fetchone()
        self.assertEqual(row["state"], "unknown")
        self.con.execute("update provider_health set since='2026-01-01T00:00:00+03:00'")
        self.прислать(state="unknown", reason="то же")
        self.assertEqual(self.con.execute("select since from provider_health").fetchone()[0],
                         "2026-01-01T00:00:00+03:00", "то же состояние — since прежний")
        self.прислать(state="degraded", reason="запись идёт, но ACR Phone + APH — reboot-unsafe")
        self.assertNotEqual(self.con.execute("select since from provider_health").fetchone()[0],
                            "2026-01-01T00:00:00+03:00")
        детали = json.loads(self.con.execute("select detail_json from provider_health")
                            .fetchone()[0])
        self.assertEqual(детали["recorders"], ["acr_phone_aph"])
        self.assertEqual(детали["device_id"], self.dev)

    def test_метрика(self):
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        строки = contextd.metrics(self.con, self.dir, None).splitlines()
        self.assertIn("mara_recording_missing_open 1", строки)
        self.assertIn('mara_capture_state{device="pura70",state="unhealthy"} 1', строки)
        self.assertIn('mara_capture_state{device="pura70",state="healthy"} 0', строки)
        self.assertTrue(any(l.startswith('mara_capture_report_age_seconds{device="pura70"} ')
                            for l in строки))
        self.прислать(closed_call_ms=ЗВОНОК, closed_as="recovered", alerts_total=1)
        строки = contextd.metrics(self.con, self.dir, None).splitlines()
        self.assertIn("mara_recording_missing_open 0", строки)
        self.assertIn('mara_capture_state{device="pura70",state="healthy"} 1', строки)
        # §15.1 «alert transitions»: заведено и чем закрылось — из таблицы
        self.assertIn('mara_recording_missing_total{transition="opened"} 1', строки)
        self.assertIn('mara_recording_missing_total{transition="recovered"} 1', строки)
        self.assertIn('mara_recording_missing_total{transition="expired"} 0', строки)
        # отозванный телефон из серий уходит
        self.con.execute("update devices set revoked_at=? where id=?", (mi.now_iso(), self.dev))
        self.assertFalse([l for l in contextd.metrics(self.con, self.dir, None).splitlines()
                          if l.startswith("mara_capture_state")])


class Сверка(_База):
    def test_тревога_это_ошибка_с_именем_и_временем(self):
        self.прислать(state="unhealthy", reason="звонок был, записи нет",
                      alert_call_ms=ЗВОНОК, alerts_total=1)
        н = cr.захват_телефона(self.con)
        self.assertEqual([(x["check"], x["level"], x["device"]) for x in н],
                         [("запись-не-появилась", "error", "pura70")])
        self.assertIn(cr._когда(contextd._из_мс(ЗВОНОК)), н[0]["detail"])
        self.assertNotIn("+7", н[0]["detail"])
        self.assertIn("запись-не-появилась", [x["check"] for x in cr.run(self.con, self.dir, vault=None)])

    def test_сломано_без_тревоги_и_норма(self):
        self.прислать(state="unhealthy", reason="нет разрешений: READ_CALL_LOG")
        н = cr.захват_телефона(self.con)
        self.assertEqual([(x["check"], x["level"]) for x in н], [("захват-сломан", "warn")])
        self.assertIn("READ_CALL_LOG", н[0]["detail"])
        for норма in ("degraded", "unknown", "recovering", "healthy"):
            self.прислать(state=норма)
            self.assertEqual(cr.захват_телефона(self.con), [], норма)

    def test_старый_отчёт_не_судится(self):
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        давно = (datetime.now(mi.TZ) - timedelta(hours=cr.ЗДОРОВЬЕ_СВЕЖЕЕ_Ч + 1)).isoformat(
            timespec="seconds")
        self.con.execute("update provider_health set checked=?", (давно,))
        self.assertEqual(cr.захват_телефона(self.con), [],
                         "молчащий телефон — находка телефон-молчит, не эта")


class Сеть(unittest.TestCase):
    """Через настоящий `contextd`: путь принимает токен телефона, но не
    узла, и кривое тело — 400."""

    @classmethod
    def setUpClass(cls):
        cls.окружение = mock.patch.dict(os.environ, БЕЗ_ПРОКСИ)
        cls.окружение.start()
        cls.dir = tempfile.mkdtemp()
        mi.ROOT = cls.dir
        cls.srv = contextd.make_server(cls.dir, port=0, vault=tempfile.mkdtemp())
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.con = mi.connect(cls.dir)
        cls.телефон, cls.токен = contextd.pair(cls.con, "pura70")
        cls.узел, cls.токен_узла = contextd.pair_node(cls.con, "bigpc")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.con.close()
        cls.окружение.stop()

    def запрос(self, токен, тело):
        req = urllib.request.Request(self.url + contextd.ПУТЬ_ЗДОРОВЬЯ, method="POST",
                                     data=json.dumps(тело).encode())
        req.add_header("Authorization", "Bearer " + токен)
        req.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with contextlib.redirect_stdout(io.StringIO()), opener.open(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_телефон_да_узел_нет_кривое_400(self):
        self.assertEqual(self.запрос(self.токен, отчёт(state="unhealthy",
                                                       alert_call_ms=ЗВОНОК)), 200)
        self.assertEqual(self.con.execute("select count(*) from alerts where kind=? and "
                                          "state='open'", (mi.ЗАПИСИ_НЕТ,)).fetchone()[0], 1)
        self.assertEqual(self.запрос(self.токен_узла, отчёт()), 403)
        self.assertEqual(self.запрос(self.токен, отчёт(state="зелёный")), 400)
        self.assertEqual(self.запрос("not-a-token", отчёт()), 401)


if __name__ == "__main__":
    unittest.main()
