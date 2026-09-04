#!/usr/bin/env python3
"""Приём событий: схема, дедуп, очередь работ (ТЗ §4, §17).

Библиотека без побочных эффектов кроме записи в SQLite: её зовут и демон, и
скрипты пайплайна, и тесты. Сети тут нет и не будет — иначе тесты придётся
гонять при живом bigpc.

Дедуп живёт здесь, а не в проверке «есть ли файл карточки», как в старых
скриптах. Имя карточки зависит от контакта, который резолвится позже, а
телефон может переименовать файл записи; единственное, чего он не может
незаметно изменить — байты. Поэтому у аудио ключ дедупа это его sha256
(ТЗ §7, canonical identity), у остального — пара источник плюс id.

Имя файла с подчёркиванием, а не с дефисом, как у старых скриптов: этот модуль
импортируют, а не запускают.

    python3 scripts/mara_ingest.py --self-check
"""
import os, sys, json, time, uuid, random, sqlite3, hashlib, threading
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=float(os.environ.get("MARA_TZ_HOURS", 3))))
ROOT = os.environ.get("MARA_BLOBS", "/srv/mara-blobs")
LEASE_SEC = 600                            # упавший воркер не держит работу вечно
RETRY = [0, 60, 300, 1800, 7200, 43200]    # ТЗ §17, после последней — DLQ
PIPELINE_VERSION = 1

SCHEMA = """
create table if not exists devices(
  id text primary key, name text, token_sha256 text not null,
  created text, last_seen text, revoked_at text);
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
create table if not exists commitments(
  id text primary key, title text, status text, owner text, promised_to text,
  due text, due_explicit text, origin_event text, source_native_id text unique,
  created text, occurred text, valid_from text, confidence real,
  supersedes text, classification text);
create table if not exists conversations(
  id text primary key, title text, occurred text, valid_from text,
  origin_event text, source_native_id text unique, created text,
  classification text);
-- отпечаток того, что проектор записал в файл: по нему будущая пересборка
-- отличит свой файл от поправленного руками и не затрёт правку молча
create table if not exists projections(
  path text primary key, object_kind text, object_id text,
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


def connect(root=None):
    """Открыть базу, создав схему. Каталог 0700: в нём лежат личные разговоры."""
    root = root or ROOT
    os.makedirs(root, mode=0o700, exist_ok=True)
    con = sqlite3.connect(os.path.join(root, "contextd.db"), timeout=30,
                          isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.executescript(SCHEMA)
    return con


def dedupe_key(source, source_id, blob_sha256=None):
    """Ключ идемпотентности. У аудио — содержимое, у остального — источник и id."""
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

    Отработавшая (`done`) и брошенная (`dlq`) живой не считаются: на них стоит
    починка руками, и сверка ровно ими и чинит — ставит извлечение заново
    поверх сдохшего. Возвращается id той работы, что в итоге стоит в очереди.
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
    # Аренда берётся тем же условием, по которому работа выбиралась. Раздельные
    # `select` и `update` два воркера проходили оба, и час GPU уходил дважды —
    # та же болезнь, что вылечена в `finish_stored`. Проигравший вернётся сюда
    # следующим тиком: работа никуда не делась, а спешить некуда.
    if con.execute("update jobs set lease_until=?, updated=? "
                   "where id=? and lease_until<?",
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


def blob_path(root, sha256, ext, when=None):
    """Путь блоба: год и месяц в дереве, имя — хеш. Оригинальное имя из телефона
    ключом доверия не является (ТЗ §7)."""
    d = when or datetime.now(TZ)
    return os.path.join(root, "calls", "%04d" % d.year, "%02d" % d.month,
                        "%s.%s" % (sha256, (ext or "bin").lstrip(".")))


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
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


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
    print("mara_ingest self-check: ок")
    return 0


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        raise SystemExit(self_check())
    print("mara_ingest: библиотека, запускать нечего (есть --self-check)")
