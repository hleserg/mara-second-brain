#!/usr/bin/env python3
"""Зонд для `RES-REC-001` (ТЗ §7.3, §20): что ACR Phone на самом деле шлёт
«Отчётом на удалённый сервер» (поля Server и Secret) и облачной службой
WebHook. Документации протокола у производителя нет: справка «Call
Reporting» говорит только, что после звонка уходит POST с данными формы,
успех — 200, а 401 и 404 выключают отчёт; пример сервера WebHook
(`docs/research/RES-REC-001-acr-remote-report.md`) — косвенная улика, не
протокол. Зонд — контролируемая точка приёма из §20: записывает форму
каждого запроса, ничего не принимая в работу.

    python3 scripts/acr_report_probe.py --port 8799
    python3 scripts/acr_report_probe.py --port 8799 --status 500   # повторяет ли
    python3 scripts/acr_report_probe.py --port 8799 --status 404   # выключится ли
    python3 scripts/acr_report_probe.py --self-check

В ACR Phone: Server — `http://<doctor>:8799/` (путь любой), Secret — любое
слово; его же можно дать зонду `--secret`, тогда журнал скажет, совпало ли.

Журнал (`--log`, по умолчанию `~/.local/state/mara/acr-probe.jsonl`, права
0600) — строка JSON на запрос: время, метод, путь формой, заголовки (без
значений `Authorization` и `Cookie` — только длина), тип и размер тела, поля
формы — имя, длина и форма значения: цифры — `9`, буквы — `a`, прочее как
есть. Номер телефона, имя и заметка так в журнал не попадают, а формат
виден. Значения полей из `--show` (по умолчанию дата и длительность — по ним
видно, секунды это или миллисекунды) пишутся как есть, если в них нет семи
цифр подряд. У файла — поле, имя файла формой, тип, размер, sha256 и первые
байты (по ним виден формат: m4a, mp3, amr, wav). Секрет — только длина и
совпадение. Тело на диск не ложится, если не задан `--keep`.
"""
import argparse, hashlib, json, os, re, sys, threading, time, urllib.parse
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ЖУРНАЛ = os.path.join(os.environ.get("MARA_STATE") or os.path.expanduser("~/.local/state/mara"),
                      "acr-probe.jsonl")
ПОКАЗАТЬ = ("date", "duration", "time", "timestamp", "direction", "type", "version")
ПРЕДЕЛ_МБ = 300                 # запись звонка в час — десятки мегабайт
СЕМЬ_ЦИФР = re.compile(r"\d{7}")
СЕКРЕТНЫЕ = ("secret", "token", "password", "key")
СКРЫТЫЕ_ЗАГОЛОВКИ = ("authorization", "cookie", "proxy-authorization")


def форма(значение):
    """Форма значения без содержания: цифры — `9`, буквы любого алфавита — `a`,
    прочие знаки как есть. Длинное — обрезано до 64 знаков формы."""
    return "".join("9" if c.isdigit() else "a" if c.isalpha() else c
                   for c in значение[:64])


def эпоха(значение):
    """Целое — метка эпохи 2020–2033 годов, в секундах или миллисекундах.
    Окно узкое нарочно: номер телефона в него не попадает — у российских
    номеров нет кодов на 15–19 (городские — 3xx, 4xx, 8xx, мобильные — 9xx),
    а метки этих лет начинаются ровно так."""
    if not re.fullmatch(r"\d{10}|\d{13}", значение):
        return False
    v = int(значение)
    return 1_577_836_800 <= v < 2_000_000_000 or 1_577_836_800_000 <= v < 2_000_000_000_000


def поле(имя, значение, показать, секрет=None):
    """Запись о поле формы для журнала — без содержания."""
    з = {"name": имя, "length": len(значение)}
    низ = имя.lower()
    if any(s in низ for s in СЕКРЕТНЫЕ):
        if секрет is not None:
            з["matches_secret"] = значение == секрет
        return з
    if низ in показать and len(значение) <= 64 and (
            not СЕМЬ_ЦИФР.search(значение) or эпоха(значение)):
        з["value"] = значение
    else:
        з["shape"] = форма(значение)
    return з


def файл(имя, имя_файла, тип, тело, keep=None):
    з = {"name": имя, "filename_shape": форма(имя_файла or ""), "content_type": тип,
         "size": len(тело), "sha256": hashlib.sha256(тело).hexdigest(),
         "magic": тело[:12].hex()}
    if keep and тело:
        os.makedirs(keep, mode=0o700, exist_ok=True)
        путь = os.path.join(keep, з["sha256"])
        with open(путь, "wb") as fh:
            fh.write(тело)
        os.chmod(путь, 0o600)
        з["kept"] = True
    return з


def разобрать(тип, тело, показать=ПОКАЗАТЬ, секрет=None, keep=None):
    """(поля, файлы) тела запроса по типу содержимого."""
    поля, файлы = [], []
    базовый = (тип or "").split(";")[0].strip().lower()
    if базовый == "multipart/form-data":
        сообщение = BytesParser(policy=HTTP).parsebytes(
            b"Content-Type: " + тип.encode("latin-1") + b"\r\n\r\n" + тело)
        for часть in сообщение.iter_parts():
            имя = часть.get_param("name", header="content-disposition") or ""
            данные = часть.get_payload(decode=True) or b""
            if часть.get_filename() is not None:
                файлы.append(файл(имя, часть.get_filename(), часть.get_content_type(),
                                  данные, keep))
            else:
                поля.append(поле(имя, данные.decode("utf-8", "replace"), показать, секрет))
    elif базовый == "application/x-www-form-urlencoded":
        for имя, значение in urllib.parse.parse_qsl(тело.decode("utf-8", "replace"),
                                                     keep_blank_values=True):
            поля.append(поле(имя, значение, показать, секрет))
    elif базовый == "application/json":
        try:
            d = json.loads(тело or b"null")
        except ValueError:
            d = None
        if isinstance(d, dict):
            for имя, значение in d.items():
                поля.append(поле(str(имя), значение if isinstance(значение, str)
                                 else json.dumps(значение), показать, секрет))
    elif тело:
        файлы.append(файл("", "", базовый, тело, keep))
    return поля, файлы


class Зонд(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, адрес, журнал=ЖУРНАЛ, статус=200, задержка=0.0, секрет=None,
                 keep=None, показать=ПОКАЗАТЬ, предел=ПРЕДЕЛ_МБ << 20):
        super().__init__(адрес, _Обработчик)
        self.журнал, self.статус, self.задержка = журнал, статус, задержка
        self.секрет, self.keep, self.показать, self.предел = секрет, keep, показать, предел
        self.замок = threading.Lock()

    def записать(self, запись):
        with self.замок:
            каталог = os.path.dirname(self.журнал)
            if каталог:
                os.makedirs(каталог, mode=0o700, exist_ok=True)
            fd = os.open(self.журнал, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(запись, ensure_ascii=False) + "\n")


class _Обработчик(BaseHTTPRequestHandler):
    server_version = "mara-acr-probe"

    def log_message(self, *a):
        pass                    # адрес телефона и путь в журнал процесса не идут

    def _тело(self):
        """(тело, усечено). Длина — по Content-Length или кусками chunked."""
        предел = self.server.предел
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            куски, всего = [], 0
            while True:
                строка = self.rfile.readline(64)
                try:
                    n = int(строка.split(b";")[0].strip() or b"0", 16)
                except ValueError:
                    return b"".join(куски), True
                if n == 0:
                    self.rfile.readline(64)
                    return b"".join(куски), False
                кусок = self.rfile.read(n)
                self.rfile.readline(64)
                всего += n
                if всего > предел:
                    return b"".join(куски), True
                куски.append(кусок)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b"", True
        if n > предел:
            return b"", True
        return self.rfile.read(n) if n > 0 else b"", False

    def _принять(self):
        t0 = time.time()
        тело, усечено = self._тело()
        тип = self.headers.get("Content-Type") or ""
        заголовки = {k.lower(): (("<%d знаков>" % len(v)) if k.lower() in СКРЫТЫЕ_ЗАГОЛОВКИ
                                 else v)
                     for k, v in self.headers.items()}
        запись = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "method": self.command,
                  "path_shape": форма(urllib.parse.urlsplit(self.path).path),
                  "query_fields": [поле(и, з, self.server.показать, self.server.секрет)
                                   for и, з in urllib.parse.parse_qsl(
                                       urllib.parse.urlsplit(self.path).query,
                                       keep_blank_values=True)],
                  "http": self.request_version, "headers": заголовки,
                  "content_type": тип.split(";")[0].strip(), "body_size": len(тело),
                  "truncated": усечено, "replied": self.server.статус}
        if not усечено:
            try:
                запись["fields"], запись["files"] = разобрать(
                    тип, тело, self.server.показать, self.server.секрет, self.server.keep)
            except Exception as e:            # кривое тело — тоже ответ на вопрос
                запись["parse_error"] = type(e).__name__
        if self.server.задержка:
            time.sleep(self.server.задержка)
        запись["seconds"] = round(time.time() - t0, 3)
        self.server.записать(запись)
        print("%s %s %s → %d (%d байт, полей %d, файлов %d)" % (
            запись["at"], self.command, запись["content_type"] or "-",
            self.server.статус, len(тело), len(запись.get("fields", [])),
            len(запись.get("files", []))), flush=True)
        ответ = b"ok" if self.server.статус < 400 else b"probe"
        self.send_response(self.server.статус)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(ответ)))
        self.end_headers()
        self.wfile.write(ответ)

    do_POST = do_PUT = do_GET = _принять


def self_check():
    граница = "XyZ"
    тело = ("--{0}\r\nContent-Disposition: form-data; name=\"Source\"\r\n\r\n+79991234567\r\n"
            "--{0}\r\nContent-Disposition: form-data; name=\"Secret\"\r\n\r\nслово\r\n"
            "--{0}\r\nContent-Disposition: form-data; name=\"Date\"\r\n\r\n1760000000\r\n"
            "--{0}\r\nContent-Disposition: form-data; name=\"Note\"\r\n\r\nАнна Петрова\r\n"
            "--{0}\r\nContent-Disposition: form-data; name=\"File\"; "
            "filename=\"+79991234567 2026-10-14.m4a\"\r\nContent-Type: audio/mp4\r\n\r\n"
            ).format(граница).encode("utf-8") + b"\x00\x00\x00\x18ftypM4A " + (
            "\r\n--{0}--\r\n".format(граница)).encode("utf-8")
    поля, файлы = разобрать("multipart/form-data; boundary=" + граница, тело, секрет="слово")
    журнал = json.dumps([поля, файлы], ensure_ascii=False)
    assert "9991234567" not in журнал and "Анна" not in журнал and "слово" not in журнал, журнал
    по_имени = {п["name"]: п for п in поля}
    assert по_имени["Source"]["shape"] == "+99999999999", по_имени
    assert по_имени["Secret"] == {"name": "Secret", "length": 5, "matches_secret": True}
    assert по_имени["Date"]["value"] == "1760000000"
    assert файлы[0]["size"] == 12 and файлы[0]["magic"].endswith(b"ftypM4A ".hex())
    assert файлы[0]["filename_shape"] == "+99999999999 9999-99-99.a9a"
    assert поле("Duration", "65000", ПОКАЗАТЬ)["value"] == "65000"
    assert поле("date", "1760000000000", ПОКАЗАТЬ)["value"] == "1760000000000"
    for номер in ("79991234567", "89991234567", "9991234567", "+79991234567", "3812345678",
                  "4951234567"):
        assert "value" not in поле("Date", номер, ПОКАЗАТЬ), номер
    print("acr_report_probe self-check: ок")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="зонд RES-REC-001: что шлёт ACR Phone")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--log", default=ЖУРНАЛ)
    ap.add_argument("--status", type=int, default=200,
                    help="код ответа: 500 — повторяет ли ACR, 401/404 — выключает ли отчёт")
    ap.add_argument("--delay", type=float, default=0.0, help="секунд до ответа: таймаут ACR")
    ap.add_argument("--secret", help="слово из поля Secret: журнал скажет, совпало ли")
    ap.add_argument("--keep", help="каталог, куда сложить присланные файлы (по sha256)")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args(argv)
    if a.self_check:
        return self_check()
    зонд = Зонд((a.bind, a.port), журнал=a.log, статус=a.status, задержка=a.delay,
                секрет=a.secret, keep=a.keep)
    print("зонд слушает порт %d, отвечает %d, журнал %s" % (a.port, a.status, a.log),
          flush=True)
    try:
        зонд.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        зонд.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
