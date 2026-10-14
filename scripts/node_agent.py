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
import argparse, hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys
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
ТЕЛО_МАКС = 64 << 20          # кусок ASR — около 800 КБ, запрос к модели — килобайты
ПЕРЕСЫЛКА_С = 600             # дольше шага doctor не ждёт и сам (`HTTP_TIMEOUT`)
НОМЕР = re.compile(r"[0-9a-f]{32}\Z")


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


_доказано = {}                # база узла → monotonic последнего привета


def привет(база, секрет, таймаут=ТАЙМАУТ):
    """Узел доказывает секрет на номере doctor — до того, как ему уйдёт
    аудио или текст. Сеть упала — исключение сети, как и без подписи."""
    номер = secrets.token_hex(16)
    try:
        with urllib.request.urlopen("%s%s?nonce=%s" % (база, ПРИВЕТ, номер),
                                    timeout=таймаут) as r:
            знак = r.headers.get("X-Mara-Sig") or ""
    except urllib.error.HTTPError:
        знак = ""
    if not hmac.compare_digest(знак, подпись_привета(секрет, номер)):
        raise ЧужойУзел("узел не подтвердил общий секрет — аудио и текст ему не отданы")


def к_узлу(url, тело, тип, секрет=None, таймаут=300):
    """POST шага doctor в сервис узла; тело ответа. Без секрета — как раньше,
    напрямую. С секретом — привет (раз в `ПРИВЕТ_С` на узел), подписанный
    запрос и проверка подписи ответа: неверная — `ЧужойУзел`, ответ не
    принят. Отказ сервиса (4xx/5xx) — `HTTPError`, как и без подписи."""
    req = urllib.request.Request(url, data=тело, method="POST")
    req.add_header("Content-Type", тип)
    if not секрет:
        with urllib.request.urlopen(req, timeout=таймаут) as r:
            return r.read()
    ч = urllib.parse.urlsplit(url)
    база = "%s://%s" % (ч.scheme, ч.netloc)
    if time.monotonic() - _доказано.get(база, float("-inf")) > ПРИВЕТ_С:
        привет(база, секрет)
        _доказано[база] = time.monotonic()
    заголовки = подписать_запрос(секрет, "POST", ч.path + ("?" + ч.query if ч.query else ""),
                                 тело)
    for k, v in заголовки.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=таймаут) as r:
        ответ, код, знак = r.read(), r.status, r.headers.get("X-Mara-Sig") or ""
    if not hmac.compare_digest(знак, подпись_ответа(секрет, заголовки["X-Mara-Nonce"],
                                                     код, ответ)):
        _доказано.pop(база, None)
        raise ЧужойУзел("ответ узла без верной подписи — не принят")
    return ответ


class Прокси(ThreadingHTTPServer):
    """Прокси сервисов узла: пересылает только подписанное общим секретом и
    подписывает ответ (см. шапку). Одноразовые номера помнит `2 × ОКНО_С`:
    повтор запроса в пределах окна времени — 401."""
    daemon_threads = True

    def __init__(self, адрес, секрет, asr=None, llm=None):
        super().__init__(адрес, _ПроксиОбработчик)
        self.секрет, self.asr, self.llm = секрет, asr, llm
        self.номера, self.замок = {}, threading.Lock()

    def беда_запроса(self, метод, путь, тело, заголовки):
        """Почему запрос не пересылать, или None."""
        try:
            когда = int(заголовки.get("X-Mara-Ts") or "")
        except ValueError:
            return "нет подписи"
        номер = заголовки.get("X-Mara-Nonce") or ""
        if not НОМЕР.match(номер):
            return "нет подписи"
        сейчас = time.time()
        if abs(сейчас - когда) > ОКНО_С:
            return "время запроса вне окна — часы doctor и узла расходятся?"
        ждём = подписать_запрос(self.секрет, метод, путь, тело, когда, номер)["X-Mara-Sig"]
        if not hmac.compare_digest(заголовки.get("X-Mara-Sig") or "", ждём):
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

    def log_message(self, *a):
        pass            # без адресов и путей: журнал узла может уехать в чат

    def _ответ(self, код, тело, тип="application/json", номер=None):
        self.send_response(код)
        self.send_header("Content-Type", тип)
        self.send_header("Content-Length", str(len(тело)))
        if номер:
            self.send_header("X-Mara-Sig", подпись_ответа(self.server.секрет, номер, код, тело))
        self.end_headers()
        self.wfile.write(тело)

    def _беда(self, код, текст):
        self._ответ(код, _json({"error": текст}))

    def do_GET(self):
        ч = urllib.parse.urlsplit(self.path)
        if ч.path != ПРИВЕТ:
            return self._переслать("GET")
        номер = urllib.parse.parse_qs(ч.query).get("nonce", [""])[0]
        if not НОМЕР.match(номер):
            return self._беда(400, "нужен nonce — 32 шестнадцатеричных знака")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("X-Mara-Sig", подпись_привета(self.server.секрет, номер))
        self.end_headers()

    def do_POST(self):
        self._переслать("POST")

    def _переслать(self, метод):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._беда(400, "кривой Content-Length")
        if n < 0 or n > ТЕЛО_МАКС:
            self.close_connection = True
            return self._беда(413, "тело больше %d МиБ" % (ТЕЛО_МАКС >> 20))
        тело = self.rfile.read(n) if n else b""
        беда = self.server.беда_запроса(метод, self.path, тело, self.headers)
        if беда:
            return self._беда(401, беда)
        номер = self.headers["X-Mara-Nonce"]
        цель = self.server.куда(urllib.parse.urlsplit(self.path).path)
        if not цель:
            return self._ответ(404, _json({"error": "сервиса на узле не задано"}), номер=номер)
        req = urllib.request.Request(цель.rstrip("/") + self.path, data=тело or None, method=метод)
        if self.headers.get("Content-Type"):
            req.add_header("Content-Type", self.headers["Content-Type"])
        try:
            with _без_прокси().open(req, timeout=ПЕРЕСЫЛКА_С) as r:
                код, ответ, тип = r.status, r.read(), r.headers.get("Content-Type") or "application/json"
        except urllib.error.HTTPError as e:
            код, ответ, тип = e.code, e.read(), e.headers.get("Content-Type") or "application/json"
        except (OSError, ValueError):
            код, ответ, тип = 502, _json({"error": "сервис узла не ответил"}), "application/json"
        self._ответ(код, ответ, тип, номер=номер)


def запустить_прокси(env):
    """Прокси в потоке, если заданы секрет и порт; иначе None."""
    секрет, порт = env.get("MARA_NODE_SECRET"), env.get("MARA_NODE_PROXY_PORT")
    if not (секрет and порт):
        return None
    сервер = Прокси((env.get("MARA_NODE_PROXY_BIND") or "0.0.0.0", int(порт)), секрет,
                    asr=env.get("MARA_NODE_ASR_URL"), llm=env.get("MARA_NODE_LLM_URL"))
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    return сервер


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
    if not (url and token):
        лог("нужны MARA_URL и MARA_NODE_TOKEN (contextd.py --pair-node <узел>)")
        return 2
    if not a.once:
        try:
            прокси = запустить_прокси(os.environ)
        except (OSError, ValueError) as e:
            # занятый или кривой порт — без прокси doctor не получит ни
            # одного ответа узла; сердцебиение при этом идёт, а молчать нельзя
            лог("прокси не поднялся: %s" % type(e).__name__)
            прокси = None
        if прокси:
            лог("прокси сервисов узла слушает порт %d" % прокси.server_address[1])
        elif os.environ.get("MARA_NODE_SECRET"):
            лог("секрет задан, а прокси нет — нужен MARA_NODE_PROXY_PORT")
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
