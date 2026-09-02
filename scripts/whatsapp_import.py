#!/usr/bin/env python3
"""Импорт «Экспорта чата» WhatsApp в contextd (ТЗ §13, спека 8–9).

Уведомления на телефоне не видят чат, открытый на экране, и почти не видят
своих исходящих. Экспорт закрывает эти дыры задним числом: владелец делает
«Экспорт чата → без медиа», кладёт файл на doctor и запускает:

    whatsapp_import.py "Чат WhatsApp с Анна Петрова.txt" --me "Сергей"

Ключ сообщения тот же, что считает телефон (`MessageId.of` в Core.kt):
sha256(package|chat|sender|text'|минута эпохи). Поэтому то, что уже поймано
уведомлением, сервер отсеет как дубль — специально ничего сверять не надо.
Пин ключа — tests/fixtures/whatsapp-message-id.json, его проверяют обе стороны.

Модель не вызывается. Токен — своё устройство `whatsapp-import`, файл
/etc/mara/whatsapp.env; чужой (tdlib, gmail) не берём — отзыв на устройство.
"""
import os
import re
import sys
import json
import time
import hashlib
import zipfile
import argparse
import datetime
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmail_ingest import load_env, poster  # noqa: E402

URL = os.environ.get("MARA_CONTEXT_URL", "http://127.0.0.1:8788")
ENV = "/etc/mara/whatsapp.env"
ПАКЕТ = "com.whatsapp"

# ── ключ: ровно как в Core.kt ──────────────────────────────────────────────

ПРОБЕЛЫ = re.compile(r"[ \t\n\r]+")
КРАЯ = " \t\n\r"


def normalize(text):
    """Только ASCII-пробелы: `str.split()` режет и NBSP, а `\\s` у JVM — нет."""
    return ПРОБЕЛЫ.sub(" ", text.strip(КРАЯ))


def message_id(pkg, chat, sender, text, at_ms):
    return hashlib.sha256(("%s|%s|%s|%s|%d" % (
        pkg, chat, sender, normalize(text), at_ms // 60000)).encode("utf-8")).hexdigest()


# ── разбор экспорта ────────────────────────────────────────────────────────

# Android RU  02.09.26, 14:05 - Анна: текст
# Android EN  9/2/26, 2:05 PM - Anna: text
# iOS         [02.09.26, 14:05:33] Анна: текст
СТРОКА = re.compile(
    r"^\[?(\d{1,2})([./])(\d{1,2})[./](\d{2,4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"
    r"\s*([AaPp]\.?\s?[Mm]\.?)?\]?\s*(?:-\s*)?(.*)$")
КТО = re.compile(r"^(.+?): (.*)$", re.S)
ИМЯ_ФАЙЛА = re.compile(r"^(?:WhatsApp Chat (?:with|-) |Чат WhatsApp с )(.+)$")


def _когда(m, dmy, tz):
    a, sep, b, y = int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
    if dmy is None:
        dmy = sep == "."           # точки — русский экспорт, день впереди
    d, mo = (a, b) if dmy else (b, a)
    if mo > 12 and d <= 12:        # владелец ошибся флагом — числа сами скажут
        d, mo = mo, d
    if y < 100:
        y += 2000
    h, mi, s = int(m.group(5)), int(m.group(6)), int(m.group(7) or 0)
    ampm = (m.group(8) or "").replace(".", "").replace(" ", "").lower()
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    dt = datetime.datetime(y, mo, d, h, mi, s)
    return dt.replace(tzinfo=tz) if tz else dt.astimezone()


def parse(lines, dmy=None, tz=None):
    """Строки экспорта → сообщения. Продолжения клеятся к предыдущему,
    строки без «Кто: » после времени — системные, пропускаются."""
    out = []
    system = False
    for raw in lines:
        line = raw.rstrip("\n").replace("\u200e", "").replace("\u202f", " ")   # LRM и узкий NBSP iOS
        m = СТРОКА.match(line)
        if not m:
            if out and not system and line.strip(КРАЯ):
                out[-1]["text"] += "\n" + line
            continue
        кто = КТО.match(m.group(9))
        system = кто is None
        if system:
            continue
        dt = _когда(m, dmy, tz)
        out.append({"sender": кто.group(1).strip(), "text": кто.group(2),
                    "at_ms": int(dt.timestamp()) * 1000,
                    "iso": dt.isoformat(timespec="seconds")})
    return out


def read(path):
    """txt как есть; zip — первый .txt внутри (iOS кладёт _chat.txt)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".txt")), None)
            if not name:
                raise SystemExit("в архиве нет .txt")
            return z.read(name).decode("utf-8-sig").splitlines()
    with open(path, encoding="utf-8-sig") as fh:
        return fh.read().splitlines()


def chat_name(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    m = ИМЯ_ФАЙЛА.match(stem)
    if m:
        return m.group(1).strip()
    if stem.lower() in ("_chat", "chat"):
        return None                # iOS не подписывает — нужен --chat
    return stem


def events(msgs, chat, me=None):
    """Группа, если пишут двое и больше, не считая меня. `--me` не дан —
    свои сообщения идут под именем, как в экспорте; с ключом телефона они
    тогда не сойдутся, но ответов из шторки и так единицы."""
    другие = {m["sender"] for m in msgs if m["sender"] != me}
    группа = len(другие) > 1
    for m in msgs:
        own = me is not None and m["sender"] == me
        sender = "" if own else m["sender"]
        yield {"source": "whatsapp",
               "source_id": message_id(ПАКЕТ, chat, sender, m["text"], m["at_ms"]),
               "occurred_at": m["iso"], "classification": "personal",
               "payload": {"package": ПАКЕТ, "chat_title": chat,
                           "chat_type": "group" if группа else "private",
                           "sender_name": sender, "text": m["text"],
                           "outgoing": own, "via": "export"}}


def send_all(post, evs, log=print, sleep=time.sleep):
    """Считаем новые/дубли/отвергнутые. Сеть — три попытки, потом стоп:
    сервер дедуплицирует, повторный запуск ничего не удвоит."""
    n = {"ok": 0, "dup": 0, "rejected": 0}
    for ev in evs:
        for попытка in range(3):
            try:
                r = post(ev)
                n["dup" if r.get("duplicate") else "ok"] += 1
                break
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500:
                    log("%s: contextd отверг (%d)" % (ev["source_id"][:12], e.code))
                    n["rejected"] += 1
                    break
                if попытка == 2:
                    raise
                sleep(5 * (попытка + 1))
            except urllib.error.URLError:
                if попытка == 2:
                    raise
                sleep(5 * (попытка + 1))
    return n


# ── самопроверка ───────────────────────────────────────────────────────────

def self_check():
    import io
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    f = json.load(open(os.path.join(root, "tests", "fixtures", "whatsapp-message-id.json"), encoding="utf-8"))
    assert message_id(f["package"], f["chat"], f["sender"], f["text"], f["at_ms"]) == f["source_id"], \
        "ключ разошёлся с фиксом — Core.kt перестанет сходиться"
    assert normalize("  a \n\t b ") == "a b" and normalize("a b") == "a b"

    tz = datetime.timezone(datetime.timedelta(hours=3))
    ru = ["02.09.26, 14:05 - Анна Петрова: Купи хлеб",
          "и молоко",
          "02.09.26, 14:06 - Сергей: ок",
          "02.09.26, 14:07 - Анна Петрова добавил(а) Петю",
          "это продолжение системной строки, его не берём",
          "\u200e02.09.26, 14:08 - Петя: \u200e<Медиа отсутствует>"]
    got = parse(ru, tz=tz)
    assert [m["sender"] for m in got] == ["Анна Петрова", "Сергей", "Петя"], got
    assert got[0]["text"] == "Купи хлеб\nи молоко" and got[0]["iso"] == "2026-09-02T14:05:00+03:00"
    assert got[0]["at_ms"] // 60000 == 1788347100000 // 60000, "минута сходится с телефоном"
    en = ["9/2/26, 2:05 PM - Anna: hi", "9/2/26, 12:01 AM - Anna: night"]
    got = parse(en, tz=tz)
    assert got[0]["iso"] == "2026-09-02T14:05:00+03:00" and got[1]["iso"] == "2026-09-02T00:01:00+03:00", got
    ios = ["[02.09.26, 14:05:33] Анна: привет", "[02.09.26, 14:05:40] Анна: как дела: нормально?"]
    got = parse(ios, tz=tz)
    assert got[0]["text"] == "привет" and got[0]["at_ms"] % 60000 == 33000
    assert got[1]["sender"] == "Анна" and got[1]["text"] == "как дела: нормально?"
    assert parse(["13/2/26, 10:00 - A: x"], tz=tz)[0]["iso"].startswith("2026-02-13"), "13 не месяц"

    evs = list(events(parse(ru, tz=tz), "Семья", me="Сергей"))
    assert evs[0]["payload"]["chat_type"] == "group" and evs[1]["payload"]["outgoing"]
    assert evs[1]["payload"]["sender_name"] == "" and evs[0]["payload"]["via"] == "export"
    assert evs[0]["source_id"] == message_id(ПАКЕТ, "Семья", "Анна Петрова", "Купи хлеб\nи молоко", 1788347100000)
    один = list(events(parse(ru[:2], tz=tz), "Анна Петрова"))
    assert один[0]["payload"]["chat_type"] == "private"

    assert chat_name("/x/Чат WhatsApp с Анна Петрова.txt") == "Анна Петрова"
    assert chat_name("WhatsApp Chat with Anna.zip") == "Anna"
    assert chat_name("_chat.txt") is None and chat_name("Семья.txt") == "Семья"

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        zp = os.path.join(tmp, "WhatsApp Chat - Семья.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("_chat.txt", "\ufeff" + "\n".join(ios))
        assert len(parse(read(zp), tz=tz)) == 2 and chat_name(zp) == "Семья"

    seen = []

    def post(ev):
        seen.append(ev["source_id"])
        if ev["payload"]["text"] == "ок":
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b""))
        return {"event_id": "message_x", "duplicate": seen.count(ev["source_id"]) > 1}
    n = send_all(post, evs + evs[:1], log=lambda *_: None, sleep=lambda *_: None)
    assert n == {"ok": 2, "dup": 1, "rejected": 1}, n

    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "install", "whatsapp.env.example")
    assert "MARA_CONTEXT_TOKEN=\n" in open(env, encoding="utf-8").read(), "в примере не должно быть значений"
    print("ok: whatsapp_import")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file", nargs="?", help="экспорт чата: .txt или .zip")
    ap.add_argument("--chat", help="название беседы; по умолчанию из имени файла")
    ap.add_argument("--me", help="как подписан я в экспорте — эти сообщения станут исходящими")
    ap.add_argument("--dmy", action="store_true", help="день впереди, даже если разделитель /")
    ap.add_argument("--tz", help="зона экспорта, напр. Europe/Moscow; по умолчанию системная")
    ap.add_argument("--dry-run", action="store_true", help="разобрать и показать, не слать")
    ap.add_argument("--url", default=URL)
    ap.add_argument("--env", default=ENV)
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if not a.file:
        ap.error("нужен файл экспорта")
    tz = None
    if a.tz:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(a.tz)
    chat = a.chat or chat_name(a.file)
    if not chat:
        raise SystemExit("файл не подписан — укажи --chat 'Название беседы'")
    msgs = parse(read(a.file), dmy=True if a.dmy else None, tz=tz)
    if not msgs:
        raise SystemExit("ни одного сообщения не разобрано — формат не из трёх известных?")
    evs = list(events(msgs, chat, a.me))
    print("чат: %s (%s), сообщений: %d, с %s по %s" % (
        chat, evs[0]["payload"]["chat_type"], len(evs), msgs[0]["iso"], msgs[-1]["iso"]))
    if a.me and not any(m["sender"] == a.me for m in msgs):
        print("внимание: «%s» в экспорте не пишет — --me не совпал с подписью" % a.me)
    if a.dry_run:
        return 0
    load_env(a.env)
    token = os.environ.get("MARA_CONTEXT_TOKEN")
    if not token:
        raise SystemExit("нет MARA_CONTEXT_TOKEN: спарить устройство whatsapp-import и записать в %s" % a.env)
    n = send_all(poster(a.url, token, "/v1/ingest/message"), evs)
    print("новых: %(ok)d, уже было: %(dup)d, отвергнуто: %(rejected)d" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
