#!/usr/bin/env python3
"""Короткий дайджест звонка в телеграм (ТЗ §16).

Текст собирается шаблоном, без модели: пересказывать уже извлечённое незачем,
а один вызов LLM ради вежливой формулировки — это лишняя точка отказа и лишняя
дорога, по которой личный разговор может уехать наружу.

Отправка прямо в Bot API, а не через Мару. `ctx.inject_message` в Hermes
запускает полноценный ход модели: дайджест стоил бы вызова LLM и мог бы быть
переписан ею по дороге. И он должен доходить, когда Мара занята или лежит.
Ответ Серёги («это тоже задача, срок пятница») ловит инструмент Мары из
спеки 2: он поднимает последний дайджест через /v1/context/bootstrap.

Токен читается из /etc/mara/contextd.env, в волт и в git не попадает никогда.

    python3 scripts/call_digest.py --event call_<uuid>
    python3 scripts/call_digest.py --self-check
"""
import os, sys, json, uuid, argparse, datetime, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import call_project as cp

# Имя своё, не MARA_ENV_FILE: та уводит чтение `~/.config/mara/env` —
# пользовательского файла с адресами. Здесь другой файл, другой владелец и
# другой читатель (systemd, EnvironmentFile= в юните contextd). Пока имя было
# одно на двоих, увести в сторону один файл значило увести и второй, а увести
# только нужный было нельзя вовсе.
ENV_FILE = os.environ.get("MARA_CONTEXTD_ENV", "/etc/mara/contextd.env")
# адрес переопределяем: шаг дайджеста запускается отдельным процессом, и
# сквозной тест иначе либо лезет в настоящий телеграм, либо не проверяет
# доставку вовсе
API = os.environ.get("MARA_TELEGRAM_API",
                     "https://api.telegram.org/bot%s/sendMessage")

# Разовая догрузка отдаёт неделю молчания одной пачкой: телефон стоял, потом
# отдал всё разом — 69 звонков, 69 сообщений подряд. Живой звонок доходит до
# дайджеста за минуты, поэтому всё, что к этому моменту старше суток, — это
# догрузка, а не разговор. Текст всё равно ложится в `digests`: находка не
# пропадает, её просто не выкрикивают владельцу в час ночи.
# Порог переопределяем средой по той же причине, что и `MARA_TELEGRAM_API`
# выше: шаг дайджеста запускается отдельным процессом, и сквозной тест иначе
# не может ни отключить заставу, ни проверить её.
try:
    СВЕЖЕСТЬ_Ч = float(os.environ.get("MARA_DIGEST_MAX_AGE_H", "24"))
except ValueError:
    raise SystemExit("MARA_DIGEST_MAX_AGE_H — не число: %r"
                     % os.environ.get("MARA_DIGEST_MAX_AGE_H"))


ВИД = "telegram_digest"            # kind строки outbox (§5.2, Т2.5)


def свежий(occurred, now=None, часов=None):
    """Пустое и неразобранное время считаем свежим: промолчать из-за строки,
    которую не смогли прочитать, хуже, чем написать лишний раз."""
    if not occurred:
        return True
    # `occurred` от телефона приходит с офсетом (`Sync.kt` шлёт через
    # `ZoneId.systemDefault()`), и сравнение идёт по абсолютным моментам.
    # Наивная строка сравнится в зоне сервера — путь маловероятный, но
    # молчать об этом нельзя.
    try:
        t = datetime.datetime.fromisoformat(occurred)
    except (ValueError, TypeError):
        return True
    now = now or datetime.datetime.now(t.tzinfo)
    часов = СВЕЖЕСТЬ_Ч if часов is None else часов
    return (now - t).total_seconds() <= часов * 3600


# Порядок и названия разделов — из ТЗ §16 и совпадают с карточкой разговора.
SECTIONS = [("requests", "Попросили"), ("commitments", "Ты обещал"),
            ("decisions", "Решили"), ("changed_instructions", "Изменилось"),
            ("open_questions", "Неясно")]


КЛЮЧИ = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL")


def env(path=None):
    """Токен и канал: сначала из окружения, потом из env-файла.

    Окружение первым не для красоты: systemd читает EnvironmentFile от root и
    передаёт переменные процессу, а сам файл может быть недоступен под логином
    юнита. Без этой строки открытие файла падало бы в OSError, словарь
    оставался пустым, и каждый дайджест молча становился no-transport — без
    единой ошибки в логе.

    Ключи не печатаются никуда.
    """
    out = {k: os.environ[k] for k in КЛЮЧИ if os.environ.get(k)}
    try:
        with open(path or ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass
    return out


def задач(n):
    """«1 задача», «2 задачи», «5 задач» — иначе строка режет глаз каждый день."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return "%d задача" % n
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return "%d задачи" % n
    return "%d задач" % n


def line_of(item):
    text = item.get("new_state") or item.get("action") or ""
    if item.get("supersedes"):
        text = "%s → %s" % (item["supersedes"], text)
    due = " (до %s)" % item["due_at"] if item.get("due_at") else ""
    return "• %s%s%s" % (text, due, cp.метка(item))


def render(event, extraction, created_count):
    """(текст дайджеста, пункты для таблицы digests)."""
    day, _, human = cp.when(event)
    end = (event.get("ended") or "")[11:16]
    head = "%s · %s · %s%s" % (cp.заголовок(event), cp.contact(event), human,
                                "–" + end if end else "")
    out, items = [head], []
    if cp.строка_исхода(event):
        out.append(cp.строка_исхода(event))
    maybe = []
    for key, title in SECTIONS:
        rows = []
        for it in (extraction.get(key) or []):
            record = {"key": key, "action": it.get("action") or it.get("new_state"),
                      "disposition": it.get("disposition"), "due_at": it.get("due_at"),
                      "evidence": it.get("evidence")}
            items.append(record)
            (rows if it.get("disposition") == "task" else maybe).append(line_of(it))
        if rows:
            out += ["", title] + rows
    if created_count:
        out += ["", "Создано", "• " + задач(created_count)]
    if maybe:
        out += ["", "Возможно задача"] + maybe
    return "\n".join(out), items


def приватный_чат(chat_id):
    """Личный чат владельца — и только он (§8.3, #61).

    Исключение §8.3 написано под одного читателя, поэтому «адресат ровно один»
    обязано быть механикой, а не словом: канал заводит подписчиков без единой
    правки кода. У приватного чата id положителен, у группы отрицателен, у
    канала и супергруппы начинается с `-100`, а `@имя` не различает их вовсе —
    поэтому проходят только положительные числа.
    """
    s = str(chat_id).strip()
    return s.isascii() and s.isdigit() and int(s) > 0


def deliver(text, token, chat_id):
    """Отправить или честно сказать, что транспорта нет. Текст не теряется."""
    if not token or not chat_id:
        return "no-transport"
    if not приватный_чат(chat_id):
        # Не исключение и не тишина: ночь ронять из-за настройки нельзя, но и
        # уехать мимо §8.3 дайджест не должен. Текст остаётся в `digests`,
        # событие — незакрытым, и строку считает сверка (N11). Про stderr
        # рассчитывать не на что: под демоном шаг идёт `subprocess.run(...,
        # capture_output=True)` и на успешном коде вывод выбрасывается.
        print("call_digest: адресат %s — не личный чат владельца, "
              "не отправляю (§8.3, #61)" % chat_id, file=sys.stderr)
        return "not-private"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(API % token, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        ok = json.loads(r.read() or b"{}").get("ok", False)
    return "sent" if ok else "failed"


def _отправитель(e):
    """Транспорт для строк outbox: `kind` → исход `deliver`. Отдельной
    функцией, чтобы `--outbox` и `run` шли одной дорогой.

    Адресат — из текущего окружения, не из строки: намерение — «дайджест
    владельцу», а чат владельца — настройка. Строка, легшая при пустом или
    кривом `TELEGRAM_HOME_CHANNEL`, после починки обязана уйти в
    починенный чат, а не снова упереться в старый (Codex по #120, P1)."""
    def отправить(kind, payload):
        return deliver(payload["text"], e.get("TELEGRAM_BOT_TOKEN"),
                       e.get("TELEGRAM_HOME_CHANNEL"))
    return отправить


def _разослать(con, e, ид=None, продолжать=False):
    """Отправить ждущие строки outbox (все или одну) и перенести исход в
    `digests`/`events`. Возвращает список `(digest_id, исход)`.

    Отправка — вне транзакции, по зафиксированной строке (§5.2, Т2.5);
    исход — одной транзакцией со строкой дайджеста и состоянием события.
    `sent` закрывает событие; `no-transport`/`not-private` — настройка, не
    сбой: строка остаётся `pending`, а что их есть, скажет сверка (N11).
    `error` (только с `продолжать`) — исключение транспорта, строка тоже
    ждёт; в `digests` он не пишется: там это по-прежнему `queued`."""
    итоги = []

    def итог(row, исход):
        # внутри транзакции, переводящей строку outbox в конечное состояние;
        # `chat_id` — куда ушло на самом деле, не куда собирались при сборке
        did = json.loads(row["payload_json"])["digest_id"]
        con.execute("update digests set state=?, chat_id=? where id=?",
                    (исход, e.get("TELEGRAM_HOME_CHANNEL"), did))
        if исход == "sent":
            con.execute("update events set state='done' where id=?", (row["object_id"],))
        итоги.append((did, исход))
    for oid, исход in mi.из_outbox(con, _отправитель(e), kind=ВИД, ид=ид,
                                   продолжать=продолжать, итог=итог):
        if исход == "error":
            row = con.execute("select payload_json from outbox where id=?", (oid,)).fetchone()
            итоги.append((json.loads(row["payload_json"])["digest_id"], исход))
    return итоги


def run(event_id, root=None, env_file=None):
    root = root or mi.ROOT
    con = mi.connect(root)
    ev = mi.event_row(con, event_id)
    # из реестра (ревизия, миграция 6), файл — у извлечений до неё; тот же
    # читатель, что у проектора, иначе после переизвлечения дайджест и
    # карточки рисовались бы по разным ревизиям (ревью PR #128)
    extraction = cp.call_extract.прочитать_извлечение(con, root, event_id)
    if extraction is None:
        raise RuntimeError("нет извлечения %s" % mi.extraction_path(root, event_id))
    # та же сверка с реестром, что у проектора: пункт, ушедший там в ревью,
    # и здесь не «создан», а отклонённая ссылка не печатается (Codex по
    # #122, круг 2); аудит отказов пишет проектор, здесь — только фильтр
    extraction = cp._сверить_с_реестром(con, event_id, extraction)
    created = len(cp.commitment_cards(ev, extraction, {}))
    text, items = render(ev, extraction, created)
    e = env(env_file)
    свеж = свежий(ev["occurred"])
    did = str(uuid.uuid4())
    # Результат шага и намерение отправить его — одной транзакцией, отправка
    # — после фиксации, по строке outbox (§5.2, Т2.5). До этого шаг слал в
    # сеть, а строку `digests` писал потом: смерть между ними теряла след
    # отправленного, и ретрай слал второй раз, не зная о первом. Один
    # дайджест на событие: повтор шага заменяет строку, а не кладёт рядом
    # ещё одну; прежнее намерение, если его не успели отправить, снимается —
    # иначе `--outbox` отправил бы оба.
    with mi.транзакция(con):
        # дайджест этого события уже ушёл (строка `sent`): повтор шага после
        # того, как его отправил `--outbox` владельца или прежняя попытка,
        # — не повод слать второй раз. Принудительная досылка старья идёт
        # мимо этой ветки: `stale` в outbox не кладётся.
        ушёл = con.execute("select 1 from outbox where kind=? and object_id=? and "
                           "state='sent'", (ВИД, event_id)).fetchone()
        if ушёл:
            print("call_digest: %s — дайджест уже отправлен, не дублирую" % event_id)
            con.execute("update events set state='done' where id=?", (event_id,))
            return None
        # строку этого события прямо сейчас шлёт другой процесс (`--outbox`
        # владельца) — либо шлёт, либо умер между захватом и отправкой; снаружи
        # не отличить, пока аренда жива. Второе намерение рядом дало бы два
        # сообщения, а выйти нулём значило бы закрыть работу и оставить
        # строку висеть, если процесс умер: сверка `sending` не считает
        # (Codex по #120, круг 3). Поэтому — ошибка: работа уйдёт в ретрай
        # (60 с, 5 мин, 30 мин), и либо увидит `sent`, либо переживёт
        # аренду и пересоберёт.
        летит = con.execute(
            "select 1 from outbox where kind=? and object_id=? and state='sending' "
            "and last_attempt >= ?", (ВИД, event_id, (
                datetime.datetime.now(mi.TZ) - datetime.timedelta(
                    seconds=mi.АРЕНДА_OUTBOX_С)).isoformat(timespec="seconds"))).fetchone()
        if летит:
            raise RuntimeError("дайджест %s сейчас шлёт другой процесс — ретрай после "
                               "аренды" % event_id)
        con.execute("delete from digests where event_id=?", (event_id,))
        con.execute("update outbox set state='skipped', error='пересобран' "
                    "where kind=? and object_id=? and state in ('pending','sending')",
                    (ВИД, event_id))
        con.execute("insert into digests(id,event_id,chat_id,text,items_json,sent_at,state) "
                    "values(?,?,?,?,?,?,?)",
                    (did, event_id, e.get("TELEGRAM_HOME_CHANNEL"), text,
                     json.dumps(items, ensure_ascii=False), mi.now_iso(),
                     "queued" if свеж else "stale"))
        if свеж:
            oid = mi.в_outbox(con, ВИД, {"digest_id": did, "text": text},
                              "event", event_id)
        else:
            # Звонок обработан: дайджест собран и лежит в `digests`. Не
            # закрыть его здесь значило бы держать работу в вечном ретрае
            # ради сообщения, которое мы намеренно не шлём.
            con.execute("update events set state='done' where id=?", (event_id,))
    if not свеж:
        print("call_digest: %s — stale, пунктов %d" % (event_id, len(items)))
        # в stderr, а не в stdout: `contextd` зовёт шаг через
        # `subprocess.run(capture_output=True)` и возвращает только stderr —
        # ветка `not-private` этот урок уже выучила, эта чуть не повторила
        print("call_digest: %s старше %g ч — в телеграм не шлём, текст в "
              "digests; сверка назовёт его находкой «дайджест-догрузка»"
              % (event_id, СВЕЖЕСТЬ_Ч), file=sys.stderr)
        return did
    итоги = _разослать(con, e, ид=oid)
    if not итоги:
        # строку забрал `--outbox`, запущенный владельцем в ту же секунду
        # (ревью PR #120, P2-1). Успех шага — только если он уже довёл её
        # до исхода, и судить об этом по дайджесту: `queued` значит, что
        # исхода нет — соперник шлёт, умер после захвата (`sending`) или
        # упал на транспорте и вернул строку в `pending` (Codex, круги 4 и
        # 5); выйти нулём было бы закрыть работу над висящей строкой —
        # ошибка, ретрай. `failed` — его отказ, тоже ретрай. Остальное
        # (`sent`, `no-transport`, `not-private`) — исход, как у своей попытки.
        строка = con.execute("select state from outbox where id=?", (oid,)).fetchone()["state"]
        state = con.execute("select state from digests where id=?", (did,)).fetchone()[0]
        if state == "queued":
            raise RuntimeError("дайджест %s: строку outbox взял другой процесс (%s), исхода "
                               "ещё нет — ретрай" % (event_id, строка))
        if state == "failed" or строка == "failed":
            raise RuntimeError("телеграм не принял дайджест (другой процесс)")
        print("call_digest: %s — строку outbox взял другой процесс, состояние %s"
              % (event_id, state))
        return did
    (_, state), = итоги
    print("call_digest: %s — %s, пунктов %d" % (event_id, state, len(items)))
    if state == "failed":
        raise RuntimeError("телеграм не принял дайджест")
    if state != "sent":
        # транспорта нет (или адресат не тот) — это настройка, а не сбой:
        # повторять нечего, но и объявлять звонок обработанным нельзя. Текст
        # лежит в digests, намерение — в outbox; сверка считает такие каждый
        # час в свой лог, а владельцу называет их в суточной сводке 8:00
        # (N11) — но сводка идёт этим же `deliver`, так что при обоих
        # недоставленных состояниях владелец услышит N11 только после починки
        # настройки. Находка не протухает: строки лежат, пока их не разберут
        print("call_digest: %s не доставлен (%s) — событие остаётся %s"
              % (event_id, state, ev["state"]))
    return did


def outbox(root=None, env_file=None):
    """`--outbox`: разослать всё, что ждёт, — после починки токена или
    адресата, одной командой вместо `--event` по каждому звонку. Возвращает
    число отправленных."""
    con = mi.connect(root or mi.ROOT)
    # исключение транспорта на одной строке не прерывает очередь: исход
    # `error` ложится в строку, остальные пробуются (ревью PR #120, P3-3)
    итоги = _разослать(con, env(env_file), продолжать=True)
    for did, исход in итоги:
        print("call_digest: outbox %s — %s" % (did, исход))
    ждут = con.execute("select count(*) from outbox where kind=? and state='pending'",
                       (ВИД,)).fetchone()[0]
    if ждут:
        print("call_digest: outbox — ещё ждут: %d" % ждут)
    return sum(1 for _, и in итоги if и == "sent")


def self_check():
    event = {"id": "call_x", "occurred": "2026-09-02T14:05:00+03:00",
             "ended": "2026-09-02T14:23:11+03:00",
             "payload": {"contact_name": "Анна"}}
    extr = {"requests": [{"action": "прислать смету", "disposition": "task",
                          "due_at": "2026-09-04",
                          "evidence": [{"start_ms": 252000, "end_ms": 260000}]}],
            "open_questions": [{"action": "покрасить стены",
                                "disposition": "needs-review",
                                "evidence": [{"start_ms": 60000, "end_ms": 61000}]}]}
    text, items = render(event, extr, 1)
    assert text.startswith("Звонок · Анна · 14:05–14:23"), text[:60]
    assert "Попросили" in text and "04:12" in text
    assert "Возможно задача" in text and "покрасить стены" in text
    assert "1 задача" in text, text
    assert len(items) == 2
    assert задач(1) == "1 задача" and задач(3) == "3 задачи" and задач(11) == "11 задач"
    assert приватный_чат("123456789") and приватный_чат(123456789)
    # `.strip()` держится только этим подслучаем, и механизм назван верно
    # лишь с четвёртого раза: `env()` стрипает, но не всё. Из окружения
    # значение берётся как есть (`:57`); внешние пробелы строки из файла
    # она снимает (`:61` и `:65`), так что незакавыченное ` 123456789 `
    # доедет как `123456789`. Но кавычки снимаются **после** пробелов, и
    # внутри них ` 123456789 ` доезжает сюда целиком. В бою крайние
    # пробелы у незакавыченного значения режет ещё и systemd
    # (`man systemd.exec`, EnvironmentFile=) — там мутант «снять strip»
    # безвреден, но не здесь и не при кавычках.
    assert приватный_чат(" 123456789 ")
    assert not приватный_чат("-1001234567890") and not приватный_чат("-99")
    assert not приватный_чат("@канал") and not приватный_чат("١٢٣")
    assert deliver("x", None, None) == "no-transport"
    # застава свежести: сутки ровно ещё проходят, сутки и секунда — уже нет,
    # а нечитаемое время не имеет права глушить дайджест
    _t = datetime.datetime.fromisoformat(event["occurred"])
    assert свежий(event["occurred"], now=_t + datetime.timedelta(hours=24),
                  часов=24)
    assert not свежий(event["occurred"],
                      now=_t + datetime.timedelta(hours=24, seconds=1),
                      часов=24)
    assert свежий(None) and свежий("") and свежий("вчера днём")
    print("call_digest self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="дайджест звонка в телеграм")
    ap.add_argument("--event")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--env-file", default=ENV_FILE)
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    ap.add_argument("--outbox", action="store_true",
                    help="разослать все ждущие дайджесты из outbox")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    mi.ROOT = a.root
    if a.outbox:
        outbox(a.root, a.env_file)
        return 0
    if not a.event:
        ap.error("нужен --event или --outbox")
    run(a.event, a.root, a.env_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
