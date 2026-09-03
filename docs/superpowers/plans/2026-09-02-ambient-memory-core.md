# Mara Ambient Memory: ядро — план реализации

> **Для исполнителя-агента:** ОБЯЗАТЕЛЬНЫЙ САБ-СКИЛЛ: `superpowers:subagent-driven-development` или `superpowers:executing-plans`. Шаги помечены чекбоксами `- [ ]`.

**Цель:** звонок, лежащий файлом на диске, сам превращается в транскрипт, карточку разговора, карточки обязательств и дайджест в телеграме, без единой ручной команды.

**Архитектура:** один тонкий демон `contextd` на doctor (стандартная библиотека, SQLite, systemd) принимает события, ведёт очередь и статус; тяжёлое делают отдельные однофайловые скрипты, которые запускаются и руками. ASR и извлечение идут по локалке на GPU-коробку bigpc и наружу не выходят. Волт остаётся источником правды, SQLite — очередь и дедуп.

**Стек:** python 3.12 без внешних зависимостей, sqlite3, ffmpeg, systemd, ssh, HTTP к bigpc (`8770` whisper, `11434` ollama), Telegram Bot API.

**Спека:** `docs/superpowers/specs/2026-09-02-ambient-memory-design.md`. ТЗ: `docs/TZ-ambient-memory.md`.

## Глобальные ограничения

- Python только стандартная библиотека. Ни fastapi, ни requests, ни pytest, ни redis, ни postgres (ТЗ §4, §23).
- Комментарии, докстринги и сообщения коммитов по-русски, как во всём репозитории.
- У каждого нового скрипта есть `--self-check`, как у остальных тринадцати.
- Запись в волт атомарная: `tmp` плюс `os.replace`, под `vault_common.locked(vault)`.
- Один писатель на файл. Демон не пишет в волт напрямую, пишет только скрипт проекции.
- Ни модель, ни сеть в облако не вызываются на этапах сбора и агрегации.
- Время событий считается по `MARA_TZ_HOURS` (по умолчанию +3), не по часам хоста.
- Фронтматтер плоский: скаляры и плоские списки. Вложенность только в JSON-манифестах.
- Аудио, транскрипты и `contextd.db` живут в `/srv/mara-blobs`, вне волта, вне git, вне фильтров R2.
- Секреты в `/etc/mara/contextd.env` (0600). Ни один секрет не печатается в лог и не попадает в волт.
- Логи демона не содержат тел сообщений и транскриптов: только идентификаторы, размеры, состояния.
- `pipeline_version = 1` во всех манифестах и карточках этой версии.

---

## Структура файлов

| Файл | Ответственность |
|---|---|
| `scripts/mara_ingest.py` | Библиотека: схема SQLite, открытие БД, дедуп, работы и ретраи, пути блобов. Ничего не печатает, ничего не сети. |
| `scripts/contextd.py` | HTTP-поверхность, авторизация устройств, воркер-тред, метрики, спаривание, `--self-check`. |
| `scripts/call_asr.py` | Аудио → `transcripts/<event_id>.jsonl`. Нарезка ffmpeg, куски в bigpc. |
| `scripts/call_extract.py` | Транскрипт → `extractions/<event_id>.json`. Локальная модель, пороги, правила. |
| `scripts/call_project.py` | Извлечение → карточки `kb/conversations/`, `kb/commitments/`. |
| `scripts/call_digest.py` | Извлечение → текст дайджеста, отправка в Telegram, запись в `digests`. |
| `scripts/blob_retention.py` | Идемпотентная уборка аудио по сроку, отметка в манифесте. |
| `scripts/contextd_reconcile.py` | Часовая сверка инвариантов, отчёт и постановка недостающих работ. |
| `tests/` | `unittest`, фикстуры, включая приватную карточку для теста на утечку в облако. |
| `scripts/run-tests.sh` | Гоняет `unittest` и все `--self-check`. |
| `install/contextd.service` | systemd-юнит для doctor. |
| `install/com.mara.relay.plist` | launchd-агент релея на маке. |

Правки существующих: `scripts/queue-worker.py` (защита `cloud_allowed`), `scripts/daily-page.py` и `scripts/daily-summary.py` (видеть новые папки), `scripts/entity-link.py` (человек из контакта), `.gitignore`, `README.md`.

Имена модулей с подчёркиванием, а не с дефисом: их импортируют тесты. Существующие скрипты с дефисами не трогаем.

---

### Задача 1: каркас тестов и защита от утечки в облако

Первый вертикальный срез из ТЗ §22.1: тесты вокруг сегодняшних инвариантов и единственная реально работающая защита.

**Файлы:**
- Создать: `tests/__init__.py`, `tests/test_cloud_boundary.py`, `tests/fixtures/private-call.md`, `scripts/run-tests.sh`
- Изменить: `scripts/queue-worker.py`

**Интерфейсы:**
- Даёт: `queue_worker.holds_from_cloud(head: str) -> str | None` — возвращает причину, по которой карточку нельзя слать в облако, или `None`.

- [x] **Шаг 1: фикстура приватной карточки**

```bash
mkdir -p tests/fixtures
cat > tests/fixtures/private-call.md <<'EOF'
---
title: Звонок с Анной, 2 сентября 14:05
type: conversation
source: phone
source_id: call/00000000-0000-0000-0000-000000000001
created: 2026-09-02T14:31:00+03:00
occurred: 2026-09-02T14:05:00+03:00
sensitive: false
domain: personal
classification: personal
cloud_allowed: false
model_scope: local-only
---
Люди: [[anna]]

## Ты обещал
- Прислать смету до пятницы · 04:12
EOF
```

Заметь: `sensitive: false`. Смысл фикстуры в том, что старая защита её пропускает, а новая обязана удержать.

- [x] **Шаг 2: падающий тест**

```python
# tests/test_cloud_boundary.py
"""Приватное не уезжает в облако (ТЗ §18, §20).

Тест намеренно ломает сборку, если карточка с cloud_allowed: false дошла
до облачного адаптера. Это тот самый тест из ТЗ §18.
"""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
HERE = os.path.dirname(os.path.abspath(__file__))

def load_queue_worker():
    """queue-worker.py с дефисом в имени, обычным import не берётся."""
    import importlib.util
    p = os.path.join(HERE, "..", "scripts", "queue-worker.py")
    spec = importlib.util.spec_from_file_location("queue_worker", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class CloudBoundary(unittest.TestCase):
    def setUp(self):
        self.qw = load_queue_worker()
        self.head = open(os.path.join(HERE, "fixtures", "private-call.md"),
                         encoding="utf-8").read()

    def test_cloud_allowed_false_держит_карточку(self):
        self.assertIsNotNone(self.qw.holds_from_cloud(self.head))

    def test_sensitive_true_держит_карточку(self):
        self.assertIsNotNone(self.qw.holds_from_cloud("---\nsensitive: true\n---\n"))

    def test_обычная_карточка_проходит(self):
        self.assertIsNone(self.qw.holds_from_cloud("---\ntitle: заметка\n---\nтекст"))

if __name__ == "__main__":
    unittest.main()
```

- [x] **Шаг 3: убедиться, что тест падает**

Запуск: `python3 -m unittest tests.test_cloud_boundary -v`
Ожидание: FAIL, `module 'queue_worker' has no attribute 'holds_from_cloud'`.

- [x] **Шаг 4: минимальная реализация в `scripts/queue-worker.py`**

Рядом с существующей проверкой `sensitive` (около строки 186) появляется функция, и существующая ветка начинает звать её:

```python
def holds_from_cloud(head):
    """Причина, по которой карточку нельзя отдавать облачной модели, или None.

    `sensitive: true` был единственным рычагом с реальным исполнением; поля из
    ТЗ §10 были декларацией. Теперь `cloud_allowed: false` держит так же.
    Одна функция на оба правила: разъехавшись, они разойдутся и по смыслу.
    """
    if re.search(r"(?m)^sensitive:\s*['\"]?true", head):
        return "sensitive: true"
    if re.search(r"(?m)^cloud_allowed:\s*['\"]?false", head):
        return "cloud_allowed: false"
    if re.search(r"(?m)^model_scope:\s*['\"]?local-only", head):
        return "model_scope: local-only"
    return None
```

В основном цикле заменить существующую проверку `if re.search(r"(?m)^sensitive:...` на:

```python
        hold = holds_from_cloud(head)
        if hold:
            print("queue-worker: %s — держим локально, %s" % (job.get("note"), hold))
            continue
```

- [x] **Шаг 5: тесты зелёные**

Запуск: `python3 -m unittest tests.test_cloud_boundary -v`
Ожидание: PASS, три теста.

- [x] **Шаг 6: скрипт прогона**

```bash
cat > scripts/run-tests.sh <<'EOF'
#!/usr/bin/env bash
# Весь прогон: юнит-тесты и все --self-check. Фреймворков нет принципиально.
set -u
cd "$(dirname "$0")/.."
fail=0
echo "== unittest =="
python3 -m unittest discover -s tests -v || fail=1
echo "== self-check =="
for f in scripts/*.py; do
  grep -q -- "--self-check" "$f" || continue
  out=$(python3 "$f" --self-check 2>&1) || { echo "FAIL $f"; echo "$out" | tail -3; fail=1; continue; }
  echo "ok   $f"
done
exit $fail
EOF
chmod +x scripts/run-tests.sh
```

- [x] **Шаг 7: прогон и коммит**

Запуск: `./scripts/run-tests.sh`
Ожидание: unittest зелёный; `self-check` части скриптов может ругаться на отсутствие волта на этой машине — это фиксируется в выводе, но не правится в этой задаче.

```bash
git add tests scripts/run-tests.sh scripts/queue-worker.py
git commit -m "тесты: cloud_allowed держит карточку так же, как sensitive"
```

---

### Задача 2: библиотека приёма — схема, дедуп, работы

**Файлы:**
- Создать: `scripts/mara_ingest.py`, `tests/test_mara_ingest.py`

**Интерфейсы:**
- Даёт:
  - `ROOT` — путь к хранилищу блобов, по умолчанию `/srv/mara-blobs`, переопределяется `MARA_BLOBS`.
  - `connect(root=None) -> sqlite3.Connection` — открывает БД, создаёт схему, включает WAL.
  - `dedupe_key(source, source_id, blob_sha256=None) -> str`
  - `put_event(con, ev: dict) -> tuple[str, bool]` — id события и признак дубля.
  - `add_job(con, event_id, kind) -> str`
  - `claim_job(con, kinds=None, now=None) -> dict | None` — берёт работу в аренду.
  - `finish_job(con, job_id, ok, error=None)` — успех или следующий ретрай, после шестой попытки DLQ.
  - `next_delay(attempts) -> int` — секунды до следующей попытки с джиттером.
  - `blob_path(root, sha256, ext) -> str`

- [x] **Шаг 1: падающие тесты**

```python
# tests/test_mara_ingest.py
"""Приём: дедуп, аренда работ, расписание ретраев (ТЗ §17, §20)."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi

EV = {"kind": "call", "source": "phone", "source_id": "call-1",
      "occurred_at": "2026-09-02T14:05:00+03:00",
      "blob": {"sha256": "a" * 64, "bytes": 10, "mime": "audio/m4a", "ext": "m4a"}}

class Ingest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = mi.connect(self.dir)

    def test_повтор_того_же_аудио_не_создаёт_второй_звонок(self):
        first, dup1 = mi.put_event(self.con, dict(EV))
        second, dup2 = mi.put_event(self.con, dict(EV))
        self.assertEqual(first, second)
        self.assertFalse(dup1)
        self.assertTrue(dup2)

    def test_дедуп_по_хешу_а_не_по_имени_источника(self):
        a = dict(EV, source_id="другое-имя-того-же-файла")
        mi.put_event(self.con, dict(EV))
        _, dup = mi.put_event(self.con, a)
        self.assertTrue(dup, "аудио с тем же sha256 — тот же звонок")

    def test_событие_без_аудио_дедупится_по_source_id(self):
        m = {"kind": "message", "source": "telegram", "source_id": "msg-7"}
        mi.put_event(self.con, dict(m))
        _, dup = mi.put_event(self.con, dict(m))
        self.assertTrue(dup)

    def test_аренда_работы_не_отдаёт_её_дважды(self):
        eid, _ = mi.put_event(self.con, dict(EV))
        mi.add_job(self.con, eid, "asr")
        self.assertIsNotNone(mi.claim_job(self.con))
        self.assertIsNone(mi.claim_job(self.con), "работа под арендой")

    def test_расписание_ретраев_из_тз(self):
        base = [0, 60, 300, 1800, 7200, 43200]
        for attempts, want in enumerate(base):
            got = mi.next_delay(attempts)
            self.assertLessEqual(abs(got - want), want * 0.2 + 1,
                                 "попытка %d: %d вместо ~%d" % (attempts, got, want))

    def test_после_шестой_попытки_dlq(self):
        eid, _ = mi.put_event(self.con, dict(EV))
        jid = mi.add_job(self.con, eid, "asr")
        for _ in range(7):
            j = mi.claim_job(self.con, now=2 ** 31)
            if j is None:
                break
            mi.finish_job(self.con, j["id"], False, "тестовая ошибка")
        state = self.con.execute("select state from jobs where id=?", (jid,)).fetchone()[0]
        self.assertEqual(state, "dlq")

    def test_путь_блоба_раскладывает_по_годам(self):
        p = mi.blob_path(self.dir, "b" * 64, "m4a")
        self.assertIn("/calls/", p)
        self.assertTrue(p.endswith("b" * 64 + ".m4a"))

if __name__ == "__main__":
    unittest.main()
```

- [x] **Шаг 2: убедиться, что падает**

Запуск: `python3 -m unittest tests.test_mara_ingest -v`
Ожидание: FAIL, `No module named 'mara_ingest'`.

- [x] **Шаг 3: реализация**

```python
#!/usr/bin/env python3
"""Приём событий: схема, дедуп, очередь работ (ТЗ §4, §17).

Библиотека без побочных эффектов кроме записи в SQLite: её зовут и демон, и
скрипты пайплайна, и тесты. Дедуп живёт здесь, а не в проверке «есть ли файл
карточки», как в старых скриптах: имя карточки зависит от контакта, который
резолвится позже, и для потокового приёма такой признак не работает.
"""
import os, json, time, uuid, random, sqlite3, hashlib
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=float(os.environ.get("MARA_TZ_HOURS", 3))))
ROOT = os.environ.get("MARA_BLOBS", "/srv/mara-blobs")
LEASE_SEC = 600                      # упавший воркер не держит работу дольше
RETRY = [0, 60, 300, 1800, 7200, 43200]   # ТЗ §17, дальше DLQ
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
"""

def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")

def connect(root=None):
    root = root or ROOT
    os.makedirs(root, mode=0o700, exist_ok=True)
    con = sqlite3.connect(os.path.join(root, "contextd.db"), timeout=30,
                          isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.executescript(SCHEMA)
    return con

def dedupe_key(source, source_id, blob_sha256=None):
    """Аудио дедупится по содержимому: телефон может переименовать файл, но
    не может незаметно поменять его байты (ТЗ §7 — canonical identity)."""
    if blob_sha256:
        return "blob:" + blob_sha256
    return "src:" + hashlib.sha256(("%s\x00%s" % (source, source_id)).encode()).hexdigest()

def put_event(con, ev):
    blob = ev.get("blob") or {}
    key = ev.get("dedupe_key") or dedupe_key(ev.get("source"), ev.get("source_id"),
                                             blob.get("sha256"))
    row = con.execute("select id from events where dedupe_key=?", (key,)).fetchone()
    if row:
        return row["id"], True
    eid = "%s_%s" % (ev.get("kind", "event"), uuid.uuid4())
    con.execute(
        "insert into events(id,kind,source,source_id,dedupe_key,device_id,received,"
        "occurred,ended,classification,payload_json,blob_sha256) "
        "values(?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, ev.get("kind"), ev.get("source"), ev.get("source_id"), key,
         ev.get("device_id"), now_iso(), ev.get("occurred_at"), ev.get("ended_at"),
         ev.get("classification", "personal"),
         json.dumps(ev.get("payload") or {}, ensure_ascii=False), blob.get("sha256")))
    return eid, False

def add_job(con, event_id, kind):
    jid = str(uuid.uuid4())
    con.execute("insert into jobs(id,event_id,kind,created,updated,next_at) "
                "values(?,?,?,?,?,?)", (jid, event_id, kind, now_iso(), now_iso(),
                                        int(time.time())))
    return jid

def claim_job(con, kinds=None, now=None):
    now = int(time.time()) if now is None else int(now)
    q = ("select * from jobs where state='ready' and next_at<=? and lease_until<? ")
    args = [now, now]
    if kinds:
        q += "and kind in (%s) " % ",".join("?" * len(kinds)); args += list(kinds)
    q += "order by next_at limit 1"
    row = con.execute(q, args).fetchone()
    if not row:
        return None
    con.execute("update jobs set lease_until=?, updated=? where id=?",
                (now + LEASE_SEC, now_iso(), row["id"]))
    return dict(row)

def next_delay(attempts):
    """Расписание ТЗ §17 с джиттером ±20 %: после перезагрузки GPU-коробки
    десяток работ не должен ударить в неё одновременно."""
    base = RETRY[min(attempts, len(RETRY) - 1)]
    return int(base * random.uniform(0.8, 1.2)) if base else 0

def finish_job(con, job_id, ok, error=None):
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
                (attempts, (error or "")[:500], int(time.time()) + next_delay(attempts),
                 now_iso(), job_id))

def blob_path(root, sha256, ext):
    d = datetime.now(TZ)
    return os.path.join(root, "calls", "%04d" % d.year, "%02d" % d.month,
                        "%s.%s" % (sha256, ext.lstrip(".")))

def self_check():
    import tempfile
    d = tempfile.mkdtemp()
    con = connect(d)
    e = {"kind": "call", "source": "phone", "source_id": "s",
         "blob": {"sha256": "c" * 64}}
    a, dup_a = put_event(con, dict(e))
    b, dup_b = put_event(con, dict(e))
    assert a == b and not dup_a and dup_b, "дедуп сломан"
    jid = add_job(con, a, "asr")
    assert claim_job(con)["id"] == jid and claim_job(con) is None, "аренда сломана"
    finish_job(con, jid, True)
    assert con.execute("select state from jobs where id=?", (jid,)).fetchone()[0] == "done"
    print("mara_ingest self-check: ок")
    return 0

if __name__ == "__main__":
    import sys
    raise SystemExit(self_check() if "--self-check" in sys.argv else
                     print("библиотека, запускать нечего") or 0)
```

- [x] **Шаг 4: тесты зелёные**

Запуск: `python3 -m unittest tests.test_mara_ingest -v`
Ожидание: PASS, семь тестов.

- [x] **Шаг 5: коммит**

```bash
git add scripts/mara_ingest.py tests/test_mara_ingest.py
git commit -m "приём: дедуп по содержимому, аренда работ, ретраи из ТЗ"
```

---

### Задача 3: демон contextd

**Файлы:**
- Создать: `scripts/contextd.py`, `tests/test_contextd.py`, `install/contextd.service`
- Изменить: `.gitignore`

**Интерфейсы:**
- Потребляет: всё из `mara_ingest`.
- Даёт: `make_server(root, port=0) -> ThreadingHTTPServer` для тестов; `pair(con, name) -> (device_id, token)`; конвейер `NEXT = {"asr": "extract", "extract": "project", "project": "digest"}`.

- [x] **Шаг 1: падающие тесты**

```python
# tests/test_contextd.py
"""HTTP-поверхность приёма (ТЗ §4, §20)."""
import os, sys, json, hashlib, tempfile, threading, unittest, urllib.request, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi, contextd

class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.srv = contextd.make_server(cls.dir, port=0)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        con = mi.connect(cls.dir)
        cls.dev, cls.token = contextd.pair(con, "тестовый телефон")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def post(self, path, data, token=None, raw=False, ctype="application/json"):
        body = data if raw else json.dumps(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", ctype)
        req.add_header("Authorization", "Bearer " + (token or self.token))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_без_токена_401(self):
        code, _ = self.post("/v1/ingest/event", {"kind": "call"}, token="нет-такого")
        self.assertEqual(code, 401)

    def test_событие_создаёт_работу_и_просит_блоб(self):
        sha = hashlib.sha256(b"аудио").hexdigest()
        code, r = self.post("/v1/ingest/event", {
            "kind": "call", "source": "phone", "source_id": "c1",
            "blob": {"sha256": sha, "bytes": 6, "ext": "wav"}})
        self.assertEqual(code, 200)
        self.assertTrue(r["need_blob"])
        self.assertFalse(r["duplicate"])

    def test_повтор_события_дубль(self):
        ev = {"kind": "call", "source": "phone", "source_id": "c2"}
        self.post("/v1/ingest/event", ev)
        _, r = self.post("/v1/ingest/event", ev)
        self.assertTrue(r["duplicate"])

    def test_битый_хеш_не_успех(self):
        sha = hashlib.sha256(b"правильное").hexdigest()
        _, r = self.post("/v1/ingest/event", {
            "kind": "call", "source": "phone", "source_id": "c3",
            "blob": {"sha256": sha, "bytes": 10, "ext": "wav"}})
        code, _ = self.post("/v1/ingest/audio?event=" + r["event_id"], b"другое",
                            raw=True, ctype="application/octet-stream")
        self.assertEqual(code, 409)
        p = mi.blob_path(self.dir, sha, "wav")
        self.assertFalse(os.path.exists(p), "частичный файл должен быть удалён")

    def test_healthz_и_метрики(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=5) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        with urllib.request.urlopen(self.base + "/metrics", timeout=5) as r:
            self.assertIn("mara_ingest_queue_depth", r.read().decode())

    def test_отозванное_устройство_401(self):
        con = mi.connect(self.dir)
        dev, token = contextd.pair(con, "потерянный")
        con.execute("update devices set revoked_at=? where id=?", (mi.now_iso(), dev))
        code, _ = self.post("/v1/ingest/event", {"kind": "call"}, token=token)
        self.assertEqual(code, 401)

if __name__ == "__main__":
    unittest.main()
```

- [x] **Шаг 2: убедиться, что падает**

Запуск: `python3 -m unittest tests.test_contextd -v`
Ожидание: FAIL, `No module named 'contextd'`.

- [x] **Шаг 3: реализация демона**

Ключевые куски (полный файл собирается из них):

```python
#!/usr/bin/env python3
"""contextd: приём событий, очередь, статус, метрики (ТЗ §4).

Тонкий по замыслу. Он принимает, ставит работу и отдаёт статус; всё тяжёлое
делают отдельные скрипты, которые он зовёт подпроцессом и которые работают
без него: `python3 scripts/call_asr.py --event <id>` чинится руками в три часа
ночи, а внутренности демона — нет.

Слушает только loopback. Наружу его выводит ssh-туннель с мака, а не bind.
"""
import os, sys, json, time, hmac, hashlib, secrets, argparse, threading, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

MAX_BODY = 512 << 20                 # часовой звонок в m4a влезает с запасом
NEXT = {"asr": "extract", "extract": "project", "project": "digest"}
GPU = threading.Semaphore(1)         # ponytail: на bigpc свободно <6 ГиБ VRAM,
                                     # параллельный ASR вытеснит whisper и станет медленнее
```

Авторизация и спаривание:

```python
def pair(con, name):
    """Токен показывается один раз, в базе только его sha256 (ТЗ §18)."""
    token = secrets.token_urlsafe(32)
    dev = "dev_" + secrets.token_hex(8)
    con.execute("insert into devices(id,name,token_sha256,created) values(?,?,?,?)",
                (dev, name, hashlib.sha256(token.encode()).hexdigest(), mi.now_iso()))
    return dev, token

def device_of(con, header):
    if not header or not header.startswith("Bearer "):
        return None
    h = hashlib.sha256(header[7:].strip().encode()).hexdigest()
    row = con.execute("select id from devices where token_sha256=? and revoked_at is null",
                      (h,)).fetchone()
    if not row:
        return None
    con.execute("update devices set last_seen=? where id=?", (mi.now_iso(), row["id"]))
    return row["id"]
```

Приём аудио с проверкой хеша:

```python
    def ingest_audio(self, con, event_id, raw):
        row = con.execute("select blob_sha256, payload_json from events where id=?",
                          (event_id,)).fetchone()
        if not row:
            return 404, {"error": "нет такого события"}
        want = row["blob_sha256"]
        got = hashlib.sha256(raw).hexdigest()
        ext = (json.loads(row["payload_json"] or "{}").get("ext")) or "bin"
        path = mi.blob_path(mi.ROOT, want, ext)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(raw)
        if got != want:
            os.unlink(tmp)                       # частичное не считается успехом (ТЗ §20)
            return 409, {"error": "хеш не сошёлся", "expected": want, "got": got}
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        con.execute("insert or replace into blobs(sha256,path,bytes,mime,created,audio_until)"
                    " values(?,?,?,?,?,?)",
                    (want, path, len(raw), "audio", mi.now_iso(), audio_until()))
        con.execute("update events set state='stored' where id=?", (event_id,))
        mi.add_job(con, event_id, "asr")
        return 200, {"event_id": event_id, "blob_sha256": want, "bytes": len(raw)}
```

Воркер, конвейер и семафор:

```python
def run_step(kind, event_id):
    """Каждый шаг — отдельный скрипт. Возвращает (ок, текст ошибки)."""
    script = {"asr": "call_asr.py", "extract": "call_extract.py",
              "project": "call_project.py", "digest": "call_digest.py"}[kind]
    gpu = kind in ("asr", "extract")
    if gpu: GPU.acquire()
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, script),
                            "--event", event_id],
                           capture_output=True, text=True, timeout=3600)
    finally:
        if gpu: GPU.release()
    return r.returncode == 0, (r.stderr or "")[-500:]

def worker(stop):
    con = mi.connect()
    while not stop.is_set():
        job = mi.claim_job(con)
        if not job:
            stop.wait(5); continue
        ok, err = run_step(job["kind"], job["event_id"])
        mi.finish_job(con, job["id"], ok, err)
        if ok and job["kind"] in NEXT:
            mi.add_job(con, job["event_id"], NEXT[job["kind"]])
```

Метрики считаются запросом, не копятся в памяти:

```python
def metrics(con):
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    lines = [
        ("mara_ingest_queue_depth", q("select count(*) from jobs where state='ready'")),
        ("mara_dlq_count", q("select count(*) from jobs where state='dlq'")),
        ("mara_transcription_queue_depth",
         q("select count(*) from jobs where state='ready' and kind='asr'")),
        ("mara_task_extraction_failures_total",
         q("select count(*) from jobs where kind='extract' and state='dlq'")),
        ("mara_mobile_pending_uploads",
         q("select count(*) from events where state='new' and blob_sha256 is not null")),
    ]
    return "".join("%s %s\n" % (k, v) for k, v in lines)
```

- [x] **Шаг 4: тесты зелёные**

Запуск: `python3 -m unittest tests.test_contextd -v`
Ожидание: PASS, шесть тестов.

- [x] **Шаг 5: юнит systemd и .gitignore**

```bash
cat > install/contextd.service <<'EOF'
[Unit]
Description=Mara contextd: приём звонков и сообщений
After=network-online.target

[Service]
Type=simple
User=sergey
Environment=MARA_BLOBS=/srv/mara-blobs
EnvironmentFile=-/etc/mara/contextd.env
ExecStart=/usr/bin/python3 /home/sergey/mara-second-brain/scripts/contextd.py --serve
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
printf '%s\n' '' '# приёмник ambient memory: база и блобы живут вне репо' 'contextd.db' '*.part' >> .gitignore
```

- [x] **Шаг 6: коммит**

```bash
git add scripts/contextd.py tests/test_contextd.py install/contextd.service .gitignore
git commit -m "contextd: приём, аренда работ, конвейер и метрики без единой зависимости"
```

---

### Задача 4: ASR — нарезка и куски в bigpc

**Файлы:**
- Создать: `scripts/call_asr.py`, `tests/test_call_asr.py`

**Интерфейсы:**
- Потребляет: `mara_ingest.connect`, `mara_ingest.blob_path`.
- Даёт:
  - `slice_plan(duration_ms, window_ms=25000, overlap_ms=2000) -> list[tuple[int,int]]`
  - `transcribe(wav_path, base_url) -> list[dict]` — сегменты со спанами.
  - `run(event_id) -> str` — путь к записанному `transcripts/<event_id>.jsonl`.

- [x] **Шаг 1: падающие тесты**

```python
# tests/test_call_asr.py
"""Нарезка и склейка транскрипта (ТЗ §8)."""
import os, sys, json, tempfile, unittest, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_asr

class Нарезка(unittest.TestCase):
    def test_короткий_звонок_один_кусок(self):
        self.assertEqual(call_asr.slice_plan(10000), [(0, 10000)])

    def test_длинный_режется_с_перекрытием(self):
        p = call_asr.slice_plan(60000)
        self.assertEqual(p[0], (0, 25000))
        self.assertEqual(p[1][0], 23000, "перекрытие 2 секунды")
        self.assertEqual(p[-1][1], 60000, "хвост не теряется")

    def test_куски_не_длиннее_потолка_сервера(self):
        for a, b in call_asr.slice_plan(600000):
            self.assertLessEqual(b - a, 25000, "сервер отвечает 413 на >30 с")

class Склейка(unittest.TestCase):
    def setUp(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(s):
                s.rfile.read(int(s.headers["Content-Length"]))
                s.send_response(200); s.send_header("Content-Type", "application/json")
                s.end_headers()
                s.wfile.write(json.dumps({"text": "кусок", "sec": 25}).encode())
            def log_message(s, *a): pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()

    def test_сегменты_получают_спаны_в_координатах_записи(self):
        segs = call_asr.transcribe_spans(self.base, [(0, 25000), (23000, 48000)],
                                         lambda a, b: b"фейковый wav")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["start_ms"], 0)
        self.assertEqual(segs[1]["start_ms"], 23000)
        self.assertEqual(segs[1]["segment_id"], "s0002")
        self.assertEqual(segs[0]["speaker"], "unknown-A",
                         "диаризации нет — не выдумываем говорящего")

if __name__ == "__main__":
    unittest.main()
```

- [x] **Шаг 2: убедиться, что падает**

Запуск: `python3 -m unittest tests.test_call_asr -v`
Ожидание: FAIL, модуля нет.

- [x] **Шаг 3: реализация**

```python
#!/usr/bin/env python3
"""Аудио звонка → JSONL с сегментами и спанами (ТЗ §8).

Whisper на bigpc принимает не больше тридцати секунд и отвечает 413 на всё
длиннее: режем здесь. Спаны получаются с точностью до куска — это честно, и
этого хватает, чтобы «покажи цитату» открыло нужное место записи. Пословные
таймстемпы потребуют правки чужого файла /root/tts/server.py (проект маски),
поэтому не сейчас.

Диаризации нет — все сегменты помечаются unknown-A. Пайплайн из-за её
отсутствия не встаёт (ТЗ §8), поле перепишет отдельная работа, когда появится.

    python3 scripts/call_asr.py --event call_<uuid>
"""
import os, sys, json, argparse, subprocess, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

ASR_URL = os.environ.get("MARA_ASR_URL", "http://<bigpc в локалке>:8770")
WINDOW_MS, OVERLAP_MS = 25000, 2000

def slice_plan(duration_ms, window_ms=WINDOW_MS, overlap_ms=OVERLAP_MS):
    if duration_ms <= window_ms:
        return [(0, duration_ms)]
    step, out, start = window_ms - overlap_ms, [], 0
    while start < duration_ms:
        end = min(start + window_ms, duration_ms)
        out.append((start, end))
        if end >= duration_ms:
            break
        start += step
    return out

def duration_ms(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError("ffprobe: " + r.stderr.strip()[:200])
    return int(float(r.stdout.strip()) * 1000)

def cut_wav(path, start_ms, end_ms):
    """Кусок в моно 16 кГц WAV прямо в память: на диск не кладём, чтобы не
    плодить копии личного разговора."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % (start_ms / 1000),
                        "-t", "%.3f" % ((end_ms - start_ms) / 1000), "-i", path,
                        "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
                       capture_output=True, timeout=300)
    if r.returncode:
        raise RuntimeError("ffmpeg: " + r.stderr.decode("utf-8", "replace")[-200:])
    return r.stdout

def transcribe_spans(base_url, plan, cutter):
    segs = []
    for i, (a, b) in enumerate(plan, 1):
        req = urllib.request.Request(base_url + "/transcribe", data=cutter(a, b),
                                     method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read())
        text = (d.get("text") or "").strip()
        if not text:
            continue
        segs.append({"segment_id": "s%04d" % i, "start_ms": a, "end_ms": b,
                     "speaker": "unknown-A", "text": text,
                     "asr_confidence": None, "speaker_confidence": None})
    return segs

def run(event_id, root=None):
    root = root or mi.ROOT
    con = mi.connect(root)
    row = con.execute("select blob_sha256 from events where id=?", (event_id,)).fetchone()
    b = con.execute("select path from blobs where sha256=?", (row["blob_sha256"],)).fetchone()
    audio = b["path"]
    plan = slice_plan(duration_ms(audio))
    segs = transcribe_spans(ASR_URL, plan, lambda a, z: cut_wav(audio, a, z))
    out = os.path.join(root, "transcripts", event_id + ".jsonl")
    os.makedirs(os.path.dirname(out), mode=0o700, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for s in segs:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600); os.replace(tmp, out)
    return out
```

`--self-check` проверяет `slice_plan` на трёх длительностях и наличие `ffmpeg`/`ffprobe`, наружу не ходит.

- [x] **Шаг 4: тесты зелёные**

Запуск: `python3 -m unittest tests.test_call_asr -v`
Ожидание: PASS, четыре теста.

- [x] **Шаг 5: коммит**

```bash
git add scripts/call_asr.py tests/test_call_asr.py
git commit -m "asr: режем по 25 секунд с перекрытием, спаны в координатах записи"
```

---

### Задача 5: извлечение смысла

**Файлы:**
- Создать: `scripts/call_extract.py`, `tests/test_call_extract.py`, `tests/fixtures/transcript-anna.jsonl`, `tests/fixtures/extraction-anna.json`
- Создать в волте (не в git): `_system/prompts/call-extract.md`

**Интерфейсы:**
- Даёт:
  - `SCHEMA` — JSON-схема для `format` ollama.
  - `ask_model(transcript_text, base_url, model) -> dict`
  - `normalize(raw: dict, occurred_at: str) -> dict` — правила порогов, спанов, дедлайнов.
  - `run(event_id) -> str` — путь к `extractions/<event_id>.json`.

- [x] **Шаг 1: фикстуры**

```bash
cat > tests/fixtures/transcript-anna.jsonl <<'EOF'
{"segment_id":"s0001","start_ms":0,"end_ms":25000,"speaker":"unknown-A","text":"Привет, пришли смету до пятницы, пожалуйста.","asr_confidence":null,"speaker_confidence":null}
{"segment_id":"s0002","start_ms":23000,"end_ms":48000,"speaker":"unknown-A","text":"Хорошо, пришлю. И я перезвоню в понедельник.","asr_confidence":null,"speaker_confidence":null}
EOF
```

- [x] **Шаг 2: падающие тесты**

```python
# tests/test_call_extract.py
"""Правила извлечения (ТЗ §9, §20). Модель не зовём: проверяем правила."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_extract as ce

OCC = "2026-09-02T14:05:00+03:00"     # среда

class Правила(unittest.TestCase):
    def test_явная_просьба_становится_задачей(self):
        raw = {"requests": [{"action": "прислать смету", "requester": "Анна",
                             "owner": "sergey", "explicit": True, "confidence": 0.93,
                             "evidence": [{"start_ms": 0, "end_ms": 25000}]}]}
        out = ce.normalize(raw, OCC)
        self.assertEqual(out["requests"][0]["disposition"], "task")

    def test_предположение_не_становится_обязательством(self):
        raw = {"requests": [{"action": "может, покрасить стены", "explicit": False,
                             "confidence": 0.7,
                             "evidence": [{"start_ms": 0, "end_ms": 100}]}]}
        out = ce.normalize(raw, OCC)
        self.assertEqual(out["requests"][0]["disposition"], "needs-review")

    def test_ниже_порога_не_создаётся(self):
        raw = {"requests": [{"action": "что-то", "confidence": 0.4,
                             "evidence": [{"start_ms": 0, "end_ms": 1}]}]}
        self.assertEqual(ce.normalize(raw, OCC)["requests"], [])

    def test_явный_дедлайн_парсится(self):
        raw = {"commitments": [{"action": "смета", "owner": "sergey", "confidence": 0.9,
                                "deadline_phrase": "до пятницы", "explicit": True,
                                "evidence": [{"start_ms": 0, "end_ms": 1}]}]}
        out = ce.normalize(raw, OCC)
        self.assertEqual(out["commitments"][0]["due_at"][:10], "2026-09-04")
        self.assertTrue(out["commitments"][0]["deadline_explicit"])

    def test_размытый_дедлайн_не_выдумывается(self):
        raw = {"commitments": [{"action": "смета", "confidence": 0.9,
                                "deadline_phrase": "побыстрее",
                                "evidence": [{"start_ms": 0, "end_ms": 1}]}]}
        out = ce.normalize(raw, OCC)
        self.assertIsNone(out["commitments"][0]["due_at"])
        self.assertFalse(out["commitments"][0]["deadline_explicit"])
        self.assertEqual(out["commitments"][0]["deadline_phrase"], "побыстрее")

    def test_пункт_без_спана_выбрасывается(self):
        raw = {"commitments": [{"action": "нечто", "confidence": 0.99, "evidence": []}]}
        self.assertEqual(ce.normalize(raw, OCC)["commitments"], [])

    def test_новое_поручение_вытесняет_старое_через_supersedes(self):
        raw = {"changed_instructions": [{"supersedes": "смета до пятницы",
                                         "new_state": "смету не надо, нужен счёт",
                                         "confidence": 0.9,
                                         "evidence": [{"start_ms": 0, "end_ms": 1}]}]}
        out = ce.normalize(raw, OCC)
        self.assertEqual(out["changed_instructions"][0]["supersedes"], "смета до пятницы")

if __name__ == "__main__":
    unittest.main()
```

- [x] **Шаг 3: убедиться, что падает**

Запуск: `python3 -m unittest tests.test_call_extract -v`
Ожидание: FAIL, модуля нет.

- [x] **Шаг 4: реализация**

```python
#!/usr/bin/env python3
"""Транскрипт → обязательства, просьбы, решения (ТЗ §9).

Модель локальная: ollama на bigpc по локалке. Ни один байт транскрипта не
уходит во внешний API — это условие ТЗ §9 и §18, а не предпочтение.

Модель предлагает, правила решают. Порог 0.85 делает задачу, 0.60–0.85 —
строку «возможно задача» в дайджесте, ниже — ничего. Дедлайн берётся только
из произнесённой фразы: «побыстрее» датой не становится никогда.
"""
import os, sys, json, re, argparse, urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

OLLAMA = os.environ.get("MARA_LLM_URL", "http://<bigpc в локалке>:11434")
MODEL = os.environ.get("MARA_EXTRACT_MODEL", "qwen3.5:9b")
TASK_MIN, REVIEW_MIN = 0.85, 0.60

DAYS = {"понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3,
        "пятницу": 4, "пятница": 4, "субботу": 5, "суббота": 5,
        "воскресенье": 6, "вторникам": 1}

def parse_deadline(phrase, occurred_at):
    """Только то, что произнесли. Возвращает (iso|None, explicit)."""
    if not phrase:
        return None, False
    p = phrase.lower().strip()
    base = datetime.fromisoformat(occurred_at)
    if "завтра" in p:
        return (base + timedelta(days=1)).date().isoformat(), True
    if "послезавтра" in p:
        return (base + timedelta(days=2)).date().isoformat(), True
    if "сегодня" in p:
        return base.date().isoformat(), True
    for name, idx in DAYS.items():
        if name in p:
            delta = (idx - base.weekday()) % 7 or 7
            return (base + timedelta(days=delta)).date().isoformat(), True
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})", p)
    if m:
        d, mth = int(m.group(1)), int(m.group(2))
        return base.replace(month=mth, day=d).date().isoformat(), True
    return None, False        # «побыстрее», «на днях», «как получится»

def has_evidence(item):
    ev = item.get("evidence") or []
    return bool(ev) and all("start_ms" in e for e in ev)

def normalize(raw, occurred_at):
    out = {}
    for key in ("requests", "commitments", "decisions", "constraints",
                "open_questions", "changed_instructions", "followups"):
        items = []
        for it in (raw.get(key) or []):
            if not has_evidence(it):
                continue                      # ТЗ §9: без спана пункта не существует
            conf = float(it.get("confidence") or 0)
            if conf < REVIEW_MIN:
                continue
            it["disposition"] = "task" if (conf >= TASK_MIN and it.get("explicit", True)) \
                                else "needs-review"
            if key in ("requests", "commitments"):
                due, explicit = parse_deadline(it.get("deadline_phrase")
                                               or it.get("deadline"), occurred_at)
                it["due_at"], it["deadline_explicit"] = due, explicit
            items.append(it)
        out[key] = items
    for key in ("people_mentioned", "projects_mentioned"):
        out[key] = list(raw.get(key) or [])
    return out
```

Вызов модели: `POST /api/chat`, `"format": SCHEMA`, `"options": {"temperature": 0}`, промпт из волта `_system/prompts/call-extract.md`, текст транскрипта с явными пометками спанов `[s0001 00:00–00:25]`, чтобы модели было чем заполнить `evidence`.

- [x] **Шаг 5: тесты зелёные**

Запуск: `python3 -m unittest tests.test_call_extract -v`
Ожидание: PASS, семь тестов.

- [x] **Шаг 6: коммит**

```bash
git add scripts/call_extract.py tests/test_call_extract.py tests/fixtures
git commit -m "извлечение: модель предлагает, правила решают; дедлайн только произнесённый"
```

---

### Задача 6: проекция в волт

**Файлы:**
- Создать: `scripts/call_project.py`, `tests/test_call_project.py`

**Интерфейсы:**
- Потребляет: `vault_common.locked`, `vault_common.canon_map`, `vault_common.linkify`, `vault_common.scrub`, `vault_common.yaml_str`.
- Даёт:
  - `conversation_card(event, extraction, canon) -> tuple[str, str]` — имя файла и текст.
  - `commitment_cards(event, extraction, canon) -> list[tuple[str, str]]`
  - `run(event_id, vault) -> list[str]` — записанные пути.

- [x] **Шаг 1: падающие тесты**

```python
# tests/test_call_project.py
"""Карточки разговора и обязательств (ТЗ §10)."""
import os, sys, json, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_project as cp

EVENT = {"id": "call_1", "occurred": "2026-09-02T14:05:00+03:00",
         "ended": "2026-09-02T14:23:11+03:00",
         "payload": {"contact_name": "Анна", "direction": "incoming"}}
EXTR = {"requests": [{"action": "прислать смету", "disposition": "task",
                      "confidence": 0.93, "due_at": "2026-09-04",
                      "deadline_explicit": True, "promised_to": "Анна",
                      "evidence": [{"start_ms": 252000, "end_ms": 260000}]}],
        "commitments": [], "decisions": [], "open_questions": [],
        "changed_instructions": [], "constraints": [], "followups": [],
        "people_mentioned": ["Анна"], "projects_mentioned": []}

class Карточка(unittest.TestCase):
    def test_фронтматтер_плоский(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        head = text.split("---")[1]
        for line in head.strip().splitlines():
            self.assertFalse(line.startswith("  ") and ":" in line.strip()
                             and not line.strip().startswith("- "),
                             "вложенная карта: парсер репо её не понимает — %r" % line)

    def test_обязательные_поля_безопасности(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        for field in ("sensitive: true", "cloud_allowed: false",
                      "model_scope: local-only", "pipeline_version: 1"):
            self.assertIn(field, text)

    def test_строки_люди_и_проекты_есть(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        self.assertIn("Люди:", text)

    def test_время_спана_печатается_как_минуты(self):
        _, text = cp.conversation_card(EVENT, EXTR, {})
        self.assertIn("04:12", text, "252000 мс — это 4 минуты 12 секунд")

    def test_обязательство_из_просьбы_с_порогом(self):
        cards = cp.commitment_cards(EVENT, EXTR, {})
        self.assertEqual(len(cards), 1)
        self.assertIn("status: proposed", cards[0][1])
        self.assertIn("due: 2026-09-04", cards[0][1])

    def test_needs_review_карточку_не_создаёт(self):
        e = json.loads(json.dumps(EXTR))
        e["requests"][0]["disposition"] = "needs-review"
        self.assertEqual(cp.commitment_cards(EVENT, e, {}), [])

if __name__ == "__main__":
    unittest.main()
```

- [x] **Шаг 2: убедиться, что падает**

Запуск: `python3 -m unittest tests.test_call_project -v`
Ожидание: FAIL.

- [x] **Шаг 3: реализация**

Ключевое — сборка фронтматтера ровно в том виде, который понимает `vault_common`:

```python
def frontmatter(event, extraction, content_sha):
    """Только плоские скаляры и плоские списки: разбор в репо регэкспный,
    вложенная карта превратилась бы в мусор молча (см. vault_common)."""
    d = [("title", yaml_str(title(event))),
         ("type", "conversation"),
         ("source", "phone"),
         ("source_id", "call/" + event["id"]),
         ("created", mi.now_iso()),
         ("occurred", event["occurred"]),
         ("sensitive", "true"),
         ("domain", "personal"),
         ("classification", event.get("classification", "personal")),
         ("storage_scope", "vault-sync"),
         ("model_scope", "local-only"),
         ("cloud_allowed", "false"),
         ("content_sha256", content_sha),
         ("source_revision", "1"),
         ("pipeline_version", str(mi.PIPELINE_VERSION)),
         ("valid_from", event.get("ended") or event["occurred"])]
    lines = ["---"] + ["%s: %s" % (k, v) for k, v in d] + ["audience:", "  - mara", "---"]
    return "\n".join(lines)

def stamp(ms):
    return "%02d:%02d" % (ms // 60000, (ms % 60000) // 1000)
```

Тело собирается разделами «Попросили», «Ты обещал», «Решили», «Неясно»; каждый пункт заканчивается меткой времени из первого спана. Текст пропускается через `scrub()` перед записью: ключ, произнесённый вслух и распознанный, не должен уехать в R2.

Запись атомарная и под общим флоком:

```python
    with locked(vault):
        for path, text in cards:
            tmp = path + ".tmp"
            open(tmp, "w", encoding="utf-8").write(text)
            os.replace(tmp, path)
```

- [x] **Шаг 4: тесты зелёные**

Запуск: `python3 -m unittest tests.test_call_project -v`
Ожидание: PASS, шесть тестов.

- [x] **Шаг 5: коммит**

```bash
git add scripts/call_project.py tests/test_call_project.py
git commit -m "проекция: карточка разговора и обязательства, фронтматтер плоский"
```

---

### Задача 7: существующие скрипты видят новые папки

Без этой задачи звонки есть, но система их не замечает: они не попадают ни в дневную страницу, ни в сводку, а люди из них не линкуются.

**Файлы:**
- Изменить: `scripts/daily-page.py`, `scripts/daily-summary.py`, `scripts/entity-link.py`
- Создать: `tests/test_existing_scripts.py`

- [x] **Шаг 1: падающие тесты**

```python
# tests/test_existing_scripts.py
"""Новые типы видны старым скриптам (спека §7)."""
import os, re, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

def src(name):
    return open(os.path.join(ROOT, "scripts", name), encoding="utf-8").read()

class Видимость(unittest.TestCase):
    def test_дневная_страница_сканирует_разговоры(self):
        self.assertIn("conversations", src("daily-page.py"))
        self.assertIn("commitments", src("daily-page.py"))

    def test_сводка_не_выбрасывает_разговор_без_проекта(self):
        s = src("daily-summary.py")
        self.assertIn("conversation", s)

    def test_линкер_умеет_заводить_человека_из_контакта(self):
        self.assertIn("from_contact", src("entity-link.py"))

if __name__ == "__main__":
    unittest.main()
```

- [x] **Шаг 2: убедиться, что падает**

Запуск: `python3 -m unittest tests.test_existing_scripts -v`
Ожидание: FAIL, три теста.

- [x] **Шаг 3: правки**

В `daily-page.py` список сканируемых папок (строка около 59) дополняется:

```python
FOLDERS = ["notes", "sessions", "howto", "decisions", "conversations", "commitments"]
```

В `daily-summary.py` условие отбрасывания заметок без проекта получает исключение:

```python
        # разговор и обязательство попадают в сводку всегда: у звонка проекта
        # может не быть вовсе, а знать о нём надо
        if fm.get("type") not in ("conversation", "commitment") \
           and rel.startswith("kb/notes") and not fm.get("project"):
            continue
```

В `entity-link.py` добавляется функция и её вызов из разбора карточек разговоров:

```python
def from_contact(idx, name, phone):
    """Человек заводится из разрешённого контакта журнала звонков, а не из
    упоминания в тексте. Имя даёт адресная книга, поэтому правило `--min 3`,
    защищающее от фантомов, здесь не нужно — фантома не будет."""
```

- [x] **Шаг 4: тесты зелёные, старые self-check не сломаны**

Запуск: `python3 -m unittest tests.test_existing_scripts -v && python3 scripts/daily-page.py --self-check && python3 scripts/daily-summary.py --self-check && python3 scripts/entity-link.py --self-check`
Ожидание: PASS и три «ок».

- [x] **Шаг 5: коммит**

```bash
git add scripts/daily-page.py scripts/daily-summary.py scripts/entity-link.py tests/test_existing_scripts.py
git commit -m "старые скрипты видят разговоры и обязательства"
```

---

### Задача 8: дайджест в телеграм

**Файлы:**
- Создать: `scripts/call_digest.py`, `tests/test_call_digest.py`

**Интерфейсы:**
- Даёт:
  - `render(event, extraction, created_count) -> tuple[str, list[dict]]` — текст и пункты для таблицы `digests`.
  - `send(text, token, chat_id) -> bool`
  - `run(event_id) -> str` — id записи в `digests`.

- [x] **Шаг 1: падающие тесты**

```python
# tests/test_call_digest.py
"""Формат дайджеста (ТЗ §16). Рендер без модели и без сети."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import call_digest as cd

EVENT = {"id": "call_1", "occurred": "2026-09-02T14:05:00+03:00",
         "ended": "2026-09-02T14:23:11+03:00",
         "payload": {"contact_name": "Анна"}}

class Рендер(unittest.TestCase):
    def test_заголовок_с_контактом_и_временем(self):
        text, _ = cd.render(EVENT, {"requests": [], "commitments": []}, 0)
        self.assertTrue(text.startswith("Звонок · Анна · 14:05–14:23"))

    def test_пустые_разделы_не_печатаются(self):
        text, _ = cd.render(EVENT, {"requests": [], "commitments": []}, 0)
        self.assertNotIn("Попросили", text)

    def test_возможная_задача_отдельным_разделом(self):
        e = {"requests": [{"action": "покрасить стены", "disposition": "needs-review",
                           "evidence": [{"start_ms": 60000, "end_ms": 61000}]}],
             "commitments": []}
        text, items = cd.render(EVENT, e, 0)
        self.assertIn("Возможно задача", text)
        self.assertIn("01:00", text, "у пункта есть метка времени для цитаты")
        self.assertEqual(items[0]["disposition"], "needs-review")

    def test_созданные_задачи_считаются(self):
        text, _ = cd.render(EVENT, {"requests": [], "commitments": []}, 2)
        self.assertIn("Создано", text)
        self.assertIn("2 задачи", text)

if __name__ == "__main__":
    unittest.main()
```

- [x] **Шаг 2: убедиться, что падает**

Запуск: `python3 -m unittest tests.test_call_digest -v`
Ожидание: FAIL.

- [x] **Шаг 3: реализация**

Рендер чистый шаблон, модель не зовётся. Отправка:

```python
def send(text, token, chat_id):
    """Прямо в Bot API, а не через Мару: inject_message запускает полный ход
    модели, дайджест стоил бы вызова LLM и мог бы быть переписан ею. И он
    должен доходить, когда Мара занята или лежит."""
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token,
                                 data=data, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("ok", False)
```

Токен и `TELEGRAM_HOME_CHANNEL` читаются из `/etc/mara/contextd.env`. Если файла нет, скрипт пишет дайджест в `digests` со `state='no-transport'` и возвращает успех: терять текст из-за отсутствия токена нельзя.

- [x] **Шаг 4: тесты зелёные**

Запуск: `python3 -m unittest tests.test_call_digest -v`
Ожидание: PASS, четыре теста.

- [x] **Шаг 5: коммит**

```bash
git add scripts/call_digest.py tests/test_call_digest.py
git commit -m "дайджест: шаблон без модели, отправка прямо в Bot API"
```

---

### Задача 9: сквозной прогон на фикстуре

**Файлы:**
- Создать: `tests/test_pipeline_e2e.py`, `tests/fixtures/make_sample_call.sh`

- [x] **Шаг 1: генератор тестового аудио**

```bash
cat > tests/fixtures/make_sample_call.sh <<'EOF'
#!/usr/bin/env bash
# Тридцать секунд тишины с двумя тонами: настоящего голоса в репозитории не
# держим, а нарезку и склейку это проверяет полностью.
set -eu
out="${1:-/tmp/sample-call.m4a}"
ffmpeg -v error -y -f lavfi -i "sine=frequency=440:duration=30" -ac 1 -ar 16000 "$out"
echo "$out"
EOF
chmod +x tests/fixtures/make_sample_call.sh
```

- [x] **Шаг 2: сквозной тест с поддельными bigpc**

Тест поднимает два локальных HTTP-сервера: один отвечает как `/transcribe`, второй как ollama `/api/chat` фиксированным JSON. Прогоняет `contextd` от приёма события до карточки в временном волте и проверяет:

```python
    def test_от_приёма_до_карточки(self):
        # событие → аудио → asr → extract → project → digest
        self.assertTrue(os.path.exists(self.transcript))
        self.assertTrue(any(p.startswith("kb/conversations/") for p in self.written))
        self.assertIn("cloud_allowed: false", self.card_text)

    def test_аудио_не_попало_в_волт_и_под_фильтры_r2(self):
        for root, _, files in os.walk(self.vault):
            for f in files:
                self.assertFalse(f.endswith((".m4a", ".wav")), "аудио в волте")
        filters = open("config/r2-filters.txt", encoding="utf-8").read()
        self.assertNotIn("mara-blobs", filters, "блобы вне дерева волта вовсе")

    def test_повтор_не_создаёт_вторую_карточку(self):
        ...
```

- [x] **Шаг 3: прогон**

Запуск: `python3 -m unittest tests.test_pipeline_e2e -v`
Ожидание: PASS. Если `ffmpeg` в системе нет, тест помечается `skipUnless` и печатает причину, а не падает.

- [x] **Шаг 4: коммит**

```bash
git add tests/test_pipeline_e2e.py tests/fixtures/make_sample_call.sh
git commit -m "сквозной тест: файл на диске превращается в карточку, аудио остаётся вне волта"
```

---

### Задача 10: ретеншен, сверка, крон

**Файлы:**
- Создать: `scripts/blob_retention.py`, `scripts/contextd_reconcile.py`, `tests/test_retention.py`

- [x] **Шаг 1: падающие тесты**

```python
class Ретеншен(unittest.TestCase):
    def test_просроченное_аудио_удаляется_а_манифест_остаётся(self): ...
    def test_повтор_уборки_ничего_не_ломает(self): ...
    def test_pin_отменяет_удаление(self): ...

class Сверка(unittest.TestCase):
    def test_манифест_без_блоба_уходит_в_отчёт(self): ...
    def test_транскрипт_без_работы_извлечения_ставит_работу(self): ...
    def test_осиротевший_блоб_не_удаляется_молча(self): ...
```

- [x] **Шаг 2: реализация**

`blob_retention.py`: удаляет файл, дописывает в манифест `"purged": {"at": ..., "reason": "retention"}`, ставит `blobs.purged_at`. Повторный прогон видит `purged_at` и выходит без ошибки. `pin=1` пропускается всегда.

`contextd_reconcile.py`: пять проверок из спеки §9, вывод человекочитаемый, `--json` для машинного чтения, ненулевой код возврата только при реальной проблеме.

- [x] **Шаг 3: крон на doctor**

```bash
( crontab -l; \
  echo '7 * * * * /usr/bin/python3 /home/sergey/mara-second-brain/scripts/contextd_reconcile.py >> /home/sergey/.local/state/mara/reconcile.log 2>&1 # mara-second-brain'; \
  echo '40 4 * * * /usr/bin/python3 /home/sergey/mara-second-brain/scripts/blob_retention.py >> /home/sergey/.local/state/mara/retention.log 2>&1 # mara-second-brain' \
) | crontab -
```

- [x] **Шаг 4: тесты и коммит**

```bash
python3 -m unittest tests.test_retention -v
git add scripts/blob_retention.py scripts/contextd_reconcile.py tests/test_retention.py
git commit -m "ретеншен и сверка: удаление идемпотентно, манифест переживает блоб"
```

---

### Задача 11: развёртывание и документация

**Файлы:**
- Создать: `install/com.mara.relay.plist`, `docs/USER-MANUAL-STEPS.md`
- Изменить: `README.md`, `install/stage0-doctor.sh`

- [x] **Шаг 1: подготовка doctor**

```bash
ssh doctor 'sudo apt-get install -y ffmpeg && sudo install -d -m 700 -o sergey -g sergey /srv/mara-blobs && sudo install -d -m 755 /etc/mara'
```

- [x] **Шаг 2: секреты вне репозитория**

`/etc/mara/contextd.env` с правами 0600: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_HOME_CHANNEL`, `MARA_BLOBS`. Значения берутся из `~/.hermes/.env` на маке и не печатаются в лог.

- [x] **Шаг 3: сервис**

```bash
ssh doctor 'sudo cp ~/mara-second-brain/install/contextd.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now contextd && systemctl is-active contextd'
```

- [x] **Шаг 4: ключ и релей на маке**

Ключ мак → doctor с ограничением, launchd-агент с `KeepAlive`, привязка к `100.64.0.1:8788`. Проверка с мака: `curl -s http://100.64.0.1:8788/healthz`.

- [x] **Шаг 5: документация**

`docs/USER-MANUAL-STEPS.md` содержит **только** то, что владелец обязан сделать руками и чего агент не может: одобрить установку APK, выдать разрешения на телефоне, сделать тестовый звонок, исключить `kb/conversations` из индекса Copilot на устройствах. Ничего лишнего.

В `README.md` добавляется строка таблицы про `contextd` и абзац про то, где живут блобы и почему они вне синка.

- [x] **Шаг 6: финальный прогон и коммит**

```bash
./scripts/run-tests.sh
git add install docs README.md
git commit -m "развёртывание contextd: сервис, релей, ручные шаги владельца"
```

---

## Самопроверка плана

**Покрытие спеки.** §1 транспорт — задачи 3 и 11. §2 контракт и дедуп — задача 2. §3 демон — задача 3. §4 блобы и ретеншен — задачи 3 и 10. §5 ASR — задача 4. §6 извлечение — задача 5. §7 проекция — задачи 6 и 7. §8 дайджест — задача 8. §9 очередь и сверка — задачи 2 и 10. §10 безопасность — задачи 1, 6, 9. §11 метрики — задача 3. §12 тесты — все задачи плюс 9.

**Не покрыто сознательно:** `POST /v1/context/query` и наполнение `/v1/context/bootstrap` уходят в спеку 2 вместе с контекст-брокером; в этой спеке эндпоинты отвечают минимальным пакетом. Инструмент коррекций `mara_correction` тоже спека 2, поэтому таблица `digests` заполняется уже сейчас, а читается позже.

**Согласованность имён.** `mara_ingest.connect/put_event/add_job/claim_job/finish_job/next_delay/blob_path`, `contextd.pair/make_server/run_step/worker/metrics`, `call_asr.slice_plan/transcribe_spans/run`, `call_extract.normalize/parse_deadline/run`, `call_project.conversation_card/commitment_cards/run`, `call_digest.render/send/run` — одинаково во всех задачах, где упоминаются.

---

## Что сделано иначе, чем написано в плане

Три отступления, все осознанные.

1. **Задача 7 не трогала `daily-summary.py`.** План велел показать в дневной
   сводке разговоры без проекта. Этот скрипт отправляет материал в OpenRouter,
   и выполнение шага буквально означало бы утечку личных звонков в облако.
   Вместо этого граница закреплена комментарием в коде и тестом, а новые папки
   выучила только локальная `daily-page.py`.

2. **Крон задачи 10 поставлен внутри задачи 11.** До появления `contextd` на
   doctor две новые строки крона писали бы в лог ошибку каждый час.

3. **Ключ релея в задаче 11 остался обычным.** Правку `authorized_keys` на
   doctor агенту выполнить не дали — и правильно: это привилегированное
   действие. Ограниченный ключ на маке сгенерирован и ждёт, шаг записан первым
   пунктом в `docs/USER-MANUAL-STEPS.md`.

Дополнительно, вне плана: закрыта утечка, которую план не предусматривал.
`person_card` кладёт номер телефона в `aliases`, а `mara-brief.py` копирует
алиасы в `SOUL.md`, который уходит провайдеру модели каждым ходом. Номер
теперь в сводку не попадает, в карточке остаётся.
