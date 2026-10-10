#!/usr/bin/env python3
"""Приём событий: схема, дедуп, очередь работ (ТЗ §4, §17).

Библиотека без побочных эффектов кроме записи в SQLite: её зовут и демон, и
скрипты пайплайна, и тесты. Сети тут нет и не будет — иначе тесты придётся
гонять при живом bigpc.

Дедуп живёт здесь, а не в проверке «есть ли файл карточки», как в старых
скриптах. Имя карточки зависит от контакта, который резолвится позже, а
телефон может переименовать файл записи; единственное, чего он не может
незаметно изменить — байты. Поэтому у аудио ключ дедупа это его sha256
(ТЗ §7, canonical identity), у остального — пара источник плюс id. Хеш при
этом устройство объявляет о себе само, и один ключ на всю базу означает, что
занять чужой можно: держит это проверка на приёме (`blob` только у звонка,
`sha256` — 64 hex), а не сам ключ.

Имя файла с подчёркиванием, а не с дефисом, как у старых скриптов: этот модуль
импортируют, а не запускают.

    python3 scripts/mara_ingest.py --self-check
    MARA_BLOBS=/srv/mara-blobs python3 scripts/mara_ingest.py --migrate
    MARA_BLOBS=/srv/mara-blobs python3 scripts/mara_ingest.py --migrate 1   # откат
"""
import os, sys, json, time, uuid, random, sqlite3, hashlib, threading
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=float(os.environ.get("MARA_TZ_HOURS", 3))))
ROOT = os.environ.get("MARA_BLOBS", "/srv/mara-blobs")
STATE = os.environ.get("MARA_STATE",
                       os.path.expanduser("~/.local/state/mara"))
# Отметка носителей: пишет её `core-backup.py`, читает `contextd_reconcile.py`.
# Имя здесь, а не по литералу с каждой стороны: разъехавшись, они не ломают
# ни один тест — писатель пишет, читатель читает пустоту, и весь механизм
# отвала молчит при зелёном гейте.
ОТМЕТКА_НОСИТЕЛЕЙ = os.path.join(STATE, "core-targets.json")


def снимки(root):
    """Каталог частых локальных снимков базы (Т3б.5): пишет `core-backup.py
    --snapshot`, читает сверка. По той же причине, что отметка выше, — имя
    одно на писателя и читателя. По умолчанию под корнем блобов: та же ФС,
    что у базы, свой владелец (под `/srv` непривилегированный `mkdir`
    откажет), и в суточный архив он не попадает — `мелочь` обходит только
    три каталога метаданных."""
    return os.environ.get("MARA_CORE_SNAPSHOTS") or os.path.join(root, "snapshots")
LEASE_SEC = 600                            # упавший воркер не держит работу вечно
RETRY = [0, 60, 300, 1800, 7200, 43200]    # ТЗ §17, после последней — DLQ
PIPELINE_VERSION = 1

SCHEMA = """
create table if not exists devices(
  id text primary key, name text, token_sha256 text not null,
  created text, last_seen text, revoked_at text, scopes text);
create table if not exists events(
  id text primary key, kind text, source text, source_id text,
  dedupe_key text unique, device_id text, received text, occurred text,
  ended text, classification text, payload_json text, blob_sha256 text,
  state text default 'new');
create table if not exists jobs(
  id text primary key, event_id text, kind text, state text default 'ready',
  attempts integer default 0, next_at integer default 0, last_error text,
  created text, updated text, lease_until integer default 0);
create table if not exists blobs(
  sha256 text primary key, path text, bytes integer, mime text, created text,
  pin integer default 0, audio_until text, purged_at text);
create table if not exists digests(
  id text primary key, event_id text, chat_id text, text text,
  items_json text, sent_at text, state text default 'new');
create index if not exists jobs_ready on jobs(state, next_at);
create index if not exists events_state on events(state);

-- ledger по ADR-0001: сюда переезжает власть, Markdown становится проекцией.
-- Ревизий и version тут нет намеренно: это §4.5 и свой ADR, а колонку
-- добавить потом — одна строка alter table. Пишет в эти таблицы пока только
-- разовый перенос (ledger_import.py), проектор на них ещё не переключён.
-- `text primary key` в SQLite null не запрещает: это признанная ошибка,
-- которую там не чинят ради совместимости. Отсюда явные `not null` — без
-- них объект без ключа ложится в базу и всплывает уже дублем.
create table if not exists commitments(
  id text primary key not null, title text, status text, owner text,
  promised_to text, due text, due_explicit text, origin_event text,
  source_native_id text unique not null,
  created text, occurred text, valid_from text, confidence real,
  supersedes text, classification text);
create table if not exists conversations(
  id text primary key not null, title text, occurred text, valid_from text,
  origin_event text, source_native_id text unique not null, created text,
  classification text);
-- отпечаток того, что проектор записал в файл: по нему будущая пересборка
-- отличит свой файл от поправленного руками и не затрёт правку молча
create table if not exists projections(
  path text primary key not null, object_kind text, object_id text,
  content_sha256 text, written text);
create index if not exists projections_object on projections(object_id);
"""


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


_ПОСЛЕДНИЙ = [0, 0]        # миллисекунда и хвост предыдущего id
_ЗАМОК = threading.Lock()  # contextd принимает загрузки в несколько потоков


def uuid7():
    """Стабильный id по ADR-0002: 48 бит миллисекунд, версия, вариант, хвост.

    Своя реализация, потому что `uuid.uuid7()` появляется только в Python 3.14,
    а на doctor 3.12. Внутри одной миллисекунды хвост не случайный, а растущий:
    иначе два объекта одного прогона (два обязательства из одного звонка)
    вставали бы в произвольном порядке, и сортировка по id перестала бы
    совпадать со временем ровно там, где она нужна.

    Хвост стартует с 72 бит из отведённых 74 — запас на рост внутри
    миллисекунды. Кончился запас — занимаем следующую миллисекунду вперёд:
    id уходит на пару миллисекунд впереди часов, но остаётся монотонным.

    Под замком, потому что читаем и пишем `_ПОСЛЕДНИЙ`: contextd обслуживает
    загрузки в несколько потоков, и два потока в одной миллисекунде без замка
    прочитали бы один хвост и выдали одинаковый id.

    Часы могут шагнуть назад (ntp): берём максимум с прошлой миллисекундой,
    иначе новый id встал бы перед старым, а всё в ADR-0002 держится на том,
    что строковый порядок id — это порядок времени.
    """
    with _ЗАМОК:
        ms = max(int(time.time() * 1000), _ПОСЛЕДНИЙ[0])
        if ms != _ПОСЛЕДНИЙ[0]:
            tail = random.getrandbits(72)
        elif _ПОСЛЕДНИЙ[1] + 1024 < (1 << 74):
            tail = _ПОСЛЕДНИЙ[1] + random.randrange(1, 1024)
        else:
            ms, tail = ms + 1, random.getrandbits(72)
        _ПОСЛЕДНИЙ[0], _ПОСЛЕДНИЙ[1] = ms, tail
    n = ((ms << 80) | (0x7 << 76) | ((tail >> 62) << 64)
         | (0b10 << 62) | (tail & ((1 << 62) - 1)))
    return str(uuid.UUID(int=n))


# Ключи ledger и колонка, по которой их узнают в старой базе. Хватит одного
# ключа на таблицу: `not null` на обоих ставится разом, одной перестройкой.
ЛЕДЖЕР = (("commitments", "id"), ("conversations", "id"),
          ("projections", "path"))


def _операторы(схема=None):
    """SCHEMA (или другая схема миграции) по одному оператору, без комментариев.

    Нужно потому, что `executescript` перед запуском делает commit: подай он
    DDL внутри перестройки — и она перестанет быть одной транзакцией, а
    оборвавшись на середине, оставит базу без таблицы.
    """
    for кусок in (схема or SCHEMA).split(";"):
        сжато = "\n".join(с for с in кусок.splitlines()
                          if not с.lstrip().startswith("--")).strip()
        if сжато:
            yield сжато


def _обязателен(con, таблица, поле):
    return any(r["name"] == поле and r["notnull"]
               for r in con.execute("pragma table_info(%s)" % таблица))


def _ужать_ledger(con):
    """Дотянуть ключи ledger до `not null`.

    Аддитивной миграцией это не делается: `alter table add column` заводит
    новую колонку, а уже созданную не трогает. В SQLite ужесточение колонки —
    только перестройка таблицы целиком. Транзакцию держит `_сдвинуть`.
    """
    if all(_обязателен(con, т, к) for т, к in ЛЕДЖЕР):
        return
    for таблица, ключ in ЛЕДЖЕР:
        if _обязателен(con, таблица, ключ):
            continue
        поля = ",".join(r["name"] for r in
                        con.execute("pragma table_info(%s)" % таблица))
        con.execute("alter table %s rename to %s_old" % (таблица, таблица))
        con.execute(next(о for о in _операторы() if о.startswith(
            "create table if not exists %s(" % таблица)))
        con.execute("insert into %s(%s) select %s from %s_old"
                    % (таблица, поля, поля, таблица))
        con.execute("drop table %s_old" % таблица)
    # только теперь, когда `_old` снесены вместе со своими индексами:
    # индекс уезжает за переименованной таблицей, сохраняя имя, и
    # `create index if not exists` увидел бы имя занятым и промолчал —
    # проекции остались бы без индекса, и никто бы не заметил
    for о in _операторы():
        if о.startswith("create index"):
            con.execute(о)


def _миграция_1(con):
    """Базлайн: схема, какой её оставил код до версий.

    SCHEMA по одному оператору, а не `executescript`: тот перед запуском
    делает commit и вынес бы миграцию из транзакции `_сдвинуть`.
    """
    for о in _операторы():
        con.execute(о)
    if "scopes" not in {r["name"] for r in
                        con.execute("pragma table_info(devices)")}:
        con.execute("alter table devices add column scopes text")  # ADR-0009
    _ужать_ledger(con)                             # НБ12 из #39


# Миграция 2: весь минимальный набор сущностей §4.2 мастер-ТЗ (Т2.1).
# Только добавляет: новые таблицы и колонки, старые не перестраивает — Г4
# не нужен. SCHEMA выше заморожена как базлайн: её проигрывает миграция 1, и
# допиши туда — новая база и боевая разъедутся. Колонки, которых ТЗ не
# задаёт, — минимум: ключ, время, `*_json` на остальное. Время — ISO-8601
# текстом со сдвигом, как `events.occurred`: и UTC, и исходный сдвиг в одной
# строке (§5.1). Ключи на чужие объекты разных видов (`object_kind` плюс
# `object_id`) внешними быть не могут — SQLite не знает полиморфных ссылок.
# Не свои таблицы: source_events — это `events`, DLQ — `jobs.state='dlq'`,
# projection_state — колонки `projections`, claims живут в `facts`.
SCHEMA_2 = """
create table if not exists messages(
  id text primary key not null, event_id text not null references events(id),
  conversation_id text references conversations(id), sender text,
  recipients_json text, sent text, body text, created text);
create table if not exists transcripts(
  id text primary key not null, event_id text not null references events(id),
  blob_sha256 text, engine text, model text, language text, created text);
create table if not exists transcript_segments(
  id text primary key not null,
  transcript_id text not null references transcripts(id),
  seq integer not null, start_ms integer not null, end_ms integer not null,
  speaker text, text text, unique(transcript_id, seq),
  check(start_ms >= 0 and end_ms >= start_ms));
create table if not exists entities(
  id text primary key not null, kind text not null, name text not null,
  version integer not null default 1, created text, updated text);
create table if not exists entity_aliases(
  entity_id text not null references entities(id), alias text not null,
  source text, created text, primary key(entity_id, alias));
create table if not exists decisions(
  id text primary key not null, title text, status text, decided text,
  conversation_id text references conversations(id), origin_event text,
  version integer not null default 1, created text, updated text,
  classification text);
create table if not exists facts(
  id text primary key not null, subject_kind text not null,
  subject_id text not null, predicate text not null, object_json text,
  confidence real check(confidence between 0 and 1), valid_from text,
  valid_until text, origin_event text, version integer not null default 1,
  created text, updated text, classification text);
create table if not exists evidence_refs(
  id text primary key not null, object_kind text not null,
  object_id text not null,
  kind text not null check(kind in ('audio', 'message', 'derived')),
  segment_id text references transcript_segments(id), start_ms integer,
  end_ms integer, message_id text references messages(id),
  derived_from_json text,
  producer text not null check(producer in ('rule', 'model', 'human')),
  created text);
create table if not exists relations(
  id text primary key not null, from_kind text not null,
  from_id text not null, type text not null, to_kind text not null,
  to_id text not null, confidence real, valid_from text, valid_until text,
  origin_event text, created text);
create table if not exists revisions(
  object_kind text not null, object_id text not null,
  version integer not null, changed_json text, actor_type text,
  actor_id text, reason text, origin_event text, occurred text not null,
  primary key(object_kind, object_id, version));
create table if not exists corrections(
  id text primary key not null, object_kind text not null,
  object_id text not null, version integer, field text, old_json text,
  new_json text, actor_type text, actor_id text, reason text,
  origin_event text, occurred text not null);
create table if not exists ingest_attempts(
  id text primary key not null, source text, device_id text,
  idempotency_key text, received text not null, outcome text not null,
  event_id text references events(id), error text);
create table if not exists job_attempts(
  job_id text not null references jobs(id), attempt integer not null,
  started text not null, finished text, outcome text, error text,
  primary key(job_id, attempt));
create table if not exists audit_events(
  id text primary key not null, occurred text not null, actor_type text,
  actor_id text, action text not null, object_kind text, object_id text,
  detail_json text);
create table if not exists provider_health(
  provider text primary key not null,
  state text not null default 'unknown' check(state in
    ('healthy', 'degraded', 'unhealthy', 'unknown', 'recovering')),
  checked text, since text, detail_json text);
create table if not exists alerts(
  id text primary key not null, kind text not null, severity text,
  state text not null default 'open' check(state in
    ('open', 'acked', 'resolved')),
  object_kind text, object_id text, opened text not null, resolved text,
  detail_json text);
create table if not exists compute_nodes(
  id text primary key not null, name text not null unique, role text,
  last_seen text, capabilities_json text);
create index if not exists segments_transcript on transcript_segments(transcript_id);
create index if not exists evidence_object on evidence_refs(object_kind, object_id);
create index if not exists corrections_object on corrections(object_kind, object_id);
create index if not exists audit_object on audit_events(object_kind, object_id)
"""
# Колонки, которые миграция 2 добавляет к старым таблицам. `drop column`
# откатывает их только пока на них нет индекса и ограничений — не вешать.
КОЛОНКИ_2 = (
    ("commitments", "version", "integer not null default 1"),   # ADR-0003
    ("commitments", "updated", "text"),
    ("commitments", "source_account", "text"),                  # ADR-0002
    ("commitments", "extractor", "text"),                       # ТЗ: модель
    ("commitments", "prompt_version", "text"),
    ("projections", "ledger_version", "integer"),   # = projection_state
    ("projections", "projector_version", "integer"),
    ("projections", "manifest_hash", "text"),
)


def _миграция_2(con):
    for о in _операторы(SCHEMA_2):
        con.execute(о)
    for таблица, поле, тип in КОЛОНКИ_2:
        con.execute("alter table %s add column %s %s" % (таблица, поле, тип))


def _откат_2(con):
    """Назад к 1 — только пока в новое ничего не записали.

    Записали — отказ: такой откат стёр бы данные, и идёт он через
    восстановление из бэкапа (RUNBOOK-deploy.md §6а), а не этой командой.
    """
    таблицы = [о.split("(")[0].split()[-1] for о in _операторы(SCHEMA_2)
               if о.startswith("create table")]
    занято = [т for т in таблицы
              if con.execute("select 1 from %s limit 1" % т).fetchone()]
    занято += sorted({т for т, п, тип in КОЛОНКИ_2 if con.execute(
        "select 1 from %s where %s is not %s limit 1" % (
            т, п, "1" if "default 1" in тип else "null")).fetchone()})
    if занято:
        raise RuntimeError("contextd.db: откат 2 → 1 стёр бы записанное в %s"
                           " — только восстановлением из бэкапа"
                           % ", ".join(занято))
    for таблица, поле, _ in КОЛОНКИ_2:
        con.execute("alter table %s drop column %s" % (таблица, поле))
    for т in reversed(таблицы):          # дети раньше родителей: внешние ключи
        con.execute("drop table %s" % т)


# Миграция 3 (Т2.9): квитанция идемпотентности — одна на устройство и ключ.
# Уникальность нужна не для красоты: два повтора одного запроса в одну
# секунду без неё проходили бы оба и писали две квитанции, а §4.4 обещает
# один и тот же результат. Частичный индекс: строки без ключа (приём без
# `idempotency_key`, старые телефоны) под уникальность не попадают.
def _миграция_3(con):
    con.execute("create unique index if not exists ingest_idem on "
                "ingest_attempts(device_id, idempotency_key) "
                "where idempotency_key is not null")


def _откат_3(con):
    con.execute("drop index if exists ingest_idem")


# Т2.5, §5.2: transactional outbox. Строка — намерение произвести внешний
# эффект (сообщение в телеграм), записанное той же транзакцией, что и
# результат, ради которого эффект нужен (`digests`). Сама отправка идёт
# отдельно, вне транзакции и после её фиксации, по строке; исход ложится в
# ту же строку. До этого `call_digest` слал в сеть, а потом писал результат:
# смерть между отправкой и записью теряла след сообщения, и ретрай слал ещё
# раз, не зная о первом. Повтор при обрыве между отправкой и пометкой
# `sent` outbox не исключает (at-least-once), но делает его видимым:
# `attempts` и `last_attempt` остаются в строке.
SCHEMA_4 = """
create table if not exists outbox(
  id text primary key not null, kind text not null,
  object_kind text, object_id text, payload_json text not null,
  created text not null, attempts integer not null default 0,
  state text not null default 'pending' check(state in
    ('pending', 'sending', 'sent', 'failed', 'skipped')),
  last_attempt text, sent text, error text);
create index if not exists outbox_state on outbox(state, created)
"""


def _миграция_4(con):
    for о in _операторы(SCHEMA_4):
        con.execute(о)


def _откат_4(con):
    """Назад к 3 — пока outbox пуст: строка в нём — след отправленного или
    ждущего отправки сообщения, и стереть его командой нельзя."""
    if con.execute("select 1 from outbox limit 1").fetchone():
        raise RuntimeError("contextd.db: откат 4 → 3 стёр бы записанное в outbox "
                           "— только восстановлением из бэкапа")
    con.execute("drop table outbox")


# Миграция 5 (Т5.0, ТЗ §9): у расшифровки — конфигурация прогона и версия
# конвейера. Имя и версия модели (`engine`, `model`) и хеш входа
# (`blob_sha256`) в строке были с миграции 2; окно и перекрытие нарезки —
# нет, а при их смене те же сегменты режутся иначе, и переобработка
# неотличима от первой. `config_json` — словарь ручек, с которыми шёл прогон
# (`call_asr.конфигурация`), `pipeline_version` — `PIPELINE_VERSION` кода.
# Аддитивно, без индексов и ограничений: `drop column` на откате проходит.
КОЛОНКИ_5 = (
    ("transcripts", "config_json", "text"),
    ("transcripts", "pipeline_version", "integer"),
)


def _миграция_5(con):
    for таблица, поле, тип in КОЛОНКИ_5:
        con.execute("alter table %s add column %s %s" % (таблица, поле, тип))


def _откат_5(con):
    """Назад к 4 — пока в новые колонки ничего не записано (как `_откат_2`):
    записали — откат стёр бы происхождение расшифровки."""
    занято = sorted({т for т, п, _ in КОЛОНКИ_5 if con.execute(
        "select 1 from %s where %s is not null limit 1" % (т, п)).fetchone()})
    if занято:
        raise RuntimeError("contextd.db: откат 5 → 4 стёр бы записанное в %s"
                           " — только восстановлением из бэкапа" % ", ".join(занято))
    for таблица, поле, _ in КОЛОНКИ_5:
        con.execute("alter table %s drop column %s" % (таблица, поле))


# Номер миграции — её место здесь плюс один: `user_version` N значит, что
# прошли первые N. Дописывать только в конец (migration-plan.md §2).
МИГРАЦИИ = (_миграция_1, _миграция_2, _миграция_3, _миграция_4, _миграция_5)
# Путь вниз: `ОТКАТЫ[N-1]` возвращает версию N к N-1. Базлайн назад не идёт —
# ниже него только пустая база.
ОТКАТЫ = (None, _откат_2, _откат_3, _откат_4, _откат_5)
ВЕРСИЯ = len(МИГРАЦИИ)
КОМАНДА = "python3 scripts/mara_ingest.py --migrate"


def _версия(con):
    return con.execute("pragma user_version").fetchone()[0]


def _новее(v):
    return RuntimeError("contextd.db: схема версии %d новее кода (он знает до "
                        "%d) — код откачен без базы? Не пишу в неё." % (v, ВЕРСИЯ))


def _сдвинуть(con, цель=ВЕРСИЯ):
    """Довести базу до версии `цель`, по номеру за раз, каждый — одной
    транзакцией. Обычно вверх, до `ВЕРСИЯ`; ниже — откат по `ОТКАТЫ`.

    `begin immediate` берёт запись сразу, и версия перечитывается уже под
    замком: второй `--migrate`, пришедший в ту же секунду, ждёт до 30 с
    (timeout соединения) и видит готовое. `user_version` живёт в заголовке
    базы и откатывается вместе с транзакцией — сорвался шаг, номер прежний.
    """
    if _версия(con) == цель:
        return                       # не брать замок на запись ради `pragma`
    while True:
        con.execute("begin immediate")
        try:
            v = _версия(con)
            if v > ВЕРСИЯ:
                raise _новее(v)
            if v == цель:
                con.execute("commit")
                return
            if v < цель:
                МИГРАЦИИ[v](con)
                v += 1
            else:
                # Вниз — весь путь одной транзакцией: отказ третьего шага
                # (в новое уже писали) не должен оставлять базу на
                # промежуточной версии, которую код уже не откроет.
                while v > цель:
                    if ОТКАТЫ[v - 1] is None:
                        raise RuntimeError("contextd.db: версия %d — базлайн, "
                                           "ниже не откатывается" % v)
                    ОТКАТЫ[v - 1](con)
                    v -= 1
            con.execute("pragma user_version=%d" % v)
            con.execute("commit")
        except BaseException:
            con.execute("rollback")
            raise


def _открыть(root):
    root = root or ROOT
    os.makedirs(root, mode=0o700, exist_ok=True)
    con = sqlite3.connect(os.path.join(root, "contextd.db"), timeout=30,
                          isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    # Вне транзакции, иначе молча не включится — потому здесь, а не в
    # миграции. По умолчанию SQLite ссылки не проверяет вовсе (ADR-0005).
    con.execute("pragma foreign_keys=on")
    return con


def connect(root=None):
    """Открыть базу. Каталог 0700: в нём лежат личные разговоры.

    Схему не трогает: миграция — отдельной командой (`migrate`), иначе выкат
    кода и есть миграция, и проводит её первый же крон. Исключение — пустой
    файл: беречь в нём нечего, и он сразу заводится последней версией.
    """
    con = _открыть(root)
    v = _версия(con)
    if v == 0 and con.execute("select 1 from sqlite_master").fetchone() is None:
        # ponytail: при двух и более миграциях сосед, открывший новую базу
        # между шагами, увидит промежуточный номер и откажет — один раз, на
        # первом запуске; его крон пройдёт в следующий заход.
        _сдвинуть(con)
    elif v != ВЕРСИЯ:
        con.close()
        raise _новее(v) if v > ВЕРСИЯ else RuntimeError(
            "contextd.db: схема версии %d, код ждёт %d — сначала `%s`"
            % (v, ВЕРСИЯ, КОМАНДА))
    return con


def migrate(root=None, цель=ВЕРСИЯ):
    """Довести схему до `цель` (по умолчанию — версия кода) и вернуть
    открытое соединение. Ниже версии кода `connect()` эту базу не откроет:
    откат делают перед тем, как вернуть старый код (RUNBOOK-deploy.md §6а)."""
    con = _открыть(root)
    try:
        _сдвинуть(con, цель)
    except BaseException:
        con.close()
        raise
    return con


def _migrate_cli():
    db = os.path.join(ROOT, "contextd.db")
    if not os.path.exists(db):
        print("mara_ingest --migrate: базы нет (MARA_BLOBS=%s?)" % ROOT,
              file=sys.stderr)
        return 2
    после = sys.argv[sys.argv.index("--migrate") + 1:]
    цель = int(после[0]) if после and после[0].isdigit() else ВЕРСИЯ
    if not 0 < цель <= ВЕРСИЯ:
        print("mara_ingest --migrate: версии %d код не знает (1…%d)"
              % (цель, ВЕРСИЯ), file=sys.stderr)
        return 2
    con = _открыть(ROOT)
    было = _версия(con)
    con.close()
    con = migrate(ROOT, цель)
    итог = con.execute("pragma integrity_check").fetchone()[0]
    ссылки = "ok" if not con.execute(
        "pragma foreign_key_check").fetchall() else "битые ссылки"
    print("contextd.db: версия %d → %d, integrity_check: %s, "
          "foreign_key_check: %s" % (было, _версия(con), итог, ссылки))
    return 0 if итог == ссылки == "ok" else 1


class транзакция:
    """`begin immediate` … `commit`, откат на исключении (§5.2, Т2.1в).

    Соединения приёма — в autocommit (`isolation_level=None`), и каждый
    оператор был сам себе транзакцией. Внутри уже открытой транзакции блок
    становится savepoint'ом: исключение откатывает ровно его, а не делает
    вид, что всё хорошо (ревью PR #118, P2 — без savepoint полусостояние
    вложенного шага коммитилось вместе с внешней транзакцией). Упавший
    `commit` откатывает: иначе соединение потока остаётся в транзакции, и
    все следующие блоки на нём «вложенные» и никогда не коммитят.
    """
    _n = 0

    def __init__(self, con):
        self.con, self.точка = con, None

    def __enter__(self):
        if self.con.in_transaction:
            транзакция._n += 1
            self.точка = "sp%d" % транзакция._n
            self.con.execute("savepoint " + self.точка)
        else:
            self.con.execute("begin immediate")
        return self

    def __exit__(self, тип, *_):
        if self.точка:
            if тип:
                self.con.execute("rollback to " + self.точка)
            self.con.execute("release " + self.точка)
            return False
        if тип:
            self.con.execute("rollback")
            return False
        try:
            self.con.execute("commit")
        except BaseException:
            self.con.execute("rollback")
            raise
        return False


def dedupe_key(source, source_id, blob_sha256=None):
    """Ключ идемпотентности. У аудио — содержимое, у остального — источник и id.

    Ключ аудио нарочно один на всю базу и не делится по виду или устройству:
    два события на один блоб — две цепочки asr→extract→project→digest на одну
    и ту же запись. Занять чужой ключ мешает не он сам, а то, что блоб бывает
    только у звонка (проверка на приёме, #26).
    """
    if blob_sha256:
        return "blob:" + blob_sha256
    return "src:" + hashlib.sha256(
        ("%s\x00%s" % (source, source_id)).encode("utf-8")).hexdigest()


def put_event(con, ev):
    """Событие в базу. Возвращает (id, дубль ли). Повтор ничего не создаёт."""
    blob = ev.get("blob") or {}
    key = ev.get("dedupe_key") or dedupe_key(ev.get("source"), ev.get("source_id"),
                                             blob.get("sha256"))
    row = con.execute("select id from events where dedupe_key=?", (key,)).fetchone()
    if row:
        return row["id"], True
    eid = "%s_%s" % (ev.get("kind") or "event", uuid.uuid4())
    payload = dict(ev.get("payload") or {})
    for extra in ("ext", "mime", "bytes"):          # пригодится при приёме блоба
        if extra in blob and extra not in payload:
            payload[extra] = blob[extra]
    try:
        con.execute(
            "insert into events(id,kind,source,source_id,dedupe_key,device_id,received,"
            "occurred,ended,classification,payload_json,blob_sha256) "
            "values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, ev.get("kind"), ev.get("source"), ev.get("source_id"), key,
             ev.get("device_id"), now_iso(), ev.get("occurred_at"), ev.get("ended_at"),
             ev.get("classification") or "personal",
             json.dumps(payload, ensure_ascii=False), blob.get("sha256")))
    except sqlite3.IntegrityError:
        # между select и insert успел вставить параллельный запрос: телефон
        # просыпается и разом досылает всю очередь. Это дубль, а не поломка
        row = con.execute("select id from events where dedupe_key=?", (key,)).fetchone()
        if not row:
            raise
        return row["id"], True
    return eid, False


def add_job(con, event_id, kind):
    """Поставить работу. Пока живая работа того же вида на это событие уже
    стоит, второй не заводится.

    Цепочку asr→extract→project→digest ставят из трёх мест, и каждое умеет
    сработать дважды: `finish_stored` после недописанной записи, конвейер
    воркера после смерти между работой и отметкой, сверка после сбоя. Вторая
    работа — это второй час GPU и второй дайджест в телеграм.

    Отработавшая (`done`) и брошенная (`dlq`) живой не считаются, и это дверь
    для починки руками: после разбора причины `add_job` ставит работу заново
    поверх сдохшей, не требуя сначала чистить очередь. Сверка сюда не ходит —
    у обеих её проверок собственный guard по любому состоянию работы, так что
    сама она поверх `dlq` ничего не ставит. Возвращается id той работы, что в
    итоге стоит в очереди.
    """
    jid = str(uuid.uuid4())
    # одним запросом, а не «проверить и вставить»: обработчики живут в разных
    # потоках со своими соединениями и раздельную проверку проходили оба
    if con.execute("insert into jobs(id,event_id,kind,created,updated,next_at) "
                   "select ?,?,?,?,?,? where not exists ("
                   "select 1 from jobs where event_id=? and kind=? and state='ready')",
                   (jid, event_id, kind, now_iso(), now_iso(), int(time.time()),
                    event_id, kind)).rowcount == 1:
        return jid
    row = con.execute("select id from jobs where event_id=? and kind=? and "
                      "state='ready'", (event_id, kind)).fetchone()
    return row["id"] if row else None


def claim_job(con, kinds=None, now=None):
    """Взять работу в аренду. Аренда, а не флаг: воркер может умереть посреди
    часового транскрипта, и работа должна вернуться в очередь сама."""
    now = int(time.time()) if now is None else int(now)
    q = "select * from jobs where state='ready' and next_at<=? and lease_until<? "
    args = [now, now]
    if kinds:
        q += "and kind in (%s) " % ",".join("?" * len(kinds))
        args += list(kinds)
    q += "order by next_at limit 1"
    row = con.execute(q, args).fetchone()
    if not row:
        return None
    # Аренда берётся тем же условием, по которому работа выбиралась, включая
    # `state`: `finish_job` при завершении сбрасывает `lease_until` в ноль и
    # аренду не проверяет, так что по одному `lease_until<?` доигравший воркер
    # взял бы уже отработавшую работу. Раздельные
    # `select` и `update` два воркера проходили оба, и час GPU уходил дважды —
    # та же болезнь, что вылечена в `finish_stored`. Проигравший вернётся сюда
    # следующим тиком: работа никуда не делась, а спешить некуда.
    if con.execute("update jobs set lease_until=?, updated=? "
                   "where id=? and lease_until<? and state='ready'",
                   (now + LEASE_SEC, now_iso(), row["id"], now)).rowcount != 1:
        return None
    job = dict(row)
    job["lease_until"] = now + LEASE_SEC
    return job


def next_delay(attempts):
    """Задержка перед следующей попыткой, ТЗ §17, с джиттером ±20 %.

    Джиттер не украшение: после включения GPU-коробки десяток отложенных работ
    иначе ударит в неё одной секундой и все получат таймаут заново.
    """
    base = RETRY[min(attempts, len(RETRY) - 1)]
    return int(base * random.uniform(0.8, 1.2)) if base else 0


def finish_job(con, job_id, ok, error=None):
    """Закрыть работу успехом или назначить ретрай; после последней — DLQ."""
    row = con.execute("select attempts from jobs where id=?", (job_id,)).fetchone()
    if not row:
        return
    if ok:
        con.execute("update jobs set state='done', lease_until=0, updated=? where id=?",
                    (now_iso(), job_id))
        return
    attempts = row["attempts"] + 1
    if attempts >= len(RETRY):
        con.execute("update jobs set state='dlq', attempts=?, last_error=?, "
                    "lease_until=0, updated=? where id=?",
                    (attempts, (error or "")[:500], now_iso(), job_id))
        return
    con.execute("update jobs set attempts=?, last_error=?, next_at=?, lease_until=0, "
                "updated=? where id=?",
                (attempts, (error or "")[:500],
                 int(time.time()) + next_delay(attempts), now_iso(), job_id))


def audit(con, action, actor, object_kind=None, object_id=None, detail=None,
          когда=None):
    """Строка `audit_events` (§5.2, Т2.5): кто, что сделал и над чем.

    Пишется той же транзакцией, что и само действие — вызывающий держит её.
    Содержимого не несёт (ТЗ §6.2: audit metadata без утечки содержимого):
    имена полей, версии и исход — да, значения полей и тексты — нет, они в
    `revisions` и `corrections`. `actor` — `(actor_type, actor_id[, reason])`,
    как у ревизий; третий элемент здесь не нужен.
    """
    con.execute("insert into audit_events(id,occurred,actor_type,actor_id,"
                "action,object_kind,object_id,detail_json) values(?,?,?,?,?,?,?,?)",
                (uuid7(), когда or now_iso(), actor[0], actor[1], action,
                 object_kind, object_id,
                 json.dumps(detail, ensure_ascii=False) if detail is not None
                 else None))


def в_outbox(con, kind, payload, object_kind=None, object_id=None, когда=None):
    """Положить намерение внешнего эффекта в outbox (§5.2, Т2.5). Возвращает
    id строки. Вызывается внутри транзакции с результатом, ради которого
    эффект нужен; отправляет — `из_outbox`, уже после фиксации."""
    oid = uuid7()
    con.execute("insert into outbox(id,kind,object_kind,object_id,payload_json,"
                "created) values(?,?,?,?,?,?)",
                (oid, kind, object_kind, object_id,
                 json.dumps(payload, ensure_ascii=False), когда or now_iso()))
    return oid


# Аренда строки outbox: `sending` старше этого — брошенная попытка (процесс
# умер между захватом и пометкой), её можно брать снова.
АРЕНДА_OUTBOX_С = 600


def из_outbox(con, отправить, kind=None, object_id=None, ид=None, limit=100,
              продолжать=False, итог=None):
    """Разослать ждущие строки outbox. Возвращает список `(id, исход)`.

    `итог(row, исход)` — что вызывающий ведёт рядом со строкой (состояние
    дайджеста, события): зовётся внутри той же транзакции, что переводит
    строку в конечное состояние, — чтобы не было окна, где outbox уже
    `sent`, а результат ещё нет: такую строку `--outbox` не взял бы
    никогда, а ретрай шага послал бы второй раз (Codex по #120, P1).
    Сорвался `итог` — строка остаётся `sending` и уйдёт по аренде.

    `отправить(kind, payload)` → исход: `sent`, `failed`, либо любая другая
    строка — «не сейчас» (нет транспорта, адресат не тот): строка
    возвращается в `pending` с этой строкой в `error`, попытка считается.
    Исключение из `отправить` — тоже попытка и тоже `pending`, с текстом
    исключения; без `продолжать` оно летит дальше (решать, ретрай это или
    DLQ, вызывающему), с `продолжать` — исход `error`, и очередь идёт
    дальше.
    Захват строки — её же запись: `pending` → `sending` с `attempts+1` и
    `last_attempt` **до** отправки, одним `update … where state='pending'`,
    который проходит ровно у одного из двух процессов, взявших одну строку
    (`--outbox` владельца рядом с воркером; ревью PR #120, P2-1); второй
    её не видит и не шлёт. Обрыв между отправкой и пометкой оставляет
    `sending`: такую строку берут снова через `АРЕНДА_OUTBOX_С` — повтор
    возможен (at-least-once), но виден по `attempts`. Строк, которые
    забрал другой, в итогах нет.
    """
    # порог аренды в том же формате и поясе, что `now_iso`: сравнение
    # строк, а не времени, но формат фиксированный, и пояс один
    просрочено = (datetime.now(TZ) - timedelta(seconds=АРЕНДА_OUTBOX_С)
                  ).isoformat(timespec="seconds")
    sql = ("select * from outbox where (state='pending' or "
           "(state='sending' and last_attempt < ?))")
    args = [просрочено]
    if kind:
        sql, args = sql + " and kind=?", args + [kind]
    if object_id:
        sql, args = sql + " and object_id=?", args + [object_id]
    if ид:
        sql, args = sql + " and id=?", args + [ид]
    rows = con.execute(sql + " order by created limit ?", args + [limit]).fetchall()
    итоги = []
    for r in rows:
        когда = now_iso()
        взял = con.execute(
            "update outbox set state='sending', attempts=attempts+1, last_attempt=? "
            "where id=? and (state='pending' or (state='sending' and last_attempt=?))",
            (когда, r["id"], r["last_attempt"])).rowcount
        if not взял:
            continue
        try:
            исход = отправить(r["kind"], json.loads(r["payload_json"]))
        except Exception as e:
            con.execute("update outbox set state='pending', error=? where id=?",
                        ("%s: %s" % (type(e).__name__, e), r["id"]))
            if not продолжать:
                raise
            итоги.append((r["id"], "error"))
            continue
        with транзакция(con):
            if исход == "sent":
                con.execute("update outbox set state='sent', sent=?, error=null "
                            "where id=?", (когда, r["id"]))
            elif исход == "failed":
                con.execute("update outbox set state='failed', error=? where id=?",
                            (исход, r["id"]))
            else:
                con.execute("update outbox set state='pending', error=? where id=?",
                            (исход, r["id"]))
            if итог is not None:
                итог(r, исход)
        итоги.append((r["id"], исход))
    return итоги


def blob_path(root, sha256, ext, when=None):
    """Путь блоба: год и месяц в дереве, имя — хеш. Оригинальное имя из телефона
    ключом доверия не является (ТЗ §7).

    Расширение приходит из тела запроса и попадает прямо в путь, поэтому чистка
    стоит здесь, а не у зовущих: `ext: "../../etc/x"` уводил запись из дерева
    блобов, а `ext: 7` ронял обработчик до сохранения тела — обрыв без ответа,
    после которого телефон повторяет тот же неисправимый запрос вечно.
    """
    d = when or datetime.now(TZ)
    # не «вычистить и взять что осталось», а «принять или заменить»: из
    # `жwav` вычистилось бы `wav`, и в имени файла оказалось бы расширение,
    # которого никто не присылал
    сырой = str(ext if ext is not None else "").lower().lstrip(".")
    чистый = сырой if (0 < len(сырой) <= 8 and сырой.isascii()
                       and сырой.isalnum()) else "bin"
    return os.path.join(root, "calls", "%04d" % d.year, "%02d" % d.month,
                        "%s.%s" % (sha256, чистый))


def manifest_path(root, event_id):
    return os.path.join(root, "manifests", event_id + ".json")


def transcript_path(root, event_id):
    return os.path.join(root, "transcripts", event_id + ".jsonl")


def extraction_path(root, event_id):
    return os.path.join(root, "extractions", event_id + ".json")


def write_json(path, data):
    """Атомарно и только для владельца: рядом ходит уборщик ретеншена."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        # fsync до rename: манифест — результат, раньше которого база не
        # вправе сказать «stored» (§5.2); без него после сбоя питания файл
        # есть, а байт в нём нет
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def носители(строка):
    """Разбор списка носителей из env: пути через пробел.

    Пробел внутри пути не поддерживается и поддержан быть не может — тем же
    пробелом разделяется список. Молча такой путь распадается на два
    несуществующих, запись на них пропускается по `continue`, и третьей копии
    по §10 `TZ.md` нет без единого внятного сообщения.

    Относительный кусок — самый частый признак этой беды, но не единственно
    возможный: `/mnt/Мой /диск` распадается на два абсолютных пути и проходит
    заставу молча. Ловим то, что ловится; полностью закрыть дыру можно только
    сменой разделителя, а он записан в env на живой машине.

    ValueError, а не SystemExit: сверке падать нельзя — из-за опечатки в одной
    переменной она унесла бы все остальные находки. Кто как реагирует, решает
    вызывающий: бэкап отказывается писать, сверка докладывает находкой.
    """
    пути = [os.path.normpath(p) for p in строка.split()]
    кривые = [p for p in пути if not p.startswith("/")]
    if кривые:
        raise ValueError("носители: %s — путь не абсолютный; список носителей "
                         "разделяется пробелами, пробел внутри пути не "
                         "поддерживается" % " ".join(кривые))
    return пути


def смонтирован(target, root):
    """Лежит ли `target` на устройстве, отличном от устройства `root`.

    Оба бэкапных скрипта делают `mkdir -p` перед записью. Если внешний диск или
    сетевая шара отвалились, каталог просто создаётся заново на корневой ФС —
    запись проходит, скрипт рапортует успех, а третья копия по §10 `TZ.md`
    лежит на том же физическом диске, что и первые две. Отказ при этом выглядит
    ровно как норма: `isdir` истинен, архив на месте, сверка молчит.

    Сравниваем `st_dev`, а не зовём `ismount`: интересует не «это точка
    монтирования», а «это другой диск» — на том же вопросе стоит и §10 `TZ.md`.

    Обе стороны берём по ближайшему существующему предку. Для носителя это
    очевидно: на свежем диске `mara/` ещё нет, а точка монтирования есть
    всегда — и смонтированная, и нет. Для корня это первый шаг рунбука
    восстановления: `--drill-only` на замене железа зовут раньше, чем создают
    `/srv/mara-blobs`, — и `os.stat` в лоб убивал бы команду «убедиться, что
    архив читается» ровно в тот день, ради которого она и написана. Побочно
    у функции не остаётся предсказуемого способа бросить (гонка между
    `exists` и `stat` — остаётся), а зовут её из общего прохода сверки, где
    одно исключение уносит все остальные находки.

    MARA_BACKUP_ALLOW_SAME_DEV=1 снимает требование. Это не удобство, а
    единственный способ прогнать самопроверки и тесты: их «носители» —
    временные каталоги на той же ФС, что и игрушечный корень, и другого
    устройства взять неоткуда.
    """
    if os.environ.get("MARA_BACKUP_ALLOW_SAME_DEV"):
        return True
    return _существующий(target).st_dev != _существующий(root).st_dev


def _существующий(path):
    p = os.path.abspath(path)
    while not os.path.exists(p) and p != os.path.dirname(p):
        p = os.path.dirname(p)
    return os.stat(p)


def event_row(con, event_id):
    row = con.execute("select * from events where id=?", (event_id,)).fetchone()
    if not row:
        raise KeyError("нет события %s" % event_id)
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json") or "{}")
    return d


def message_state(con, source, key):
    """Сообщение с учётом правок и удалений (ТЗ §11): в базе три события,
    наружу одно состояние. Последняя ревизия побеждает, надгробие — None.
    Единственный вход для чтения переписки: сырые строки events — не интерфейс.
    """
    rows = con.execute("select source_id, payload_json from events where source=? "
                       "and (source_id=? or source_id like ?) order by occurred, received",
                       (source, key, key + "/%")).fetchall()
    state = None
    for r in rows:
        p = json.loads(r["payload_json"] or "{}")
        if p.get("tombstone_of") == key:
            return None
        if r["source_id"] == key or p.get("revision_of") == key:
            state = p
    return state


def self_check():
    import tempfile
    d = tempfile.mkdtemp()
    con = connect(d)
    ev = {"kind": "call", "source": "phone", "source_id": "s",
          "blob": {"sha256": "c" * 64, "ext": "m4a"}}
    a, dup_a = put_event(con, dict(ev))
    b, dup_b = put_event(con, dict(ev))
    assert a == b and not dup_a and dup_b, "дедуп сломан"
    jid = add_job(con, a, "asr")
    assert claim_job(con)["id"] == jid, "работа не выдалась"
    assert claim_job(con) is None, "работа выдана дважды"
    finish_job(con, jid, True)
    assert con.execute("select state from jobs where id=?", (jid,)).fetchone()[0] == "done"
    assert blob_path(d, "c" * 64, "m4a").endswith("c" * 64 + ".m4a")
    assert next_delay(0) == 0 and 48 <= next_delay(1) <= 72, "расписание ретраев"
    assert носители(" /mnt/a  /mnt/b ") == ["/mnt/a", "/mnt/b"]
    assert носители("/mnt/a/") == ["/mnt/a"], "хвостовой слэш даёт второй ключ"
    try:
        носители("/mnt/мой диск")
        raise AssertionError("пробел в пути прошёл молча")
    except ValueError as e:
        assert "не абсолютный" in str(e), str(e)
    # `_операторы()` режет SCHEMA по `;`. Точка с запятой в комментарии или
    # в литерале разрежет её посреди оператора, и перестройка не найдёт
    # `create table` — упадёт `StopIteration` из миграции, то есть ни
    # `--migrate`, ни новая база не поднимутся. `executescript` такую SCHEMA
    # проглотит молча,
    # так что заметить можно только здесь.
    assert all(о.startswith("create ") and sqlite3.complete_statement(о + ";")
               for схема in (SCHEMA, SCHEMA_2) for о in _операторы(схема)), \
        "SCHEMA разъехалась по `;`"
    # Целость по `;` — не весь инвариант. `create table if not exists
    # commitments (` с лишним пробелом оставляет SCHEMA целой, а перестройка
    # ищет свой оператор по префиксу с открывающей скобкой вплотную — и не
    # находит: `StopIteration` из миграции, то есть не поднимется ни одна
    # база. Тесты это ловят, но на машине без тестов заметить можно только
    # здесь.
    for таблица, _ in ЛЕДЖЕР:
        assert sum(о.startswith("create table if not exists %s(" % таблица)
                   for о in _операторы()) == 1, \
            "перестройка не найдёт `create table %s`" % таблица
    print("mara_ingest self-check: ок")
    return 0


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        raise SystemExit(self_check())
    if "--migrate" in sys.argv:
        raise SystemExit(_migrate_cli())
    print("mara_ingest: библиотека; есть --self-check и --migrate")
