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
только /healthz, и наружу он отдаёт один `ok` — счётчики видны лишь с петли,
как и /metrics целиком. Там не секреты, но имена устройств и время последнего
синка каждого канала: по этим числам читается распорядок дня. Сам токен едет
по проводу открытым текстом — шифрует его участок VPN, а не протокол.

Логи санитарные (ТЗ §18): в них идут метод, путь, код, id события и размеры.
Тела сообщений, транскрипты и токены не логируются никогда.

    python3 scripts/contextd.py --serve
    python3 scripts/contextd.py --pair "телефон Серёги"
    python3 scripts/contextd.py --revoke dev_ab12...
    python3 scripts/contextd.py --scopes dev_ab12...
    python3 scripts/contextd.py --allow dev_ab12... call message
    python3 scripts/contextd.py --allow dev_ab12... @history
    python3 scripts/contextd.py --self-check

У устройства есть allowlist видов событий (ADR-0009): `scopes is null` —
«всё можно», список — только перечисленное, остальное 403. Новое устройство
приходит с `null` намеренно: телефон, у которого истории ещё нет, иначе был бы
отбит на первом же событии. Список ставится руками, и `@history` берёт его из
того, что устройство реально слало за месяц, а не из наших предположений. Если
за месяц не слало ничего, `@history` уже заданный список **не снимает**:
молчание — не разрешение. Снять его можно только явно — `--allow ID` без
видов; вид пустой или с пробелом внутри не принимается вовсе, потому что
список хранится строкой через пробел и такой вид развалился бы на два
разрешения. Дозагрузка аудио (`/v1/ingest/audio`) идёт под тем же allowlist,
что и событие, к которому она прикладывается.

Слова в списке — это виды из `events.kind`: сегодня заводятся `call`,
`message`, `correction` и `email`. `audio` из ADR-0009 в их число не входит —
это не вид, а дозагрузка к уже принятому событию, и телефону в список пишется
`call`.
"""
import os, sys, io, re, json, time, hashlib, secrets, argparse, threading, subprocess
from datetime import datetime, timedelta
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

VAULT = os.environ.get("VAULT", "/srv/vault")
# Час разговора в m4a на 192 kb/s — около 85 МБ, два часа — 170. Тело теперь
# льётся на диск кусками, так что лимит бережёт не память, а место на плате;
# зато 413 у телефона терминальный (JobFlow.next), и запись сверх лимита не
# доедет никогда. 256 МиБ — примерно три часа разговора.
MAX_BODY = 256 << 20
# JSON-эндпоинты принимают событие, а не файл. Мегабайта хватает на самое
# длинное письмо; всё, что больше, — либо ошибка клиента, либо попытка занять
# память демона.
MAX_JSON = 1 << 20
# Сколько согласны вычитать в никуда, чтобы отдать 413 по-человечески.
СЛИВ = 8 << 20
NEXT = {"asr": "extract", "extract": "project", "project": "digest"}
STEP = {"asr": "call_asr.py", "extract": "call_extract.py",
        "project": "call_project.py", "digest": "call_digest.py"}
# ponytail: один семафор на все GPU-работы. На bigpc свободно меньше шести
# гигабайт VRAM: параллельный ASR вытеснит whisper, и обе работы поедут
# медленнее, чем одна. Появится вторая карта — станет Semaphore(2).
GPU = threading.Semaphore(1)
OPEN_PATHS = ("/healthz",)              # мануал велит проверять его с телефона
# /metrics без токена отдаётся только с петли, и /healthz из локалки отдаёт
# один `ok` без счётчиков. Там не секреты, но имена устройств и время
# последнего синка каждого канала: по этим числам читается распорядок дня
# владельца, а порт теперь открыт в локалку.
LOOPBACK = ("127.0.0.1", "::1")
# Роутер, которому разрешено подставлять реальный адрес клиента в
# X-Forwarded-For. Пусто — никому: заголовок подделывается одной строкой, и без
# явного списка лимит попыток обходится сменой выдуманного адреса.
TRUSTED_PROXY = tuple(a.strip() for a in
                      (os.environ.get("MARA_TRUSTED_PROXY") or "").split(",") if a.strip())
ПОПЫТОК = 10                            # подряд неудачных 401 с одного адреса
ОКНО = 300                              # за столько секунд
ОСТЫТЬ = 300                            # и столько же потом получает 429
_неудачи = {}                           # адрес -> [сколько, когда первая]
_замок = threading.Lock()


def клиент(handler):
    """Кто стучится, с точки зрения лимита попыток.

    Адрес сокета, а если стучится доверенный прокси — последний адрес из
    X-Forwarded-For. Только для счётчика: проверки на петлю (`/metrics`,
    полный `/healthz`) остаются на `client_address`, иначе заголовком можно
    было бы притвориться петлёй.
    """
    peer = handler.client_address[0]
    if peer in TRUSTED_PROXY:
        # get_all, а не get: при двух заголовках `get` берёт первый, то есть
        # присланный клиентом, а не дописанный прокси
        fwd = (handler.headers.get_all("X-Forwarded-For") or [""])[-1]
        цепочка = [a.strip() for a in fwd.split(",") if a.strip()]
        if цепочка:
            return цепочка[-1]
    return peer


def под_замком(adr, ok):
    """Считает неудачи. Возвращает, сколько секунд ещё ждать (0 — пускаем)."""
    сейчас = time.time()
    with _замок:
        for a, (_, когда) in list(_неудачи.items()):
            if сейчас - когда > ОКНО + ОСТЫТЬ:
                _неудачи.pop(a, None)          # чтобы словарь не рос от сканеров
        if ok:
            _неудачи.pop(adr, None)
            return 0
        было, когда = _неудачи.get(adr, (0, сейчас))
        if сейчас - когда > ОКНО and было < ПОПЫТОК:
            было, когда = 0, сейчас            # окно истекло, счёт заново
        _неудачи[adr] = (было + 1, когда)
        if было + 1 > ПОПЫТОК:
            return int(ОКНО + ОСТЫТЬ - (сейчас - когда)) or 1
        return 0


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


def scope_ok(con, device_id, kind):
    """Разрешён ли устройству этот вид события (ADR-0009, откат п. 2).

    `scopes is null` означает «как до этой колонки — всё можно», и это не
    временная поблажка, а рабочее состояние: устройство, ещё ничего не
    приславшее, списка не получает, иначе телефон с пустой историей был бы
    отбит на первом же событии. Список заполняется отдельным шагом
    (`--allow`), и только по тому, что устройство реально шлёт.
    """
    if not device_id:
        return False
    row = con.execute("select scopes from devices where id=?", (device_id,)).fetchone()
    if not row:
        # Токен опознан в `authed()`, а строки уже нет — устройство отозвали
        # между двумя запросами к базе. Пропустить значит записать событие с
        # `device_id is null` уже после отзыва; отбить — то, чего отзыв и хотел.
        return False
    if row["scopes"] is None:
        return True
    return kind in row["scopes"].split()


def scopes_from_history(con, device_id, days=30):
    """Виды, которые устройство слало за `days` дней. Пусто — истории нет."""
    since = (datetime.now(mi.TZ) - timedelta(days=days)).isoformat(timespec="seconds")
    return sorted({r["kind"] for r in con.execute(
        "select distinct kind from events where device_id=? and received>=?",
        (device_id, since)) if r["kind"]})


ВИД = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def плохой_вид(kind):
    """Почему такой вид нельзя принимать и класть в allowlist, или None.

    Список хранится одной строкой через пробел, а читается `split()`. Вид с
    пробелом внутри развалился бы на два разрешения (`"call correction"`
    открыл бы настоящий `correction`), а пустой — снял бы ограничение целиком,
    молча и не тем способом, каким его снимают намеренно.

    Остальное — форма, и она здесь не ради красоты: вид уходит в идентификатор
    события (`mi.put_event`: `"%s_%s" % (kind, uuid4)`), а тот — в имена файлов
    манифеста, расшифровки и извлечения (`mi.manifest_path` и соседи). Дотянуться
    туда видом сегодня нельзя: эти файлы заводятся только у звонка, а всё, кроме
    `call`, отсекается выше по обработчику проверкой блоба. Но держать форму
    идентификатора на одном `if` в одной ручке — ровно то, на чём погорела
    история `ext` (#27), поэтому проверка стоит там же, где принимается вид.

    Вид приходит из json, поэтому строкой он быть не обязан: `{"kind": 7}`
    доезжал до базы целым числом, а `kind.split()` на нём просто падал.
    """
    if not isinstance(kind, str):
        return "вид — не строка"
    if not kind:
        return "пустой вид"
    if kind.split() != [kind]:
        return "в виде «%s» пробел" % kind[:40]
    if not ВИД.match(kind):
        return ("вид «%s» не по форме: строчные латинские, цифры и `_`, "
                "с буквы, не длиннее 32" % kind[:40])
    return None


def set_scopes(con, device_id, kinds):
    """Записать allowlist. Пустой список снимает ограничение (`null`).

    Возвращает (нашлось ли устройство, что записано). Откат по ADR-0009 —
    перестать проверять колонку; удалять её не нужно и не следует.
    """
    for k in kinds:
        беда = плохой_вид(k)
        if беда:
            raise ValueError(беда)
    val = " ".join(sorted(set(kinds))) or None
    cur = con.execute("update devices set scopes=? where id=?", (val, device_id))
    return bool(cur.rowcount), val


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
        # брошенные: телефон объявил один хеш, а прислал байты с другим и
        # ушёл заводить новое событие. Растёт при каждом звонке — почти
        # наверняка диктофон дописывает файл после того, как телефон его
        # посчитал; порча в канале даёт то же, но не на каждом звонке
        ("mara_ingest_stale_events",
         q("select count(*) from events where state='stale'")),
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


def health(con, root, full=True):
    """Счётчики только с петли.

    Наружу уходит один `ok`: приложение читает код ответа, а не тело
    (`Api.kt`), self-check и тесты — только `ok`. А `events`, `queue` и
    `free_gb` — монотонные счётчики, которые дёргаются ровно в момент прихода
    записи: опрашивая их раз в минуту, из локалки восстанавливается тот же
    распорядок дня, ради которого закрыли /metrics. Грубее, но того же сорта.
    """
    if not full:
        return {"ok": True}
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


def job_view(row, loopback):
    """Строка `jobs` наружу.

    `last_error` — хвост stderr шага, последние 500 символов (`run_step`
    режет уже декодированный текст). Шаги
    зовут `call_asr.py` и `call_extract.py`, а там в текст исключения попадают
    пути и вывод ffmpeg; расшифровки аудит там не нашёл
    (`docs/current-state-audit.md:239-244`), но канал существует и держится на
    аккуратности. Закрываем канал, а не роут: роутом телефон проверяет свой
    токен (`Api.kt:83-88`). ADR-0009, «Откат и миграция» п. 3.
    """
    d = dict(row)
    if not loopback:
        d.pop("last_error", None)
    return d


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

    timeout = 60                     # зависшая заливка иначе держит поток вечно

    def log_message(self, fmt, *args):
        pass                                  # свой лог, санитарный

    def say(self, code, obj, ctype="application/json", closing=False, ещё=None):
        body = (obj if isinstance(obj, (bytes, bytearray)) else
                json.dumps(obj, ensure_ascii=False).encode("utf-8")
                if ctype == "application/json" else obj.encode("utf-8"))
        self.send_response(code)
        if closing:
            self.send_header("Connection", "close")
        for k, v in (ещё or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def отлуп(self, code, текст, ещё=None):
        """Ответ на тело, которое мы отказались читать.

        Клиент в этот момент ещё шлёт: закроем соединение молча — он увидит
        broken pipe вместо кода. Поэтому вычитываем в никуда, но не больше
        СЛИВ: отказ читать полгигабайта — половина смысла лимита.
        """
        слить(self.rfile, min(длина(self) or 0, СЛИВ), Никуда())
        self.close_connection = True
        return self.say(code, {"error": текст}, closing=True, ещё=ещё)

    def authed(self, path):
        if path in OPEN_PATHS:
            return True
        if path == "/metrics" and self.client_address[0] in LOOPBACK:
            return True
        adr = клиент(self)
        # Считаем попытку до проверки токена, но верный токен всё равно
        # проходит и обнуляет счёт: за одним NAT с подбирающим может сидеть
        # телефон владельца, и запирать его заодно — это отказ в обслуживании
        # своими руками.
        ждать = под_замком(adr, False)
        if device_of(self.con(), self.headers.get("Authorization")):
            под_замком(adr, True)
            return True
        if ждать:
            print("429 %s %s: подбор токена, ждать %dс" % (adr, path, ждать), flush=True)
            self.отлуп(429, "слишком много попыток", {"Retry-After": str(ждать)})
            return False
        print("401 %s %s" % (adr, path), flush=True)
        # тело неопознанного запроса тоже вычитываем: иначе его хвост уедет в
        # следующий запрос на том же соединении и сервер ответит 400 на мусор
        self.отлуп(401, "устройство не опознано")
        return False

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if not self.authed(p.path):
            return
        con = self.con()
        if p.path == "/healthz":
            return self.say(200, health(con, self.server.root,
                                        full=self.client_address[0] in LOOPBACK))
        if p.path == "/metrics":
            return self.say(200, metrics(con, self.server.root, self.server.vault),
                            ctype="text/plain; version=0.0.4")
        if p.path.startswith("/v1/jobs/"):
            row = con.execute("select id,kind,state,attempts,next_at,last_error "
                              "from jobs where id=?", (p.path[9:],)).fetchone()
            if not row:
                return self.say(404, {"error": "нет такой работы"})
            # проверка на петлю — по адресу сокета, как у /metrics: заголовком
            # петлёй не притвориться (см. `клиент`). Обманул бы прокси на самом
            # doctor; такого нет — KeenDNS живёт на роутере, отдельной коробкой
            петля = self.client_address[0] in LOOPBACK
            return self.say(200, job_view(row, петля))
        if p.path == "/v1/context/bootstrap":
            return self.say(200, bootstrap(con, self.server.vault))
        return self.say(404, {"error": "нет такого пути"})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if not self.authed(p.path):
            return
        n = длина(self)
        if n is None:
            # границу тела мы не знаем: сливать нечего, а соединение дальше
            # использовать нельзя — хвост уехал бы в следующий запрос
            self.close_connection = True
            return self.say(400, {"error": "нужен числовой Content-Length"},
                            closing=True)
        con = self.con()
        if p.path == "/v1/ingest/audio":
            if n > MAX_BODY:
                # в логе не было ни строчки, а на телефоне отказ терминален
                # (`Core.kt:146`) — звонок пропадал совсем и молча. Прочие
                # 400/404 молчат так же, но чинятся не здесь (#30)
                print(log_line("POST", p.path, 413), flush=True)
                return self.отлуп(413, "тело больше %d МиБ" % (MAX_BODY >> 20))
            q = urllib.parse.parse_qs(p.query)
            eid = (q.get("event") or [""])[0]
            # Дозагрузка — продолжение уже принятого события, и allowlist
            # обязан её накрывать: ADR-0009 прямо ждёт, что телефон с пустым
            # списком получит отлуп на первом же `audio`. Тело здесь ещё не
            # вычитано, поэтому отказ идёт через `отлуп`, а не через `say`.
            # Владельца события здесь не сверяем, и это решение, а не
            # недосмотр (#26). Событие адресуется содержимым: `dedupe_key`
            # у аудио — само содержимое, один на всю базу, иначе два
            # события на один блоб дали бы две цепочки ASR. Значит телефон
            # получает на свой же звонок старое событие со старым
            # `device_id`, и проверка по устройству похоронила бы очередь
            # навсегда: `Core.kt:137` считает 403 терминальным. Содержимое
            # при этом защищено само: `ingest_audio` сверяет хеш (ТЗ §20) и
            # чужие байты не примет.
            row = con.execute("select kind from events where id=?",
                              (eid,)).fetchone()
            if row and not scope_ok(con, device_of(
                    con, self.headers.get("Authorization")), row["kind"]):
                print(log_line("POST", p.path, 403), flush=True)
                return self.отлуп(
                    403, "устройству не разрешён вид «%s»" % row["kind"])
            code, out = ingest_audio(con, self.server.root,
                                     eid, self.rfile, n)
            print(log_line("POST", p.path, code), flush=True)
            return self.say(code, out)
        if n > MAX_JSON:
            print(log_line("POST", p.path, 413), flush=True)
            return self.отлуп(413, "тело больше %d МиБ" % (MAX_JSON >> 20))
        raw = self.rfile.read(n) if n else b""
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            return self.say(400, {"error": "не json"})
        if not isinstance(data, dict):
            # `5` и `[]` — валидный json, но дальше по коду сплошь data.get,
            # и обработчик падал без ответа
            return self.say(400, {"error": "не json"})
        if p.path in ("/v1/ingest/event", "/v1/ingest/message", "/v1/ingest/email"):
            kind = {"/v1/ingest/message": "message",
                    "/v1/ingest/email": "email"}.get(p.path) or data.get("kind") or "event"
            беда = плохой_вид(kind)
            if беда:
                # Без этой проверки вид ложился в `events.kind` как есть, и
                # `--allow <id> @history` потом падал навсегда: он собирает
                # список из истории, а `set_scopes` такой вид не принимает —
                # не записывается ничего, включая настоящие виды (#29).
                print(log_line("POST", p.path, 400, data), flush=True)
                return self.say(400, {"error": беда})
            data["kind"] = kind
            data["device_id"] = device_of(con, self.headers.get("Authorization"))
            # Ключ дедупа считает сервер по полям самого события. Присланный
            # ключ позволял завести строку `blob:<sha>` вообще без `blob`, и
            # тогда настоящий звонок с тем же хешем дедуплицировался в неё, а
            # дозагрузка отвечала «событие без аудио» (400) — терминально для
            # клиента. Из своих полей такая строка получиться не может.
            data.pop("dedupe_key", None)
            blob = data.get("blob") or {}
            if blob and not isinstance(blob, dict):
                # `"blob": 7` роняло обработчик на `.get()` — обрыв без ответа
                return self.say(400, {"error": "blob — не объект"})
            if blob:
                # Блоб бывает только у звонка, и это не вкусовщина: ключ дедупа
                # у события с блобом — сам блоб, один на всю базу. Без этой
                # проверки устройство объявляло чужой sha под видом `message` и
                # забирало себе будущий звонок — телефон получал на дозагрузке
                # 403 по чужому виду, терминально (`Core.kt:137`).
                sha = blob.get("sha256")
                if kind != "call":
                    return self.say(
                        400, {"error": "блоб бывает только у звонка"})
                if not (isinstance(sha, str) and len(sha) == 64
                        and all(c in "0123456789abcdef" for c in sha)):
                    # хеш идёт в имя файла и в сверку содержимого
                    return self.say(400, {"error": "blob.sha256 — не sha256"})
            if not scope_ok(con, data["device_id"], kind):
                print(log_line("POST", p.path, 403, data), flush=True)
                # не `отлуп`: тело уже вычитано целиком, а он слил бы ещё
                # столько же и повис бы, ожидая байты, которых не будет
                return self.say(403, {"error": "устройству не разрешён вид «%s»" % kind})
            if kind == "correction":
                import call_project
                err = call_project.check_correction(data.get("payload") or {})
                if err:
                    return self.say(400, {"error": err})
            eid, dup = mi.put_event(con, data)
            need = need_blob(con, self.server.root, eid,
                             (data.get("blob") or {}).get("sha256"))
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


КУСОК = 1 << 20                          # мегабайт за чтение


class Никуда:
    """Сток для тела, которое нужно вычитать, но некуда девать.

    io.BytesIO на этом месте копил в памяти всё, что читал: заливка дубля на
    128 МиБ поднимала RSS демона на те же 128 МиБ — ровно та беда, ради
    которой заливку делали потоковой.
    """

    def write(self, кусок):
        return len(кусок)


def слить(поток, n, куда):
    """Тело запроса на диск кусками, попутно считая sha256.

    Читаем ровно `n` байт и не больше: `rfile` — это сокет, `read()` без
    границы на keep-alive соединении заберёт и следующий запрос. Возвращает
    (хеш, сколько байт легло).
    """
    h = hashlib.sha256()
    осталось, всего = n, 0
    while осталось > 0:
        кусок = поток.read(min(КУСОК, осталось))
        if not кусок:
            break                            # клиент оборвался: вернём меньше n
        h.update(кусок)
        куда.write(кусок)
        осталось -= len(кусок)
        всего += len(кусок)
    return h.hexdigest(), всего


def длина(handler):
    """Content-Length числом. None — читать тело нельзя, границы не знаем.

    isdigit, а не int(): `int` глотает `+5`, `1_000` и `-1`, а HTTP — нет.
    Сервер прочитал бы пять байт, а хвост тела уехал бы в следующий запрос по
    тому же keep-alive соединению. isascii — потому что `isdigit` верен и для
    `²`, на котором `int` падает. Длину ограничиваем 19 цифрами: с CPython 3.11
    `int()` от строки длиннее 4300 цифр кидает ValueError, а заголовок влезает
    в 64 КиБ — то есть анонимный запрос ронял обработчик без ответа вовсе.
    Число из 19 цифр это 9 эксабайт, до лимита тела ему в любом случае далеко.

    Chunked разбирать мы не умеем, поэтому любой Transfer-Encoding — отказ,
    даже с Content-Length рядом: сервер прочитал бы столько байт, а в
    соединении осталась бы чанковая обвязка. Два разных Content-Length — тот
    же случай: выбирать один из них не наше дело.
    """
    if handler.headers.get("Transfer-Encoding"):
        return None
    все = handler.headers.get_all("Content-Length") or []
    if len(все) > 1:
        return None
    s = (все[0] if все else "").strip()
    if not s:
        return 0
    return int(s) if s.isascii() and s.isdigit() and len(s) <= 19 else None


def blob_row(con, sha256):
    """Что база знает о блобе. Спрашиваем её, а не файловую систему: путь
    считается от даты загрузки, поэтому августовская запись, долитая в
    сентябре, по вычисленному сегодня пути не находится (N5)."""
    if not sha256:
        return None
    return con.execute("select path, bytes, purged_at from blobs where sha256=?",
                       (sha256,)).fetchone()


def finish_stored(con, root, event_id):
    """Довести событие с уже лежащим блобом до `stored`: манифест и работа ASR.
    Идемпотентно — второй вызов не заводит вторую работу. Нужно потому, что
    между записью блоба и переводом события демон может умереть.

    Принимаются `new` и `stale`: событие, брошенное на несошедшемся хеше, ещё
    может дойти — если файл не менялся, телефон пересчитает тот же sha и
    попадёт по дедупу в эту же строку. Перевод состояния — одним
    `update ... where state in (...)`, а не проверкой и записью по отдельности:
    обработчики живут в разных потоках со своими соединениями, и два
    одновременных запроса по одному событию проходили раздельную проверку оба.
    Ценой была вторая цепочка asr→extract→project→digest: час GPU и второй
    дайджест в телеграм. `rowcount` показывает, кто
    успел первым.
    """
    if con.execute("update events set state='stored' "
                   "where id=? and state in ('new','stale')",
                   (event_id,)).rowcount != 1:
        return
    mi.write_json(mi.manifest_path(root, event_id), manifest(con, root, event_id))
    mi.add_job(con, event_id, "asr")


def need_blob(con, root, event_id, sha256):
    """Нужен ли серверу блоб этого события.

    Считается по базе блобов, а не по признаку «событие новое». Иначе повтор
    после потерянного ответа получает `duplicate: true, need_blob: false`,
    телефон закрывает работу успешной, и запись не приезжает никогда (N1).
    Вычищенное ретеншеном аудио заново не просим: его удалили намеренно."""
    if not sha256:
        return False
    row = blob_row(con, sha256)
    if row is None:
        return True
    if not row["purged_at"]:
        finish_stored(con, root, event_id)
    return False


def ingest_audio(con, root, event_id, поток, n=None):
    """Блоб на диск с проверкой содержимого. Хеш не сошёлся — не успех (ТЗ §20).

    `поток` — либо готовые байты (так зовут тесты), либо `rfile` запроса; во
    втором случае тело льётся кусками, а не поднимается в память целиком.
    """
    if isinstance(поток, (bytes, bytearray)):
        n = len(поток)
        поток = io.BytesIO(поток)
    row = con.execute("select blob_sha256, payload_json, state from events where id=?",
                      (event_id,)).fetchone()
    if not row:
        слить(поток, n or 0, Никуда())    # тело всё равно вычитать, иначе
        return 404, {"error": "нет такого события"}   # keep-alive поедет вразнос
    want = row["blob_sha256"]
    if not want:
        слить(поток, n or 0, Никуда())
        return 400, {"error": "событие без аудио"}
    ext = (json.loads(row["payload_json"] or "{}").get("ext")) or "bin"
    известен = blob_row(con, want)
    if известен is not None:
        слить(поток, n or 0, Никуда())
        # строку не трогаем: в ней pin, audio_until и purged_at, а insert or
        # replace их обнулил бы
        if not известен["purged_at"]:
            finish_stored(con, root, event_id)
        return 200, {"event_id": event_id, "blob_sha256": want,
                     "bytes": известен["bytes"], "duplicate": True,
                     "purged": bool(известен["purged_at"])}
    path = mi.blob_path(root, want, ext)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    # каждой загрузке свой временный файл: на общем ".part" второй писатель
    # усекал бы файл первого, и os.replace публиковал бы склейку (N12)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".part")
    # finally, а не unlink по месту: обрыв связи посреди потоковой заливки
    # оставлял .part навсегда, а уборщика для них в системе нет — десяток
    # переключений WiFi→LTE забил бы диск платы
    try:
        with os.fdopen(fd, "wb") as fh:
            got, размер = слить(поток, n or 0, fh)
        if got != want:
            # Событие с этим хешем не долить уже некому: телефон на 409
            # пересчитывает файл с начала (`Core.kt:143-145`), а дописанный
            # за время чтения файл даёт другой sha — значит другой
            # `source_id`, другой ключ дедупа и новая строка. Старая иначе
            # осталась бы в `new` навсегда и час за часом поднимала бы
            # «запись не долита», заслоняя настоящее «телефон замолчал».
            con.execute("update events set state='stale' "
                        "where id=? and state='new'", (event_id,))
            return 409, {"error": "хеш не сошёлся", "expected": want, "got": got}
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    # or ignore, а не or replace: две параллельные загрузки одного аудио не
    # повод обнулять pin и audio_until уже лежащей строки
    con.execute("insert or ignore into blobs(sha256,path,bytes,mime,created,audio_until)"
                " values(?,?,?,?,?,?)",
                (want, path, размер, "audio", mi.now_iso(), audio_until()))
    finish_stored(con, root, event_id)
    return 200, {"event_id": event_id, "blob_sha256": want, "bytes": размер}


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
    assert scope_ok(con, dev, "call"), "null в scopes обязан пускать всё"
    set_scopes(con, dev, ["call"])
    assert scope_ok(con, dev, "call") and not scope_ok(con, dev, "message"), \
        "allowlist не проверяется"
    set_scopes(con, dev, [])
    assert not scope_ok(con, "нет такого", "call"), "неизвестное устройство пущено"
    try:
        set_scopes(con, dev, ["call correction"])
        raise AssertionError("вид с пробелом записан")
    except ValueError:
        pass
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="приём событий Mara Ambient Memory")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--port", type=int, default=int(os.environ.get("MARA_PORT", 8788)))
    # Loopback по умолчанию: разработка и тесты не должны случайно открыть порт
    # наружу. Открывает его ровно одна строка в /etc/mara/contextd.env на doctor.
    ap.add_argument("--bind", default=bind_default())
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--pair", metavar="ИМЯ")
    ap.add_argument("--revoke", metavar="ID")
    ap.add_argument("--scopes", metavar="ID", help="показать allowlist устройства")
    ap.add_argument("--allow", nargs="+", metavar="ID|ВИД",
                    help="задать allowlist видов из `events.kind` "
                         "(сегодня заводятся `call`, `message`, `correction`, "
                         "`email`; `audio` — не вид, а дозагрузка): "
                         "`--allow ID вид вид`; "
                         "`--allow ID` снимает ограничение (и только так); "
                         "`--allow ID @history` берёт виды из событий за 30 "
                         "дней, а при пустой истории ничего не меняет")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args(argv)
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
    if a.scopes:
        con = mi.connect(a.root)
        row = con.execute("select scopes from devices where id=?",
                          (a.scopes,)).fetchone()
        if not row:
            print("нет такого устройства"); return 1
        print(row["scopes"] if row["scopes"] is not None else "без ограничения (null)")
        return 0
    if a.allow:
        con = mi.connect(a.root)
        dev, kinds = a.allow[0], a.allow[1:]
        if kinds == ["@history"]:
            kinds = scopes_from_history(con, dev)
            if not kinds:
                # Тридцать дней тишины — не разрешение. Если список уже задан,
                # `@history` его не снимает: молчавшее устройство иначе
                # открылось бы обратно само, без чьего-либо решения.
                row = con.execute("select scopes from devices where id=?",
                                  (dev,)).fetchone()
                if not row:
                    print("нет такого устройства"); return 1
                print("событий за 30 дней нет — список не трогаю (%s)" %
                      ("null" if row["scopes"] is None else row["scopes"]))
                return 0
        try:
            found, val = set_scopes(con, dev, kinds)
        except ValueError as e:
            print("не записал: %s" % e); return 1
        if not found:
            print("нет такого устройства"); return 1
        print("%s: %s" % (dev, val if val is not None else "без ограничения (null)"))
        return 0
    if a.serve:
        return serve(a.root, a.port, a.bind)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
