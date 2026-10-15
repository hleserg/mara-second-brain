#!/usr/bin/env python3
"""Нажатия кнопок утра (Т-У.5): callback телеграма → правка словами.

Кнопки под пунктами утра ставит `morning_send.py`; здесь — приём нажатий.
Крон doctor раз в минуту держит long-poll `getUpdates` почти всю минуту
(замок на файле смещения — два прогона не делят обновления), и нажатие
превращается в ту же команду, что правка словами: `POST /v1/ingest/event`
вида `correction` (`mara.command.correction.v1`) на петлю doctor с токеном
`MARA_BUTTONS_TOKEN` — весь штатный путь правки (лимит в час, дедуп,
квитанции, аудит, конфликт версии) бесплатно. Прямой записи в волт или
реестр здесь нет.

- «сделал» — `status: done`; «не моё» — `status: cancelled`; «завтра» —
  `due` на завтрашний день владельца.
- `expected_version` — версия из кнопки: карточка, изменившаяся после утра,
  даёт конфликт, а не слепую правку; ответ владельцу — словами Мары
  (`applied.text`), во всплывашке `answerCallbackQuery`.
- Нажимать может только владелец: `from.id` чужого — отказ без похода в
  doctor (§8.3: команды — только личный чат владельца).
- Ключ дедупа — id нажатия (`tgbtn/<callback id>`): повторная доставка того
  же update телеграмом правку не повторяет.
- После применённой правки ряд кнопок пункта убирается из сообщения
  (`editMessageReplyMarkup`) — остальные пункты остаются нажимаемыми.

Бот у кнопок СВОЙ (`MARA_BUTTONS_BOT_TOKEN`), не бот Hermes: очередь
`getUpdates` у телеграма бот-глобальна, второй потребитель крал бы
обновления у чата Мары, а с вебхуком не работал бы вовсе (Codex).
Этот же бот и отправляет утро (`morning_send`): кнопки приходят
обновлениями того бота, чьё сообщение.

Не настроено (`MARA_BUTTONS_BOT_TOKEN`, `TELEGRAM_HOME_CHANNEL`,
`MARA_BUTTONS_TOKEN` в `/etc/mara/contextd.env`) — тихий выход: крон не
шумит в журнал каждую минуту. Включение — `docs/USER-MANUAL-STEPS.md`.

    python3 scripts/telegram_buttons.py              # один прогон ~50 с
    python3 scripts/telegram_buttons.py --seconds 0  # один проход без ожидания
    python3 scripts/telegram_buttons.py --self-check
"""
import argparse, datetime, fcntl, json, os, re, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import call_digest as cd

# те же причины, что у MARA_TELEGRAM_API дайджеста: сквозной тест иначе
# ходит в настоящий телеграм
БОТ_API = os.environ.get("MARA_TELEGRAM_BOT_API", "https://api.telegram.org/bot%s/%s")
# умолчание адреса демона; настройка читается в `цикл` из env-файла
# (крон окружения не несёт, Codex, круг 11), имена — как у соседей по
# doctor (MARA_CONTEXT_URL у `contextd_reconcile`), своя — только поверх
ДОКТОР = "http://127.0.0.1:8788"
ДАННЫЕ = re.compile(r"^([dnt]):([0-9a-f]{8}):([0-9]{1,18}|-)$")
СТАТУС = {"d": "done", "n": "cancelled"}


def завтра(now=None):
    now = now or datetime.datetime.now(mi.TZ)
    return (now.astimezone(mi.TZ) + datetime.timedelta(days=1)).date().isoformat()


def разобрать(data):
    """`(действие, код, версия|None)` или None — кривые данные не команда."""
    m = ДАННЫЕ.match(str(data or ""))
    if not m:
        return None
    действие, код, версия = m.groups()
    return действие, код, (None if версия == "-" else int(версия))


def _бот(e, метод, поля, api=None, timeout=30):
    """Вызов Bot API; телеграм недоступен — исключение наружу, прогон
    закончится, крон придёт через минуту."""
    токен = e.get("TELEGRAM_BOT_TOKEN") or ""
    if not re.fullmatch(r"[A-Za-z0-9:_-]+", токен):
        raise ValueError("TELEGRAM_BOT_TOKEN не похож на токен бота")
    req = urllib.request.Request((api or БОТ_API) % (токен, метод),
                                 data=urllib.parse.urlencode(поля).encode(),
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _в_доктор(e, событие, url=None):
    """Правка штатным приёмом contextd. Возвращает `(код, тело)`; отказ
    приёма (4xx) — тоже ответ, а не исключение."""
    req = urllib.request.Request(
        (url or ДОКТОР).rstrip("/") + "/v1/ingest/event",
        data=json.dumps(событие, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % e.get("MARA_BUTTONS_TOKEN", "")},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            тело = json.loads(err.read() or b"{}")
        except ValueError:
            тело = {}
        return err.code, тело


def обработать(update, e, api=None, url=None, now=None, снято=None):
    """Одно обновление телеграма. Возвращает исход для журнала."""
    cb = update.get("callback_query")
    if not isinstance(cb, dict):
        return "skip"                   # мы просим только callback_query
    ид = str(cb.get("id") or "")

    def ответить(текст):
        # best-effort, как `_убрать_ряд`: протухшее нажатие телеграм отбивает
        # 400 «query is too old», и падать на нём значило бы встать на этом
        # update до его выброса (~сутки) — смещение обязано двигаться (ревью)
        try:
            _бот(e, "answerCallbackQuery",
                 {"callback_query_id": ид, "text": текст[:190]}, api=api)
        except Exception as err:
            print("telegram_buttons: всплывашка не ушла: %s" % type(err).__name__,
                  file=sys.stderr)
    # §8.3: команды принимает только владелец; чужое нажатие не доезжает
    # до doctor вовсе (бот в группе, пересланное сообщение)
    if str((cb.get("from") or {}).get("id") or "") != str(e.get("TELEGRAM_HOME_CHANNEL") or ""):
        ответить("кнопки Мары слушаются только владельца")
        return "foreign"
    данные = разобрать(cb.get("data"))
    if данные is None:
        ответить("кнопку не поняла — скажи словами")
        return "bad-data"
    действие, код, версия = данные
    payload = {"id": код}
    if действие in СТАТУС:
        payload["status"] = СТАТУС[действие]
    else:
        payload["due"] = завтра(now)
    if версия is not None:
        payload["expected_version"] = версия
    # ключ идемпотентности (Т2.9): повтор после потерянного ответа получает
    # сохранённую квитанцию с настоящим итогом, а не голое «дубль» (Codex)
    код_ответа, тело = _в_доктор(e, {
        "source": "mara", "source_id": "tgbtn/" + ид, "kind": "correction",
        "occurred_at": mi.now_iso(), "payload": payload,
        "idempotency_key": "tgbtn/" + ид}, url=url)
    applied = тело.get("applied") if isinstance(тело.get("applied"), dict) else {}
    if тело.get("duplicate") and not applied:
        # дубль без квитанции (итога первой доставки нет): успехом не врём и
        # кнопки не трогаем — итог владелец видит в карточке (Codex). С
        # ключом идемпотентности сюда попадает только потерявшая квитанцию
        # экзотика: обычный повтор получает её с полным `applied` и идёт
        # обычным путём ниже.
        ответить("уже принимала это нажатие — итог в карточке")
        return "duplicate"
    if код_ответа != 200:
        # текст отказа приёма выверен и без содержимого карточек (контракт)
        ответить("doctor не принял: %s" % (тело.get("error") or код_ответа))
        return "rejected"
    ответить(applied.get("text") or "записала, но итога не прислал")
    # ряд снимает и применённая правка, и подтверждённое «уже так»: после
    # сорвавшегося editMessageReplyMarkup повторное нажатие — новый callback
    # id, новый ключ и новое событие-пустышка на каждый тык, а кнопки так и
    # висели бы (Codex, круг 7). Конфликт и «не нашёл» кнопки сохраняют.
    if (applied.get("applied") or applied.get("created")
            or (applied.get("found") and not applied.get("error"))):
        _убрать_ряд(e, cb, код, api=api, снято=снято)
        return "applied"
    return "answered"                   # конфликт или «не нашёл» — словами


def _убрать_ряд(e, cb, код, api=None, снято=None):
    """Снять кнопки применённого пункта, оставив остальные: клавиатура
    приходит в самом нажатии, хранить её не нужно. Сбой здесь — не сбой
    правки: кнопки без дела, повтор нажатия упрётся в дедуп.

    `снято` — коды, уже снятые этим прогоном по тому же сообщению: два
    нажатия до первого edit несут один и тот же снимок клавиатуры, и без
    этого второй edit воскрешал бы ряд первого (Codex, круг 10)."""
    msg = cb.get("message") or {}
    клава = ((msg.get("reply_markup") or {}).get("inline_keyboard")) or []
    прежние = (снято.setdefault(((msg.get("chat") or {}).get("id"),
                                 msg.get("message_id")), set())
               if снято is not None else set())
    лишние = прежние | {код}
    свои = lambda ряд: any((str(к.get("callback_data") or "").split(":") + ["", ""])[1]
                           in лишние for к in ряд)
    новые = [ряд for ряд in клава if not свои(ряд)]
    if новые == клава or not msg.get("message_id"):
        return
    try:
        _бот(e, "editMessageReplyMarkup",
             {"chat_id": (msg.get("chat") or {}).get("id"),
              "message_id": msg["message_id"],
              "reply_markup": json.dumps({"inline_keyboard": новые}, ensure_ascii=False)},
             api=api)
        прежние.add(код)
    except Exception as err:
        # не снялось — код в `снято` не идёт: следующий edit попробует снова
        print("telegram_buttons: клавиатуру не поправил: %s" % type(err).__name__,
              file=sys.stderr)


def цикл(root=None, env_file=None, секунд=50, api=None, url=None, now=None):
    """Прогон крона: long-poll почти минуту, затем выход. Смещение — в файле
    под корнем реестра; замок на нём же не даёт двум прогонам делить
    обновления. Возвращает число обработанных нажатий."""
    e = cd.env(env_file)
    if not (e.get("MARA_BUTTONS_BOT_TOKEN") and e.get("TELEGRAM_HOME_CHANNEL")
            and e.get("MARA_BUTTONS_TOKEN")):
        путь_env = env_file or cd.ENV_FILE
        if os.path.exists(путь_env) and not os.access(путь_env, os.R_OK):
            # «не настроено» и «файл есть, а крону не по правам» — разные
            # беды: вторая тиха у `env()` по задумке (докстринг), а здесь
            # без неё кнопки молча не работали бы (Codex)
            print("telegram_buttons: %s есть, но из-под крона не читается — "
                  "кнопки не работают, поправь права файла" % путь_env,
                  file=sys.stderr)
            return None                 # поломка настройки, не «нечего делать»
        return 0                        # не настроено — тишина, крон не шумит
    путь = os.path.join(root or mi.ROOT, "tg-buttons.offset")
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    with open(путь, "a+", encoding="utf-8") as ф:
        try:
            fcntl.flock(ф, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0                    # обновления читает прошлый прогон
        # дальше всюду — бот кнопок: _бот берёт TELEGRAM_BOT_TOKEN
        e = dict(e, TELEGRAM_BOT_TOKEN=e["MARA_BUTTONS_BOT_TOKEN"])
        # адрес демона — из той же настройки, что токены: окружение крона
        # пусто, и константа импорта видела бы только умолчание (Codex)
        url = url or e.get("MARA_BUTTONS_URL") or e.get("MARA_CONTEXT_URL") or ДОКТОР
        ф.seek(0)
        хвост = ф.read().strip()
        смещение = int(хвост) if хвост.isdigit() else 0
        конец = time.monotonic() + max(секунд, 0)
        сделано, первый, снято = 0, True, {}
        # хвост короче секунды не стоит холостого зова getUpdates
        while первый or time.monotonic() + 1 < конец:
            первый = False
            ждать = min(20, max(0, int(конец - time.monotonic())))
            ответ = _бот(e, "getUpdates",
                         {"offset": смещение, "timeout": ждать,
                          "allowed_updates": '["callback_query"]'},
                         api=api, timeout=ждать + 15)
            for u in ответ.get("result") or []:
                исход = обработать(u, e, api=api, url=url, now=now, снято=снято)
                if исход not in ("skip",):
                    сделано += 1
                    print("telegram_buttons: %s" % исход)
                # смещение — после обработки: упавший прогон увидит нажатие
                # снова, а правку повторно не применит (дедуп tgbtn/<id>)
                смещение = max(смещение, int(u.get("update_id", 0)) + 1)
                ф.seek(0); ф.truncate(); ф.write(str(смещение)); ф.flush()
        return сделано


def self_check():
    assert разобрать("d:0000abcd:7") == ("d", "0000abcd", 7)
    assert разобрать("t:00ffee11:-") == ("t", "00ffee11", None)
    assert разобрать("n:0000abcd:3") == ("n", "0000abcd", 3)
    for кривое in ("x:0000abcd:1", "d:0000ABCD:1", "d:0000abcd:", "d:short:1",
                   "d:0000abcd:19999999999999999999", "", None, "done"):
        assert разобрать(кривое) is None, кривое
    now = datetime.datetime(2026, 10, 15, 9, 0, tzinfo=mi.TZ)
    assert завтра(now) == "2026-10-16"
    # чужое нажатие не доезжает до doctor: _в_доктор уронил бы AssertionError
    сказано = []
    e = {"TELEGRAM_BOT_TOKEN": "t:1", "TELEGRAM_HOME_CHANNEL": "42",
         "MARA_BUTTONS_TOKEN": "x"}
    global _бот
    настоящий = _бот
    _бот = lambda e, метод, поля, api=None, timeout=30: сказано.append((метод, поля))
    try:
        исход = обработать({"callback_query": {"id": "1", "from": {"id": 777},
                                               "data": "d:0000abcd:1"}}, e)
    finally:
        _бот = настоящий
    assert исход == "foreign" and сказано[0][0] == "answerCallbackQuery", (исход, сказано)
    assert "владельца" in сказано[0][1]["text"]
    print("telegram_buttons: самопроверка ок")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="нажатия кнопок утра (Т-У.5)")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--seconds", type=int, default=50,
                    help="сколько держать long-poll; 0 — один проход")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args(argv)
    if a.self_check:
        return self_check()
    mi.ROOT = a.root
    # None — поломка настройки (нечитаемый env): ненулевой код виден крону
    # и сторожу; число обработанных нажатий кодом выхода не является (Codex)
    return 1 if цикл(a.root, a.env_file, a.seconds) is None else 0


if __name__ == "__main__":
    raise SystemExit(main())
