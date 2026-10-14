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

Настройка — окружение (адреса и токен в репозиторий не кладутся, Г7):
  MARA_URL            адрес contextd на doctor, например http://<doctor>:8788
  MARA_NODE_TOKEN     токен из `contextd.py --pair-node <узел>`
  MARA_NODE_ASR_URL   локальный STT-сервис (необязательно)
  MARA_NODE_LLM_URL   локальная Ollama (необязательно)
  MARA_NODE_INTERVAL  секунд между сердцебиениями, по умолчанию 60
  MARA_NODE_ACCEPT    0 — пауза, по умолчанию 1
  MARA_GAME_PROCESSES процессы игры через запятую

    python node_agent.py            # раз в интервал, пока не остановят
    python node_agent.py --once     # одно сердцебиение и выход
    python node_agent.py --self-check

Чего агент не делает: аутентификации запросов к самим сервисам — STT и
Ollama чужие, и шаг ходит к ним с doctor напрямую. Это записанный риск
(threat-model п. 9), а не свойство.
"""
import argparse, json, os, shutil, subprocess, sys, time, urllib.error, urllib.request

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


def лог(текст):
    # без адресов и токена: лог узла тоже может уехать в чат с вопросом
    print("%s node_agent: %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), текст), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="сердцебиение узла на doctor (Т7.2)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args(argv)
    if a.self_check:
        return self_check()
    url, token = os.environ.get("MARA_URL"), os.environ.get("MARA_NODE_TOKEN")
    if not (url and token):
        лог("нужны MARA_URL и MARA_NODE_TOKEN (contextd.py --pair-node <узел>)")
        return 2
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
    print("node_agent self-check: ок")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
