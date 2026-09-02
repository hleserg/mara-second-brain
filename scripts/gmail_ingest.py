#!/usr/bin/env python3
"""Личный Gmail → contextd (ТЗ §12, спека 7).

Раз в десять минут по крону: users.history.list от сохранённого historyId,
новое письмо — событием в /v1/ingest/email, удаление — надгробием, корзина —
ревизией. Тела остаются на doctor: raw/ и база contextd, наружу не уходят.

Рабочий ящик сюда не подключить: --login принимает только @gmail.com.
Библиотек Google нет — три REST-вызова и обновление токена делает urllib.
"""
import os, re, sys, json, time, base64, fcntl, hashlib, secrets, argparse
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta, date
from html.parser import HTMLParser
from http.server import HTTPServer, BaseHTTPRequestHandler

ROOT = os.environ.get("MARA_BLOBS", "/srv/mara-blobs")
URL = os.environ.get("MARA_CONTEXT_URL", "http://127.0.0.1:8788")
API = "https://gmail.googleapis.com/gmail/v1/users/me/"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
ЛИЧНЫЕ = ("@gmail.com", "@googlemail.com")
ПРОПУСКАЕМ = {"SPAM", "TRASH", "DRAFT"}
ДНЕЙ = 30            # глубина первого забора
ПРОТУХ_ДНЕЙ = 7      # догон после 404 на курсор (ТЗ §17)
МАКС_ЗАБОР = 3000    # писем за один прогон догона
ТЕКСТ = 100_000      # символов тела в событии; полное письмо лежит в raw/
ПОРТ = 8765
ENV = "/etc/mara/gmail.env"


def load_env(path, environ=None):
    """KEY=VALUE из файла в окружение: у крона нет EnvironmentFile, как у
    systemd, а `set -a; . файл` перед каждым запуском забудется. Уже
    выставленное не трогаем. Нет файла — нет входа, ошибки не бывает."""
    environ = os.environ if environ is None else environ
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return 0
    n = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        if k and v and k not in environ:
            environ[k] = v
            n += 1
    return n


def state_dir(root=ROOT):
    return os.path.join(root, "gmail")


def iso(ts):
    return datetime.fromtimestamp(int(ts or 0), timezone.utc).astimezone().isoformat(timespec="seconds")


def личный(email):
    return (email or "").lower().endswith(ЛИЧНЫЕ)


# ── разбор письма: чистые функции ─────────────────────────────────────────

class _Текст(HTMLParser):
    БЛОКИ = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "blockquote", "table", "pre", "hr"}
    ТИХО = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.ТИХО:
            self.skip += 1
        elif tag in self.БЛОКИ:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.ТИХО:
            self.skip = max(0, self.skip - 1)
        elif tag in self.БЛОКИ:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(s):
    p = _Текст()
    p.feed(s or "")
    p.close()
    t = re.sub(r"[ \t\r\f\v\xa0]+", " ", "".join(p.out))
    t = re.sub(r" *\n[ \n]*", "\n", t)
    return t.strip()


def _charset(part):
    for h in part.get("headers") or []:
        if (h.get("name") or "").lower() == "content-type":
            m = re.search(r'charset="?([\w.-]+)"?', h.get("value") or "", re.I)
            if m:
                return m.group(1)
    return "utf-8"


def _decode(data, part):
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    for cs in (_charset(part), "utf-8"):
        try:
            return raw.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _части(p):
    yield p
    for c in p.get("parts") or []:
        yield from _части(c)


def тело(payload):
    """(текст, вложения, был ли html). Вложения — метаданные, тел нет (§12)."""
    plain = html = None
    вложения = []
    for part in _части(payload):
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        name = part.get("filename") or ""
        if name or body.get("attachmentId"):
            вложения.append({"name": name, "mime": mime, "size": body.get("size") or 0,
                             "attachment_id": body.get("attachmentId")})
            continue
        if not body.get("data"):
            continue
        text = _decode(body["data"], part)
        if mime == "text/plain" and plain is None:
            plain = text
        elif mime == "text/html" and html is None:
            html = text
    text = plain if plain and plain.strip() else html_text(html or "")
    return text, вложения, html is not None


def заголовки(payload):
    h = {}
    for x in payload.get("headers") or []:
        n = (x.get("name") or "").lower()
        if n in ("from", "to", "cc", "subject", "date", "message-id", "in-reply-to") and n not in h:
            h[n] = x.get("value") or ""
    return h


def email_event(msg, любые=False):
    """Событие по письму формата full. None — спам/корзина/черновик,
    если не сказано брать любые (ревизия корзины как раз про TRASH)."""
    labels = msg.get("labelIds") or []
    if not любые and ПРОПУСКАЕМ & set(labels):
        return None
    p = msg.get("payload") or {}
    h = заголовки(p)
    text, вложения, has_html = тело(p)
    return {"source": "gmail", "source_id": msg["id"],
            "occurred_at": iso(int(msg.get("internalDate") or 0) // 1000),
            "classification": "personal",
            "payload": {"message_id": msg["id"], "thread_id": msg.get("threadId"),
                        "labels": labels, "outgoing": "SENT" in labels,
                        "from": h.get("from", ""), "to": h.get("to", ""), "cc": h.get("cc", ""),
                        "subject": h.get("subject", ""), "date": h.get("date", ""),
                        "rfc_message_id": h.get("message-id", ""),
                        "in_reply_to": h.get("in-reply-to", ""),
                        "snippet": msg.get("snippet", ""), "text": text[:ТЕКСТ],
                        "has_html": has_html, "attachments": вложения,
                        "size": msg.get("sizeEstimate") or 0}}


def revision_event(msg, history_id):
    ev = email_event(msg, любые=True)
    ev["source_id"] = "%s/labels/%s" % (msg["id"], history_id)
    ev["payload"]["revision_of"] = msg["id"]
    ev["payload"]["trashed"] = "TRASH" in (msg.get("labelIds") or [])
    return ev


def tombstone(mid, history_id, now=time.time):
    return {"source": "gmail", "source_id": mid + "/deleted", "occurred_at": iso(now()),
            "classification": "personal",
            "payload": {"tombstone_of": mid, "history_id": history_id}}


# ── синк ──────────────────────────────────────────────────────────────────

class НетВхода(RuntimeError):
    pass


class ОшибкаAPI(RuntimeError):
    def __init__(self, code, text=""):
        super().__init__("Gmail API %s %s" % (code, text))
        self.code = code


class Синк:
    """api(path, **params) → dict (ОшибкаAPI с .code при ошибке);
    post(ev) → ответ contextd, бросает при обрыве сети."""

    def __init__(self, api, post, home, log=None, sleep=time.sleep, now=time.time, today=date.today):
        self.api, self.post, self.home = api, post, home
        self.log = log or (lambda s: print(s, flush=True))
        self.sleep, self.now, self.today = sleep, now, today
        os.makedirs(os.path.join(home, "raw"), mode=0o700, exist_ok=True)
        try:
            with open(os.path.join(home, "cursor.json"), encoding="utf-8") as fh:
                self.cursor = json.load(fh)
        except (OSError, ValueError):
            self.cursor = {}

    # ── диск ──
    def save(self):
        p = os.path.join(self.home, "cursor.json")
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(self.cursor, fh)
        os.replace(p + ".tmp", p)

    def raw(self, msg):
        p = os.path.join(self.home, "raw", self.today().isoformat() + ".jsonl")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def beat(self):
        p = os.path.join(self.home, "heartbeat")
        with open(p, "a"):
            pass
        os.utime(p, None)

    # ── contextd ──
    def send(self, ev):
        for i in range(5):
            try:
                r = self.post(ev)
                self.log("%s %s" % (ev["source_id"], "дубль" if r.get("duplicate") else "ok"))
                return r
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500:
                    self.log("%s отвергнуто %s" % (ev["source_id"], e.code))
                    return None
                err = e
            except Exception as e:
                err = e
            self.sleep(2 ** i)
        raise RuntimeError("contextd недоступен: %s" % err)

    # ── письма ──
    def fetch(self, mid):
        try:
            return self.api("messages/%s" % mid, format="full")
        except ОшибкаAPI as e:
            if e.code == 404:
                return None       # удалено, пока шли за ним; history принесёт надгробие
            raise

    def новое(self, mid):
        msg = self.fetch(mid)
        if not msg:
            return 0
        self.raw(msg)
        ev = email_event(msg)
        if ev:
            self.send(ev)
        return 1

    def ревизия(self, mid, history_id):
        msg = self.fetch(mid)
        if not msg:
            return 0
        self.raw(msg)
        self.send(revision_event(msg, history_id))
        return 1

    # ── прогоны ──
    def backfill(self, days):
        """Сначала historyId, потом письма: пришедшее во время догона окажется
        в history после курсора и не потеряется."""
        hid = self.api("profile")["historyId"]
        q = "after:%s" % (self.today() - timedelta(days=days)).strftime("%Y/%m/%d")
        ids, token = [], None
        while True:
            params = {"q": q, "maxResults": 500}
            if token:
                params["pageToken"] = token
            r = self.api("messages", **params)
            ids += [m["id"] for m in r.get("messages") or []]
            token = r.get("nextPageToken")
            if not token or len(ids) >= МАКС_ЗАБОР:
                break
        n = sum(self.новое(m) for m in reversed(ids[:МАКС_ЗАБОР]))
        self.cursor["history_id"] = hid
        self.log("догон %d дн.: %d писем%s" % (days, n, ", есть ещё" if token else ""))
        return n

    def history(self, start):
        hid, token, n, seen = start, None, 0, set()
        while True:
            params = {"startHistoryId": start, "maxResults": 500,
                      "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]}
            if token:
                params["pageToken"] = token
            r = self.api("history", **params)
            for h in r.get("history") or []:
                for a in h.get("messagesAdded") or []:
                    mid = a["message"]["id"]
                    if mid not in seen:
                        seen.add(mid)
                        n += self.новое(mid)
                for d in h.get("messagesDeleted") or []:
                    self.send(tombstone(d["message"]["id"], h.get("id"), self.now))
                    n += 1
                for l in (h.get("labelsAdded") or []) + (h.get("labelsRemoved") or []):
                    if "TRASH" in (l.get("labelIds") or []):
                        n += self.ревизия(l["message"]["id"], h.get("id"))
            hid = r.get("historyId") or hid
            token = r.get("nextPageToken")
            if not token:
                break
        self.cursor["history_id"] = hid
        return n

    def run(self, backfill_days=None):
        if backfill_days or not self.cursor.get("history_id"):
            n = self.backfill(backfill_days or ДНЕЙ)
        else:
            try:
                n = self.history(self.cursor["history_id"])
            except ОшибкаAPI as e:
                if e.code != 404:
                    raise
                self.log("курсор протух, догоняю %d дн." % ПРОТУХ_ДНЕЙ)
                n = self.backfill(ПРОТУХ_ДНЕЙ)
        self.save()
        self.beat()
        return n


def poster(url, token):
    def post(ev):
        req = urllib.request.Request(url + "/v1/ingest/email", method="POST",
                                     data=json.dumps(ev, ensure_ascii=False).encode("utf-8"))
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    return post


# ── Google: только здесь ходим наружу ─────────────────────────────────────

def _json(req, timeout=60):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _form(url, **fields):
    return urllib.request.Request(url, method="POST",
                                  data=urllib.parse.urlencode(fields).encode(),
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})


class Google:
    def __init__(self, client_id, client_secret, refresh_token, sleep=time.sleep):
        self.cid, self.secret, self.refresh, self.sleep = client_id, client_secret, refresh_token, sleep
        self.access, self.exp = None, 0

    def token(self):
        if self.access and time.time() < self.exp - 60:
            return self.access
        try:
            r = _json(_form(TOKEN, client_id=self.cid, client_secret=self.secret,
                            refresh_token=self.refresh, grant_type="refresh_token"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                raise НетВхода("токен отозван (%s), нужен --login" % e.code)
            raise
        self.access = r["access_token"]
        self.exp = time.time() + int(r.get("expires_in") or 3600)
        return self.access

    def __call__(self, path, **params):
        url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        for i in range(3):
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token()})
            try:
                return _json(req)
            except urllib.error.HTTPError as e:
                if e.code == 401 and i == 0:
                    self.access = None          # токен отозвали или истёк раньше срока
                    continue
                if e.code in (429, 500, 502, 503, 504) and i < 2:
                    self.sleep(5 * (i + 1))
                    continue
                raise ОшибкаAPI(e.code, e.reason)
            except urllib.error.URLError as e:
                if i < 2:
                    self.sleep(5 * (i + 1))
                    continue
                raise ОшибкаAPI(0, str(e.reason))


def _env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise НетВхода("нет %s в окружении (см. install/gmail.env.example)" % name)
    return v


def login(home, client_id, client_secret, port=ПОРТ):
    """Loopback-редирект: браузер владельца доходит до doctor через ssh -L."""
    os.makedirs(home, mode=0o700, exist_ok=True)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    redirect = "http://127.0.0.1:%d/" % port
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})
    print("Открой в браузере ноутбука (порт %d проброшен через ssh -L):\n\n%s\n" % (port, url))
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = q.get("state", [None])[0] == state and q.get("code")
            if ok:
                got["code"] = q["code"][0]
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Готово, вернись в терминал." if ok else "Не тот ответ.").encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", port), H)
    while "code" not in got:
        srv.handle_request()
    srv.server_close()
    tok = _json(_form(TOKEN, code=got["code"], client_id=client_id, client_secret=client_secret,
                      redirect_uri=redirect, grant_type="authorization_code", code_verifier=verifier))
    if not tok.get("refresh_token"):
        raise SystemExit("Google не отдал refresh token: отзови доступ в аккаунте и войди снова")
    req = urllib.request.Request(API + "profile", headers={"Authorization": "Bearer " + tok["access_token"]})
    email = _json(req).get("emailAddress", "")
    if not личный(email):
        raise SystemExit("%s — не личный Gmail, рабочую почту не подключаем (ТЗ §12); ничего не сохранил" % email)
    p = os.path.join(home, "token.json")
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"refresh_token": tok["refresh_token"], "email": email}, fh)
    os.replace(p + ".tmp", p)
    print("вошёл как %s" % email)


def _connect(home):
    cid, secret = _env("GMAIL_CLIENT_ID"), _env("GMAIL_CLIENT_SECRET")
    try:
        with open(os.path.join(home, "token.json"), encoding="utf-8") as fh:
            t = json.load(fh)
    except (OSError, ValueError):
        raise НетВхода("нет token.json — сначала --login")
    if not личный(t.get("email")):
        raise НетВхода("token.json не от личного ящика, отказываюсь")
    return Google(cid, secret, t["refresh_token"])


def sync(home, backfill_days=None):
    token = _env("MARA_CONTEXT_TOKEN")
    os.makedirs(home, mode=0o700, exist_ok=True)
    lock = open(os.path.join(home, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("уже идёт", flush=True)
        return 0
    api = _connect(home)
    n = Синк(api, poster(URL, token), home).run(backfill_days)
    print("%s событий: %d" % (datetime.now().isoformat(timespec="seconds"), n), flush=True)
    return 0


# ── самопроверка ──────────────────────────────────────────────────────────

def _b64(s, cs="utf-8"):
    return base64.urlsafe_b64encode(s.encode(cs)).rstrip(b"=").decode()


def _msg(mid, text="привет", labels=("INBOX",), html=None, att=None, ts=1756800000):
    parts = [{"mimeType": "text/plain", "body": {"data": _b64(text)}}]
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    if att:
        parts.append({"mimeType": "application/pdf", "filename": att,
                      "body": {"attachmentId": "att1", "size": 12345}})
    return {"id": mid, "threadId": "t" + mid, "labelIds": list(labels),
            "internalDate": str(ts * 1000), "snippet": text[:20], "sizeEstimate": 777,
            "payload": {"mimeType": "multipart/mixed", "headers": [
                {"name": "From", "value": "Анна <anna@example.com>"},
                {"name": "To", "value": "me@gmail.com"},
                {"name": "Subject", "value": "Тема " + mid},
                {"name": "Date", "value": "Tue, 2 Sep 2026 12:00:00 +0300"},
                {"name": "Message-ID", "value": "<%s@example.com>" % mid}],
                "parts": parts}}


class _API:
    """Фальшивый Gmail: профиль, список, письма, history по сценарию."""
    def __init__(self):
        self.hid = "100"
        self.msgs = {"m1": _msg("m1"), "m2": _msg("m2", html="<p>x</p>"),
                     "m3": _msg("m3", labels=("SPAM",)), "m4": _msg("m4", att="счёт.pdf")}
        self.history = []
        self.expired = False
        self.calls = []

    def __call__(self, path, **p):
        self.calls.append(path)
        if path == "profile":
            return {"emailAddress": "me@gmail.com", "historyId": self.hid}
        if path == "messages":
            assert p["q"].startswith("after:")
            return {"messages": [{"id": m} for m in ("m3", "m2", "m1")]}
        if path.startswith("messages/"):
            m = self.msgs.get(path.split("/")[1])
            if not m:
                raise ОшибкаAPI(404, "gone")
            return m
        if path == "history":
            if self.expired:
                raise ОшибкаAPI(404, "history expired")
            assert "messageDeleted" in p["historyTypes"]
            return {"history": self.history, "historyId": self.hid}
        raise AssertionError(path)


def self_check():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "gmail")
        api = _API()
        sent, fails = [], [urllib.error.URLError("нет сети"), OSError("сброс")]

        def post(ev):
            if fails:
                raise fails.pop()
            if ev["source_id"] == "m9":
                raise urllib.error.HTTPError("u", 400, "плохо", {}, None)
            sent.append(ev)
            return {"event_id": "e", "duplicate": False}

        log = []
        s = Синк(api, post, home, log=log.append, sleep=lambda t: None,
                 now=lambda: 1756800000, today=lambda: date(2026, 9, 2))
        # первый прогон: курсора нет → догон, спам пропущен, старые→новые
        assert s.run() == 3 and [e["source_id"] for e in sent] == ["m1", "m2"], sent
        assert s.cursor["history_id"] == "100" and not fails, "два обрыва сети пережиты"
        ev = sent[1]["payload"]
        assert ev["subject"] == "Тема m2" and ev["from"].startswith("Анна") and ev["has_html"]
        assert ev["text"] == "привет" and ev["outgoing"] is False and ev["labels"] == ["INBOX"]
        # второй прогон: history — новое с вложением, удаление, корзина, исчезнувшее
        api.hid = "105"
        api.msgs["m2"]["labelIds"] = ["TRASH"]
        api.history = [{"id": "101", "messagesAdded": [{"message": {"id": "m4"}}]},
                       {"id": "102", "messagesDeleted": [{"message": {"id": "m1"}}]},
                       {"id": "103", "labelsAdded": [{"message": {"id": "m2"}, "labelIds": ["TRASH"]}]},
                       {"id": "104", "messagesAdded": [{"message": {"id": "gone"}}]}]
        assert s.run() == 3, log
        ids = [e["source_id"] for e in sent]
        assert ids == ["m1", "m2", "m4", "m1/deleted", "m2/labels/103"], ids
        assert sent[2]["payload"]["attachments"] == [
            {"name": "счёт.pdf", "mime": "application/pdf", "size": 12345, "attachment_id": "att1"}]
        assert "attachmentId" not in json.dumps(sent[2]) and "data" not in sent[2]["payload"]
        assert sent[3]["payload"]["tombstone_of"] == "m1"
        assert sent[4]["payload"]["revision_of"] == "m2" and sent[4]["payload"]["trashed"] is True
        assert s.cursor["history_id"] == "105"
        # диск: курсор, сырой поток, сердцебиение
        assert json.load(open(os.path.join(home, "cursor.json")))["history_id"] == "105"
        raw = open(os.path.join(home, "raw", "2026-09-02.jsonl"), encoding="utf-8").read().splitlines()
        assert len(raw) == 5 and [json.loads(l)["id"] for l in raw][:3] == ["m1", "m2", "m3"], \
            "старые→новые; в raw и спам: локально, это не утечка"
        assert os.path.exists(os.path.join(home, "heartbeat"))
        # 4xx: в журнал и дальше, без повторов
        assert s.send({"source_id": "m9"}) is None and any("отвергнуто 400" in l for l in log)
        # протухший курсор → свежий historyId и догон, а не цикл
        api.expired, api.hid = True, "200"
        n = s.run()
        assert s.cursor["history_id"] == "200" and n == 3 and any("протух" in l for l in log), (n, log)
        assert not any(("Тема" in l or "привет" in l) for l in log), "тем и тел в журнале нет"
        # html → текст
        t = html_text("<html><head><title>x</title><style>p{}</style></head><body>"
                      "<p>Привет,&nbsp;Серёга!</p><script>alert(1)</script><div>до <b>пятницы</b></div></body></html>")
        assert t == "Привет, Серёга!\nдо пятницы", repr(t)
        # кодировка из заголовка части
        m = _msg("w"); m["payload"]["parts"][0] = {
            "mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": 'text/plain; charset="windows-1251"'}],
            "body": {"data": _b64("Привет", "cp1251")}}
        assert email_event(m)["payload"]["text"] == "Привет"
        assert личный("Me@Gmail.com") and not личный("me@example.com") and not личный("")
        # env-файл: крон читает его через скрипт, а не через set -a
        envf = os.path.join(tmp, "gmail.env")
        with open(envf, "w") as fh:
            fh.write("# к\nGMAIL_CLIENT_ID=id.apps\nGMAIL_CLIENT_SECRET='sec'\nMARA_CONTEXT_TOKEN=tok\nПУСТО=\n")
        e = {"GMAIL_CLIENT_ID": "своё"}
        assert load_env(envf, e) == 2 and e == {"GMAIL_CLIENT_ID": "своё", "GMAIL_CLIENT_SECRET": "sec",
                                                "MARA_CONTEXT_TOKEN": "tok"}, e
        e = {}
        load_env(os.path.join(os.path.dirname(__file__), "..", "install", "gmail.env.example"), e)
        assert e == {}, "пример без значений ничего не выставляет"
        assert load_env(os.path.join(tmp, "нет"), {}) == 0
    print("ok: gmail_ingest")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--env", default=ENV, help="файл с GMAIL_CLIENT_ID/SECRET и MARA_CONTEXT_TOKEN")
    ap.add_argument("--login", action="store_true", help="одноразовый вход через браузер")
    ap.add_argument("--sync", action="store_true", help="прогон по history (крон)")
    ap.add_argument("--backfill", action="store_true", help="забор истории за --days")
    ap.add_argument("--days", type=int, default=ДНЕЙ)
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    os.umask(0o077)
    home = state_dir(a.root)
    try:
        if a.self_check:
            return self_check()
        load_env(a.env)
        if a.login:
            login(home, _env("GMAIL_CLIENT_ID"), _env("GMAIL_CLIENT_SECRET"))
            return 0
        if a.backfill:
            return sync(home, a.days)
        if a.sync:
            return sync(home)
    except НетВхода as e:
        print("нет входа: %s" % e, flush=True)
        return 2
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
