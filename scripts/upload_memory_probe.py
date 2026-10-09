#!/usr/bin/env python3
"""Замер памяти contextd на заливке аудио (Т3б.4; ТЗ §17.3 п.1).

§17.3 п.1 просит не «потоково по коду», а доказанный профиль: тело на сотни
мегабайт и измеренный RSS сервера, который от этого тела не растёт. Код
`слить()` читает кусками по `КУСОК` и пишет на диск, но код — обещание, а не
измерение; измерение здесь.

Как устроено. Сервер поднимается **отдельным процессом** во временном
каталоге: RSS меряется по `/proc/<pid>/status`, и мерить надо чужой процесс,
а не свой — в своём к памяти сервера примешается память клиента, который тело
и генерирует. Процесс запускается через `make_server`, а не `--serve`:
`serve()` заводит воркер, который на принятую запись тут же запустил бы
`call_asr.py` с ffmpeg на двухсотмегабайтном WAV. К памяти приёма это не
относится, зато минутами держит CPU и оставляет сироту после `kill`.
Устройство заводится тем же `pair()`, что и в тестах.

Тело — валидный WAV (заголовок `RIFF…WAVE`, дальше повторяющийся
псевдослучайный блок): `нюх()` иначе уложил бы его в карантин с 415. Карантин
для памяти ничем не хуже, но тогда замер доказывал бы профиль пути, которым
настоящая запись не ходит. Клиент тело в памяти не держит — генерирует и
шлёт кусками, и хеш для события считает первым проходом по тому же
генератору.

Что печатается: размер тела, RSS до заливки, пик RSS по выборкам во время
заливки (раз в `--period` секунд), `VmHWM` после (пик за всю жизнь
процесса — страховка на случай, если заливка кончилась быстрее одной выборки),
RSS после, код ответа, время. Критерий — `ПРЕДЕЛ_ПРИРОСТА`, см. рядом с ним.

    python3 scripts/upload_memory_probe.py                 # 200 МиБ
    python3 scripts/upload_memory_probe.py --mib 100 --json
    python3 scripts/upload_memory_probe.py --self-check    # несколько МиБ, быстро

Числа и выводы прогона — `docs/upload-memory-profile.md`.
"""
import os, sys, json, time, random, shutil, struct, hashlib, argparse, tempfile
import threading, subprocess, http.client, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import contextd

МиБ = 1 << 20
# Тело по умолчанию. Триста просил план, но `MAX_BODY` у contextd — 256 МиБ, и
# тело больше него получает 413 после слива `СЛИВ = 8 МиБ`: сервер честно не
# читает лишнее, и мерить там нечего. Двести — под лимитом с запасом, при этом
# в двадцать с лишним раз больше любого допустимого прироста, так что
# «линейно или нет» на нём видно без лупы.
ТЕЛО_МИБ = 200
# Сколько RSS сервер вправе набрать за заливку, независимо от размера тела.
# Что в этот прирост входит по коду: один `КУСОК` (1 МиБ) буфера чтения,
# столько же в `hashlib`, буферы `BufferedWriter` и сокета, соединение sqlite
# нового потока-обработчика — всё вместе несколько мегабайт. Замер
# 2026-10-05 (Python 3.11, контейнер) на телах от 4 до 256 МиБ дал прирост
# 2,2 МиБ — одинаковый при любом теле; контрольный сервер, читавший тело одним
# `read()`, на тех же приборах показал прирост в размер тела (50,2 МиБ на 50,
# 100,2 на 100). Предел — 32 МиБ: пятнадцатикратный запас на аллокатор и
# чужую версию питона, и всё равно в шесть раз меньше тела по умолчанию, на
# котором проверяется «не линейно». Если RSS растёт с телом, порог
# пробивается уже на первых тридцати мегабайтах.
ПРЕДЕЛ_ПРИРОСТА = 32 * МиБ
КУСОК = 1 * МиБ                              # шаг генерации и отправки тела
ЗАГОЛОВОК = 44                               # байт WAV-заголовка (PCM, один чанк)
ПЕРИОД = 0.2                                 # с, выборка RSS

# Служебный процесс сервера. Пишет порт в файл, а не в stdout: читать его
# трубу нам пришлось бы до самого конца — иначе журнал запросов забьёт её и
# сервер встанет на `print`. Файл с портом появляется после `bind`, так что
# первое же соединение после него встаёт в очередь, а не отбивается.
СЕРВЕР = r"""
import os, sys, threading
sys.path.insert(0, sys.argv[1])
import contextd
root, vault, порт_файл = sys.argv[2], sys.argv[3], sys.argv[4]
srv = contextd.make_server(root, 0, vault=vault)
with open(порт_файл + ".tmp", "w") as fh:
    fh.write(str(srv.server_address[1]))
os.replace(порт_файл + ".tmp", порт_файл)
srv.serve_forever()
"""


def wav_заголовок(n):
    """Заголовок PCM WAV на тело в `n` байт целиком: 16 кГц, моно, 16 бит."""
    данных = n - ЗАГОЛОВОК
    return (b"RIFF" + struct.pack("<I", n - 8) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
            + b"data" + struct.pack("<I", данных))


def тело(n, семя=1):
    """Куски тела в `n` байт: заголовок и дальше один псевдослучайный блок по
    кругу. Два прохода по одному семени дают одни и те же байты — первый
    считает хеш для события, второй шлёт."""
    assert n >= ЗАГОЛОВОК
    блок = random.Random(семя).randbytes(КУСОК)
    yield wav_заголовок(n)
    осталось = n - ЗАГОЛОВОК
    while осталось > 0:
        кусок = блок[:min(КУСОК, осталось)]
        yield кусок
        осталось -= len(кусок)


def sha256_тела(n):
    h = hashlib.sha256()
    for кусок in тело(n):
        h.update(кусок)
    return h.hexdigest()


def память(pid):
    """VmRSS и VmHWM процесса в байтах из `/proc/<pid>/status`.

    Только Linux — там и живёт contextd. Нет файла — процесс умер; это
    ошибка замера, а не ноль."""
    out = {}
    with open("/proc/%d/status" % pid) as fh:
        for line in fh:
            if line.startswith(("VmRSS:", "VmHWM:")):
                ключ, число = line.split(":", 1)
                out[ключ] = int(число.split()[0]) * 1024
    return out["VmRSS"], out["VmHWM"]


class Наблюдатель(threading.Thread):
    """Раз в `период` снимает RSS сервера и держит максимум."""

    def __init__(self, pid, период):
        super().__init__(daemon=True)
        self.pid, self.период = pid, период
        self.стоп = threading.Event()
        self.пик, self.выборок = 0, 0

    def снять(self):
        rss, _ = память(self.pid)
        self.пик = max(self.пик, rss)
        self.выборок += 1

    def run(self):
        while not self.стоп.is_set():
            try:
                self.снять()
            except (FileNotFoundError, ProcessLookupError):
                return
            self.стоп.wait(self.период)


def поднять(root, vault, журнал):
    """Сервер подпроцессом. Возвращает (процесс, порт)."""
    env = dict(os.environ, MARA_BLOBS=root, VAULT=vault, MARA_VAULT=vault,
               # как в scripts/run-tests.sh: боевые env-файлы doctor в замер
               # не читаются
               MARA_ENV_FILE="/nonexistent/mara-env",
               MARA_CONTEXTD_ENV="/nonexistent/mara-contextd-env")
    for k in ("MARA_BIND", "MARA_PORT"):
        env.pop(k, None)
    порт_файл = os.path.join(root, "порт")
    proc = subprocess.Popen([sys.executable, "-c", СЕРВЕР, HERE, root, vault,
                             порт_файл], env=env, stdout=журнал, stderr=журнал)
    предел = time.time() + 15
    while not os.path.exists(порт_файл):
        if proc.poll() is not None:
            raise RuntimeError("сервер не поднялся, код %d; журнал: %s"
                               % (proc.returncode, журнал.name))
        if time.time() > предел:
            proc.kill()
            raise RuntimeError("сервер не открыл порт за 15 с; журнал: %s"
                               % журнал.name)
        time.sleep(0.02)
    with open(порт_файл) as fh:
        return proc, int(fh.read())


def событие(base, token, sha, n):
    """Событие звонка под будущую заливку. Возвращает его id."""
    ev = {"schema": "mara.event.v1", "kind": "call", "source": "upload-memory-probe",
          "source_id": "probe-%d" % time.time_ns(),
          "occurred_at": mi.now_iso(), "classification": "personal",
          "payload": {"contact_name": "проба", "direction": "incoming",
                      "producer": "upload-memory-probe"},
          "blob": {"sha256": sha, "ext": "wav", "bytes": n}}
    req = urllib.request.Request(base + "/v1/ingest/event", method="POST",
                                 data=json.dumps(ev).encode("utf-8"))
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        got = json.loads(r.read())
    if not got.get("need_blob"):
        raise RuntimeError("сервер не просит блоб: %r" % got)
    return got["event_id"]


def залить(порт, token, eid, n):
    """Тело кусками через один сокет. Возвращает (код, ответ)."""
    c = http.client.HTTPConnection("127.0.0.1", порт, timeout=600)
    c.putrequest("POST", "/v1/ingest/audio?event=" + eid)
    c.putheader("Authorization", "Bearer " + token)
    c.putheader("Content-Type", "application/octet-stream")
    c.putheader("Content-Length", str(n))
    c.endheaders()
    for кусок in тело(n):
        c.send(кусок)
    r = c.getresponse()
    ответ = json.loads(r.read() or b"{}")
    c.close()
    return r.status, ответ


def замер(mib=ТЕЛО_МИБ, период=ПЕРИОД):
    """Один прогон: поднять сервер, залить тело, снять память, прибрать.

    Возвращает словарь с числами в байтах; `прирост` — на сколько пик RSS
    (больший из пика выборок и `VmHWM` после) выше RSS до заливки."""
    n = int(mib * МиБ)
    if n > contextd.MAX_BODY:
        raise SystemExit("тело %d МиБ больше MAX_BODY = %d МиБ: сервер ответит 413 "
                         "и прочитает только %d МиБ — мерить нечего"
                         % (mib, contextd.MAX_BODY >> 20, contextd.СЛИВ >> 20))
    tmp = tempfile.mkdtemp(prefix="mara-upload-probe.")
    root, vault = os.path.join(tmp, "blobs"), os.path.join(tmp, "vault")
    os.makedirs(vault)
    proc = None
    try:
        con = mi.connect(root)                 # заводит схему до старта сервера
        _, token = contextd.pair(con, "upload-memory-probe")
        con.close()
        журнал = open(os.path.join(tmp, "contextd.log"), "wb")
        with журнал:
            proc, порт = поднять(root, vault, журнал)
        base = "http://127.0.0.1:%d" % порт
        sha = sha256_тела(n)
        eid = событие(base, token, sha, n)
        rss_до, hwm_до = память(proc.pid)
        глаз = Наблюдатель(proc.pid, период)
        глаз.снять()
        глаз.start()
        t0 = time.monotonic()
        код, ответ = залить(порт, token, eid, n)
        секунд = time.monotonic() - t0
        глаз.стоп.set()
        глаз.join()
        глаз.снять()
        rss_после, hwm_после = память(proc.pid)
        пик = max(глаз.пик, hwm_после)
        return {"байт": n, "rss_до": rss_до, "hwm_до": hwm_до,
                "пик_выборок": глаз.пик, "выборок": глаз.выборок,
                "hwm_после": hwm_после, "rss_после": rss_после,
                # от большего из RSS и пика до заливки: пик старта (импорт,
                # схема) — не заливка, и в прирост не входит (ревью, P3-4)
                "пик": пик, "прирост": пик - max(rss_до, hwm_до),
                "код": код, "ответ": ответ, "секунд": секунд,
                "python": sys.version.split()[0]}
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)


def мб(b):
    return "%.1f МиБ" % (b / МиБ)


def печать(r):
    print("тело            %s" % мб(r["байт"]))
    print("RSS до          %s  (VmHWM до %s)" % (мб(r["rss_до"]), мб(r["hwm_до"])))
    print("пик RSS         %s  (выборок %d, VmHWM после %s)"
          % (мб(r["пик"]), r["выборок"], мб(r["hwm_после"])))
    print("RSS после       %s" % мб(r["rss_после"]))
    print("прирост         %s  (предел %s)" % (мб(r["прирост"]), мб(ПРЕДЕЛ_ПРИРОСТА)))
    print("код ответа      %d  %s" % (r["код"], json.dumps(r["ответ"], ensure_ascii=False)))
    print("время           %.2f с  (%.0f МиБ/с)"
          % (r["секунд"], r["байт"] / МиБ / max(r["секунд"], 1e-9)))
    print("python          %s" % r["python"])


def проверить(r):
    """Что замер считается пройденным. Общая для --self-check и теста."""
    assert r["код"] == 200, "заливка не принята: %d %r" % (r["код"], r["ответ"])
    assert r["ответ"].get("bytes") == r["байт"], "сервер принял не всё: %r" % r["ответ"]
    assert r["прирост"] < ПРЕДЕЛ_ПРИРОСТА, "RSS вырос на %s при пределе %s" % (
        мб(r["прирост"]), мб(ПРЕДЕЛ_ПРИРОСТА))
    assert r["прирост"] < r["байт"], "RSS вырос на размер тела: %s" % мб(r["прирост"])


def self_check():
    """Маленькое тело, тот же путь. Проверяет сам стенд, а не профиль:
    профиль на четырёх мегабайтах не виден, зато виден сломанный клиент,
    упавший сервер и 415 от испорченного заголовка."""
    assert contextd.нюх(wav_заголовок(ЗАГОЛОВОК + 4))[0] == "wav", "заголовок не узнан"
    куски = list(тело(ЗАГОЛОВОК + 3 * КУСОК + 7))
    assert sum(map(len, куски)) == ЗАГОЛОВОК + 3 * КУСОК + 7, "тело не той длины"
    assert sha256_тела(2 * МиБ) == sha256_тела(2 * МиБ), "тело не воспроизводится"
    r = замер(mib=4)
    проверить(r)
    print("upload_memory_probe self-check: ок (тело %s, прирост %s, %.2f с)"
          % (мб(r["байт"]), мб(r["прирост"]), r["секунд"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="замер RSS contextd на заливке аудио")
    ap.add_argument("--mib", type=float, default=ТЕЛО_МИБ,
                    help="размер тела в МиБ (по умолчанию %d, не больше MAX_BODY = %d)"
                         % (ТЕЛО_МИБ, contextd.MAX_BODY >> 20))
    ap.add_argument("--period", type=float, default=ПЕРИОД,
                    help="период выборки RSS, с (по умолчанию %.1f)" % ПЕРИОД)
    ap.add_argument("--json", action="store_true", help="числа в JSON, байтами")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args(argv)
    if a.self_check:
        return self_check()
    r = замер(a.mib, a.period)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        печать(r)
    try:
        проверить(r)
    except AssertionError as e:
        print("НЕ ПРОЙДЕНО: %s" % e)
        return 1
    print("ок: прирост %s при теле %s" % (мб(r["прирост"]), мб(r["байт"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
