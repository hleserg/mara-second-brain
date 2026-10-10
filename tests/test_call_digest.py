"""Формат дайджеста (ТЗ §16). Рендер без модели и без сети."""
import contextlib, io, os, sys, json, tempfile, subprocess, unittest, datetime, sqlite3
from unittest import mock

СКРИПТЫ = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "scripts")
sys.path.insert(0, СКРИПТЫ)
import call_digest as cd
import mara_ingest as mi

EVENT = {"id": "call_1", "occurred": "2026-09-02T14:05:00+03:00",
         "ended": "2026-09-02T14:23:11+03:00",
         "payload": {"contact_name": "Анна"}}
ПУСТО = {"requests": [], "commitments": [], "decisions": [], "open_questions": [],
         "changed_instructions": [], "constraints": [], "followups": []}


def с(**kw):
    d = dict(ПУСТО)
    d.update(kw)
    return d


class Рендер(unittest.TestCase):
    def test_заголовок_с_контактом_и_временем(self):
        text, _ = cd.render(EVENT, ПУСТО, 0)
        self.assertTrue(text.startswith("Звонок · Анна · 14:05–14:23"), text[:60])

    def test_недозвон_и_пропущенный_в_заголовке_и_строкой_исхода(self):
        """Т4.3: недозвон не выглядит в телеграме как состоявшийся звонок."""
        ev = dict(EVENT, payload={"contact_name": "Анна", "direction": "outgoing",
                                  "duration_s": 0})
        text, _ = cd.render(ev, ПУСТО, 0)
        self.assertTrue(text.startswith("Недозвон · Анна · 14:05–14:23\nИсход: не дозвонился"),
                        text)
        ev = dict(EVENT, payload={"contact_name": "Анна", "direction": "missed", "duration_s": 0})
        text, _ = cd.render(ev, ПУСТО, 0)
        self.assertTrue(text.startswith("Пропущенный звонок · Анна"), text)
        # состоявшийся — как раньше, без строки исхода
        ev = dict(EVENT, payload={"contact_name": "Анна", "direction": "incoming",
                                  "duration_s": 1091})
        text, _ = cd.render(ev, ПУСТО, 0)
        self.assertTrue(text.startswith("Звонок · Анна · 14:05–14:23"), text)
        self.assertNotIn("Исход:", text)

    def test_пустые_разделы_не_печатаются(self):
        text, _ = cd.render(EVENT, ПУСТО, 0)
        self.assertNotIn("Попросили", text)
        self.assertNotIn("Ты обещал", text)

    def test_просьба_попадает_в_свой_раздел(self):
        e = с(requests=[{"action": "прислать смету", "disposition": "task",
                         "due_at": "2026-09-04",
                         "evidence": [{"start_ms": 252000, "end_ms": 260000}]}])
        text, items = cd.render(EVENT, e, 1)
        self.assertIn("Попросили", text)
        self.assertIn("прислать смету", text)
        self.assertIn("04:12", text, "у пункта есть метка времени для цитаты")
        self.assertEqual(items[0]["disposition"], "task")

    def test_возможная_задача_отдельным_разделом(self):
        e = с(requests=[{"action": "покрасить стены", "disposition": "needs-review",
                         "evidence": [{"start_ms": 60000, "end_ms": 61000}]}])
        text, items = cd.render(EVENT, e, 0)
        self.assertIn("Возможно задача", text)
        self.assertNotIn("Попросили", text, "непрошедшее порог идёт только в «возможно»")
        self.assertEqual(items[0]["disposition"], "needs-review")

    def test_созданные_задачи_считаются(self):
        text, _ = cd.render(EVENT, ПУСТО, 2)
        self.assertIn("Создано", text)
        self.assertIn("2 задачи", text)

    def test_одна_задача_склоняется(self):
        text, _ = cd.render(EVENT, ПУСТО, 1)
        self.assertIn("1 задача", text)

    def test_пять_задач_склоняются(self):
        text, _ = cd.render(EVENT, ПУСТО, 5)
        self.assertIn("5 задач", text)

    def test_изменение_показывает_что_на_что(self):
        e = с(changed_instructions=[{"action": "нужен договор",
                                     "supersedes": "прислать счёт",
                                     "new_state": "нужен договор",
                                     "disposition": "task",
                                     "evidence": [{"start_ms": 0, "end_ms": 1}]}])
        text, _ = cd.render(EVENT, e, 0)
        self.assertIn("Изменилось", text)
        self.assertIn("прислать счёт", text)
        self.assertIn("нужен договор", text)

    def test_срок_виден_в_строке(self):
        e = с(commitments=[{"action": "перезвонить", "disposition": "task",
                            "due_at": "2026-09-07",
                            "evidence": [{"start_ms": 0, "end_ms": 1}]}])
        text, _ = cd.render(EVENT, e, 1)
        self.assertIn("2026-09-07", text)

    def test_дайджест_не_содержит_расшифровки(self):
        e = с(requests=[{"action": "прислать смету", "disposition": "task",
                         "quote": "тут была бы вся расшифровка целиком",
                         "evidence": [{"start_ms": 0, "end_ms": 1}]}])
        text, _ = cd.render(EVENT, e, 1)
        self.assertNotIn("расшифровка целиком", text,
                         "в телеграм уходит выжимка, а не транскрипт (ТЗ §16)")


class Транспорт(unittest.TestCase):
    def test_без_токена_текст_не_теряется(self):
        state = cd.deliver("текст", token=None, chat_id=None)
        self.assertEqual(state, "no-transport",
                         "нет токена — дайджест остаётся в базе, а не пропадает")


class Адресат(unittest.TestCase):
    """§8.3 пускает дайджест наружу без редакции ровно потому, что читатель у
    него один — владелец. Пока адресат не проверялся, это условие держалось
    словом: канал обзаводится подписчиками без единой правки кода (#61)."""

    def отправка_запрещена(self):
        return mock.patch("urllib.request.urlopen",
                          side_effect=AssertionError("дайджест ушёл в сеть"))

    def test_канал_группа_и_имя_дайджеста_не_получают(self):
        # `-100…` — канал или супергруппа, просто отрицательный — группа,
        # `@имя` не различает их вовсе, поэтому отвергается вместе с мусором.
        for чужой in ("-1001234567890", "-987654321", "@канал",
                      "не число", "0"):
            with self.subTest(chat_id=чужой):
                буфер = io.StringIO()
                with self.отправка_запрещена(), \
                        contextlib.redirect_stderr(буфер):
                    состояние = cd.deliver("текст", "t", чужой)
                self.assertEqual(состояние, "not-private",
                                 "%s принят за личный чат владельца" % чужой)
                self.assertIn(чужой, буфер.getvalue(),
                              "отказ молчит: адресата в stderr нет")

    def test_личный_чат_дайджест_получает(self):
        """Половина заставы, без которой она была бы «не отправлять
        никогда»."""
        with mock.patch("urllib.request.urlopen") as у:
            у.return_value.__enter__.return_value.read.return_value = (
                b'{"ok":true}')
            self.assertEqual(cd.deliver("текст", "t", "123456789"), "sent")


class ИмяEnvФайла(unittest.TestCase):
    """MARA_ENV_FILE сюда не относится, и это надо держать проверенным.

    Раньше имя было одно на два разных файла: `~/.config/mara/env` (адреса и
    ключ OpenRouter, владелец — логин юнита) и `/etc/mara/contextd.env` (токен
    телеграма, читается ещё и systemd через EnvironmentFile=). Увести в сторону
    один значило увести оба, а увести только нужный было нельзя вовсе.

    Читаем в подпроцессе, потому что константа берётся на импорте модуля: в уже
    импортированном подмена переменной ничего не изменит.
    """

    def имя(self, **env):
        # run-tests.sh выставляет MARA_CONTEXTD_ENV на весь прогон; убираем её,
        # иначе «переменная не задана» проверить нечем.
        e = dict(os.environ)
        e.pop("MARA_CONTEXTD_ENV", None)
        e.pop("MARA_ENV_FILE", None)
        e.update(env)
        код = "import call_digest; print(call_digest.ENV_FILE)"
        r = subprocess.run([sys.executable, "-c", код], env=e, text=True,
                           capture_output=True, cwd=СКРИПТЫ)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_своя_переменная_уводит(self):
        self.assertEqual(self.имя(MARA_CONTEXTD_ENV="/нет/свой"), "/нет/свой")

    def test_чужая_переменная_не_уводит(self):
        self.assertEqual(self.имя(MARA_ENV_FILE="/нет/чужой"),
                         "/etc/mara/contextd.env")


class _СтендДоставки(unittest.TestCase):
    """Событие, извлечение и чистое окружение; тестов не несёт."""

    def setUp(self):
        # env() читает окружение раньше env-файла: если у разработчика
        # экспортирован живой токен, тест уйдёт в настоящий Bot API
        снято = mock.patch.dict(os.environ, clear=False)
        снято.start()
        self.addCleanup(снято.stop)
        for k in cd.КЛЮЧИ:
            os.environ.pop(k, None)
        # Событие класса — от 2026-09-02, то есть к любому реальному «сейчас»
        # оно старое. Без этой поблажки все тесты доставки уехали бы в «stale»
        # и перестали бы проверять доставку: застава отключила бы своих же
        # свидетелей. Свежесть проверяется отдельно, ниже.
        свежесть = mock.patch.object(cd, "СВЕЖЕСТЬ_Ч", 10 ** 6)
        свежесть.start()
        self.addCleanup(свежесть.stop)
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.eid, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "d1",
            "occurred_at": EVENT["occurred"], "ended_at": EVENT["ended"],
            "payload": EVENT["payload"]})
        self.con.execute("update events set state='projected' where id=?", (self.eid,))
        mi.write_json(mi.extraction_path(self.dir, self.eid), ПУСТО)

    def состояние(self):
        return self.con.execute("select state from events where id=?",
                                (self.eid,)).fetchone()["state"]


class Доставка(_СтендДоставки):
    """N11: недоставленный дайджест не считается обработанным звонком."""

    def test_без_транспорта_событие_не_закрывается(self):
        пусто = os.path.join(self.dir, "нет-такого.env")
        cd.run(self.eid, root=self.dir, env_file=пусто)
        row = self.con.execute("select state from digests where event_id=?",
                               (self.eid,)).fetchone()
        self.assertEqual(row["state"], "no-transport", "текст дайджеста сохранён")
        self.assertEqual(self.состояние(), "projected",
                         "владелец дайджеста не видел — звонок не обработан")

    def test_дайджест_по_ревизии_из_реестра(self):
        """Т5.0 (миграция 6): извлечение читается из реестра, как у проектора;
        файла нет, строка есть — дайджест есть; ревизия новее файла —
        дайджест по ревизии, а не по файлу."""
        import call_extract as ce
        xid = mi.uuid7()
        ревизия = dict(ПУСТО, event_id=self.eid, extraction_id=xid,
                       requests=[{"action": "прислать смету из реестра", "explicit": True,
                                  "confidence": 0.95, "due_at": None,
                                  "deadline_explicit": False, "deadline_phrase": "",
                                  "disposition": "task", "evidence": []}])
        ce.записать_ревизию(self.con, xid, ревизия)
        self.con.commit()
        os.remove(mi.extraction_path(self.dir, self.eid))
        пусто = os.path.join(self.dir, "нет-такого.env")
        cd.run(self.eid, root=self.dir, env_file=пусто)
        row = self.con.execute("select text from digests where event_id=?",
                               (self.eid,)).fetchone()
        self.assertIn("прислать смету из реестра", row["text"])

    def test_чужой_адресат_событие_не_закрывает(self):
        """Застава живёт в `deliver`, а закрывает событие `run` — и знать про
        отказ обязан именно он. `Адресат` проверяет заставу, `Доставка` без
        транспорта — только `no-transport`, и между ними оставалась щель:
        сужение `state != "sent"` до `state == "no-transport"` проходило весь
        гейт, объявляя звонок обработанным, а владелец дайджеста не видел."""
        env = os.path.join(self.dir, "чужой.env")
        with open(env, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=t\n"
                     "TELEGRAM_HOME_CHANNEL=-1001234567890\n")
        # `deliver` настоящий: до сети он не доходит — отказ раньше `urlopen`
        cd.run(self.eid, root=self.dir, env_file=env)
        row = self.con.execute("select state from digests where event_id=?",
                               (self.eid,)).fetchone()
        self.assertEqual(row["state"], "not-private",
                         "текст дайджеста сохранён")
        self.assertEqual(self.состояние(), "projected",
                         "владелец дайджеста не видел — звонок не обработан")

    def test_доставленный_дайджест_закрывает_событие(self):
        env = os.path.join(self.dir, "есть.env")
        with open(env, "w", encoding="utf-8") as fh:
            # адресат правдоподобный: `@c` здесь держался только заглушкой
            # `deliver` и моделировал ровно то, что §8.3 запрещает
            fh.write("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_HOME_CHANNEL=123456789\n")
        было = cd.deliver
        cd.deliver = lambda text, token, chat: "sent"
        try:
            cd.run(self.eid, root=self.dir, env_file=env)
        finally:
            cd.deliver = было
        self.assertEqual(self.состояние(), "done")

    def test_сбой_отправки_роняет_шаг(self):
        """`failed` — сбой сети, а не настройка: работа обязана уйти в ретрай,
        а встанет насовсем — скажет `dlq()`. Держится это одним `raise`, и без
        него шаг выходил нулём: звонок оставался `projected` навсегда, ретрая
        не было, а сверка про `failed` молчит намеренно (N11 — про настройку).
        Мутант «убрать `raise`» проходил весь гейт."""
        env = os.path.join(self.dir, "сбой.env")
        with open(env, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_HOME_CHANNEL=123456789\n")
        было = cd.deliver
        cd.deliver = lambda text, token, chat: "failed"
        try:
            with self.assertRaises(RuntimeError):
                cd.run(self.eid, root=self.dir, env_file=env)
        finally:
            cd.deliver = было
        self.assertEqual(self.состояние(), "projected",
                         "до ретрая звонок обработанным не считается")


class Outbox(_СтендДоставки):
    """Т2.5, §5.2: исходящий эффект — через outbox, а не из середины шага.
    Текст дайджеста и намерение отправить его коммитятся вместе и раньше
    отправки; исход отправки ложится в ту же строку."""

    def env(self, имя="есть.env"):
        env = os.path.join(self.dir, имя)
        with open(env, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_HOME_CHANNEL=123456789\n")
        return env

    def строки(self):
        return [dict(r) for r in self.con.execute("select * from outbox order by created")]

    def дайджест(self):
        return self.con.execute("select id, state from digests where event_id=?",
                                (self.eid,)).fetchone()

    def доставка(self, исход):
        """Подменить `deliver`; возвращает список вызовов."""
        звали = []

        def стук(text, token, chat):
            звали.append((text, chat))
            if isinstance(исход, Exception):
                raise исход
            return исход
        было = cd.deliver
        cd.deliver = стук
        self.addCleanup(setattr, cd, "deliver", было)
        return звали

    def test_намерение_лежит_в_базе_до_отправки(self):
        """Когда транспорт стучится в сеть, строка дайджеста и строка outbox
        уже зафиксированы — другим соединением видно."""
        увидели = []

        def стук(text, token, chat):
            другой = mi.connect(self.dir)
            увидели.append((другой.execute("select state from digests").fetchone()[0],
                            tuple(другой.execute("select state, attempts from outbox").fetchone())))
            return "sent"
        было = cd.deliver
        cd.deliver = стук
        self.addCleanup(setattr, cd, "deliver", было)
        cd.run(self.eid, root=self.dir, env_file=self.env())
        self.assertEqual(увидели, [("queued", ("sending", 1))],
                         "захват и попытка — до отправки, строка — зафиксирована")
        r, = self.строки()
        self.assertEqual((r["kind"], r["object_kind"], r["object_id"], r["state"],
                          r["attempts"]), (cd.ВИД, "event", self.eid, "sent", 1))
        self.assertIsNotNone(r["sent"])
        self.assertEqual(json.loads(r["payload_json"])["digest_id"], self.дайджест()["id"])
        self.assertEqual((self.дайджест()["state"], self.состояние()), ("sent", "done"))

    def test_без_транспорта_строка_ждёт_и_уходит_по_outbox(self):
        пусто = os.path.join(self.dir, "нет-такого.env")
        cd.run(self.eid, root=self.dir, env_file=пусто)
        r, = self.строки()
        self.assertEqual((r["state"], r["attempts"], r["error"]), ("pending", 1, "no-transport"))
        self.assertEqual(self.дайджест()["state"], "no-transport")
        # владелец починил настройку — `--outbox` дошлёт всё, что ждёт, без
        # пересборки и без `--event` по каждому звонку
        звали = self.доставка("sent")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cd.outbox(self.dir, self.env()), 1)
        self.assertEqual(len(звали), 1)
        r, = self.строки()
        self.assertEqual((r["state"], r["attempts"], r["error"]), ("sent", 2, None))
        self.assertEqual((self.дайджест()["state"], self.состояние()), ("sent", "done"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cd.outbox(self.dir, self.env()), 0, "второй раз слать нечего")
        self.assertEqual(len(звали), 1)

    def test_сбой_сети_оставляет_попытку_в_строке(self):
        """Исключение из транспорта — попытка считается, строка ждёт, шаг
        падает в ретрай; повтор шага снимает старое намерение и кладёт новое."""
        self.доставка(OSError("сеть упала"))
        with self.assertRaises(OSError):
            cd.run(self.eid, root=self.dir, env_file=self.env())
        r, = self.строки()
        self.assertEqual((r["state"], r["attempts"]), ("pending", 1))
        self.assertIn("сеть упала", r["error"])
        self.assertEqual((self.дайджест()["state"], self.состояние()), ("queued", "projected"))
        self.доставка("sent")
        cd.run(self.eid, root=self.dir, env_file=self.env())
        старая, новая = self.строки()
        self.assertEqual((старая["state"], старая["error"]), ("skipped", "пересобран"))
        self.assertEqual(новая["state"], "sent")
        self.assertEqual(self.состояние(), "done")

    def test_отказ_телеграма_закрывает_строку_как_failed(self):
        self.доставка("failed")
        with self.assertRaises(RuntimeError):
            cd.run(self.eid, root=self.dir, env_file=self.env())
        r, = self.строки()
        self.assertEqual(r["state"], "failed")
        self.assertEqual(self.дайджест()["state"], "failed")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cd.outbox(self.dir, self.env()), 0,
                             "failed — не pending: его повторяет ретрай шага, не outbox")

    def test_старый_звонок_в_outbox_не_кладётся(self):
        with mock.patch.object(cd, "СВЕЖЕСТЬ_Ч", 24):
            cd.run(self.eid, root=self.dir, env_file=self.env())
        self.assertEqual(self.строки(), [])
        self.assertEqual((self.дайджест()["state"], self.состояние()), ("stale", "done"))

    def test_строку_забирает_один_из_двух(self):
        """Ревью #120, P2-1: `--outbox` владельца рядом с воркером. Строку
        захватывает запись попытки; второй её пропускает, а не шлёт."""
        звали = self.доставка("sent")
        другой = mi.connect(self.dir)
        # «другой процесс» успел целиком между фиксацией намерения и
        # рассылкой шага: моделируем его работой `--outbox` из транспорта
        # первого вызова

        def стук(text, token, chat):
            звали.append((text, chat))
            cd.deliver = lambda t, tk, c: звали.append((t, c)) or "sent"
            cd._разослать(другой, cd.env(self.env()), продолжать=True)
            return "sent"
        было = cd.deliver
        cd.deliver = стук
        self.addCleanup(setattr, cd, "deliver", было)
        cd.run(self.eid, root=self.dir, env_file=self.env())
        r, = self.строки()
        self.assertEqual((r["state"], r["attempts"]), ("sent", 1), "строка ушла один раз")
        self.assertEqual(len(звали), 1, "второй процесс пропустил занятую строку")
        self.assertEqual((self.дайджест()["state"], self.состояние()), ("sent", "done"))

    def test_шаг_не_падает_если_строку_забрали(self):
        """Та же гонка, но второй процесс успел до рассылки шага: шаг
        видит исход чужой, не шлёт и не роняет работу."""
        было_в = mi.в_outbox

        def в_outbox(con, *a, **kw):
            oid = было_в(con, *a, **kw)
            # пока транзакция шага не зафиксирована, другой процесс строку
            # не видит; захватим её сразу после фиксации — подменой
            # `_разослать`-входа: тут проще взять ту же строку заранее
            return oid
        self.доставка("sent")
        было = cd._разослать

        def разослать(con, e, ид=None, продолжать=False):
            другой = mi.connect(self.dir)
            было(другой, e, ид=ид)                    # забрал и отправил
            return было(con, e, ид=ид, продолжать=продолжать)   # шагу — пусто
        cd._разослать = разослать
        self.addCleanup(setattr, cd, "_разослать", было)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cd.run(self.eid, root=self.dir, env_file=self.env())
        self.assertIn("взял другой процесс", out.getvalue())
        r, = self.строки()
        self.assertEqual((r["state"], r["attempts"]), ("sent", 1))
        self.assertEqual((self.дайджест()["state"], self.состояние()), ("sent", "done"))

    def test_шаг_падает_если_строку_забрали_и_не_довели(self):
        """Codex по #120, круг 4: соперник захватил свежую строку и умер до
        исхода — шаг не вправе выйти нулём (работа закрылась бы над висящей
        строкой); ошибка держит работу в ретрае до конца аренды."""
        звали = self.доставка("sent")
        было = cd._разослать

        def разослать(con, e, ид=None, продолжать=False):
            другой = mi.connect(self.dir)
            другой.execute("update outbox set state='sending', attempts=1, "
                           "last_attempt=? where id=?", (mi.now_iso(), ид))
            return было(con, e, ид=ид, продолжать=продолжать)
        cd._разослать = разослать
        self.addCleanup(setattr, cd, "_разослать", было)
        with self.assertRaises(RuntimeError):
            cd.run(self.eid, root=self.dir, env_file=self.env())
        self.assertEqual(звали, [], "шаг не слал")
        r, = self.строки()
        self.assertEqual((r["state"], r["attempts"]), ("sending", 1))
        self.assertEqual((self.дайджест()["state"], self.состояние()), ("queued", "projected"))

    def test_шаг_падает_если_соперник_вернул_строку_в_очередь(self):
        """Codex по #120, круг 5: соперник захватил строку, упал на
        транспорте и вернул её в `pending` — исхода нет, дайджест `queued`;
        шаг обязан уйти в ретрай, а не выйти нулём."""
        звали = self.доставка("sent")
        было = cd._разослать

        def разослать(con, e, ид=None, продолжать=False):
            # соперник держит строку, пока шаг выбирает (шагу — пусто),
            # а потом падает на транспорте и возвращает её в очередь
            другой = mi.connect(self.dir)
            другой.execute("update outbox set state='sending', attempts=1, "
                           "last_attempt=? where id=?", (mi.now_iso(), ид))
            итоги = было(con, e, ид=ид, продолжать=продолжать)
            другой.execute("update outbox set state='pending', error='OSError: сеть' "
                           "where id=?", (ид,))
            return итоги
        cd._разослать = разослать
        self.addCleanup(setattr, cd, "_разослать", было)
        with self.assertRaises(RuntimeError):
            cd.run(self.eid, root=self.dir, env_file=self.env())
        self.assertEqual(звали, [])
        r, = self.строки()
        self.assertEqual((r["state"], r["attempts"]), ("pending", 1))
        self.assertIn("сеть", r["error"])
        self.assertEqual((self.дайджест()["state"], self.состояние()), ("queued", "projected"))
        cd._разослать = было
        # ретрай шага: строку, вернувшуюся в очередь, берёт сам
        self.доставка("sent")
        cd.run(self.eid, root=self.dir, env_file=self.env())
        self.assertEqual(self.состояние(), "done")

    def test_outbox_продолжает_после_сбоя_одной_строки(self):
        """Ревью #120, P3-3: исключение транспорта на одной строке не
        прерывает очередь, строка ждёт с `error`, остальные уходят."""
        второй, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "d2",
            "occurred_at": EVENT["occurred"], "ended_at": EVENT["ended"],
            "payload": EVENT["payload"]})
        self.con.execute("update events set state='projected' where id=?", (второй,))
        mi.write_json(mi.extraction_path(self.dir, второй), ПУСТО)
        пусто = os.path.join(self.dir, "нет-такого.env")
        cd.run(self.eid, root=self.dir, env_file=пусто)
        cd.run(второй, root=self.dir, env_file=пусто)
        n = []

        def стук(text, token, chat):
            n.append(1)
            if len(n) == 1:
                raise OSError("сеть упала")
            return "sent"
        было = cd.deliver
        cd.deliver = стук
        self.addCleanup(setattr, cd, "deliver", было)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cd.outbox(self.dir, self.env()), 1)
        a, b = self.строки()
        self.assertEqual((a["state"], a["attempts"]), ("pending", 2))
        self.assertIn("сеть упала", a["error"])
        self.assertEqual(b["state"], "sent")
        self.assertIn("ещё ждут: 1", out.getvalue())

    def test_брошенную_строку_берут_снова_после_аренды(self):
        """Процесс умер между захватом и пометкой: строка `sending`. Пока
        аренда жива — её никто не трогает, после — берут снова."""
        self.доставка(OSError("умер"))
        with self.assertRaises(OSError):
            cd.run(self.eid, root=self.dir, env_file=self.env())
        r, = self.строки()
        self.assertEqual(r["state"], "pending", "исключение возвращает строку в очередь")
        # смоделируем смерть без пометки: строка осталась `sending`
        self.con.execute("update outbox set state='sending', error=null")
        звали = self.доставка("sent")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cd.outbox(self.dir, self.env()), 0, "аренда жива — не трогаем")
        self.assertEqual(звали, [])
        давно = (datetime.datetime.now(mi.TZ) - datetime.timedelta(
            seconds=mi.АРЕНДА_OUTBOX_С + 5)).isoformat(timespec="seconds")
        self.con.execute("update outbox set last_attempt=?", (давно,))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cd.outbox(self.dir, self.env()), 1)
        r, = self.строки()
        self.assertEqual((r["state"], r["attempts"]), ("sent", 2), "повтор виден по счётчику")
        self.assertEqual(self.состояние(), "done")

    def test_шаг_не_дублирует_строку_которую_сейчас_шлют(self):
        """Ретрай шага, пока строку события держит другой процесс: второго
        намерения не кладём, дайджест не пересобираем — и не выходим нулём
        (Codex по #120, круг 3: процесс мог умереть после захвата, и шаг,
        вышедший нулём, закрыл бы работу, оставив строку висеть). Ошибка —
        ретрай; после аренды строку берут снова."""
        пусто = os.path.join(self.dir, "нет-такого.env")
        cd.run(self.eid, root=self.dir, env_file=пусто)
        self.con.execute("update outbox set state='sending', last_attempt=?",
                         (mi.now_iso(),))
        звали = self.доставка("sent")
        было = self.дайджест()["id"]
        with self.assertRaises(RuntimeError):
            cd.run(self.eid, root=self.dir, env_file=self.env())
        self.assertEqual(len(self.строки()), 1)
        self.assertEqual(self.дайджест()["id"], было)
        self.assertEqual(звали, [])
        # аренда истекла — ретрай шага пересобирает и шлёт
        давно = (datetime.datetime.now(mi.TZ) - datetime.timedelta(
            seconds=mi.АРЕНДА_OUTBOX_С + 5)).isoformat(timespec="seconds")
        self.con.execute("update outbox set last_attempt=?", (давно,))
        cd.run(self.eid, root=self.dir, env_file=self.env())
        старая, новая = self.строки()
        self.assertEqual((старая["state"], новая["state"]), ("skipped", "sent"))
        self.assertEqual(self.состояние(), "done")

    def test_отправленный_дайджест_повтор_шага_не_шлёт_снова(self):
        """`--outbox` владельца отправил, пока работа ждала ретрая: повтор
        шага видит `sent` и закрывает событие, не пересобирая."""
        пусто = os.path.join(self.dir, "нет-такого.env")
        cd.run(self.eid, root=self.dir, env_file=пусто)
        звали = self.доставка("sent")
        with contextlib.redirect_stdout(io.StringIO()):
            cd.outbox(self.dir, self.env())
        self.assertEqual(len(звали), 1)
        было = self.дайджест()["id"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(cd.run(self.eid, root=self.dir, env_file=self.env()))
        self.assertIn("уже отправлен", out.getvalue())
        self.assertEqual(len(звали), 1, "второго сообщения нет")
        self.assertEqual(len(self.строки()), 1)
        self.assertEqual((self.дайджест()["id"], self.состояние()), (было, "done"))

    def test_исход_и_строка_outbox_ложатся_вместе(self):
        """Codex по #120, P1: нет окна, где outbox уже `sent`, а дайджест ещё
        `queued` — тогда строку не взял бы никто, а ретрай послал бы снова."""
        self.доставка("sent")
        # сорвём запись `digests` после отправки — как смерть процесса между
        # пометкой outbox и исходом: триггер роняет транзакцию целиком
        self.con.execute("create trigger обрыв before update on digests begin "
                         "select raise(abort, 'смоделированный обрыв после отправки'); end")
        self.addCleanup(self.con.execute, "drop trigger if exists обрыв")
        with self.assertRaises(sqlite3.IntegrityError):
            cd.run(self.eid, root=self.dir, env_file=self.env())
        self.con.execute("drop trigger обрыв")
        r, = self.строки()
        self.assertEqual((r["state"], r["attempts"]), ("sending", 1),
                         "строка не конечная — уйдёт по аренде, а не потеряется")
        self.assertEqual((self.дайджест()["state"], self.состояние()), ("queued", "projected"))

    def test_outbox_шлёт_в_починенный_чат(self):
        """Codex по #120, круг 2: адресат — настройка, не часть намерения.
        Строка, легшая при пустом `TELEGRAM_HOME_CHANNEL`, после починки
        уходит в починенный чат — настоящим `deliver`, с сетью под заглушкой."""
        пусто = os.path.join(self.dir, "нет-такого.env")
        cd.run(self.eid, root=self.dir, env_file=пусто)
        self.assertEqual(self.дайджест()["state"], "no-transport")
        self.assertIsNone(self.con.execute("select chat_id from digests").fetchone()[0])
        ответ = mock.MagicMock()
        ответ.__enter__.return_value.read.return_value = b'{"ok": true}'
        with mock.patch("urllib.request.urlopen", return_value=ответ) as у, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cd.outbox(self.dir, self.env()), 1)
        тело = у.call_args[0][0].data.decode()
        self.assertIn("chat_id=123456789", тело)
        self.assertEqual((self.дайджест()["state"], self.состояние()), ("sent", "done"))
        self.assertEqual(self.con.execute("select chat_id from digests").fetchone()[0],
                         "123456789", "в строке — куда ушло на самом деле")

    def test_outbox_в_командной_строке(self):
        r = subprocess.run([sys.executable, os.path.join(СКРИПТЫ, "call_digest.py"),
                            "--root", self.dir, "--env-file", self.env(), "--outbox"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class СверкаСРеестром(_СтендДоставки):
    """Codex по #122, круг 2: дайджест сверяет извлечение с реестром так же,
    как проектор — пункт с отклонённой ссылкой не «создан» и без метки."""

    def test_отклонённый_пункт_не_создан_и_без_метки(self):
        import call_asr, call_project as cp
        call_asr.записать_сегменты(self.con, self.eid, None, [
            {"segment_id": "s0002", "start_ms": 25000, "end_ms": 50000, "text": "смета"}])
        свой = self.con.execute("select id from transcript_segments").fetchone()[0]
        другой, _ = mi.put_event(self.con, {"kind": "call", "source": "phone",
                                            "source_id": "d9", "occurred_at": EVENT["occurred"],
                                            "payload": {}})
        call_asr.записать_сегменты(self.con, другой, None, [
            {"segment_id": "s0001", "start_ms": 0, "end_ms": 25000, "text": "чужое"}])
        чужой = self.con.execute("select id from transcript_segments where seq=1").fetchone()[0]
        extr = с(requests=[
            {"action": "прислать смету", "explicit": True, "confidence": 0.95,
             "disposition": "task", "deadline_phrase": "",
             "evidence": [{"segment": "s0002", "segment_id": свой,
                           "start_ms": 25000, "end_ms": 50000}]},
            {"action": "выдумка", "explicit": True, "confidence": 0.95,
             "disposition": "task", "deadline_phrase": "",
             "evidence": [{"segment": "s0001", "segment_id": чужой,
                           "start_ms": 0, "end_ms": 25000}]}])
        mi.write_json(mi.extraction_path(self.dir, self.eid), extr)
        cd.run(self.eid, root=self.dir, env_file=os.path.join(self.dir, "нет-такого.env"))
        text = self.con.execute("select text from digests").fetchone()[0]
        self.assertIn("1 задач", text.replace("задача", "задач"), "создана одна, не две")
        self.assertIn("• прислать смету · 00:25–00:50", text)
        self.assertIn("• выдумка\n", text + "\n", "без выдуманной метки")
        self.assertNotIn("выдумка · 00:00", text)
        self.assertEqual(self.con.execute("select count(*) from audit_events").fetchone()[0],
                         0, "аудит отказов — дело проектора, дайджест только фильтрует")


class Свежесть(unittest.TestCase):
    """Разовая догрузка отдаёт неделю молчания одной пачкой: 69 звонков —
    69 сообщений подряд. Старьё в телеграм не идёт, но и не теряется."""

    def setUp(self):
        снято = mock.patch.dict(os.environ, clear=False)
        снято.start()
        self.addCleanup(снято.stop)
        for k in cd.КЛЮЧИ:
            os.environ.pop(k, None)
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.eid, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "d1",
            "occurred_at": EVENT["occurred"], "ended_at": EVENT["ended"],
            "payload": EVENT["payload"]})
        self.con.execute("update events set state='projected' where id=?",
                         (self.eid,))
        mi.write_json(mi.extraction_path(self.dir, self.eid), ПУСТО)
        self.env = os.path.join(self.dir, "есть.env")
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_HOME_CHANNEL=123456789\n")

    def прогон(self):
        """Возвращает, звали ли `deliver`. Настоящий `deliver` до сети не
        дошёл бы, но проверять надо не исход отправки, а сам факт похода."""
        звали = []
        было = cd.deliver
        cd.deliver = lambda text, token, chat: звали.append(1) or "sent"
        try:
            cd.run(self.eid, root=self.dir, env_file=self.env)
        finally:
            cd.deliver = было
        row = self.con.execute(
            "select state,text from digests where event_id=?",
            (self.eid,)).fetchone()
        сост = self.con.execute("select state from events where id=?",
                                (self.eid,)).fetchone()["state"]
        return bool(звали), row, сост

    def test_старый_звонок_в_телеграм_не_уходит(self):
        with mock.patch.object(cd, "СВЕЖЕСТЬ_Ч", 24):
            звали, row, сост = self.прогон()
        self.assertFalse(звали, "догрузка недельной давности не выкрикивается")
        self.assertEqual(row["state"], "stale")
        self.assertTrue(row["text"], "текст дайджеста всё равно сохранён")
        self.assertEqual(сост, "done",
                         "иначе работа осталась бы в вечном ретрае ради "
                         "сообщения, которого мы намеренно не шлём")

    def test_свежий_звонок_уходит(self):
        """Застава, которая глушит всё, — не застава. Тот же звонок при
        достаточном пороге обязан дойти."""
        with mock.patch.object(cd, "СВЕЖЕСТЬ_Ч", 10 ** 6):
            звали, row, сост = self.прогон()
        self.assertTrue(звали)
        self.assertEqual(row["state"], "sent")
        self.assertEqual(сост, "done")

    def test_время_без_смысла_считается_свежим(self):
        """Промолчать из-за неразобранной строки хуже, чем написать лишний
        раз: пустое и кривое время не имеют права глушить дайджест."""
        self.assertTrue(cd.свежий(None))
        self.assertTrue(cd.свежий(""))
        self.assertTrue(cd.свежий("вчера днём"))

    def test_порог_считается_по_занятому_времени(self):
        """Сутки ровно — ещё свежий, сутки и секунда — уже нет. Мутант
        `<=` → `<` и мутант в множителе 3600 ловятся здесь."""
        import datetime as dt
        t = dt.datetime.fromisoformat("2026-09-02T14:05:00+03:00")
        self.assertTrue(cd.свежий(t.isoformat(),
                                  now=t + dt.timedelta(hours=24), часов=24))
        self.assertFalse(cd.свежий(t.isoformat(),
                                   now=t + dt.timedelta(hours=24, seconds=1),
                                   часов=24))


class БоевойПорог(unittest.TestCase):
    """Каждый прогон `run()` выше заставу отключает — патчем `СВЕЖЕСТЬ_Ч` или
    переменной среды, а прямые тесты `свежий()` передают `часов=` аргументом.
    Значит боевое значение константы не держал ни один свидетель: мутант
    `"24"` → `"0"` проходил и юниты, и self-check, и сквозной тест, а в бою
    при нём ни один дайджест не доходил бы до телеграма никогда.

    Здесь заставу не трогают вовсе. Событие двигают, а не порог."""

    def setUp(self):
        снято = mock.patch.dict(os.environ, clear=False)
        снято.start()
        self.addCleanup(снято.stop)
        for k in cd.КЛЮЧИ:
            os.environ.pop(k, None)
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)
        self.env = os.path.join(self.dir, "есть.env")
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_HOME_CHANNEL=123456789\n")
        # Константа читается из среды на импорте. `run-tests.sh` пиннит
        # `MARA_ENV_FILE` и `MARA_CONTEXTD_ENV` ровно от этой болезни, а эту
        # переменную — нет: с ней в шелле гейт краснел бы «True is not false»
        # вместо внятной причины. Заодно это прямой свидетель того, что
        # проверяется боевое значение, а не чьё-то чужое.
        self.assertEqual(cd.СВЕЖЕСТЬ_Ч, 24,
                         "в среде торчит MARA_DIGEST_MAX_AGE_H")

    def прогон(self, часов_назад):
        import datetime as dt
        t = dt.datetime.now().astimezone() - dt.timedelta(hours=часов_назад)
        eid, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone",
            "source_id": "s%g" % часов_назад,
            "occurred_at": t.isoformat(),
            "ended_at": (t + dt.timedelta(minutes=1)).isoformat(),
            "payload": EVENT["payload"]})
        self.con.execute("update events set state='projected' where id=?", (eid,))
        mi.write_json(mi.extraction_path(self.dir, eid), ПУСТО)
        звали = []
        было = cd.deliver
        cd.deliver = lambda text, token, chat: звали.append(1) or "sent"
        try:
            cd.run(eid, root=self.dir, env_file=self.env)
        finally:
            cd.deliver = было
        state = self.con.execute("select state from digests where event_id=?",
                                 (eid,)).fetchone()["state"]
        return bool(звали), state

    def test_часовой_давности_звонок_доходит(self):
        """Держит мутанта `СВЕЖЕСТЬ_Ч = 0`: при нём молчит вообще всё."""
        звали, state = self.прогон(1)
        self.assertTrue(звали, "живой звонок обязан дойти при боевом пороге")
        self.assertEqual(state, "sent")

    def test_догрузка_объясняется_в_stderr(self):
        """`contextd` зовёт шаг через `subprocess.run(capture_output=True)` и
        возвращает только stderr. Мутант «убрать `file=sys.stderr`» набор
        проходил, а урок ветки `not-private` терялся молча."""
        import io as _io, contextlib
        out, err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.прогон(25)
        self.assertIn("дайджест-догрузка", err.getvalue())
        self.assertNotIn("дайджест-догрузка", out.getvalue())

    def test_вчерашняя_догрузка_не_доходит(self):
        """Держит мутанта `СВЕЖЕСТЬ_Ч = 10**6` и мутанта
        `ev["occurred"]` → `ev["received"]`: второй считает возраст от
        приёма, а приём у догрузки — сию секунду."""
        звали, state = self.прогон(25)
        self.assertFalse(звали)
        self.assertEqual(state, "stale")


if __name__ == "__main__":
    unittest.main()
