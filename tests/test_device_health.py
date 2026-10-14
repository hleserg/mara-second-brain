"""Т4.3, ТЗ §8.4 п.5: телефон присылает здоровье захвата — сервер видит
«звонок был, записи нет» (тревога `recording_missing_after_call` в
`alerts`, одна на звонок, закрытие с историей) и состояние §8.5
(`provider_health`), сверка говорит о них владельцу. Ключ — имя телефона:
перевыпуск токена ведёт те же строки."""
import contextlib, io, json, os, sys, tempfile, threading, time, unittest
import urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import mara_ingest as mi
import contextd
import contextd_reconcile as cr

БЕЗ_ПРОКСИ = {k: "" for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                               "all_proxy", "ALL_PROXY")}
# начало звонка, мс эпохи: вчерашний, чтобы тревога была в окне читателей
ЗВОНОК = (int(time.time()) - 24 * 3600) * 1000
ДРУГОЙ = ЗВОНОК + 3_600_000
ПРОВЕРЕНО = ЗВОНОК + 600_000


def отчёт(**kw):
    d = {"state": "healthy", "broken": False, "reason": "после перезагрузки звонок записался",
         "alert_call_ms": 0, "alerts_total": 0, "recorders": ["acr_phone_aph"],
         "checked_at_ms": ПРОВЕРЕНО, "sent_at_ms": ПРОВЕРЕНО + 60_000}
    d.update(kw)
    return d


class Разбор(unittest.TestCase):
    def test_закрытый_список(self):
        з, беда = contextd.разобрать_здоровье(отчёт(лишнее="x" * 10 ** 5,
                                                    recorders=["b", "a", "a"]))
        self.assertIsNone(беда)
        self.assertEqual(set(з), {"state", "broken", "reason", "alert_call_ms",
                                  "closed_call_ms", "checked_at_ms", "sent_at_ms",
                                  "closed_as", "alerts_total", "recorders"})
        self.assertEqual(з["recorders"], ["a", "b"])

    def test_кривые_поля_отказ(self):
        """Перечни и числа строги: 400 сверка считает с первого
        (`отказы-растут`), молча отчёт не пропадёт. Миллисекунды за 2100 год
        роняли обработчик раньше ответа (ревью, P2-2)."""
        for плохо in ({"state": "зелёный"}, {"reason": 5}, {"broken": "да"},
                      {"alert_call_ms": -1}, {"alert_call_ms": True}, {"alert_call_ms": "1"},
                      {"alert_call_ms": 253_402_300_799_000}, {"checked_at_ms": 10 ** 15},
                      {"sent_at_ms": 2 ** 53}, {"closed_call_ms": contextd.МС_МАКС + 1},
                      {"closed_as": "fixed"}, {"alerts_total": -1},
                      {"recorders": "acr"}, {"recorders": ["ACR Phone"]},
                      {"recorders": ["a"] * 9}):
            with self.subTest(плохо=плохо):
                з, беда = contextd.разобрать_здоровье(отчёт(**плохо))
                self.assertIsNone(з)
                self.assertTrue(беда)
        self.assertTrue(contextd.разобрать_здоровье({})[1], "state обязателен")
        з, беда = contextd.разобрать_здоровье(отчёт(alert_call_ms=contextd.МС_МАКС))
        self.assertIsNone(беда, "граница включена")
        contextd._из_мс(contextd.МС_МАКС)

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
        self.assertEqual(з, тело, "фикс проходит разбор без правок")

    def test_причина_санируется_а_не_отвергается(self):
        """Кривой знак в причине выключал бы отчёт целиком, а телефон ответа
        не читает (ревью, P3-6)."""
        з, беда = contextd.разобрать_здоровье(отчёт(
            reason="нет разрешений: READ_CALL_LOG\x07 за​пись" + "я" * 400))
        self.assertIsNone(беда)
        self.assertTrue(з["reason"].startswith("нет разрешений: READ_CALL_LOG запись"),
                        з["reason"])
        self.assertEqual(len(з["reason"]), 300)
        self.assertTrue(з["reason"].isprintable())

    def test_причина_без_номера_и_пути_карточки(self):
        """Шаблоны причин номеров не несут, но путь записи ACR несёт номер, и
        в Telegram он уехать не должен (ревью, P3-7)."""
        з, _ = contextd.разобрать_здоровье(отчёт(
            reason="папка Documents/ACR/ACRPhone/+79991234567 не читается; "
                   "8 (999) 123-45-67; kb/commitments/2026-10-14-смета.md; мало места: 512 МиБ"))
        self.assertNotIn("9991234567", з["reason"])
        self.assertNotIn("123-45-67", з["reason"])
        self.assertNotIn("смета", з["reason"])
        self.assertIn("<номер>", з["reason"])
        self.assertIn("512 МиБ", з["reason"], "короткие числа — не номер")


class _База(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.addCleanup(self.con.close)
        self.dev, _ = contextd.pair(self.con, "pura70")

    def прислать(self, dev=None, **kw):
        з, беда = contextd.разобрать_здоровье(отчёт(**kw))
        assert беда is None, беда
        return contextd.принять_здоровье(self.con, dev or self.dev, з)

    def тревоги(self):
        return [(r[0], r[1], r[2]) for r in self.con.execute(
            "select json_extract(detail_json, '$.call_ms'), state, "
            "json_extract(detail_json, '$.resolution') from alerts where kind=? "
            "order by opened, id", (mi.ЗАПИСИ_НЕТ,))]

    def переходы(self):
        return [r[0] for r in self.con.execute(
            "select json_extract(detail_json, '$.transition') from audit_events "
            "where action=? order by occurred, rowid", (mi.ЗАПИСИ_НЕТ,))]

    def метрики(self):
        return contextd.metrics(self.con, self.dir, None).splitlines()


class Тревога(_База):
    def test_одна_на_звонок_и_закрытие_с_историей(self):
        self.прислать(state="unhealthy", reason="звонок был, записи нет",
                      alert_call_ms=ЗВОНОК, alerts_total=1)
        self.прислать(state="unhealthy", reason="звонок был, записи нет",
                      alert_call_ms=ЗВОНОК, alerts_total=1)
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "open", None)], "повтор не множит (§8.4 п.4)")
        # запись появилась — телефон закрыл тревогу как восстановленную (п.7)
        self.прислать(alerts_total=1, closed_call_ms=ЗВОНОК, closed_as="recovered")
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "resolved", "recovered")])
        self.assertEqual(self.con.execute(
            "select count(*) from alerts where resolved is not null").fetchone()[0], 1)
        self.assertEqual(self.переходы(), ["opened", "recovered"])
        детали = json.loads(self.con.execute("select detail_json from alerts").fetchone()[0])
        self.assertEqual(детали["call_started"], contextd._из_мс(ЗВОНОК), "время для показа")
        self.assertEqual(детали["device_id"], self.dev)

    def test_новый_звонок_без_записи_закрывает_прежнюю(self):
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        # прежний ушёл из недельного журнала, следом — тревога по другому
        self.прислать(state="unhealthy", alert_call_ms=ДРУГОЙ, alerts_total=2,
                      closed_call_ms=ЗВОНОК, closed_as="expired")
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "resolved", "expired"),
                                          (ДРУГОЙ, "open", None)])
        # телефон не сказал, чем закрылась, — `closed`, а не догадка
        self.прислать(alerts_total=2)
        self.assertEqual(self.тревоги()[1][1:], ("resolved", "closed"))

    def test_исход_телефона_только_у_его_звонка(self):
        """`recovered` про другой звонок открытую тревогу восстановленной не
        делает (ревью, P3-10: мутант без сверки звонка проходил)."""
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        self.прислать(alerts_total=1, closed_call_ms=ДРУГОЙ, closed_as="recovered")
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "resolved", "closed")])

    def test_закрытая_и_названная_снова_переоткрывается(self):
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        self.прислать(alerts_total=1, closed_call_ms=ЗВОНОК, closed_as="recovered")
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "open", None)])
        # переоткрытие прошлого закрытия не стирает: счёт монотонный (P3-1)
        self.assertEqual(self.переходы(), ["opened", "recovered", "reopened"])
        строки = self.метрики()
        self.assertIn('mara_recording_missing_total{transition="recovered"} 1', строки)
        self.assertIn('mara_recording_missing_total{transition="reopened"} 1', строки)

    def test_принятая_остаётся_принятой(self):
        """`acked` разрешён схемой: названная снова не переоткрывается молча,
        пропавшая из отчёта закрывается, как открытая (ревью, P3-5)."""
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        self.con.execute("update alerts set state='acked'")
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "acked", None)])
        self.прислать(alerts_total=1, closed_call_ms=ЗВОНОК, closed_as="recovered")
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "resolved", "recovered")])

    def test_пояс_doctor_тревогу_не_переоткрывает(self):
        """Ключ — миллисекунды звонка, а не строка во времени doctor: смена
        `MARA_TZ_HOURS` закрывала тревогу и открывала её же (ревью, P3-2)."""
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        with mock.patch.object(mi, "TZ", timezone(timedelta(hours=-5))):
            self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "open", None)])
        self.assertEqual(self.переходы(), ["opened"])

    def test_старый_отчёт_после_нового_не_принимается(self):
        """Периодика и разовая сверка на телефоне идут в любом порядке: отчёт
        с более ранней проверкой не закрывает тревогу и не откатывает
        состояние (ревью, P3-3)."""
        self.assertTrue(self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1,
                                      checked_at_ms=ПРОВЕРЕНО + 60_000))
        self.assertFalse(self.прислать(state="healthy", checked_at_ms=ПРОВЕРЕНО))
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "open", None)])
        self.assertEqual(self.con.execute("select state from provider_health").fetchone()[0],
                         "unhealthy")
        # тот же вердикт ещё раз — принимается: отчёт повторяется каждой сверкой
        self.assertTrue(self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1,
                                      checked_at_ms=ПРОВЕРЕНО + 60_000))
        # час спустя это уже не гонка, а часы телефона, убежавшие вперёд:
        # отчёты не запираются навсегда
        давно = (datetime.now(mi.TZ) - timedelta(seconds=contextd.ПОРЯДОК_С + 60)).isoformat(
            timespec="seconds")
        self.con.execute("update provider_health set detail_json="
                         "json_set(detail_json, '$.received', ?)", (давно,))
        self.assertTrue(self.прислать(state="healthy", checked_at_ms=ПРОВЕРЕНО))
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "resolved", "closed")])

    def test_состояние_и_since(self):
        self.прислать(state="unknown", reason="после перезагрузки звонка ещё не было")
        row = self.con.execute("select state from provider_health where provider=?",
                               ("phone:pura70",)).fetchone()
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

    def test_проверка_на_часах_doctor(self):
        """Возраст вердикта — разница отправки и проверки на часах телефона:
        застывшая проверка свежим отчётом не притворяется, а сдвиг часов
        телефона сокращается (ревью, P2-4)."""
        сдвиг = 10 ** 12          # часы телефона убежали на десятилетия — неважно
        self.прислать(state="healthy", checked_at_ms=сдвиг, sent_at_ms=сдвиг + 10 * 3600_000)
        возраст = cr._возраст(self.con.execute(
            "select checked from provider_health").fetchone()[0])
        self.assertAlmostEqual(возраст, 10 * 3600, delta=120)
        строки = self.метрики()
        age = [l for l in строки if l.startswith('mara_capture_check_age_seconds{name="pura70"} ')]
        self.assertEqual(len(age), 1)
        self.assertAlmostEqual(int(age[0].split()[-1]), 10 * 3600, delta=120)

    def test_метрика(self):
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        строки = self.метрики()
        self.assertIn("mara_recording_missing_open 1", строки)
        self.assertIn('mara_capture_state{name="pura70",state="unhealthy"} 1', строки)
        self.assertIn('mara_capture_state{name="pura70",state="healthy"} 0', строки)
        self.assertIn('mara_capture_phone_alerts_total{name="pura70"} 1', строки)
        self.прислать(closed_call_ms=ЗВОНОК, closed_as="recovered", alerts_total=1)
        строки = self.метрики()
        self.assertIn("mara_recording_missing_open 0", строки)
        self.assertIn('mara_capture_state{name="pura70",state="healthy"} 1', строки)
        # §15.1 «alert transitions»: заведено и чем закрылось — из аудита, с нулями
        self.assertIn('mara_recording_missing_total{transition="opened"} 1', строки)
        self.assertIn('mara_recording_missing_total{transition="recovered"} 1', строки)
        self.assertIn('mara_recording_missing_total{transition="expired"} 0', строки)
        # отозванный телефон из серий уходит
        self.con.execute("update devices set revoked_at=? where id=?", (mi.now_iso(), self.dev))
        self.assertFalse([l for l in self.метрики() if l.startswith("mara_capture_")])

    def test_открытые_считаются_только_у_живых_телефонов(self):
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        self.con.execute("update devices set revoked_at=? where id=?", (mi.now_iso(), self.dev))
        self.assertIn("mara_recording_missing_open 0", self.метрики())

    def test_тревога_старше_окна_не_висит(self):
        """Замолчавший телефон свою тревогу не закроет: читатели старше окна
        не судят, и счёт не висит на ней вечно."""
        давно = ЗВОНОК - mi.ЗАПИСИ_НЕТ_ОКНО_С * 1000
        self.прислать(state="unhealthy", alert_call_ms=давно, alerts_total=1)
        self.assertIn("mara_recording_missing_open 0", self.метрики())
        self.assertEqual(cr.захват_телефона(self.con), [])


class Перевыпуск(_База):
    """Ревью, P2-1: `--pair` того же имени — штатный перевыпуск токена (#47),
    без него не обойтись при переустановке. Строки ведутся по имени."""

    def test_новый_токен_ведёт_те_же_строки(self):
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        новый, _ = contextd.pair(self.con, "pura70")
        self.прислать(dev=новый, state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1)
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "open", None)], "не двоится")
        self.assertEqual(self.con.execute("select count(*) from provider_health")
                         .fetchone()[0], 1)
        # старый отозван — тревогу ведёт новый, она остаётся
        contextd.revoke(self.con, self.dev)
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "open", None)])
        self.assertEqual([x["check"] for x in cr.захват_телефона(self.con)],
                         ["запись-не-появилась"], "одна строка, не две")
        self.прислать(dev=новый, alerts_total=1, closed_call_ms=ЗВОНОК, closed_as="recovered")
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "resolved", "recovered")])

    def test_отзыв_последнего_токена_закрывает_тревоги(self):
        self.прислать(state="unhealthy", broken=True, alert_call_ms=ЗВОНОК, alerts_total=1)
        contextd.revoke(self.con, self.dev)
        self.assertEqual(self.тревоги(), [(ЗВОНОК, "resolved", "device_revoked")])
        self.assertEqual(self.переходы(), ["opened", "device_revoked"])
        self.assertEqual(cr.захват_телефона(self.con), [])
        строки = self.метрики()
        self.assertIn("mara_recording_missing_open 0", строки)
        self.assertIn('mara_recording_missing_total{transition="device_revoked"} 1', строки)
        актор = self.con.execute("select actor_type from audit_events where action=? "
                                 "order by rowid desc limit 1", (mi.ЗАПИСИ_НЕТ,)).fetchone()[0]
        self.assertEqual(актор, "human")


class Сверка(_База):
    def test_тревога_это_ошибка_с_именем_временем_и_советом(self):
        self.прислать(state="unhealthy", reason="звонок был, записи нет",
                      alert_call_ms=ЗВОНОК, alerts_total=1)
        н = cr.захват_телефона(self.con)
        self.assertEqual([(x["check"], x["level"], x["device"]) for x in н],
                         [("запись-не-появилась", "error", "pura70")])
        self.assertIn(cr._когда(contextd._из_мс(ЗВОНОК)), н[0]["detail"])
        self.assertIn("ACR", н[0]["detail"], "совет по рекордеру из отчёта")
        self.assertIn("запись-не-появилась", [x["check"] for x in cr.run(self.con, self.dir, vault=None)])
        # у другого рекордера про ACR не говорим (ревью, P3-8)
        self.прислать(state="unhealthy", alert_call_ms=ЗВОНОК, alerts_total=1,
                      recorders=["cube_acr"], checked_at_ms=ПРОВЕРЕНО + 1)
        н = cr.захват_телефона(self.con)
        self.assertNotIn("ACR", н[0]["detail"])
        self.assertIn(cr.СОВЕТ_ОБЩИЙ, н[0]["detail"])

    def test_сломано_и_тревога_разом(self):
        """Тревога по старому звонку не прячет сломанный захват, и совет
        «позвони себе» тогда неверен (ревью, P2-3)."""
        self.прислать(state="unhealthy", broken=True, reason="нет разрешений: READ_CALL_LOG",
                      alert_call_ms=ЗВОНОК, alerts_total=1)
        н = cr.захват_телефона(self.con)
        self.assertEqual([(x["check"], x["level"]) for x in н],
                         [("запись-не-появилась", "error"), ("захват-сломан", "warn")])
        self.assertIn("почини", н[0]["detail"])
        self.assertNotIn("позвони себе", н[0]["detail"])
        self.assertIn("READ_CALL_LOG", н[1]["detail"])

    def test_сломано_без_тревоги_и_норма(self):
        self.прислать(state="unhealthy", broken=True, reason="нет разрешений: READ_CALL_LOG")
        н = cr.захват_телефона(self.con)
        self.assertEqual([(x["check"], x["level"]) for x in н], [("захват-сломан", "warn")])
        self.assertIn("READ_CALL_LOG", н[0]["detail"])
        for норма in ("degraded", "unknown", "recovering", "healthy"):
            self.прислать(state=норма)
            self.assertEqual(cr.захват_телефона(self.con), [], норма)

    def test_старый_отчёт_не_судится(self):
        self.прислать(state="unhealthy", broken=True, alert_call_ms=ЗВОНОК, alerts_total=1)
        давно = (datetime.now(mi.TZ) - timedelta(hours=cr.ЗДОРОВЬЕ_СВЕЖЕЕ_Ч + 1)).isoformat(
            timespec="seconds")
        self.con.execute("update provider_health set detail_json="
                         "json_set(detail_json, '$.received', ?)", (давно,))
        self.assertEqual(cr.захват_телефона(self.con), [],
                         "молчащий телефон — находка телефон-молчит, не эта")

    def test_проверка_стоит_при_свежем_отчёте(self):
        """Отчёт свежий, а вердикт в нём давний — проверка захвата на
        телефоне не запускается (ревью, P2-4)."""
        self.прислать(state="healthy", checked_at_ms=ПРОВЕРЕНО,
                      sent_at_ms=ПРОВЕРЕНО + (cr.ПРОВЕРКА_ЗАХВАТА_Ч + 1) * 3600_000)
        н = cr.захват_телефона(self.con)
        self.assertEqual([(x["check"], x["level"]) for x in н],
                         [("проверка-захвата-стоит", "warn")])
        self.прислать(state="healthy", checked_at_ms=ПРОВЕРЕНО + 1, sent_at_ms=ПРОВЕРЕНО + 2)
        self.assertEqual(cr.захват_телефона(self.con), [])


class Сеть(unittest.TestCase):
    """Через настоящий `contextd`: путь принимает токен телефона, но не
    узла и не токен без `call` в allowlist, и кривое тело — 400, а не
    оборванное соединение."""

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
        cls.плагин, cls.токен_плагина = contextd.pair(cls.con, "mara-hermes")
        contextd.set_scopes(cls.con, cls.плагин, ["correction"])

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

    def test_телефон_да_узел_и_плагин_нет_кривое_400(self):
        self.assertEqual(self.запрос(self.токен, отчёт(state="unhealthy",
                                                       alert_call_ms=ЗВОНОК)), 200)
        self.assertEqual(self.con.execute("select count(*) from alerts where kind=? and "
                                          "state='open'", (mi.ЗАПИСИ_НЕТ,)).fetchone()[0], 1)
        self.assertEqual(self.запрос(self.токен_узла, отчёт()), 403)
        # allowlist без `call` тревог не заводит (ревью, P2-5)
        self.assertEqual(self.запрос(self.токен_плагина, отчёт(state="unhealthy",
                                                               alert_call_ms=ДРУГОЙ)), 403)
        self.assertEqual(self.con.execute("select count(*) from alerts").fetchone()[0], 1)
        self.assertEqual(self.запрос(self.токен, отчёт(state="зелёный")), 400)
        self.assertEqual(self.запрос("not-a-token", отчёт()), 401)

    def test_огромные_миллисекунды_400_а_не_обрыв(self):
        """Год 10000 ронял обработчик раньше ответа: соединение рвалось, а
        `mara_http_crashes_total` рос (ревью, P2-2)."""
        было = contextd._упало["http"]
        for k in ("alert_call_ms", "closed_call_ms", "checked_at_ms", "sent_at_ms"):
            with self.subTest(поле=k):
                self.assertEqual(self.запрос(self.токен, отчёт(**{k: 253_402_300_799_000})), 400)
        self.assertEqual(contextd._упало["http"], было, "обработчик не падал")


if __name__ == "__main__":
    unittest.main()
