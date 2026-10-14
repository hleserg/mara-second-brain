#!/usr/bin/env python3
"""Зонд для `RES-REC-001` (ТЗ §7.3, §20): что ACR Phone на самом деле шлёт
«Отчётом на удалённый сервер» (поля Server и Secret) и облачной службой
WebHook. Протокол производитель не описал: справка «Call Reporting» говорит
только, что после звонка уходит запрос с данными формы, успех — 200, а 401
и 404 выключают отчёт; пример сервера WebHook
(`docs/research/RES-REC-001-acr-remote-report.md`) — косвенная улика, а не
протокол. Зонд — контролируемая точка приёма из §20: принимает любой запрос
и записывает его форму, ничего не беря в работу.

    python3 scripts/acr_report_probe.py --port 8799
    python3 scripts/acr_report_probe.py --port 8799 --status 500   # повторяет ли
    python3 scripts/acr_report_probe.py --port 8799 --status 404   # выключится ли
    python3 scripts/acr_report_probe.py --port 8799 --delay 120    # таймаут
    python3 scripts/acr_report_probe.py --self-check

В ACR Phone: Server — `http://<doctor>:8799/acr`, Secret — одноразовое
слово, не пароль: оно останется в истории оболочки и видно в `ps`. Его же
дать зонду `--secret` — журнал скажет, где и как оно пришло.

Журнал (`--log`, по умолчанию `~/.local/state/mara/acr-probe.jsonl`, права
0600) — строка JSON на событие, ASCII. `start` — настройки прогона (код,
задержка, список показа). На запрос две строки с общим `id`: `received`
пишется сразу, как пришло тело, ещё до задержки; `outcome` — после ответа:
код, ушёл ли ответ, через сколько секунд клиент бросил ждать, разбор тела.
`rejected` — то, что не стало HTTP-запросом (TLS на открытый порт и т. п.).

Содержания в журнале нет:
- значение — длина и форма: цифра — `9`, буква любого алфавита — `a`,
  пробельный знак — пробел, прочий печатный ASCII как есть, остальное — `·`.
  Номер, имя и заметка так не читаются, а формат виден;
- как есть пишутся только значения полей из `--show` (по умолчанию
  `ПОКАЗАТЬ`: по дате и длительности видны единицы), и то если значение —
  печатный ASCII до 64 знаков, а цифр в нём не больше шести или это метка
  эпохи 2020–2033 годов;
- имя поля как есть — только похожее на идентификатор, иначе форма;
- заголовки как есть — только служебные (`КАК_ЕСТЬ` и тип содержимого),
  прочие — форма; `Host` тоже форма: адрес doctor в журнал не идёт;
- секрет: поле или заголовок с «секретным» именем (`СЕКРЕТНЫЕ`) — только
  длина; с `--secret` — ещё и как связан со словом: `equal`, `contains`,
  `base64` (как в Basic), `hmac-sha256-hex` и т. п. (подпись тела);
  прочие значения, в которых есть слово, помечены `has_secret`;
- тело-текст и текстовый файл — только форма начала; двоичное — размер,
  формат по сигнатуре (m4a, mp3, amr, wav, ogg), sha256 — от 4 КиБ;
- отпечаток тела и файла — HMAC со случайным ключом прогона: повтор того же
  тела виден, а подобрать тело по отпечатку нельзя — ключ нигде не пишется.

Тело на диск не ложится без `--keep`. Тело больше `--max-mb` не читается:
ответ 413. Разбор тела в сотню мегабайт занимает секунды и гигабайт памяти —
для опыта хватает звонков в минуту-две.
"""
import argparse, base64, binascii, codecs, hashlib, hmac, json, os, re, select, socket, sys
import threading, time, urllib.parse
from email.parser import BytesParser
from email.policy import HTTP
from email.utils import collapse_rfc2231_value
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ЖУРНАЛ = os.path.join(os.environ.get("MARA_STATE") or os.path.expanduser("~/.local/state/mara"),
                      "acr-probe.jsonl")
ПОКАЗАТЬ = ("date", "duration", "time", "timestamp", "direction", "type", "version")
ПРЕДЕЛ_МБ = 100                 # час записи на 192 кбит/с — 86 МБ
ЦИФР_МАКС = 6                   # в значении «как есть» цифр не больше шести
СЕКРЕТНЫЕ = ("secret", "token", "password", "passwd", "key", "auth", "sign", "cookie",
             "hmac", "digest", "hash")
КАК_ЕСТЬ = ("content-length", "transfer-encoding", "content-encoding", "connection",
            "accept-encoding", "accept", "expect", "te", "user-agent")
СХЕМЫ = ("basic", "bearer", "digest", "token", "hmac", "signature", "apikey", "key", "oauth")
ПОЛЕЙ_МАКС = 200                # полей и заголовков в строке журнала
ГЛУБИНА = 3                     # вложенность JSON, которую разворачивать в поля
ДВОИЧНОЕ_SHA = 4096             # sha256 — только у двоичного не короче этого
ИМЯ = re.compile(r"[A-Za-z0-9_.\-\[\]]{1,40}")
ДЛИНА = re.compile(r"[0-9]{1,15}")
КУСОК = re.compile(rb"[0-9A-Fa-f]{1,16}")
МЕТОД = re.compile(r"[A-Z]{1,16}")
ВЕРСИЯ = re.compile(r"HTTP/[0-9]\.[0-9]")
ТИП = re.compile(r"[a-z0-9!#$&^_.+\-]{1,40}/[a-z0-9!#$&^_.+\-]{1,80}")
ЭПОХА = re.compile(r"[0-9]{10}|[0-9]{13}")


def цифр(значение):
    return sum(c.isdigit() for c in значение)


def форма(значение, предел=64):
    """Форма значения без содержания: цифра — `9`, буква любого алфавита —
    `a`, пробельный знак — пробел, прочий печатный ASCII как есть, остальное
    — `·`. Берутся первые `предел` знаков."""
    знаки = []
    for c in значение[:предел]:
        if c.isdigit():
            знаки.append("9")
        elif c.isalpha():
            знаки.append("a")
        elif c.isspace():
            знаки.append(" ")
        elif " " < c < "\x7f":
            знаки.append(c)
        else:
            знаки.append("·")
    return "".join(знаки)


def эпоха(значение):
    """Метка эпохи 2020–2033 годов в секундах или миллисекундах, ASCII-цифрами.
    Окно узкое нарочно: российский и североамериканский номер в него не
    попадают — у российских нет кодов на 15–19 (городские — 3xx, 4xx, 8xx,
    мобильные — 9xx), код зоны NANP не начинается с единицы, а метки этих
    лет начинаются ровно так."""
    if not ЭПОХА.fullmatch(значение):
        return False
    v = int(значение)
    return 1_577_836_800 <= v < 2_000_000_000 or 1_577_836_800_000 <= v < 2_000_000_000_000


def показать_ли(значение):
    """Можно ли значение поля из списка показа писать как есть."""
    return (len(значение) <= 64 and all(" " <= c < "\x7f" for c in значение)
            and (цифр(значение) <= ЦИФР_МАКС or эпоха(значение)))


def имя_поля(имя):
    """{"name": имя}, если имя похоже на идентификатор, иначе {"name_shape": форма}."""
    if ИМЯ.fullmatch(имя) and цифр(имя) <= ЦИФР_МАКС:
        return {"name": имя}
    return {"name_shape": форма(имя, 40)}


def секретное(имя):
    низ = имя.lower()
    return any(s in низ for s in СЕКРЕТНЫЕ)


def _хвост(имя):
    """Последнее звено имени из JSON (`call.duration` → `duration`)."""
    звенья = [з for з in re.split(r"[.\[\]]", имя) if з]
    return звенья[-1].lower() if звенья else ""


def сверить(значение, секрет, тело=None):
    """Как слово `--secret` связано со значением, не раскрывая ни того, ни
    другого: `equal`, `contains`, `base64` (как в Basic), `hmac-<алгоритм>-<hex
    или base64>` — подпись тела этим словом, иначе `no`."""
    if значение == секрет:
        return "equal"
    if секрет in значение:
        return "contains"
    слова = значение.split()
    слово = слова[-1] if слова else ""
    ключ = секрет.encode("utf-8")
    try:
        if ключ in base64.b64decode(слово, validate=True):
            return "base64"
    except (binascii.Error, ValueError):
        pass
    if тело is not None and слово:
        кандидаты = {слово, слово.lower()}
        префикс = re.fullmatch(r"[A-Za-z0-9\-]{2,12}=(.+)", слово)   # sha256=<подпись>
        if префикс:
            кандидаты |= {префикс.group(1), префикс.group(1).lower()}
        for алгоритм in ("sha256", "sha1", "sha512", "md5"):
            mac = hmac.new(ключ, тело, алгоритм).digest()
            if mac.hex() in кандидаты:
                return "hmac-%s-hex" % алгоритм
            в64 = {base64.b64encode(mac).decode(), base64.urlsafe_b64encode(mac).decode()}
            if кандидаты & (в64 | {v.rstrip("=") for v in в64}):
                return "hmac-%s-base64" % алгоритм
    return "no"


def поле(имя, значение, показать=ПОКАЗАТЬ, секрет=None, тело=None):
    """Запись о поле формы, параметре запроса или заголовке — без содержания."""
    з = имя_поля(имя)
    з["length"] = len(значение)
    if секретное(имя):
        if секрет:
            з["secret"] = сверить(значение, секрет, тело)
        return з
    есть = bool(секрет) and секрет in значение
    if есть:
        з["has_secret"] = True
    if not есть and "name" in з and _хвост(имя) in показать and показать_ли(значение):
        з["value"] = значение
    else:
        з["shape"] = форма(значение)
    return з


def базовый_тип(тип):
    """Тип содержимого без параметров: как есть, если похож на тип, иначе форма."""
    базовый = (тип or "").split(";")[0].strip().lower()
    return базовый if not базовый or ТИП.fullmatch(базовый) else форма(базовый, 40)


def заголовок(имя, значение, секрет=None, тело=None):
    """Запись о заголовке. Служебные (`КАК_ЕСТЬ`) — как есть, если в значении
    не больше шести цифр; Content-Type — тип как есть, параметры — как поля
    (`charset` как есть, `boundary` формой); «секретные» — длина, схема из
    `СХЕМЫ` и связь со словом; прочие — форма."""
    try:                        # http.client читает заголовки как latin-1
        значение = значение.encode("latin-1").decode("utf-8")
    except UnicodeError:
        pass
    низ = имя.lower()
    if низ == "content-type":
        з = {"name": низ, "type": базовый_тип(значение)}
        параметры = []
        for кусок in значение.split(";")[1:ПОЛЕЙ_МАКС]:
            п, _, v = кусок.strip().partition("=")
            параметры.append(поле(п.strip(), v.strip().strip('"'), ("charset",)))
        if параметры:
            з["params"] = параметры
        return з
    if низ in КАК_ЕСТЬ and len(значение) <= 200 and (
            ДЛИНА.fullmatch(значение) if низ == "content-length" else
            all(" " <= c < "\x7f" for c in значение) and цифр(значение) <= ЦИФР_МАКС):
        return {"name": низ, "value": значение}
    з = поле(низ, значение, (), секрет, тело)
    if секретное(низ):
        схема = значение.split(" ", 1)[0].lower() if " " in значение else ""
        if схема in СХЕМЫ:
            з["scheme"] = схема
    return з


def текст_ли(данные):
    """Похоже ли на текст: UTF-8 без NUL или UTF-16 с меткой порядка байтов.
    Смотрятся первые 4 КиБ; обрезанный на краю знак UTF-8 не мешает."""
    начало = данные[:4096]
    if начало[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return True
    if b"\x00" in начало:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(начало, final=False)
    except UnicodeDecodeError:
        return False
    return True


def _текст(данные, предел=256):
    начало = данные[:предел * 4]
    if начало[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return начало.decode("utf-16", "replace")[:предел]
    return начало.decode("utf-8", "replace")[:предел]


def формат(данные):
    """Формат двоичного тела по сигнатуре — без содержания."""
    if данные[4:8] == b"ftyp":
        бренд = данные[8:12]
        return "ftyp:" + (бренд.decode("ascii") if re.fullmatch(rb"[A-Za-z0-9 ]{4}", бренд)
                          else "?")
    if данные[:4] == b"RIFF" and данные[8:12] == b"WAVE":
        return "wav"
    for сигнатура, имя in ((b"ID3", "mp3-id3"), (b"#!AMR-WB\n", "amr-wb"), (b"#!AMR\n", "amr"),
                           (b"OggS", "ogg"), (b"fLaC", "flac"), (b"\x1a\x45\xdf\xa3", "webm"),
                           (b"PK\x03\x04", "zip"), (b"\x1f\x8b", "gzip")):
        if данные.startswith(сигнатура):
            return имя
    if len(данные) > 1 and данные[0] == 0xFF and данные[1] & 0xE0 == 0xE0:
        return "mpeg-audio"     # кадр MP3 или ADTS AAC
    return "unknown"


def форма_байтов(данные, n=12):
    """Форма первых байтов неизвестного двоичного: цифра — `9`, латинская
    буква — `a`, прочий печатный ASCII как есть, остальное — `~`."""
    return "".join("9" if 48 <= b <= 57 else "a" if 65 <= b <= 90 or 97 <= b <= 122
                   else chr(b) if 32 < b < 127 else "~" for b in данные[:n])


def файл(имя, имя_файла, тип, данные):
    """Запись о файле или теле, которое не форма: размер, форма начала текста
    или формат двоичного."""
    з = имя_поля(имя) if имя else {}
    if имя_файла is not None:
        з["filename_shape"] = форма(имя_файла)
    з["content_type"] = базовый_тип(тип)
    з["size"] = len(данные)
    if текст_ли(данные):
        з["text_shape"] = форма(_текст(данные), 256)
        return з
    з["format"] = формат(данные)
    if з["format"] == "unknown":
        з["head_shape"] = форма_байтов(данные)
    if len(данные) >= ДВОИЧНОЕ_SHA:
        з["sha256"] = hashlib.sha256(данные).hexdigest()
    return з


def сохранить(каталог, имя, данные):
    """Копия присланного файла: каталог 0700, файл 0600 с момента создания."""
    os.makedirs(каталог, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(каталог, имя), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        вид = memoryview(данные)
        while вид:
            вид = вид[os.write(fd, вид):]
    finally:
        os.close(fd)
    return имя


class _Разбор:
    """Разбор тела одного запроса: поля, файлы и заметки — без содержания."""

    def __init__(self, показать, секрет, keep, ключ, метка):
        self.показать, self.секрет, self.keep = показать, секрет, keep
        self.ключ, self.метка = ключ, метка
        self.поля, self.файлы, self.заметки = [], [], []

    def заметка(self, текст):
        if текст not in self.заметки:
            self.заметки.append(текст)

    def поле(self, имя, значение):
        if len(self.поля) >= ПОЛЕЙ_МАКС:
            self.заметка("fields-cut:%d" % ПОЛЕЙ_МАКС)
            return {}
        з = поле(имя, значение, self.показать, self.секрет)
        self.поля.append(з)
        return з

    def файл(self, имя, имя_файла, тип, данные):
        з = файл(имя, имя_файла, тип, данные)
        з["fingerprint"] = hmac.new(self.ключ, данные, "sha256").hexdigest()[:16]
        if self.keep and данные:
            з["kept"] = сохранить(self.keep, "%s-%d.bin" % (self.метка, len(self.файлы)),
                                  данные)
        self.файлы.append(з)

    def тело(self, тип, данные):
        базовый = (тип or "").split(";")[0].strip().lower()
        if базовый.startswith("multipart/"):
            return self.multipart(тип, данные)
        if ("json" in базовый or данные.lstrip()[:1] in (b"{", b"[")) and \
                self.json(базовый, данные):
            return
        if базовый == "application/x-www-form-urlencoded":
            for имя, значение in urllib.parse.parse_qsl(данные.decode("utf-8", "replace"),
                                                         keep_blank_values=True):
                self.поле(имя, значение)
        elif данные:
            self.файл("", None, базовый, данные)

    def json(self, базовый, данные, префикс=""):
        try:
            d = json.loads(данные)
        except (ValueError, RecursionError):
            if "json" in базовый:
                self.заметка("json-broken")
            return False
        if "json" not in базовый:
            self.заметка("json-as:" + (базовый_тип(базовый) or "none"))
        if isinstance(d, dict):
            self.json_поля(префикс, d, 0)
        elif isinstance(d, list):
            self.заметка("json-array:%d" % len(d))
            if d and isinstance(d[0], dict):
                self.json_поля(префикс + "[0].", d[0], 1)
        else:
            self.заметка("json-scalar")
        return True

    def json_поля(self, префикс, d, глубина):
        for ключ, v in d.items():
            имя = префикс + str(ключ)
            if isinstance(v, dict):
                if глубина < ГЛУБИНА:
                    self.json_поля(имя + ".", v, глубина + 1)
                elif len(self.поля) < ПОЛЕЙ_МАКС:
                    self.поля.append({**имя_поля(имя), "kind": "object", "length": len(v)})
            elif isinstance(v, list):
                if len(self.поля) < ПОЛЕЙ_МАКС:
                    self.поля.append({**имя_поля(имя), "kind": "array", "length": len(v)})
                if v and isinstance(v[0], dict) and глубина < ГЛУБИНА:
                    self.json_поля(имя + "[0].", v[0], глубина + 1)
            else:
                вид = ("string" if isinstance(v, str) else "bool" if isinstance(v, bool)
                       else "null" if v is None else "number")
                з = self.поле(имя, v if isinstance(v, str) else json.dumps(v))
                if з:
                    з["kind"] = вид

    def multipart(self, тип, данные):
        try:
            шапка = ("Content-Type: %s\r\n\r\n" % тип).encode("latin-1")
        except UnicodeEncodeError:
            шапка = b"Content-Type: multipart/form-data\r\n\r\n"
        сообщение = BytesParser(policy=HTTP).parsebytes(шапка + данные)
        частей = 0
        if сообщение.is_multipart():
            for часть in сообщение.walk():
                if часть is сообщение:
                    continue
                if часть.is_multipart():
                    self.заметка("nested:" + базовый_тип(часть.get_content_type()))
                    continue
                частей += 1
                self.часть(часть)
        дефекты = sorted({type(д).__name__ for ч in сообщение.walk() for д in ч.defects})
        if дефекты:
            self.заметка("defects:" + ",".join(дефекты))
        if not частей:
            self.заметка("multipart-no-parts")
            if данные:
                self.файл("", None, тип, данные)

    def часть(self, часть):
        имя = часть.get_param("name", header="content-disposition")
        имя = collapse_rfc2231_value(имя) if имя is not None else ""
        имя_файла = часть.get_filename()
        данные = часть.get_payload(decode=True) or b""
        тип = часть.get_content_type()
        явный = часть.get("Content-Type") is not None
        if имя_файла is None and явный and "json" in тип and \
                self.json("application/json", данные, имя + "." if имя else ""):
            self.заметка("json-part")
        elif имя_файла is not None or (явный and тип != "text/plain") or not текст_ли(данные):
            self.файл(имя, имя_файла, тип, данные)
        else:
            try:
                значение = данные.decode(часть.get_content_charset() or "utf-8", "replace")
            except LookupError:
                значение = данные.decode("utf-8", "replace")
            self.поле(имя, значение)


def разобрать(тип, тело, показать=ПОКАЗАТЬ, секрет=None, keep=None, ключ=None, метка="0"):
    """{"fields", "files", "note"} тела запроса по типу содержимого и по виду
    самого тела (JSON узнаётся и под чужим типом)."""
    р = _Разбор(показать, секрет, keep, ключ or os.urandom(32), метка)
    р.тело(тип, тело)
    return {"fields": р.поля, "files": р.файлы, "note": р.заметки}


class Зонд(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, адрес, журнал=ЖУРНАЛ, статус=200, задержка=0.0, секрет=None,
                 keep=None, показать=ПОКАЗАТЬ, предел=ПРЕДЕЛ_МБ << 20, тихо=False):
        super().__init__(адрес, _Обработчик)
        self.журнал, self.статус, self.задержка = журнал, статус, задержка
        self.секрет, self.keep, self.предел, self.тихо = секрет or None, keep, предел, тихо
        self.показать = tuple(п.lower() for п in показать)
        self.ключ = os.urandom(32)      # отпечатки тел — только в пределах прогона
        self.прогон = time.strftime("%Y%m%dT%H%M%S")
        self.замок = threading.Lock()
        self._номер = 0
        self.записать({"run": self.прогон, "phase": "start", "status": статус,
                       "delay": задержка, "show": list(self.показать),
                       "max_bytes": предел, "secret_set": bool(self.секрет),
                       "keep": bool(keep)})

    def номер(self):
        with self.замок:
            self._номер += 1
            return self._номер

    def записать(self, запись):
        строка = (json.dumps(запись, ensure_ascii=True) + "\n").encode("ascii")
        with self.замок:
            каталог = os.path.dirname(self.журнал)
            if каталог:
                os.makedirs(каталог, mode=0o700, exist_ok=True)
            fd = os.open(self.журнал, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, строка)
            finally:
                os.close(fd)

    def сказать(self, строка):
        if not self.тихо:
            print(строка, flush=True)

    def handle_error(self, request, client_address):
        # Стандартный печатает адрес клиента и трассу; здесь — только тип.
        print("зонд: запрос упал: %s" % sys.exc_info()[0].__name__, file=sys.stderr,
              flush=True)


class _Обработчик(BaseHTTPRequestHandler):
    server_version = "mara-acr-probe"
    sys_version = ""
    timeout = 120               # тишина сокета, с: зависший клиент не держит поток вечно

    def log_message(self, *a):
        pass                    # адрес телефона и путь в вывод процесса не идут

    def handle_one_request(self):
        try:
            первый = self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            первый = b""
        if not первый:
            self.close_connection = True
            return
        if первый == b"\x16":
            # Рукопожатие TLS на открытый порт: в ACR адрес с https://.
            с = self.server
            с.записать({"run": с.прогон, "id": с.номер(), "phase": "rejected",
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reason": "tls-handshake"})
            с.сказать("зонд: пришло рукопожатие TLS — в ACR адрес с https://, нужен http://")
            self.close_connection = True
            return
        super().handle_one_request()

    def send_error(self, code, message=None, explain=None):
        # Запрос, который http.server не разобрал: строка без содержания.
        с = self.server
        сырьё = (getattr(self, "raw_requestline", b"") or b"")[:64]
        с.записать({"run": с.прогон, "id": с.номер(), "phase": "rejected",
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reason": "http-%d" % code,
                    "line_shape": форма(сырьё.decode("latin-1"), 64)})
        super().send_error(code, message, explain)

    def _тело(self):
        """(тело, беда): беда — None, `limit`, `cut` (обрыв или тишина дольше
        timeout) или `bad-length`. Тело — по Content-Length или кусками chunked;
        кусок сверх предела не читается."""
        предел = self.server.предел
        куски = []
        try:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                return self._куски(предел, куски)
            длина = self.headers.get("Content-Length")
            if длина is None:
                return b"", None
            if not ДЛИНА.fullmatch(длина.strip()):
                return b"", "bad-length"
            n = int(длина)
            if n > предел:
                return b"", "limit"
            всего = 0
            while всего < n:
                кусок = self.rfile.read(min(n - всего, 1 << 20))
                if not кусок:
                    return b"".join(куски), "cut"
                куски.append(кусок)
                всего += len(кусок)
            return b"".join(куски), None
        except OSError:
            return b"".join(куски), "cut"

    def _куски(self, предел, куски):
        всего = 0
        while True:
            строка = self.rfile.readline(1024)
            if not строка:
                return b"".join(куски), "cut"
            размер = строка.split(b";", 1)[0].strip()
            if not строка.endswith(b"\n") or not КУСОК.fullmatch(размер):
                return b"".join(куски), "bad-length"
            n = int(размер, 16)
            if n == 0:
                for _ in range(64):             # трейлеры — до пустой строки
                    if self.rfile.readline(1024) in (b"\r\n", b"\n", b""):
                        break
                return b"".join(куски), None
            if всего + n > предел:
                return b"".join(куски), "limit"
            кусок = self.rfile.read(n)
            куски.append(кусок)
            всего += len(кусок)
            if len(кусок) < n:
                return b"".join(куски), "cut"
            конец = self.rfile.readline(4)
            if конец not in (b"\r\n", b"\n"):
                return b"".join(куски), "cut" if not конец else "bad-length"

    def _ждать(self, t0, до):
        """Ждёт до момента `до`, следя за сокетом. Клиент закрыл соединение
        раньше — через сколько секунд от прихода запроса; дождался — None."""
        while True:
            осталось = до - time.monotonic()
            if осталось <= 0:
                return None
            try:
                готов, _, _ = select.select([self.connection], [], [], min(осталось, 1.0))
                if готов and not self.connection.recv(4096):
                    return round(time.monotonic() - t0, 3)
            except OSError:
                return round(time.monotonic() - t0, 3)

    def _дослушать(self, секунд=2.0, предел=1 << 20):
        """После ответа на кривое или лишнее тело: дочитать и выбросить то,
        что клиент ещё шлёт, — иначе закрытие сокета с непрочитанным (RST)
        может сбросить уже отправленный ответ."""
        try:
            self.connection.shutdown(socket.SHUT_WR)
            до, прочитано = time.monotonic() + секунд, 0
            while прочитано < предел:
                осталось = до - time.monotonic()
                if осталось <= 0 or not select.select([self.connection], [], [], осталось)[0]:
                    return
                кусок = self.connection.recv(65536)
                if not кусок:
                    return
                прочитано += len(кусок)
        except OSError:
            pass

    def _ответить(self, код, закрыть):
        тело = b"" if код in (204, 304) else b"ok" if код < 400 else b"probe"
        try:
            self.send_response(код)
            self.send_header("Content-Type", "text/plain")
            if тело:
                self.send_header("Content-Length", str(len(тело)))
            if закрыть:
                self.send_header("Connection", "close")
            self.end_headers()
            if тело and self.command != "HEAD":
                self.wfile.write(тело)
            return True
        except OSError:
            return False

    def _принять(self):
        t0 = time.monotonic()
        с = self.server
        номер = с.номер()
        тело, беда = self._тело()
        тип = self.headers.get("Content-Type") or ""
        адрес = urllib.parse.urlsplit(self.path)
        приход = {"run": с.прогон, "id": номер, "phase": "received",
                  "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "method": self.command if МЕТОД.fullmatch(self.command)
                  else форма(self.command, 16),
                  "http": self.request_version if ВЕРСИЯ.fullmatch(self.request_version)
                  else форма(self.request_version, 16),
                  "path_shape": форма(адрес.path, 128),
                  "query": [поле(и, з, с.показать, с.секрет, тело) for и, з in
                            urllib.parse.parse_qsl(адрес.query, keep_blank_values=True)
                            ][:ПОЛЕЙ_МАКС],
                  "headers": [заголовок(к, з, с.секрет, тело)
                              for к, з in self.headers.items()][:ПОЛЕЙ_МАКС],
                  "content_type": базовый_тип(тип), "body_size": len(тело),
                  "body_problem": беда, "read_seconds": round(time.monotonic() - t0, 3)}
        if тело:
            приход["fingerprint"] = hmac.new(с.ключ, тело, "sha256").hexdigest()[:16]
        с.записать(приход)
        с.сказать("%s #%d %s %s, %d байт%s" % (приход["at"], номер, приход["method"],
                                             приход["content_type"] or "-", len(тело),
                                             ", " + беда if беда else ""))
        разбор = {}
        if беда is None:
            try:
                разбор = разобрать(тип, тело, с.показать, с.секрет, с.keep, с.ключ,
                                   "%s-%04d" % (с.прогон, номер))
            except Exception as e:      # кривое тело — тоже ответ на вопрос
                разбор = {"parse_error": type(e).__name__}
        код = с.статус if беда is None else 413 if беда == "limit" else 400
        ушёл = self._ждать(t0, t0 + с.задержка) if с.задержка else None
        отправлен = self._ответить(код, беда is not None)
        if беда is not None and отправлен:
            self._дослушать()
        исход = {"run": с.прогон, "id": номер, "phase": "outcome", "replied": код,
                 "sent": отправлен, "client_gone_after": ушёл,
                 "seconds": round(time.monotonic() - t0, 3), **разбор}
        с.записать(исход)
        с.сказать("#%d → %d за %.1f с, полей %d, файлов %d%s%s" % (
            номер, код, исход["seconds"], len(разбор.get("fields", [])),
            len(разбор.get("files", [])),
            "" if ушёл is None else ", клиент ушёл через %.1f с" % ушёл,
            "" if отправлен else ", ответ не ушёл"))

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _принять


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
    итог = разобрать("multipart/form-data; boundary=" + граница, тело, секрет="слово")
    журнал = json.dumps(итог, ensure_ascii=False)
    for тайное in ("9991234567", "Анна", "слово"):
        assert тайное not in журнал, журнал
    по_имени = {п.get("name"): п for п in итог["fields"]}
    assert по_имени["Source"]["shape"] == "+99999999999", по_имени
    assert по_имени["Secret"] == {"name": "Secret", "length": 5, "secret": "equal"}
    assert по_имени["Date"]["value"] == "1760000000"
    ф = итог["files"][0]
    assert (ф["size"], ф["format"]) == (12, "ftyp:M4A "), ф
    assert ф["filename_shape"] == "+99999999999 9999-99-99.a9a"
    assert поле("Duration", "65000")["value"] == "65000"
    assert поле("date", "1760000000000")["value"] == "1760000000000"
    for номер in ("79991234567", "89991234567", "9991234567", "+79991234567", "3812345678",
                  "4951234567", "+7 999 123-45-67", "8 (999) 123-45-67", "٧٩٩٩١٢٣٤٥٦٧"):
        assert "value" not in поле("Date", номер), номер
        assert "value" not in поле("type", номер), номер
    текст = файл("", None, "text/plain", "+79991234567 Анна".encode("utf-8"))
    assert "sha256" not in текст and текст["text_shape"] == "+99999999999 aaaa", текст
    assert "name_shape" in имя_поля("+79991234567")
    assert "198.51" not in json.dumps(заголовок("Host", "198.51.100.7:8799"))
    assert заголовок("X-Secret", "слово", "слово")["secret"] == "equal"
    подпись = hmac.new(b"k", b"abc", "sha256").hexdigest()
    assert заголовок("X-Signature", "sha256=" + подпись, "k", b"abc")["secret"] == \
        "hmac-sha256-hex"
    print("acr_report_probe self-check: ок")
    return 0


def _код(s):
    v = int(s)
    if not 200 <= v <= 599:
        raise argparse.ArgumentTypeError("код ответа — от 200 до 599")
    return v


def _секунды(s):
    v = float(s)
    if not 0 <= v <= 3600:
        raise argparse.ArgumentTypeError("задержка — от 0 до 3600 секунд")
    return v


def _мегабайты(s):
    v = int(s)
    if not 1 <= v <= 2048:
        raise argparse.ArgumentTypeError("предел — от 1 до 2048 МБ")
    return v


def аргументы():
    ap = argparse.ArgumentParser(description="зонд RES-REC-001: что шлёт ACR Phone")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--log", default=ЖУРНАЛ)
    ap.add_argument("--status", type=_код, default=200,
                    help="код ответа: 500 — повторяет ли ACR, 401/404 — выключает ли отчёт")
    ap.add_argument("--delay", type=_секунды, default=0.0,
                    help="секунд от прихода запроса до ответа: таймаут ACR")
    ap.add_argument("--secret", help="одноразовое слово из поля Secret, не пароль: "
                                     "журнал скажет, где и как оно пришло")
    ap.add_argument("--keep", help="каталог для присланных файлов (0700, файлы 0600)")
    ap.add_argument("--show", default=",".join(ПОКАЗАТЬ),
                    help="имена полей через запятую, чьи значения пишутся как есть "
                         "(если в них не больше шести цифр или это метка эпохи)")
    ap.add_argument("--max-mb", type=_мегабайты, default=ПРЕДЕЛ_МБ, dest="max_mb",
                    help="тело больше — не читается, ответ 413")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    return ap


def зонд_из(a, тихо=False):
    показать = tuple(п.strip() for п in a.show.split(",") if п.strip())
    return Зонд((a.bind, a.port), журнал=a.log, статус=a.status, задержка=a.delay,
                секрет=a.secret, keep=a.keep, показать=показать, предел=a.max_mb << 20,
                тихо=тихо)


def main(argv=None):
    a = аргументы().parse_args(argv)
    if a.self_check:
        return self_check()
    зонд = зонд_из(a)
    дом = os.path.expanduser("~")
    журнал = "~" + a.log[len(дом):] if a.log.startswith(дом + os.sep) else a.log
    print("зонд слушает порт %d, отвечает %d%s, журнал %s" % (
        зонд.server_address[1], a.status,
        " через %g с" % a.delay if a.delay else "", журнал), flush=True)
    try:
        зонд.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        зонд.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
