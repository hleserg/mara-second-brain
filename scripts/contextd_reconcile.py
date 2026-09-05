#!/usr/bin/env python3
"""Сверка состояния приёма (спека §9, ТЗ §17). Крон раз в час.

Инварианты приёма. То, что чинится однозначно, сверка чинит сама: ставит
пропущенную работу извлечения, снимает с ретраев работу, у которой пропал
исходник. То, где нужен человек, она только докладывает — файлы не удаляет
никогда, даже осиротевшие: единственная копия личного разговора стирается по
ретеншену или по прямой команде, а не по догадке.

`--telegram` — раз в день, только о реальных проблемах, и ничего, если их
нет: DLQ не должен превращаться в админку (ТЗ §17).

    python3 scripts/contextd_reconcile.py
    python3 scripts/contextd_reconcile.py --json
    python3 scripts/contextd_reconcile.py --telegram
    python3 scripts/contextd_reconcile.py --self-check
"""
import os, sys, json, glob, time, sqlite3, argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

VAULT = os.environ.get("VAULT", "/srv/vault")
# Ни одна настройка не имеет права уронить сверку при импорте: с ней замолчат
# и DLQ, и сердцебиения, и ретеншен, а в Telegram тишина неотличима от «всё
# хорошо». Разбираем мягко, копим жалобы и докладываем находкой.
ОШИБКИ_КОНФИГА = []
try:
    НОСИТЕЛИ = mi.носители(os.environ.get(
        "MARA_CORE_TARGETS", "/mnt/backup/mara /mnt/win-backups/mara"))
except ValueError as e:
    НОСИТЕЛИ = []
    ОШИБКИ_КОНФИГА.append(str(e))
def _порог(имя, дефолт):
    сырое = os.environ.get(имя)
    if сырое is None:
        return дефолт
    try:
        return float(сырое)
    except ValueError:
        ОШИБКИ_КОНФИГА.append("%s=%r — не число, беру %s"
                              % (имя, сырое, дефолт))
        return дефолт


# Порог один на оба отказа и меряется в пропущенных ночах: запись идёт раз в
# сутки в 4:10, поэтому «старше двух суток» — это ровно «две ночи подряд без
# записи». Отдельный порог для отвала я в первой редакции завёл и убрал:
# машину, выключенную на выходные, он не спасает (она возвращается позже
# 4:10, простой доходит до четырёх суток и звенит всё равно), а живому
# отказу дарит лишние сутки молчания. Разбор — в docs/backup-core.md.
# 2.2, а не ровно 2: при записи в 4:10 возраст архива после одной
# пропущенной ночи доходит до 1.999 к 4:09 следующих суток, и прогон,
# задержавшийся на час (медленный gpg, тормозящая шара, сдвинутый крон),
# ловил бы отказ на исправной системе. 0.2 суток запаса стоят ноль.
БЭКАП_СУТКИ = _порог("MARA_CORE_BACKUP_MAX_DAYS", 2.2)
# Свой порог, и он меньше бэкапного нарочно. Пересборка пакета — тоже раз в
# сутки (крон в 4:25), поэтому форма та же: «сутки плюс запас». Но пропущенная
# ночь у бэкапа значит «третья копия на сутки старше», а у пакета — «Маре
# подан вчерашний список открытых обязательств, и подан как текущий»; вторых
# суток молчания это не стоит.
#
# Отсюда и цифра. Наблюдаемая владельцем поверхность одна — сводка в 8:00
# (крон `--telegram`), лог часовых прогонов не читает никто. Между пересборкой
# в 4:25 и сводкой 3 ч 35 мин, значит после пропущенной ночи к сводке возраст
# 27.58 ч = 1.149 сут: порог обязан быть ниже. Снизу его держит здоровая
# система: перед самой пересборкой, на прогоне в 4:07, возраст доходит до
# 23.7 ч = 0.988 сут. Окно (0.988; 1.149) — в нём 1.1, и запаса на задержку
# самой пересборки в нём 2.7 часа: столько есть до прогона в 7:07,
# который на опоздавшую пересборку пожалуется в лог. До владельца
# ложная жалоба доедет позже — сводка в 8:00 читает манифест заново,
# так что молчит, пока пересборка опоздала меньше чем на 3.58 часа.
#
# 1.2 попадал бы мимо окна сверху: 28 ч 48 мин от 4:25 — это 9:13, прогон
# в 9:07 ещё молчит, жалоба уходит в лог с 10:07, а в сводку — только
# следующим утром, вторыми сутками молчания там, где §3 обещает первые.
ПАКЕТ_СУТКИ = 1.1
BM_DB = os.environ.get("MARA_BM_DB", os.path.expanduser("~/.basic-memory/memory.db"))
КАРТОЧКИ = ("kb/conversations", "kb/commitments")


def находка(check, level, detail, **kw):
    d = {"check": check, "level": level, "detail": detail}
    d.update(kw)
    return d


def манифест_без_блоба(con, root):
    """Манифест есть, записи нет, и она не убрана по ретеншену — это поломка."""
    out = []
    for path in sorted(glob.glob(os.path.join(root, "manifests", "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                man = json.load(fh)
        except (OSError, ValueError) as e:
            out.append(находка("манифест-не-читается", "error", "%s: %s" % (path, e)))
            continue
        if man.get("purged"):
            continue
        sha = (man.get("recording") or {}).get("audio_sha256")
        if not sha:
            continue
        b = con.execute("select path, purged_at from blobs where sha256=?",
                        (sha,)).fetchone()
        if b and b["purged_at"]:
            continue
        if b and b["path"] and os.path.exists(b["path"]):
            continue
        eid = man.get("id") or os.path.basename(path)[:-5]
        con.execute("update jobs set state='dlq', last_error=? "
                    "where event_id=? and state='ready'",
                    ("манифест есть, записи нет: " + sha[:12], eid))
        out.append(находка("манифест-без-блоба", "error",
                           "у события %s пропала запись %s" % (eid, sha[:12]),
                           event_id=eid))
    return out


def блоб_без_манифеста(con, root):
    """Осиротевший файл. Только доклад: удалять единственную копию не наше дело."""
    out = []
    for path in sorted(glob.glob(os.path.join(root, "calls", "*", "*", "*"))):
        sha = os.path.basename(path).split(".")[0]
        if con.execute("select 1 from blobs where sha256=?", (sha,)).fetchone():
            continue
        out.append(находка("блоб-без-манифеста", "warn",
                           "файл %s не знаком базе, руками решить что с ним" % path,
                           path=path, bytes=os.path.getsize(path)))
    return out


def запись_без_расшифровки(con, root):
    """Запись принята, а работы `asr` нет вовсе — поставить.

    Дыра §5.2 в чистом виде. `finish_stored` переводит состояние, потом пишет
    манифест, потом ставит работу — тремя отдельными коммитами, транзакции
    вокруг них нет (ADR-0005). Смерть демона между первым и третьим оставляет
    запись, которую никто не расшифрует, и сам по себе повтор не спасает:
    перевод уже сделан, и `update ... where state='new'` больше не сработает.
    Заметить это может только сверка — до неё дыра держалась на человеке.

    Пропавший манифест чинится не здесь: собрать его заново — это дело демона,
    у которого лежит payload. Сверка о нём докладывает.

    Признак принятой записи — невычищенная строка в `blobs`, а не состояние
    события, поэтому берём `stored`, `new` и `stale` одинаково: `join blobs`
    и `purged_at is null` для всех трёх. Пара «блоб есть, событие не
    сдвинулось» получается, если демон умер между `insert into blobs` и
    переводом состояния, а ещё — если после выката откатили один только
    демон: старый `finish_stored` знает лишь `state='new'`, на `stale`
    промолчит и всё равно ответит телефону 200. До этой правки такую строку
    не видел никто: `запись_не_долита` ищет отсутствие блоба, а блоб-то как
    раз есть. `call_asr` доведёт состояние до `transcribed` сам, поэтому
    чинить достаточно постановкой работы.

    Вычищенное ретеншеном сюда не попадает намеренно — раньше `stored` без
    блоба получал работу и уходил в DLQ на пустом месте. По той же причине
    работа не ставится, если строка есть, а файла по её пути нет. Пропажу
    записи, которую не вычищали, ловит `манифест_без_блоба`.

    Известное ограничение, не заведённое этой веткой: проверка манифеста
    живёт в этом же цикле, поэтому после успешной расшифровки (состояние
    ушло в `transcribed`) пропавший манифест больше не находится. Так было и
    до правки, разбирается отдельно.
    """
    out = []
    # `fetchall` не для удобства: `add_job` ниже пишет, а запись под
    # недочитанным курсором в WAL упирается в снимок и валит весь часовой
    # прогон крона `database is locked` — ровно тогда, когда чинить и надо
    for r in con.execute(
            "select e.id, b.path from events e "
            "join blobs b on b.sha256=e.blob_sha256 "
            "where b.purged_at is null and e.state in ('stored','new','stale') "
            "order by e.id").fetchall():
        eid = r["id"]
        if not os.path.exists(mi.manifest_path(root, eid)):
            out.append(находка(
                "манифест-не-дописан", "warn",
                "у события %s принята запись, манифеста нет" % eid,
                event_id=eid))
        if con.execute("select 1 from jobs where event_id=? and kind='asr'",
                       (eid,)).fetchone():
            continue
        if not (r["path"] and os.path.exists(r["path"])):
            # строка в `blobs` — ещё не файл: ретеншен удаляет запись и только
            # потом ставит `purged_at` (`blob_retention.py:82-87`), и смерть
            # между этими двумя шагами оставляет строку живой при пустом
            # диске. Работа на таком событии не расшифруется никогда
            # (`call_asr.py:121-122` требует существующий путь) — только займёт
            # очередь и уйдёт в DLQ. Пропажу докладывает `манифест_без_блоба`,
            # а событие без манифеста уже названо находкой выше
            continue
        mi.add_job(con, eid, "asr")
        out.append(находка("расшифровка-поставлена", "fixed",
                           "у %s была запись без работы расшифровки" % eid,
                           event_id=eid))
    return out


def транскрипт_без_извлечения(con, root):
    """Расшифровка есть, извлечения нет и работу никто не поставил — поставить."""
    out = []
    for path in sorted(glob.glob(os.path.join(root, "transcripts", "*.jsonl"))):
        eid = os.path.basename(path)[:-6]
        if os.path.exists(mi.extraction_path(root, eid)):
            continue
        занято = con.execute(
            "select 1 from jobs where event_id=? and kind in ('extract','project') "
            "and state in ('ready','dlq','done')", (eid,)).fetchone()
        if занято:
            continue
        if not con.execute("select 1 from events where id=?", (eid,)).fetchone():
            out.append(находка("транскрипт-без-события", "warn",
                               "расшифровка %s без события в базе" % eid))
            continue
        mi.add_job(con, eid, "extract")
        out.append(находка("извлечение-поставлено", "fixed",
                           "у %s была расшифровка без работы извлечения" % eid,
                           event_id=eid))
    return out


def лаг_индекса(vault, bm_db):
    """Карточки, которых Basic Memory ещё не видит. Только счёт, чинить нечего."""
    if not vault or not os.path.exists(bm_db or ""):
        return []
    свои = set()
    for sub in КАРТОЧКИ:
        for p in glob.glob(os.path.join(vault, sub, "*.md")):
            свои.add(os.path.relpath(p, vault))
    if not свои:
        return []
    con = sqlite3.connect("file:%s?mode=ro" % bm_db, uri=True, timeout=10)
    try:
        видит = {r[0] for r in con.execute("select file_path from entity")}
    except sqlite3.Error as e:
        return [находка("лаг-индекса", "warn", "база Basic Memory не читается: %s" % e)]
    finally:
        con.close()
    нет = sorted(свои - видит)
    if not нет:
        return []
    return [находка("лаг-индекса", "warn",
                    "не проиндексировано карточек: %d" % len(нет),
                    count=len(нет), sample=нет[:5])]


def пакет_устарел(vault):
    """Пакет контекста перестал пересобираться. Крон в 4:25 собирает его
    каждую ночь, разбор звонка и правка голосом — ещё чаще; не сдвинулось
    ничего сутки с лишним — значит не отработал никто, а Мара всё это время
    получает вчерашний список как текущий (ADR-0008, решение 5).

    Смотрим `manifest.json`, а не `now.md`, хотя интересен нам пакет. У
    манифеста писатель ровно один — `build_now`, и только под `locked(vault)`.
    У `now.md` их два: следом за нами туда пишет Basic Memory, возвращая свой
    фронтматтер (ADR-0008, решения 6 и 7). Признак живости обязан приходить из
    файла, который пишет только тот, чью живость мы меряем; когда писателей
    двое и второй не наш, «файл свежий» уже не значит «пересборка была».

    Чего мы про второго писателя НЕ знаем — тронет ли он `now.md` в ночь без
    пересборки. На doctor он выглядит реагирующим на чужую правку, а не
    ходящим по расписанию (5 сентября: за полтора часа после 4:25 файл не
    тронут ни разу). Проверять эту догадку и незачем: она про то, насколько
    плох был бы `now.md`, а выбран манифест не поэтому.

    `mtime` здесь не противоречит решению 2 того же ADR, где он объявлен
    негодным: там спрашивают «изменилось ли содержание» (и время сборки на
    это не отвечает — `os.replace` двигает его на каждой пересборке), здесь —
    «пересборка вообще идёт?». Ровно этим он и хорош.

    Волта нет — не находка: сверка ходит и на машинах без волта, а
    самопроверка зовёт `run` с `vault=None`."""
    if not vault or not os.path.isdir(vault):
        return []
    try:
        import context_pack      # ленивый импорт: см. contextd.now_pack
    except Exception as e:
        # `context_pack` на импорте читает с диска `mara-brief.py` (`_brief`,
        # `exec_module`), а сверка в кроне — отдельный процесс, где импорт
        # каждый час первый. Без заставы сломанный файл унёс бы отсюда наверх
        # исключение, а `run` зовёт проверки подряд: до вывода не добрались бы
        # семь следующих — ретеншен, сердцебиение, DLQ, §12-бэкап — и сводка
        # в 8:00 не ушла бы вовсе. Тот же ход, что у `лаг_индекса` с
        # `sqlite3.Error`: деградировать до жалобы, а не падать.
        return [находка("пакет-не-проверен", "warn",
                        "проверка пакета не запустилась: %s" % e)]
    путь = os.path.join(vault, context_pack.DIR, "manifest.json")
    try:
        возраст = (time.time() - os.stat(путь).st_mtime) / 86400.0
    except FileNotFoundError:
        # Пакета нет вовсе — это ненастроенность, а не отказ: до первого
        # прогона `context_pack.py` её и не должно быть. Тот же расклад, что
        # у носителя без архивов.
        return [находка("пакет-не-собран", "warn",
                        "пакета контекста нет: %s — запустить "
                        "scripts/context_pack.py" % путь)]
    except OSError as e:
        # Файл есть, но не читается: снятые права на каталоге, отвалившаяся
        # ФС. Совет «запустить сборку» тут был бы враньём — сборка не поможет,
        # и звать её на непрочитанный каталог тем более незачем.
        return [находка("пакет-не-прочитан", "warn",
                        "пакет контекста не читается: %s: %s" % (путь, e))]
    if возраст <= ПАКЕТ_СУТКИ:
        return []
    return [находка("пакет-устарел", "error",
                    "пакет контекста не пересобирался %.1f сут. — Мара "
                    "получает вчерашний список обязательств как текущий, "
                    "смотреть крон 4:25" % возраст,
                    days=round(возраст, 1))]


def ретеншен_просрочен(con):
    """Уборка не отработала или настроена мимо. Не чиним здесь ни того, ни
    другого: у уборки свой крон и свой лог, а конфиг правит владелец. Но
    докладываем оба — сводка в 8:00 единственный канал, который до него
    доезжает."""
    try:
        import blob_retention as br
    except Exception as e:
        # Зеркало заставы у `пакет_устарел`: тот же ленивый импорт соседа и та
        # же цена отказа — шесть проверок ниже вместе со сводкой в 8:00 (у
        # соседа выше их семь: число тут своё). Опечатка в `MARA_RAW_DAYS`
        # сюда больше не приходит, её ловит сам `blob_retention`; застава
        # стоит против остального — модуль читается с диска, и отказать он
        # может чем угодно.
        return [находка("ретеншен-не-проверен", "warn",
                        "проверка ретеншена не запустилась: %s" % e)]
    ф = []
    if br.ОШИБКА_КОНФИГА:
        # `error`, хотя уборка отработала: дефект в конфиге владельца, и
        # чинится он только владельцем. Прецедент — `бэкап-ядра-конфиг`
        # (`5d8bc67`), там опечатка в пороге тоже не мешает прогону и тоже
        # даёт `error` каждый час, пока её не поправят.
        ф.append(находка("ретеншен-конфиг", "error", br.ОШИБКА_КОНФИГА))
    late = br.просроченные(con)
    if late:
        ф.append(находка("ретеншен-просрочен", "warn",
                         "записей ждут уборки: %d" % len(late),
                         count=len(late)))
    return ф


ИСТОЧНИКИ = (("telegram-tdlib", "tdlib", "демон TDLib"), ("gmail", "gmail", "синк Gmail"))


def сердцебиение(root):
    """Сердцебиение источника старше часа — связи нет или процесс лежит.
    Файла нет вовсе — источник ещё не подключали, это состояние, а не находка."""
    out = []
    for check, name, кто in ИСТОЧНИКИ:
        p = os.path.join(root, name, "heartbeat")
        if not os.path.exists(p):
            continue
        age = int(time.time() - os.stat(p).st_mtime)
        if age > 3600:
            out.append(находка(check, "warn", "%s молчит %d мин" % (кто, age // 60)))
    return out


ТЕЛЕФОННЫЕ = ("whatsapp", "sms")   # молчат, когда слушатель умер или разрешение сняли


def _возраст(iso):
    """Секунды с момента в ISO; None, если момента нет или он не читается."""
    try:
        return time.time() - datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def источник_замолчал(con, days=3, fresh_hours=24):
    """Источник с телефона раньше слал, три дня молчит, а телефон на связи —
    слушатель умер, разрешение сняли или Huawei вычистил фон. Телефон сам не
    на связи — не находка: это mara_mobile_last_seen_seconds, чинится не тут.
    Ни разу не слал — тоже не находка: источник ещё не подключали.
    Считается по каждому устройству отдельно: свежий экспорт через импортёр
    не должен прикрывать умерший слушатель на телефоне, а сам экспорт — не
    живой источник, его тишина ничего не значит."""
    out = []
    for src in ТЕЛЕФОННЫЕ:
        # max() в SQLite тянет остальные колонки из той же строки
        for last in con.execute("select device_id, max(received) as received, payload_json "
                                "from events where source=? group by device_id", (src,)):
            if json.loads(last["payload_json"] or "{}").get("via") == "export":
                continue
            age = _возраст(last["received"])
            if age is None or age < days * 86400:
                continue
            dev = con.execute("select name, last_seen from devices where id=?",
                              (last["device_id"],)).fetchone()
            seen = _возраст(dev["last_seen"]) if dev else None
            if seen is None or seen > fresh_hours * 3600:
                continue
            # ponytail: три дня без единого сообщения — эвристика; SMS может и правда
            # молчать, поэтому это warn в дневной сводке, а не error
            out.append(находка("источник-замолчал", "warn",
                               "%s на связи, а %s с него молчит %d дн.: слушатель умер или разрешение снято"
                               % (dev["name"], src, int(age // 86400)), source=src, device=dev["name"]))
    return out


def запись_не_долита(con, hours=24):
    """Телефон объявил звонок с записью, а сама запись за сутки не пришла —
    очередь загрузки на телефоне против серверной базы (ТЗ §17). Свежий
    звонок ещё может долиться с плохой сети, поэтому порог в сутки.

    Брошенные (`stale`) сюда не попадают: там телефон уже ушёл заводить новое
    событие, доливать нечего, и такая находка не погасла бы никогда — а
    вечное предупреждение заслоняет ровно то, ради чего эта проверка и
    заведена. Их считает `mara_ingest_stale_events`."""
    rows = con.execute(
        "select e.id, e.received from events e "
        "left join blobs b on b.sha256=e.blob_sha256 "
        "where e.blob_sha256 is not null and b.sha256 is null "
        "and e.state!='stale' order by e.received").fetchall()
    старые = [r["id"] for r in rows if (_возраст(r["received"]) or 0) > hours * 3600]
    if not старые:
        return []
    return [находка("запись-не-долита", "warn",
                    "звонков без записи дольше суток: %d — телефон не долил, смотреть очередь в мастере"
                    % len(старые), count=len(старые), sample=старые[:5])]


def дайджест_не_доставлен(con):
    """Дайджест собран, но до владельца не дошёл: нет токена или канала в
    /etc/mara/contextd.env. Молча такое висеть не должно: звонок разобран, а
    человек о нём не узнал (ТЗ §17). Смотрим только `no-transport`: `failed`
    уводит работу в ретрай, и если та встанет насовсем — о ней скажет dlq(),
    а дублировать одну беду двумя находками ни к чему."""
    rows = con.execute("select event_id, state from digests "
                       "where state='no-transport' order by sent_at").fetchall()
    if not rows:
        return []
    return [находка("дайджест-не-доставлен", "warn",
                    "дайджестов без доставки: %d — проверить токен и канал в "
                    "/etc/mara/contextd.env, потом call_digest.py --event <id>"
                    % len(rows), count=len(rows),
                    sample=[r["event_id"] for r in rows[:5]])]


def dlq(con):
    n = con.execute("select count(*) from jobs where state='dlq'").fetchone()[0]
    if not n:
        return []
    row = con.execute("select kind, last_error from jobs where state='dlq' "
                      "order by updated desc limit 1").fetchone()
    return [находка("работы-в-dlq", "warn", "в DLQ работ: %d, последняя %s — %s"
                    % (n, row["kind"], (row["last_error"] or "")[:120]), count=n)]


def _отмечено(путь):
    """Когда бэкап в последний раз реально писал на каждый носитель.
    Пишет файл `core-backup.py`, сверка только читает."""
    try:
        with open(путь, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def бэкап_ядра(targets=None, days=БЭКАП_СУТКИ, root=None):
    """Бэкап ядра встал. Сам прогон бэкапа проверяет, что архив
    разворачивается; здесь мы проверяем, что архив вообще появляется —
    молчащий крон выглядит точно так же, как отсутствующий (P0-2, P0-5).

    По каждому носителю отдельно. Общий «самый свежий архив» закрывал вопрос
    первым же живым носителем: диск отваливался в понедельник, третьей копии
    по §12 не было всю неделю, и сверка молчала — копия-то есть. Теперь
    молчание означает, что копий столько, сколько носителей в конфиге.

    Отвалившийся носитель ничего о себе не сообщает, поэтому его простой
    меряем по отметке, которую оставляет бэкап. Отключённая на вечер шара
    соседней машины — не поломка и в сводку не попадает; та же шара, не
    принимавшая архив дольше `days`, — отказ. Порог общий с возрастом архива
    и меряется в пропущенных ночах: и там, и там «старше двух суток» значит
    «две ночи подряд без записи».

    Ноль носителей, отдающих архив, — находка независимо от отметок: сколь
    угодно свежая отметка не отменяет того, что внешних копий сейчас нет ни
    одной. Без этой ветки свежая отметка отвечала бы за всех ровно так же,
    как раньше отвечал живой носитель. Считаются именно копии, а не
    смонтированные носители: вытертый носитель смонтирован, а копии на нём
    нет. Протухшая копия — копия: за её возраст отвечает отдельная находка.

    Архивов нет вовсе — это ещё не поломка, а несделанная настройка: warn.
    Были и перестали — это отказ, о котором надо знать: error."""
    ошибки = ОШИБКИ_КОНФИГА
    targets = НОСИТЕЛИ if targets is None else targets
    out = []
    if ошибки:
        out.append(находка("бэкап-ядра-конфиг", "error",
                           "настройки бэкапа не разобрать: %s"
                           % "; ".join(ошибки)))
    if not targets:
        # Пустой список — не «носителей нет», а «третьей копии по §12 быть
        # неоткуда». Молчать об этом нельзя тем более, что теперь переменные
        # живут в crontab, где их легко затереть пустым значением.
        if not ошибки:
            # Иначе вторая строка про ту же беду: пустой список — следствие
            # неразобранного списка, и обе находки под одним именем.
            out.append(находка("бэкап-ядра-конфиг", "error",
                               "список носителей пуст — третью копию по §12 "
                               "класть некуда"))
        # Возврат только здесь: проверять нечего. Жалоба на нечисловой порог
        # — беда восстановимая, `_порог` подставил дефолт и считать есть чем;
        # уходя по ней, сверка выключала бы весь §12 из-за опечатки в цифре.
        return out
    # root — тот же, что у остальных проверок: `mi.ROOT` здесь был бы вторым
    # источником правды, и на вызове с другим корнем (тест, self_check)
    # `смонтирован` спотыкался бы об отсутствующий /srv/mara-blobs — а с ним
    # падала бы вся сверка, не только эта находка.
    root = root or mi.ROOT
    писали = _отмечено(mi.ОТМЕТКА_НОСИТЕЛЕЙ)
    с_копией = 0
    for t in targets:
        # isdir мало: отвалившийся носитель бэкап сам себе воссоздаёт на
        # корневой ФС (mkdir -p), и тогда «смонтирован» ложно истинно —
        # мониторинг зелёный, а копия одна.
        if not os.path.isdir(t) or not mi.смонтирован(t, root):
            когда = писали.get(t)
            if когда is None:
                out.append(находка("бэкап-ядра-носители", "warn",
                                   "носитель %s не смонтирован и ни разу не "
                                   "принимал архив ядра" % t, target=t))
                continue
            простой = (time.time() - когда) / 86400.0
            if простой > days:
                out.append(находка("бэкап-ядра-отвалился", "error",
                                   "носитель %s не принимал архив %.1f сут. — "
                                   "копии по §12 нет, смотреть монтирование"
                                   % (t, простой), target=t,
                                   days=round(простой, 1)))
            continue
        архивы = glob.glob(os.path.join(t, "core-*.tar.gz.gpg"))
        if not архивы:
            # Носитель, который архив уже принимал, а теперь пуст, — это не
            # ненастроенность: копию с него кто-то убрал.
            if t in писали:
                out.append(находка("бэкап-ядра-пропали", "error",
                                   "с носителя %s исчезли все архивы ядра, "
                                   "хотя запись на него шла" % t, target=t))
            else:
                out.append(находка("бэкап-ядра-нет", "warn",
                                   "на носителе %s нет ни одного архива ядра — "
                                   "core-backup.py ещё не поставлен в крон" % t,
                                   target=t))
            continue
        # После проверки архивов, а не до неё: считаем копии, как и обещает
        # имя находки. Со счётом смонтированных носителей два вытертых
        # диска давали два «пропали» и ни строки о том, что копий ноль.
        с_копией += 1
        свежий = max(os.path.getmtime(f) for f in архивы)
        возраст = (time.time() - свежий) / 86400.0
        if возраст > days:
            out.append(находка("бэкап-ядра-устарел", "error",
                               "на носителе %s последний бэкап ядра %.1f сут. "
                               "назад — смотреть крон и core-backup.log"
                               % (t, возраст), target=t,
                               days=round(возраст, 1)))
    if not с_копией:
        # Всегда, а не «когда иначе тишина»: подавление по непустому `out`
        # означало, что за факт «внешних копий ноль» отвечает первая
        # попавшаяся строка — например «носитель A ни разу не принимал
        # архив», из которой читается ровно обратное: A не настроили, B цел.
        # Это единственная строка, называющая состояние по §12 целиком.
        out.append(находка("бэкап-ядра-копий-ноль", "warn",
                           "внешних копий ядра сейчас ноль: ни один из "
                           "носителей (%s) не отдаёт архив — не смонтирован "
                           "или пуст" % " ".join(targets)))
    return out


def run(con, root=None, vault=VAULT, bm_db=BM_DB, targets=None):
    root = root or mi.ROOT
    out = []
    out += манифест_без_блоба(con, root)
    out += блоб_без_манифеста(con, root)
    out += запись_без_расшифровки(con, root)
    out += транскрипт_без_извлечения(con, root)
    out += лаг_индекса(vault, bm_db)
    out += пакет_устарел(vault)
    out += ретеншен_просрочен(con)
    out += сердцебиение(root)
    out += источник_замолчал(con)
    out += запись_не_долита(con)
    out += дайджест_не_доставлен(con)
    out += dlq(con)
    out += бэкап_ядра(targets, root=root)
    return out


def код(находки):
    """Ненулевой код только на настоящей поломке: крон не должен кричать зря."""
    return 1 if any(f["level"] == "error" for f in находки) else 0


def текст(находки, limit=12):
    """Сводка владельцу — только о проблемах. Нечего сказать — None, и в канал
    не уходит ничего: тишина и есть хорошая новость."""
    серьёзные = [f for f in находки if f["level"] in ("error", "warn")]
    if not серьёзные:
        return None
    lines = ["Сверка Мары %s: проблем %d" % (datetime.now(mi.TZ).strftime("%d.%m %H:%M"),
                                              len(серьёзные))]
    lines += ["• " + f["detail"] for f in серьёзные[:limit]]
    if len(серьёзные) > limit:
        lines.append("… и ещё %d" % (len(серьёзные) - limit))
    return "\n".join(lines)


def доложить(находки):
    """В домашний канал через транспорт дайджестов; ключи — из того же env."""
    import call_digest as cd
    t = текст(находки)
    if not t:
        return "nothing"
    e = cd.env()
    try:
        return cd.deliver(t, e.get("TELEGRAM_BOT_TOKEN"), e.get("TELEGRAM_HOME_CHANNEL"))
    except OSError as err:
        return "failed: %s" % err


def main():
    ap = argparse.ArgumentParser(description="сверка состояния приёма")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--bm-db", default=BM_DB)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--telegram", action="store_true",
                    help="доложить владельцу о реальных проблемах; нет их — молчать")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    mi.ROOT = a.root
    находки = run(mi.connect(a.root), a.root, a.vault, a.bm_db)
    if a.as_json:
        print(json.dumps(находки, ensure_ascii=False, indent=2))
    elif not находки:
        print("%s сверка: всё сходится" % mi.now_iso())
    else:
        print("%s сверка: находок %d" % (mi.now_iso(), len(находки)))
        for f in находки:
            print("  [%s] %s — %s" % (f["level"], f["check"], f["detail"]))
    if a.telegram:
        print("  telegram: " + доложить(находки))
    return код(находки)


def self_check():
    import tempfile
    os.environ["MARA_BACKUP_ALLOW_SAME_DEV"] = "1"   # см. mi.смонтирован
    root = tempfile.mkdtemp()
    mi.ROOT = root
    con = mi.connect(root)
    # свежий «архив» на игрушечном носителе: иначе проверка бэкапа честно
    # ругалась бы на любую машину, где /mnt/backup не смонтирован
    носитель = os.path.join(root, "backup")
    os.makedirs(носитель)
    open(os.path.join(носитель, "core-0000-00-00.tar.gz.gpg"), "wb").write(b"gpg")
    assert run(con, root, vault=None, targets=[носитель]) == [], \
        "на пустой базе находок быть не должно"
    assert код([]) == 0
    eid, _ = mi.put_event(con, {"kind": "call", "source": "sc", "source_id": "1",
                                "blob": {"sha256": "d" * 64, "ext": "m4a"}})
    mi.write_json(mi.manifest_path(root, eid),
                  {"id": eid, "recording": {"audio_sha256": "d" * 64}, "purged": None})
    mi.add_job(con, eid, "asr")
    f = run(con, root, vault=None)
    assert [x for x in f if x["check"] == "манифест-без-блоба"], "пропавший блоб не найден"
    assert код(f) == 1, "поломка должна давать ненулевой код"
    assert con.execute("select state from jobs where event_id=?",
                       (eid,)).fetchone()["state"] == "dlq", "работа осталась в ретраях"
    hb = os.path.join(root, "tdlib", "heartbeat")
    os.makedirs(os.path.dirname(hb)); open(hb, "w").close()
    assert сердцебиение(root) == [], "свежее сердцебиение — не находка"
    os.utime(hb, (time.time() - 7200, time.time() - 7200))
    assert [f["check"] for f in сердцебиение(root)] == ["telegram-tdlib"], "молчание два часа — warn"
    gh = os.path.join(root, "gmail", "heartbeat")
    os.makedirs(os.path.dirname(gh)); open(gh, "w").close()
    os.utime(gh, (time.time() - 7200, time.time() - 7200))
    assert [f["check"] for f in сердцебиение(root)] == ["telegram-tdlib", "gmail"], "второй источник — вторая находка"
    tp = mi.transcript_path(root, eid)
    os.makedirs(os.path.dirname(tp), mode=0o700, exist_ok=True)
    open(tp, "w").close()
    run(con, root, vault=None)
    kinds = {r["kind"] for r in con.execute("select kind from jobs where event_id=?",
                                            (eid,))}
    assert "extract" in kinds, "извлечение не поставлено"
    n = con.execute("select count(*) from jobs where kind='extract'").fetchone()[0]
    run(con, root, vault=None)
    assert con.execute("select count(*) from jobs where kind='extract'").fetchone()[0] == n, \
        "повторная сверка плодит работы"
    # источник с телефона замолчал при живом устройстве
    con.execute("insert into devices(id,name,token_sha256,created,last_seen) values(?,?,?,?,?)",
                ("dev_t", "тел", "h", mi.now_iso(), mi.now_iso()))
    wid, _ = mi.put_event(con, {"kind": "message", "source": "whatsapp", "source_id": "w1",
                                "device_id": "dev_t", "payload": {"text": "x"}})
    assert источник_замолчал(con) == [], "свежее сообщение — не тишина"
    давно = datetime.fromtimestamp(time.time() - 5 * 86400, mi.TZ).isoformat(timespec="seconds")
    con.execute("update events set received=? where id=?", (давно, wid))
    assert [f["source"] for f in источник_замолчал(con)] == ["whatsapp"], "тишина не найдена"
    con.execute("update devices set last_seen=? where id='dev_t'", (давно,))
    assert источник_замолчал(con) == [], "телефон не на связи — не наша находка"
    # запись обещана, но не долита
    assert запись_не_долита(con) == [], "свежий звонок ещё может долиться"
    con.execute("update events set received=? where id=?", (давно, eid))
    z = запись_не_долита(con)
    assert z and z[0]["count"] == 1 and z[0]["sample"] == [eid], z
    # брошенное событие не должно звенеть вечно
    con.execute("update events set state='stale' where id=?", (eid,))
    assert запись_не_долита(con) == [], "брошенную запись доливать некому"
    # но если блоб всё-таки лёг, а состояние не сдвинулось (откат демона,
    # смерть между insert и переводом) — расшифровку обязаны поставить
    sid, _ = mi.put_event(con, {"kind": "call", "source": "sc",
                                "source_id": "2",
                                "blob": {"sha256": "e" * 64, "ext": "m4a"}})
    есть_asr = lambda: con.execute(
        "select 1 from jobs where event_id=? and kind='asr'", (sid,)).fetchone()
    con.execute("update events set state='stale' where id=?", (sid,))
    запись_без_расшифровки(con, root)
    assert not есть_asr(), "блоба ещё нет — работе неоткуда взяться"
    путь = os.path.join(root, "есть.m4a")
    con.execute("insert into blobs(sha256,path,bytes,mime,created) "
                "values(?,?,?,?,?)",
                ("e" * 64, путь, 1, "audio", mi.now_iso()))
    run(con, root, vault=None)
    assert not есть_asr(), "строка в blobs — ещё не файл"
    open(путь, "wb").write(b"a")
    run(con, root, vault=None)
    assert есть_asr(), "принятая запись осталась без расшифровки"
    # вычищенное ретеншеном работу не получает: расшифровывать нечего, а
    # раньше `stored` без блоба уходил в DLQ на пустом месте
    con.execute("delete from jobs where event_id=?", (sid,))
    con.execute("update events set state='stored' where id=?", (sid,))
    con.execute("update blobs set purged_at=? where sha256=?",
                (mi.now_iso(), "e" * 64))
    run(con, root, vault=None)
    assert not есть_asr(), "у вычищенной записи расшифровывать нечего"
    con.execute("delete from jobs where event_id=?", (sid,))
    con.execute("delete from events where id=?", (sid,))
    con.execute("delete from blobs where sha256=?", ("e" * 64,))
    con.execute("update events set state='new' where id=?", (eid,))
    assert текст([]) is None and текст([находка("x", "fixed", "починено")]) is None, \
        "без проблем в канал не пишем"
    assert "проблем 1" in текст([находка("x", "warn", "беда")])
    # Снимаем за собой: иначе после самопроверки в этом же процессе проверка
    # носителя мертва. Не в `finally` — упавшая самопроверка убивает процесс
    # трейсбеком, восстанавливать окружение там некому и незачем.
    os.environ.pop("MARA_BACKUP_ALLOW_SAME_DEV", None)
    print("contextd_reconcile self-check: ок")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
