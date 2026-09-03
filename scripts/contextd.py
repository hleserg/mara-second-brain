#!/usr/bin/env python3
"""contextd: приём событий, очередь, статус, метрики (ТЗ §4).

Тонкий по замыслу. Он принимает, ставит работу, отдаёт статус и метрики; всё
тяжёлое делают отдельные скрипты, которые он зовёт подпроцессом и которые
работают без него: `python3 scripts/call_asr.py --event <id>` чинится руками в
три часа ночи, а внутренности демона — нет.

По умолчанию слушает только loopback, и адрес меняется одной переменной
MARA_BIND. На doctor стоит 0.0.0.0: телефон приходит по домашней локалке, куда
его пускает VPN на роутере. Раньше вместо этого был ssh-туннель с мака, и мак
стоял в цепочке только потому, что на нём жил адрес тайлнета — приложению он
не был нужен ни для чего.

Наружу, в интернет, демон не смотрит и теперь: границу держит роутер, а не
bind. Но из локалки он виден, и это осознанный размен. Без токена отдаются
только /healthz, всё остальное — 401. /metrics отдаётся без токена лишь с
петли: там не секреты, но имена устройств и время последнего синка каждого
канала. Сам токен едет по проводу открытым текстом — шифрует его участок VPN,
а не протокол.

Логи санитарные (ТЗ §18): в них идут метод, путь, код, id события и размеры.
Тела сообщений, транскрипты и токены не логируются никогда.

    python3 scripts/contextd.py --serve
    python3 scripts/contextd.py --pair "телефон Серёги"
    python3 scripts/contextd.py --revoke dev_ab12...
    python3 scripts/contextd.py --self-check
"""
import os, sys, json, time, hashlib, secrets, argparse, threading, subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

VAULT = os.environ.get("VAULT", "/srv/vault")
MAX_BODY = 512 << 20                 # часовой звонок в m4a влезает с запасом
NEXT = {"asr": "extract", "extract": "project", "project": "digest"}
STEP = {"asr": "call_asr.py", "extract": "call_extract.py",
        "project": "call_project.py", "digest": "call_digest.py"}
# ponytail: один семафор на все GPU-работы. На bigpc свободно меньше шести
# гигабайт VRAM: параллельный ASR вытеснит whisper, и обе работы поедут
# медленнее, чем одна. Появится вторая карта — станет Semaphore(2).
GPU = threading.Semaphore(1)
OPEN_PATHS = ("/healthz",)              # мануал велит проверять его с телефона
# /metrics без токена отдаётся только с петли. Там не секреты, но имена
# устройств и время последнего синка каждого канала: по этим числам читается
# распорядок дня владельца, а порт теперь открыт в локалку.
LOOPBACK = ("127.0.0.1", "::1")


# --- устройства -------------------------------------------------------------

def pair(con, name):
    """Новое устройство. Токен показывается один раз, в базе только sha256."""
    token = secrets.token_urlsafe(32)
    dev = "dev_" + secrets.token_hex(8)
    con.execute("insert into devices(id,name,token_sha256,created) values(?,?,?,?)",
                (dev, name, hashlib.sha256(token.encode()).hexdigest(), mi.now_iso()))
    return dev, token


def revoke(con, device_id):
    con.execute("update devices set revoked_at=? where id=? and revoked_at is null",
                (mi.now_iso(), device_id))
    return con.execute("select revoked_at from devices where id=?",
                       (device_id,)).fetchone()


def device_of(con, header):
    """Устройство по заголовку Authorization или None. Отозванное — тоже None."""
    if not header or not header.startswith("Bearer "):
        return None
    h = hashlib.sha256(header[7:].strip().encode()).hexdigest()
    row = con.execute("select id from devices where token_sha256=? and revoked_at is null",
                      (h,)).fetchone()
    if not row:
        return None
    con.execute("update devices set last_seen=? where id=?", (mi.now_iso(), row["id"]))
    return row["id"]


# --- логи и метрики ---------------------------------------------------------

def log_line(method, path, code, payload=None):
    """Строка лога без единого байта содержимого (ТЗ §18)."""
    extra = ""
    if isinstance(payload, dict):
        keys = ",".join(sorted(k for k in payload if k != "payload"))
        extra = " keys=%s" % keys if keys else ""
    return "%s %s %s -> %s%s" % (mi.now_iso(), method, path, code, extra)


def heartbeat_lag(root, name):
    """Возраст сердцебиения источника (tdlib, gmail); −1 — его ещё не подключали."""
    try:
        return int(time.time() - os.stat(os.path.join(root, name, "heartbeat")).st_mtime)
    except OSError:
        return -1


def metrics(con, root=None, vault=None):
    """Считаем запросом, а не копим в памяти: перезапуск не теряет счётчики."""
    root = root or mi.ROOT
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    last = con.execute("select received from events order by received desc limit 1").fetchone()
    lag = age_of(last[0] if last else None, 0)
    seen = con.execute("select last_seen from devices where revoked_at is null "
                       "order by last_seen desc limit 1").fetchone()
    mobile = age_of(seen[0] if seen else None)
    pack_age, pack_bytes = pack_stat(vault)
    # mara_mobile_* берёт любое устройство, и Gmail-крон раз в 10 минут его
    # всегда «освежит» — телефон виден только поимённо
    devices = [(n, age_of(t)) for n, t in con.execute(
        "select name, last_seen from devices where revoked_at is null order by name")]
    rows = [
        ("mara_ingest_queue_depth", q("select count(*) from jobs where state='ready'")),
        ("mara_ingest_lag_seconds", lag),
        ("mara_dlq_count", q("select count(*) from jobs where state='dlq'")),
        ("mara_transcription_queue_depth",
         q("select count(*) from jobs where state='ready' and kind='asr'")),
        ("mara_task_extraction_failures_total",
         q("select count(*) from jobs where kind='extract' and state='dlq'")),
        ("mara_mobile_last_seen_seconds", mobile),
        *[('mara_device_last_seen_seconds{name="%s"}' % n.replace('\\', '\\\\').replace('"', '\\"'), a)
          for n, a in devices],
        ("mara_mobile_pending_uploads",
         q("select count(*) from events where state='new' and blob_sha256 is not null")),
        ("mara_tdlib_lag_seconds", heartbeat_lag(root, "tdlib")),
        ("mara_gmail_lag_seconds", heartbeat_lag(root, "gmail")),
        ("mara_whatsapp_lag_seconds", source_lag(con, "whatsapp")),
        ("mara_sms_lag_seconds", source_lag(con, "sms")),
        ("mara_context_pack_age_seconds", pack_age),
        ("mara_context_pack_bytes", pack_bytes),
    ]
    return "".join("%s %s\n" % (k, v) for k, v in rows)


def age_of(iso, none=-1):
    """Секунды с момента в ISO; `none`, если момента нет или он не читается."""
    from datetime import datetime
    try:
        return int(time.time() - datetime.fromisoformat(iso).timestamp())
    except (TypeError, ValueError):
        return none


def source_lag(con, source):
    """Секунды с последнего события источника; −1 — не было ни одного."""
    row = con.execute("select received from events where source=? "
                      "order by received desc limit 1", (source,)).fetchone()
    return age_of(row[0] if row else None)


def pack_stat(vault=None):
    """Возраст и размер пакета контекста (ТЗ §19); −1 — ещё не собран."""
    try:
        st = os.stat(os.path.join(vault or VAULT, "_system/context", "now.md"))
    except OSError:
        return -1, -1
    return int(time.time() - st.st_mtime), st.st_size


def health(con, root):
    st = os.statvfs(root)
    return {"ok": True, "queue": con.execute(
                "select count(*) from jobs where state='ready'").fetchone()[0],
            "dlq": con.execute(
                "select count(*) from jobs where state='dlq'").fetchone()[0],
            "events": con.execute("select count(*) from events").fetchone()[0],
            "free_gb": round(st.f_bavail * st.f_frsize / 1e9, 1),
            "pipeline_version": mi.PIPELINE_VERSION}


def audio_until(days=None):
    from datetime import datetime, timedelta
    days = int(os.environ.get("MARA_AUDIO_DAYS", days or 90))
    return (datetime.now(mi.TZ) + timedelta(days=days)).date().isoformat()


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "contextd/1"
    protocol_version = "HTTP/1.1"

    # база одна на процесс, но соединение на поток: sqlite не любит делиться
    def con(self):
        if not hasattr(self.server, "_local"):
            self.server._local = threading.local()
        if not hasattr(self.server._local, "con"):
            self.server._local.con = mi.connect(self.server.root)
        return self.server._local.con

    def log_message(self, fmt, *args):
        pass                                  # свой лог, санитарный

    def say(self, code, obj, ctype="application/json"):
        body = (obj if isinstance(obj, (bytes, bytearray)) else
                json.dumps(obj, ensure_ascii=False).encode("utf-8")
                if ctype == "application/json" else obj.encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("тело больше %d МиБ" % (MAX_BODY >> 20))
        return self.rfile.read(n) if n else b""

    def authed(self, path):
        if path in OPEN_PATHS:
            return True
        if path == "/metrics" and self.client_address[0] in LOOPBACK:
            return True
        if device_of(self.con(), self.headers.get("Authorization")):
            return True
        self.say(401, {"error": "устройство не опознано"})
        return False

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if not self.authed(p.path):
            return
        con = self.con()
        if p.path == "/healthz":
            return self.say(200, health(con, self.server.root))
        if p.path == "/metrics":
            return self.say(200, metrics(con, self.server.root, self.server.vault),
                            ctype="text/plain; version=0.0.4")
        if p.path.startswith("/v1/jobs/"):
            row = con.execute("select id,kind,state,attempts,next_at,last_error "
                              "from jobs where id=?", (p.path[9:],)).fetchone()
            return self.say(200 if row else 404,
                            dict(row) if row else {"error": "нет такой работы"})
        if p.path == "/v1/context/bootstrap":
            return self.say(200, bootstrap(con, self.server.vault))
        return self.say(404, {"error": "нет такого пути"})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if not self.authed(p.path):
            return
        con = self.con()
        try:
            raw = self.body()
        except ValueError as e:
            return self.say(413, {"error": str(e)})
        if p.path == "/v1/ingest/audio":
            q = urllib.parse.parse_qs(p.query)
            code, out = ingest_audio(con, self.server.root,
                                     (q.get("event") or [""])[0], raw)
            print(log_line("POST", p.path, code), flush=True)
            return self.say(code, out)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return self.say(400, {"error": "не json"})
        if p.path in ("/v1/ingest/event", "/v1/ingest/message", "/v1/ingest/email"):
            kind = {"/v1/ingest/message": "message",
                    "/v1/ingest/email": "email"}.get(p.path) or data.get("kind") or "event"
            data["kind"] = kind
            data["device_id"] = device_of(con, self.headers.get("Authorization"))
            if kind == "correction":
                import call_project
                err = call_project.check_correction(data.get("payload") or {})
                if err:
                    return self.say(400, {"error": err})
            eid, dup = mi.put_event(con, data)
            need = bool((data.get("blob") or {}).get("sha256")) and not dup
            applied = None
            if kind == "correction" and not dup:
                # синхронно, а не через очередь: Серёга ждёт ответа Мары, а не
                # ночного крона. Писатель карточек один — call_project.
                try:
                    applied = call_project.apply_correction(self.server.vault,
                                                            dict(data, id=eid))
                except Exception as e:
                    print("correction %s: %s: %s" % (eid, type(e).__name__, e), flush=True)
                    applied = {"found": False,
                               "text": "не применил: %s" % type(e).__name__}
            print(log_line("POST", p.path, 200, data), flush=True)
            return self.say(200, {"event_id": eid, "duplicate": dup,
                                  "need_blob": need, "applied": applied})
        if p.path == "/v1/context/query":
            # пакетов по сущностям нет и не заводится, пока now.md влезает в
            # бюджет (docs/superpowers/specs/2026-09-02-context-broker-design.md).
            # Отдаём единственный существующий, чтобы клиент не гадал.
            пакет = now_pack(self.server.vault)
            return self.say(200, {"packs": [пакет] if пакет else [], "entities": [],
                                  "note": "пакет один — now; по сущностям их нет"})
        return self.say(404, {"error": "нет такого пути"})


def ingest_audio(con, root, event_id, raw):
    """Блоб на диск с проверкой содержимого. Хеш не сошёлся — не успех (ТЗ §20)."""
    row = con.execute("select blob_sha256, payload_json, state from events where id=?",
                      (event_id,)).fetchone()
    if not row:
        return 404, {"error": "нет такого события"}
    want = row["blob_sha256"]
    if not want:
        return 400, {"error": "событие без аудио"}
    ext = (json.loads(row["payload_json"] or "{}").get("ext")) or "bin"
    path = mi.blob_path(root, want, ext)
    if os.path.exists(path):
        return 200, {"event_id": event_id, "blob_sha256": want,
                     "bytes": os.path.getsize(path), "duplicate": True}
    got = hashlib.sha256(raw).hexdigest()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(raw)
    if got != want:
        os.unlink(tmp)
        return 409, {"error": "хеш не сошёлся", "expected": want, "got": got}
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    con.execute("insert or replace into blobs(sha256,path,bytes,mime,created,audio_until)"
                " values(?,?,?,?,?,?)",
                (want, path, len(raw), "audio", mi.now_iso(), audio_until()))
    con.execute("update events set state='stored' where id=?", (event_id,))
    mi.write_json(mi.manifest_path(root, event_id), manifest(con, root, event_id))
    mi.add_job(con, event_id, "asr")
    return 200, {"event_id": event_id, "blob_sha256": want, "bytes": len(raw)}


def manifest(con, root, event_id):
    """Неизменяемый манифест звонка (ТЗ §7). Вложенность допустима: это JSON,
    а не фронтматтер, который в этом репо разбирается регэкспами."""
    e = mi.event_row(con, event_id)
    b = con.execute("select * from blobs where sha256=?", (e["blob_sha256"],)).fetchone()
    p = e["payload"]
    return {
        "id": event_id, "type": e["kind"], "source": e["source"],
        "source_id": e["source_id"],
        "started_at": e["occurred"], "ended_at": e["ended"],
        "direction": p.get("direction", "unknown"),
        "participants": ["person:sergey", "person:" + (p.get("contact_name") or "unknown")],
        "classification": e["classification"], "sensitive": True, "cloud_allowed": False,
        "recording": {"producer": p.get("producer", "unknown"),
                      "audio_sha256": e["blob_sha256"],
                      "blob_ref": "local://" + os.path.relpath(b["path"], root) if b else None,
                      "codec": p.get("codec"), "channels": p.get("channels")},
        "processing": {"pipeline_version": mi.PIPELINE_VERSION},
        "retention": {"audio_until": b["audio_until"] if b else None,
                      "transcript_until": None},
        "purged": None,
    }


def now_pack(vault=None):
    """Пакет открытых обязательств для инжекта в ход Мары (ТЗ §15).

    Читаем с диска на каждый запрос, без кэша: файл маленький, а лишний слой
    хранения — лишняя причина однажды отдать вчерашний список. Нет файла —
    None, а не ошибка: контекст-брокер может быть ещё не собран.
    """
    import context_pack
    d = os.path.join(vault or VAULT, "_system/context")
    try:
        with open(os.path.join(d, "now.md"), encoding="utf-8") as fh:
            text = context_pack.выделить(fh.read())
        with open(os.path.join(d, "manifest.json"), encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        return None
    if not text:
        return None                      # пусто или один чужой фронтматтер
    return {"text": text, "sha256": m.get("sha256"),
            "generated": m.get("generated"), "items": m.get("items")}


def bootstrap(con, vault=None):
    """Что клиент должен знать, ничего больше не спрашивая: открытые
    обязательства, последний дайджест, глубина очереди."""
    row = con.execute("select id,event_id,text,items_json,sent_at from digests "
                      "order by sent_at desc limit 1").fetchone()
    return {"now": now_pack(vault),
            "last_digest": dict(row) if row else None,
            "queue": con.execute(
                "select count(*) from jobs where state='ready'").fetchone()[0],
            "pipeline_version": mi.PIPELINE_VERSION}


def bind_default():
    """Адрес из окружения, иначе петля.

    `or`, а не значение по умолчанию у `.get`: systemd передаёт пустую строку,
    если ключ в env-файле оставили без значения, а пустая строка для
    ThreadingHTTPServer означает 0.0.0.0 — порт открылся бы молча.
    """
    return os.environ.get("MARA_BIND") or "127.0.0.1"


def make_server(root, port=8788, vault=None, host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.root = root
    srv.vault = vault or VAULT
    srv.daemon_threads = True
    return srv


# --- воркер -----------------------------------------------------------------

def run_step(kind, event_id):
    """Шаг пайплайна отдельным процессом. Возвращает (успех, хвост ошибки)."""
    script = STEP.get(kind)
    if not script:
        return False, "неизвестный шаг %s" % kind
    gpu = kind in ("asr", "extract")
    if gpu:
        GPU.acquire()
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, script),
                            "--event", event_id],
                           capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return False, "таймаут часа на шаге %s" % kind
    finally:
        if gpu:
            GPU.release()
    return r.returncode == 0, (r.stderr or "")[-500:]


def worker(stop, root):
    con = mi.connect(root)
    while not stop.is_set():
        job = mi.claim_job(con)
        if not job:
            stop.wait(5)
            continue
        ok, err = run_step(job["kind"], job["event_id"])
        mi.finish_job(con, job["id"], ok, err)
        print("%s работа %s %s %s" % (mi.now_iso(), job["kind"], job["event_id"],
                                      "ок" if ok else "сбой"), flush=True)
        if ok and job["kind"] in NEXT:
            mi.add_job(con, job["event_id"], NEXT[job["kind"]])


def serve(root, port, host="127.0.0.1"):
    con = mi.connect(root)
    con.close()
    stop = threading.Event()
    threading.Thread(target=worker, args=(stop, root), daemon=True).start()
    srv = make_server(root, port, host=host)
    print("contextd: слушаю %s:%d, блобы в %s" % (host, port, root), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop.set()
    return 0


def self_check():
    import tempfile, urllib.request
    root = tempfile.mkdtemp()
    mi.ROOT = root
    srv = make_server(root, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    con = mi.connect(root)
    dev, token = pair(con, "self-check")
    assert device_of(con, "Bearer " + token) == dev, "токен не опознан"
    revoke(con, dev)
    assert device_of(con, "Bearer " + token) is None, "отозванный токен пущен"
    with urllib.request.urlopen(base + "/healthz", timeout=5) as r:
        assert json.loads(r.read())["ok"]
    with urllib.request.urlopen(base + "/metrics", timeout=5) as r:
        assert b"mara_ingest_queue_depth" in r.read()
    assert "секрет" not in log_line("POST", "/v1/ingest/message", 200,
                                    {"payload": {"text": "секрет"}})
    srv.shutdown()
    for v, want in (("", "127.0.0.1"), (None, "127.0.0.1"), ("0.0.0.0", "0.0.0.0")):
        os.environ.pop("MARA_BIND", None)
        if v is not None:
            os.environ["MARA_BIND"] = v
        assert bind_default() == want, "MARA_BIND=%r дал %s" % (v, bind_default())
    os.environ.pop("MARA_BIND", None)
    print("contextd self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="приём событий Mara Ambient Memory")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--port", type=int, default=int(os.environ.get("MARA_PORT", 8788)))
    # Loopback по умолчанию: разработка и тесты не должны случайно открыть порт
    # наружу. Открывает его ровно одна строка в /etc/mara/contextd.env на doctor.
    ap.add_argument("--bind", default=bind_default())
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--pair", metavar="ИМЯ")
    ap.add_argument("--revoke", metavar="ID")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    mi.ROOT = a.root
    if a.pair:
        con = mi.connect(a.root)
        dev, token = pair(con, a.pair)
        print("устройство: %s\nтокен (показывается один раз): %s" % (dev, token))
        return 0
    if a.revoke:
        con = mi.connect(a.root)
        row = revoke(con, a.revoke)
        print("отозвано %s" % a.revoke if row else "нет такого устройства")
        return 0 if row else 1
    if a.serve:
        return serve(a.root, a.port, a.bind)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
