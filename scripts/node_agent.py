#!/usr/bin/env python3
"""Агент сердцебиения узла (Т7.2, ADR-0010 решение 5).

Живёт на вычислительном узле — bigpc (Windows) или GTR — и раз в интервал
шлёт на doctor, что узел умеет и можно ли ему давать работу. Направление —
с узла на doctor: doctor не знает, как опрашивать Windows, и не держит
соединений к выключенной машине. Только stdlib: на bigpc нет нашего
окружения, есть голый питон.

Что несёт (`contextd.разобрать_сердцебиение` — закрытый список):
- `capabilities` — `asr`, если отвечает локальный STT-сервис, `llm`, если
  отвечает Ollama; сервис лёг — возможности нет, и doctor держит работы
  в `ready` с причиной `node_no_capability:<узел>`, не тратя попыток;
- `models` — модели Ollama (`/api/tags`);
- `vram_free_mb`, `vram_total_mb` — `nvidia-smi`, если он есть;
- `load` — загрузка GPU в процентах (`utilization.gpu` того же `nvidia-smi`);
- `minecraft_active` — запущен ли процесс игры (застава GTR, решение 4);
- `accept_jobs` — `MARA_NODE_ACCEPT=0` ставит узел на паузу руками;
- `interval_s` — свой интервал: doctor считает узел живым три интервала.

Настройка — окружение (адреса, токен и секрет в репозиторий не кладутся, Г7):
  MARA_URL            адрес contextd на doctor, например http://<doctor>:8788
  MARA_NODE_TOKEN     токен из `contextd.py --pair-node <узел>`
  MARA_NODE_ASR_URL   локальный STT-сервис (необязательно)
  MARA_NODE_LLM_URL   локальная Ollama (необязательно)
  MARA_NODE_INTERVAL  секунд между сердцебиениями, по умолчанию 60
  MARA_NODE_ACCEPT    0 — пауза, по умолчанию 1
  MARA_GAME_PROCESSES процессы игры через запятую
  MARA_NODE_SECRET    общий секрет с doctor (`--new-secret`); с ним и с
  MARA_NODE_PROXY_PORT агент держит прокси сервисов узла (см. ниже)
  MARA_NODE_PROXY_BIND адрес прокси, по умолчанию 0.0.0.0

Прокси сервисов узла (Т0.9 п. 5, threat-model п. 9). STT и Ollama чужие и
отвечают кому угодно, а doctor принимал от адреса bigpc что угодно: узел,
занявший адрес выключенной коробки, получал аудио и возвращал «транскрипт».
С секретом шаги doctor (`call_asr`, `call_extract`) ходят не в сервисы, а в
прокси агента: `/api/…` — в Ollama, остальное — в STT. Запрос подписан
HMAC-SHA256 общего секрета (метод, путь, время, одноразовый номер, хеш
тела), ответ — тоже (тот же номер, код, хеш тела); до первого запроса
doctor спрашивает `/mara/hello` со своим номером, и аудио уходит только
узлу, доказавшему секрет. Неподписанное, просроченное (±5 минут) или
повторённое прокси не пересылает (401), ответ без верной подписи doctor не
принимает. Сами сервисы на узле должны слушать только 127.0.0.1 — иначе их
можно спросить мимо прокси. Подслушивания внутри сети подпись не снимает:
это TLS, Т0.9 п. 2.

    python node_agent.py            # раз в интервал, пока не остановят
    python node_agent.py --once     # одно сердцебиение и выход
    python node_agent.py --new-secret   # новый общий секрет для doctor и узла
    python node_agent.py --self-check
"""
import argparse, hashlib, hmac, http.client, json, os, re, secrets, shutil, subprocess, sys
import threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ИНТЕРВАЛ = 60
# Подольше, чем у пробы doctor: «сервиса нет» от агента держит работы, и
# STT, занятый куском doctor-а, не должен сойти за лёгший
ТАЙМАУТ = 10
# javaw — Java-версия игры (лаунчер запускает её этим процессом), остальные
# — магазинная версия и лаунчер
ИГРА = ("javaw.exe", "Minecraft.Windows.exe", "MinecraftLauncher.exe")


def _без_прокси():
    # узел и doctor — в одной сети; системный прокси Windows сюда не нужен
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def отвечает(адрес, путь="", таймаут=ТАЙМАУТ):
    """(ответил ли, тело). Любой HTTP-ответ — сервис жив, как у пробы doctor."""
    if not адрес:
        return False, b""
    try:
        with _без_прокси().open(адрес.rstrip("/") + путь, timeout=таймаут) as r:
            return True, r.read(1 << 20)
    except urllib.error.HTTPError:
        return True, b""
    except (OSError, ValueError):
        return False, b""


def модели(адрес):
    ок, тело = отвечает(адрес, "/api/tags")
    if not ок:
        return []
    try:
        список = json.loads(тело or b"{}").get("models")
    except (ValueError, AttributeError):
        return []
    if not isinstance(список, list):
        return []
    return sorted({m["name"] for m in список
                   if isinstance(m, dict) and isinstance(m.get("name"), str)
                   and 0 < len(m["name"]) <= 80 and m["name"].isprintable()})[:32]


def видеокарта(запуск=subprocess.run, есть=shutil.which):
    """(свободно, всего, загрузка) по первой карте: память в МБ, загрузка
    GPU в процентах; чего не узнать — None. Загрузку иные карты отдают как
    `[N/A]` — тогда без неё, память остаётся (ревью Т9.1: без `load`
    `mara_node_load` на doctor был бы −1 всегда)."""
    if not есть("nvidia-smi"):
        return None, None, None
    try:
        r = запуск(["nvidia-smi", "--query-gpu=memory.free,memory.total,utilization.gpu",
                    "--format=csv,noheader,nounits"],
                   capture_output=True, text=True, timeout=10)
        поля = [x.strip() for x in r.stdout.splitlines()[0].split(",")]
        свободно, всего = int(поля[0]), int(поля[1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None, None, None
    try:
        загрузка = int(поля[2])
    except (ValueError, IndexError):
        загрузка = None
    return свободно, всего, загрузка


def процессы(запуск=subprocess.run, windows=None):
    """Имена запущенных процессов, в нижнем регистре.

    Вывод читается байтами: `tasklist` на русской Windows пишет в OEM-866
    (колонка памяти с «КБ» и неразрывным пробелом), и декодирование
    локальной кодировкой, а в режиме UTF-8 тем более, роняло агента на
    первом же сердцебиении. Имена процессов — латиница, им замены
    непонятных байт не вредят."""
    windows = os.name == "nt" if windows is None else windows
    try:
        if windows:
            r = запуск(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, timeout=10)
            текст = (r.stdout or b"").decode("ascii", "replace")
            return {l.split('","')[0].strip('"').lower()
                    for l in текст.splitlines() if l.strip()}
        r = запуск(["ps", "-eo", "comm="], capture_output=True, timeout=10)
        текст = (r.stdout or b"").decode("utf-8", "replace")
        return {l.strip().lower() for l in текст.splitlines() if l.strip()}
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()


def игра_идёт(имена=None, игра=None):
    игра = игра or [x.strip() for x in (os.environ.get("MARA_GAME_PROCESSES") or
                                        ",".join(ИГРА)).split(",") if x.strip()]
    имена = процессы() if имена is None else имена
    return any(x.lower() in имена for x in игра)


def собрать(env=None, имена=None):
    """Тело сердцебиения."""
    env = os.environ if env is None else env
    # `checked` — что агент проверял: про незаданный адрес он молчит, и
    # doctor судит о таком сервисе по своей пробе, как без агента
    cap, проверено, out = [], [], {}
    if env.get("MARA_NODE_ASR_URL"):
        проверено.append("asr")
        if отвечает(env["MARA_NODE_ASR_URL"])[0]:
            cap.append("asr")
    if env.get("MARA_NODE_LLM_URL"):
        проверено.append("llm")
        ок, _ = отвечает(env["MARA_NODE_LLM_URL"])
        if ок:
            cap.append("llm")
            out["models"] = модели(env["MARA_NODE_LLM_URL"])
    свободно, всего, загрузка = видеокарта()
    if свободно is not None:
        out["vram_free_mb"], out["vram_total_mb"] = свободно, всего
    if загрузка is not None:
        out["load"] = загрузка
    out.update(capabilities=cap, checked=проверено, minecraft_active=игра_идёт(имена),
               accept_jobs=env.get("MARA_NODE_ACCEPT", "1") != "0",
               interval_s=интервал(env),
               versions={"agent": "1", "python": "%d.%d" % sys.version_info[:2]})
    return out


def интервал(env):
    try:
        return min(3600, max(10, int(env.get("MARA_NODE_INTERVAL") or ИНТЕРВАЛ)))
    except ValueError:
        return ИНТЕРВАЛ


def отправить(url, token, тело):
    """Код ответа doctor; 0 — не достучались."""
    req = urllib.request.Request(url.rstrip("/") + "/v1/node/heartbeat", method="POST",
                                 data=json.dumps(тело).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with _без_прокси().open(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, ValueError):
        # ValueError — и кривой токен (не латиница: заголовок не кодируется)
        return 0


# ── подпись запросов doctor к сервисам узла (Т0.9 п. 5) ──────────────────

ВЕРСИЯ_ПОДПИСИ = "mara-node-1"
ОКНО_С = 300                  # терпимое расхождение часов doctor и узла
ПРИВЕТ = "/mara/hello"
ПРИВЕТ_С = 60                 # доказательство узла doctor помнит минуту
ТЕЛО_МАКС = 8 << 20           # кусок ASR — около 800 КБ, запрос к модели — килобайты
# Не короче самого долгого шага doctor: у `call_extract` ожидание модели —
# 900 с (`HTTP_TIMEOUT`), и прокси не должен рвать генерацию раньше шага
ПЕРЕСЫЛКА_С = 1200
ЧТЕНИЕ_С = 60                 # тишина сокета, после которой соединение рвётся, как у contextd
СОЕДИНЕНИЙ = 8                # одновременных; лишние закрываются сразу, без потока
НОМЕР = re.compile(r"[0-9a-f]{32}\Z")
ПОДПИСЬ = re.compile(r"[0-9a-f]{64}\Z")
ЧИСЛО = re.compile(r"[0-9]{1,12}\Z")
# Что прокси вообще пересылает — только то, чем ходят шаги doctor. Утёкший
# секрет не открывает ни `/api/pull|create|delete` Ollama, ни голос маски на
# той же службе, что STT
РАЗРЕШЕНО = {("POST", "/transcribe"), ("POST", "/api/generate")}


def _json(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class ЧужойУзел(RuntimeError):
    """Узел не доказал общий секрет: ни аудио, ни текст ему не отдаются, а
    его ответ не принимается. Для шага — обычный сбой: ретрай, потом DLQ."""


def _хеш(тело):
    return hashlib.sha256(тело or b"").hexdigest()


def _подпись(секрет, *части):
    # первая часть — назначение (`req`, `resp`, `hello`): подпись одного
    # рода не годится за другой
    return hmac.new(секрет.encode("utf-8"), "\n".join((ВЕРСИЯ_ПОДПИСИ,) + части).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def подписать_запрос(секрет, метод, путь, тело, когда=None, номер=None):
    """Заголовки подписанного запроса: время, одноразовый номер, подпись."""
    когда = str(int(time.time() if когда is None else когда))
    номер = номер or secrets.token_hex(16)
    return {"X-Mara-Ts": когда, "X-Mara-Nonce": номер,
            "X-Mara-Sig": _подпись(секрет, "req", метод, путь, когда, номер, _хеш(тело))}


def подпись_ответа(секрет, номер, код, тело):
    return _подпись(секрет, "resp", номер, str(код), _хеш(тело))


def подпись_привета(секрет, номер):
    return _подпись(секрет, "hello", номер)


def _знак(заголовки):
    """Подпись из заголовка — ровно 64 шестнадцатеричных знака, иначе пусто:
    `compare_digest` на не-ASCII строке бросает `TypeError`, а не говорит
    «не сошлось»."""
    знак = заголовки.get("X-Mara-Sig") or ""
    return знак if ПОДПИСЬ.match(знак) else ""


class _БезПереходов(urllib.request.HTTPRedirectHandler):
    """Редирект — не ответ узла: иначе посредник отправил бы привет к живому
    bigpc дешевле, чем пересылкой."""

    def redirect_request(self, *a, **k):
        return None


_открыть = urllib.request.build_opener(_БезПереходов).open
_доказано = {}                # база узла → monotonic последнего проверенного ответа


def привет(база, секрет, таймаут=ТАЙМАУТ):
    """Узел доказывает секрет на номере doctor — до того, как ему уйдёт
    аудио или текст. Сеть упала — исключение сети, как и без подписи."""
    номер = secrets.token_hex(16)
    try:
        with _открыть("%s%s?nonce=%s" % (база, ПРИВЕТ, номер), timeout=таймаут) as r:
            знак = _знак(r.headers)
    except urllib.error.HTTPError as e:
        e.close()
        знак = ""
    if not hmac.compare_digest(знак, подпись_привета(секрет, номер)):
        raise ЧужойУзел("узел не подтвердил общий секрет — аудио и текст ему не отданы "
                        "(MARA_ASR_URL и MARA_LLM_URL — на прокси агента узла? "
                        "секреты на doctor и узле совпадают?)")


def _с_причиной(e):
    """Отказ прокси с его причиной (401: время, подпись, повтор) — иначе в
    `last_error` шага было бы голое «Unauthorized». Причина — печатные
    знаки, не длиннее 200: тело пишет кто угодно на адресе узла."""
    try:
        причина = json.loads(e.read(1 << 16) or b"{}").get("error")
    except (ValueError, AttributeError, OSError):
        причина = None
    finally:
        e.close()
    if not (isinstance(причина, str) and причина):
        return e
    причина = "".join(c for c in причина if c.isprintable())[:200]
    return urllib.error.HTTPError(e.url, e.code, "%s — %s" % (e.msg, причина), e.hdrs, None)


def к_узлу(url, тело, тип, секрет=None, таймаут=300):
    """POST шага doctor в сервис узла; тело ответа. Без секрета — как раньше,
    напрямую. С секретом — привет (раз в `ПРИВЕТ_С` на узел), подписанный
    запрос без редиректов и проверка подписи ответа: неверная — `ЧужойУзел`,
    ответ не принят. Доказательство узла живёт, только пока он отвечает
    подписанным: любой другой исход — следующий запрос снова с привета.
    Отказ сервиса (4xx/5xx) — `HTTPError`, как и без подписи, но с причиной."""
    if not секрет:
        req = urllib.request.Request(url, data=тело, method="POST")
        req.add_header("Content-Type", тип)
        with urllib.request.urlopen(req, timeout=таймаут) as r:
            return r.read()
    ч = urllib.parse.urlsplit(url)
    база = "%s://%s" % (ч.scheme, ч.netloc)
    # `http.server` сворачивает `//путь` в `/путь`: подписываем тот же, иначе
    # хвостовая `/` в MARA_ASR_URL давала бы вечный 401
    путь = "/" + ч.path.lstrip("/") + ("?" + ч.query if ч.query else "")
    проверено = False
    try:
        if time.monotonic() - _доказано.get(база, float("-inf")) > ПРИВЕТ_С:
            привет(база, секрет)
            _доказано[база] = time.monotonic()
        заголовки = подписать_запрос(секрет, "POST", путь, тело)
        req = urllib.request.Request(база + путь, data=тело, method="POST")
        req.add_header("Content-Type", тип)
        for k, v in заголовки.items():
            req.add_header(k, v)
        try:
            with _открыть(req, timeout=таймаут) as r:
                ответ, код, знак = r.read(), r.status, _знак(r.headers)
        except urllib.error.HTTPError as e:
            raise _с_причиной(e)
        if not hmac.compare_digest(знак, подпись_ответа(секрет, заголовки["X-Mara-Nonce"],
                                                         код, ответ)):
            raise ЧужойУзел("ответ узла без верной подписи — не принят")
        проверено = True
        _доказано[база] = time.monotonic()
        return ответ
    finally:
        if not проверено:
            _доказано.pop(база, None)


class Прокси(ThreadingHTTPServer):
    """Прокси сервисов узла: пересылает только подписанное общим секретом и
    подписывает ответ (см. шапку). Одноразовые номера помнит `2 × ОКНО_С`:
    повтор запроса в пределах окна времени — 401. Ресурсы до проверки
    подписи не тратятся: заголовки — до тела, тело — порциями с таймаутом
    сокета, соединений не больше `СОЕДИНЕНИЙ`."""
    daemon_threads = True
    # на Windows SO_REUSEADDR молча пускает второй процесс на тот же порт:
    # два агента делили бы соединения и память номеров
    allow_reuse_address = os.name != "nt"

    def __init__(self, адрес, секрет, asr=None, llm=None, соединений=СОЕДИНЕНИЙ):
        super().__init__(адрес, _ПроксиОбработчик)
        self.секрет, self.asr, self.llm = секрет, asr, llm
        self.номера, self.замок = {}, threading.Lock()
        self.места = threading.BoundedSemaphore(соединений)

    def process_request(self, request, client_address):
        if not self.места.acquire(blocking=False):
            self.shutdown_request(request)        # занято — ни потока, ни памяти
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.места.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.места.release()

    def handle_error(self, request, client_address):
        # без адреса клиента и трассы с путями: журнал узла может уехать в
        # чат, а путь профиля Windows несёт логин
        лог("прокси: запрос упал — %s" % sys.exc_info()[0].__name__)

    def беда_заголовков(self, заголовки, сейчас=None):
        """Почему запрос не стоит даже читать, или None: подписи нет или она
        не той формы, время вне окна. Проверяется до тела."""
        когда = заголовки.get("X-Mara-Ts") or ""
        if not (ЧИСЛО.match(когда) and НОМЕР.match(заголовки.get("X-Mara-Nonce") or "")
                and ПОДПИСЬ.match(заголовки.get("X-Mara-Sig") or "")):
            return "нет подписи"
        if abs((time.time() if сейчас is None else сейчас) - int(когда)) > ОКНО_С:
            return "время запроса вне окна — часы doctor и узла расходятся?"
        return None

    def беда_запроса(self, метод, путь, тело, заголовки, сейчас=None):
        """Почему запрос не пересылать, или None."""
        беда = self.беда_заголовков(заголовки, сейчас)
        if беда:
            return беда
        сейчас = time.time() if сейчас is None else сейчас
        номер = заголовки["X-Mara-Nonce"]
        ждём = подписать_запрос(self.секрет, метод, путь, тело, заголовки["X-Mara-Ts"],
                                номер)["X-Mara-Sig"]
        if not hmac.compare_digest(заголовки["X-Mara-Sig"], ждём):
            return "подпись не сходится"
        with self.замок:
            for н, t in list(self.номера.items()):
                if сейчас - t > 2 * ОКНО_С:
                    del self.номера[н]
            if номер in self.номера:
                return "повтор запроса"
            self.номера[номер] = сейчас
        return None

    def куда(self, путь):
        return self.llm if путь.startswith("/api/") else self.asr


class _ПроксиОбработчик(BaseHTTPRequestHandler):
    server_version = "mara-node-proxy"
    protocol_version = "HTTP/1.1"
    timeout = ЧТЕНИЕ_С

    def log_message(self, *a):
        pass            # без адресов и путей: журнал узла может уехать в чат

    def _ответ(self, код, тело, тип="application/json", номер=None, закрыть=False):
        if закрыть:
            # тело запроса не дочитано: в keep-alive оно стало бы следующим запросом
            self.close_connection = True
        self.send_response(код)
        self.send_header("Content-Type", тип)
        self.send_header("Content-Length", str(len(тело)))
        if закрыть:
            self.send_header("Connection", "close")
        if номер:
            self.send_header("X-Mara-Sig", подпись_ответа(self.server.секрет, номер, код, тело))
        self.end_headers()
        self.wfile.write(тело)

    def _беда(self, код, текст, закрыть=True):
        self._ответ(код, _json({"error": текст}), закрыть=закрыть)

    def _читать(self, n):
        """Тело порциями по мегабайту: память растёт с пришедшим, а не с
        объявленным; молчащий клиент рвётся таймаутом сокета."""
        куски, осталось = [], n
        while осталось:
            кусок = self.rfile.read(min(осталось, 1 << 20))
            if not кусок:
                break
            куски.append(кусок)
            осталось -= len(кусок)
        return b"".join(куски)

    def do_GET(self):
        ч = urllib.parse.urlsplit(self.path)
        if ч.path != ПРИВЕТ:
            return self._переслать("GET")
        номер = urllib.parse.parse_qs(ч.query).get("nonce", [""])[0]
        if not НОМЕР.match(номер):
            return self._беда(400, "нужен nonce — 32 шестнадцатеричных знака", закрыть=False)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("X-Mara-Sig", подпись_привета(self.server.секрет, номер))
        self.end_headers()

    def do_POST(self):
        self._переслать("POST")

    def _переслать(self, метод):
        if self.headers.get("Transfer-Encoding"):
            return self._беда(411, "нужен Content-Length")
        длина = self.headers.get("Content-Length") or "0"
        if not ЧИСЛО.match(длина):
            return self._беда(400, "кривой Content-Length")
        n = int(длина)
        if n > ТЕЛО_МАКС:
            return self._беда(413, "тело больше %d МиБ" % (ТЕЛО_МАКС >> 20))
        беда = self.server.беда_заголовков(self.headers)
        if беда:
            return self._беда(401, беда)
        тело = self._читать(n)
        if len(тело) < n:
            self.close_connection = True          # клиент ушёл посреди тела
            return
        беда = self.server.беда_запроса(метод, self.path, тело, self.headers)
        if беда:
            return self._беда(401, беда)
        номер = self.headers["X-Mara-Nonce"]
        путь = urllib.parse.urlsplit(self.path).path
        цель = self.server.куда(путь)
        if (метод, путь) not in РАЗРЕШЕНО or not цель:
            return self._ответ(404, _json({"error": "этого прокси не пересылает"}), номер=номер)
        req = urllib.request.Request(цель.rstrip("/") + self.path, data=тело or None, method=метод)
        if self.headers.get("Content-Type"):
            req.add_header("Content-Type", self.headers["Content-Type"])
        try:
            with _без_прокси().open(req, timeout=ПЕРЕСЫЛКА_С) as r:
                код, ответ, тип = r.status, r.read(), r.headers.get("Content-Type") or "application/json"
        except urllib.error.HTTPError as e:
            код, ответ, тип = e.code, e.read(), e.headers.get("Content-Type") or "application/json"
        except (OSError, ValueError, http.client.HTTPException):
            код, ответ, тип = 502, _json({"error": "сервис узла не ответил"}), "application/json"
        self._ответ(код, ответ, тип, номер=номер)


def запустить_прокси(env):
    """Прокси в потоке, если заданы секрет и порт; иначе None. Кривой порт —
    `ValueError`."""
    секрет, порт = env.get("MARA_NODE_SECRET"), env.get("MARA_NODE_PROXY_PORT")
    if not (секрет and порт):
        return None
    if not (порт.isdigit() and int(порт) <= 65535):
        raise ValueError("MARA_NODE_PROXY_PORT — число от 0 до 65535")
    сервер = Прокси((env.get("MARA_NODE_PROXY_BIND") or "0.0.0.0", int(порт)), секрет,
                    asr=env.get("MARA_NODE_ASR_URL"), llm=env.get("MARA_NODE_LLM_URL"))
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    return сервер


def выходить_ли(код, прокси):
    """Отказ doctor в сердцебиении (401/403) кончает агента — если он не
    держит прокси: тяжёлые шаги doctor идут через прокси, и с отозванным
    токеном сердцебиения расшифровка вставала бы заодно."""
    return код in (401, 403) and прокси is None


def лог(текст):
    # без адресов и токена: лог узла тоже может уехать в чат с вопросом
    print("%s node_agent: %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), текст), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="сердцебиение узла на doctor (Т7.2)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--new-secret", action="store_true", dest="new_secret",
                    help="напечатать новый общий секрет: MARA_NODE_SECRET на doctor и на узле")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args(argv)
    if a.self_check:
        return self_check()
    if a.new_secret:
        print(secrets.token_hex(32))
        return 0
    url, token = os.environ.get("MARA_URL"), os.environ.get("MARA_NODE_TOKEN")
    прокси = None
    if not a.once:
        try:
            прокси = запустить_прокси(os.environ)
        except (OSError, ValueError, OverflowError) as e:
            # занятый или кривой порт — без прокси doctor не получит ни
            # одного ответа узла; сердцебиение при этом идёт, а молчать нельзя
            лог("прокси не поднялся: %s" % type(e).__name__)
        if прокси:
            лог("прокси сервисов узла слушает порт %d" % прокси.server_address[1])
        elif os.environ.get("MARA_NODE_SECRET"):
            лог("секрет задан, а прокси нет — нужен MARA_NODE_PROXY_PORT")
    if not (url and token):
        лог("нужны MARA_URL и MARA_NODE_TOKEN (contextd.py --pair-node <узел>)")
        if прокси is None:
            return 2
        # прокси без сердцебиения: шаги doctor ходят через него и так
        while True:
            time.sleep(3600)
    while True:
        try:
            тело = собрать()
            код = отправить(url, token, тело)
        except Exception as e:
            # сбой одного оборота (чужой ответ на порту сервиса, обрыв
            # посреди чтения) — не смерть агента: планировщик упавшего не
            # поднимет, и doctor молча перестал бы видеть паузу
            лог("оборот не удался: %s" % type(e).__name__)
            if a.once:
                return 1
            time.sleep(интервал(os.environ))
            continue
        лог("сердцебиение: %s, возможности %s из проверенных %s, пауза %s, игра %s" % (
            код or "нет связи", ",".join(тело["capabilities"]) or "нет",
            ",".join(тело["checked"]) or "нет",
            "нет" if тело["accept_jobs"] else "да",
            "да" if тело["minecraft_active"] else "нет"))
        if код in (401, 403):
            лог("токен не принят — перевыпустить: contextd.py --pair-node <узел>")
            if a.once or выходить_ли(код, прокси):
                return 1
        if код == 409:
            лог("doctor не принял: строка узла записана под другим id")
        if a.once:
            return 0 if код == 200 else 1
        time.sleep(тело["interval_s"])


def self_check():
    assert игра_идёт({"javaw.exe", "explorer.exe"})
    assert not игра_идёт({"explorer.exe"})
    assert игра_идёт({"java"}, игра=["java"])
    assert интервал({"MARA_NODE_INTERVAL": "5"}) == 10
    assert интервал({"MARA_NODE_INTERVAL": "мусор"}) == ИНТЕРВАЛ
    т = собрать(env={"MARA_NODE_ACCEPT": "0"}, имена=set())
    assert т["capabilities"] == [] and т["checked"] == [] and т["accept_jobs"] is False
    assert т["minecraft_active"] is False and т["interval_s"] == ИНТЕРВАЛ
    assert отвечает(None) == (False, b"")

    class Ответ:
        def __init__(self, stdout):
            self.stdout = stdout
    assert видеокарта(lambda *a, **k: Ответ("1200, 16303, 37\n"), lambda x: x) == (1200, 16303, 37)
    assert видеокарта(lambda *a, **k: Ответ("1200, 16303, [N/A]\n"), lambda x: x) == (1200, 16303, None)
    assert видеокарта(lambda *a, **k: Ответ(""), lambda x: x) == (None, None, None)
    assert видеокарта(есть=lambda x: None) == (None, None, None)
    п = Прокси(("127.0.0.1", 0), "k")
    try:
        з = подписать_запрос("k", "POST", "/transcribe", b"x")
        assert п.беда_запроса("POST", "/transcribe", b"x", з) is None
        assert п.беда_запроса("POST", "/transcribe", b"x", з) == "повтор запроса"
        assert п.беда_запроса("POST", "/transcribe", b"y", подписать_запрос(
            "k", "POST", "/transcribe", b"x")) == "подпись не сходится"
        assert п.беда_запроса("POST", "/transcribe", b"x", {}) == "нет подписи"
    finally:
        п.server_close()
    assert подпись_привета("k", "0" * 32) != подпись_ответа("k", "0" * 32, 200, b"")
    print("node_agent self-check: ок")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
