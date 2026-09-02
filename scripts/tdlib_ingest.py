#!/usr/bin/env python3
"""Личный Telegram → память (ТЗ §11, спека
docs/superpowers/specs/2026-09-02-telegram-tdlib-design.md).

TDLib-клиент как пользователь на doctor. Читает апдейты и шлёт сообщения в
contextd тем же контрактом, что и звонки. Правки и удаления — отдельные
события (ревизии и надгробия), не перезапись: `put_event` дедупит по
source_id, и правка с тем же ключом просто пропала бы.

    tdlib_ingest.py --login        # один раз, в терминале: телефон, код, пароль
    tdlib_ingest.py --serve        # демон (install/tdlib-ingest.service)
    tdlib_ingest.py --catch-up     # разовый догон по истории
    tdlib_ingest.py --self-check

Окружение: TDLIB_API_ID, TDLIB_API_HASH (my.telegram.org), MARA_CONTEXT_TOKEN
(устройство `tdlib` в contextd), MARA_CONTEXT_URL, MARA_BLOBS. Библиотека
`telegram` (python-telegram, venv-tdlib) импортируется только в --login,
--serve и --catch-up: разбор и самопроверка живут без неё, гейт зелёный везде.
"""
import os, sys, json, time, signal, secrets, getpass, argparse, threading
import urllib.request, urllib.error
from datetime import datetime, timezone

ROOT = os.environ.get("MARA_BLOBS", "/srv/mara-blobs")
URL = os.environ.get("MARA_CONTEXT_URL", "http://127.0.0.1:8788")
# чат с Марой уже целиком в памяти через hermes-ingest; каналы — не переписка
ПРОПУСКАЕМ = ("bot", "channel")
ГЛУБИНА = 100          # чат, которого нет в курсоре: не глубже последних N сообщений
ДОГОН_КАЖДЫЕ = 3600
ЧАТОВ = 200
СТРАНИЦА = 100


def state_dir(root=ROOT):
    return os.path.join(root, "tdlib")


def iso(ts):
    return datetime.fromtimestamp(int(ts or 0), timezone.utc).astimezone().isoformat(timespec="seconds")


# ── разбор словарей TDLib: чистые функции, без библиотеки ─────────────────

def chat_kind(chat, user=None):
    """private|secret|group|bot|channel. Пропускаемые — в ПРОПУСКАЕМ."""
    t = chat.get("type") or {}
    tt = t.get("@type", "")
    if tt == "chatTypePrivate":
        return "bot" if ((user or {}).get("type") or {}).get("@type") == "userTypeBot" else "private"
    if tt == "chatTypeSecret":
        return "secret"
    if tt == "chatTypeBasicGroup":
        return "group"
    if tt == "chatTypeSupergroup":
        return "channel" if t.get("is_channel") else "group"
    return tt or "unknown"


def _file(obj):
    """Объект `file` внутри вложения: у фото это последний (самый большой) размер."""
    if obj.get("@type") == "file":
        return obj
    for v in obj.values():
        if isinstance(v, list) and v and isinstance(v[-1], dict):
            v = v[-1]
        if isinstance(v, dict):
            f = _file(v)
            if f:
                return f
    return None


def media(content):
    """Метаданные вложения, без тела (ТЗ §11: скачивание по policy, а её нет).
    Тип — имя поля TDLib: photo, document, voice_note, video, audio, sticker…"""
    objs = [(k, v) for k, v in content.items() if isinstance(v, dict) and k != "caption"]
    if content.get("@type") == "messageText" or not objs:
        return []
    k, obj = objs[0]
    m = {"type": k}
    for src, dst in (("mime_type", "mime"), ("file_name", "name"), ("duration", "duration")):
        if obj.get(src):
            m[dst] = obj[src]
    f = _file(obj) or {}
    if f.get("size"):
        m["size"] = f["size"]
    return [m]


def text_of(content):
    t = content.get("text") or content.get("caption") or {}
    return t.get("text", "") if isinstance(t, dict) else ""


def message_event(msg, chat, kind, sender_name):
    """Новое сообщение → событие contextd. None — чат пропускаем."""
    if kind in ПРОПУСКАЕМ:
        return None
    key = "%s/%s" % (msg["chat_id"], msg["id"])
    s = msg.get("sender_id") or {}
    reply = msg.get("reply_to") or {}
    content = msg.get("content") or {}
    return {"source": "telegram", "source_id": key, "occurred_at": iso(msg.get("date")),
            "classification": "personal",
            "payload": {"chat_id": msg["chat_id"], "chat_type": kind,
                        "chat_title": chat.get("title", ""), "message_id": msg["id"],
                        "sender_id": s.get("user_id") or s.get("chat_id"),
                        "sender_name": sender_name, "outgoing": bool(msg.get("is_outgoing")),
                        "text": text_of(content),
                        "reply_to": reply.get("message_id")
                        if reply.get("@type") == "messageReplyToMessage" else None,
                        "thread_id": msg.get("message_thread_id") or None,
                        "media": media(content)}}


def revision_event(msg, chat, kind, sender_name):
    """Правка — ревизия с собственным source_id, иначе дедуп её выбросит."""
    ev = message_event(msg, chat, kind, sender_name)
    if not ev:
        return None
    edit = int(msg.get("edit_date") or 0)
    key = ev["source_id"]
    ev["source_id"] = "%s/edit/%d" % (key, edit)
    ev["occurred_at"] = iso(edit or msg.get("date"))
    ev["payload"].update(revision_of=key, edit_date=edit)
    return ev


def tombstones(update):
    """updateDeleteMessages → надгробия. Чистка кэша TDLib — не удаление у человека."""
    if not update.get("is_permanent") or update.get("from_cache"):
        return []
    now = iso(time.time())
    out = []
    for mid in update.get("message_ids") or []:
        key = "%s/%s" % (update["chat_id"], mid)
        out.append({"source": "telegram", "source_id": key + "/deleted", "occurred_at": now,
                    "classification": "personal",
                    "payload": {"chat_id": update["chat_id"], "message_id": mid,
                                "tombstone_of": key}})
    return out


# ── демон ──────────────────────────────────────────────────────────────────

class Демон:
    """Всё, что зависит от TDLib, приходит через `client.call_method`; в
    самопроверке клиент фальшивый. `post(event) -> dict` — вход в contextd."""

    def __init__(self, client, post, home, log=None, sleep=time.sleep):
        self.c, self.post, self.home, self.sleep = client, post, home, sleep
        self.log = log or (lambda s: print(s, flush=True))
        os.makedirs(os.path.join(home, "raw"), exist_ok=True)
        self._cursor_path = os.path.join(home, "cursor.json")
        try:
            with open(self._cursor_path, encoding="utf-8") as fh:
                self.cursor = json.load(fh)
        except (OSError, ValueError):
            self.cursor = {}
        self._chats, self._users = {}, {}
        self.lock = threading.Lock()
        self.connected = True

    def call(self, method, **params):
        return self.c.call_method(method, params, block=True).update

    def chat(self, chat_id):
        if chat_id not in self._chats:
            self._chats[chat_id] = self.call("getChat", chat_id=chat_id)
        return self._chats[chat_id]

    def user(self, user_id):
        if user_id not in self._users:
            self._users[user_id] = self.call("getUser", user_id=user_id)
        return self._users[user_id]

    def kind(self, chat):
        t = chat.get("type") or {}
        user = self.user(t["user_id"]) if t.get("@type") == "chatTypePrivate" else None
        return chat_kind(chat, user)

    def sender_name(self, msg):
        s = msg.get("sender_id") or {}
        if s.get("@type") == "messageSenderUser":
            u = self.user(s["user_id"])
            return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) \
                or str(s["user_id"])
        if s.get("@type") == "messageSenderChat":
            return self.chat(s["chat_id"]).get("title", "")
        return ""

    # ── приём ──

    def handle(self, update):
        """Вход для апдейтов. Исключение не роняет демон: воркер python-telegram
        ловит и пишет в журнал, следующий апдейт идёт своим чередом."""
        t = update.get("@type")
        if t == "updateConnectionState":
            self.connected = (update.get("state") or {}).get("@type") == "connectionStateReady"
            if self.connected:
                self.beat()
            return
        cid = update.get("chat_id") or (update.get("message") or {}).get("chat_id")
        if cid is None:
            return
        if self.kind(self.chat(cid)) in ПРОПУСКАЕМ:
            return          # ни в raw, ни getMessage: канал и бот не читаем вовсе
        self.raw(update)
        if t in ("updateNewMessage", "updateMessageSendSucceeded"):
            self.message(update["message"], revision=False)
        elif t == "updateMessageEdited":
            msg = self.call("getMessage", chat_id=cid, message_id=update["message_id"])
            msg["edit_date"] = update.get("edit_date") or msg.get("edit_date")
            self.message(msg, revision=True)
        elif t == "updateDeleteMessages":
            for ev in tombstones(update):
                self.send(ev)

    def message(self, msg, revision):
        if msg.get("sending_state"):
            return          # своё, ещё не ушло: настоящий id придёт в updateMessageSendSucceeded
        chat = self.chat(msg["chat_id"])
        kind = self.kind(chat)
        if kind in ПРОПУСКАЕМ:
            return
        ev = (revision_event if revision else message_event)(msg, chat, kind, self.sender_name(msg))
        if self.send(ev) is None or revision:
            return
        with self.lock:
            cid = str(msg["chat_id"])
            if msg["id"] > self.cursor.get(cid, 0):
                self.cursor[cid] = msg["id"]
                tmp = self._cursor_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.cursor, fh)
                os.replace(tmp, self._cursor_path)

    def send(self, ev):
        """POST не сдаётся: contextd в перезапуске — ждём, а не теряем. Сдаёмся
        только на 4xx: это наша ошибка в контракте, повтор её не вылечит."""
        pause = 1
        while True:
            try:
                r = self.post(ev)
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500:
                    self.log("%s: contextd отверг (%d), пропускаю" % (ev["source_id"], e.code))
                    return None
                err = "%d" % e.code
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, e)
            else:
                self.log("%s %s" % (ev["source_id"], "dup" if r.get("duplicate") else "ok"))
                self.beat()
                return r
            self.log("contextd не отвечает (%s), повтор через %d с" % (err, pause))
            self.sleep(pause)
            pause = min(pause * 2, 60)

    def raw(self, update):
        """Сырой поток как есть, append-only (ТЗ §11). Это не лог: текст здесь есть."""
        p = os.path.join(self.home, "raw", time.strftime("%Y-%m-%d") + ".jsonl")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(update, ensure_ascii=False) + "\n")

    def beat(self):
        p = os.path.join(self.home, "heartbeat")
        with open(p, "a"):
            pass
        os.utime(p, None)

    # ── догон (ТЗ §17: Telegram TDLib cursor/history) ──

    def catch_up(self):
        """TDLib апдейты второй раз не шлёт: что пропустили лёжа — берём из истории."""
        # getChats отдаёт только то, что уже в локальной базе: после --login там
        # почти пусто. loadChats тянет список с сервера, 404 — «всё загружено».
        for _ in range(50):
            try:
                self.call("loadChats", limit=ЧАТОВ)
            except RuntimeError:
                break
        n = 0
        for cid in self.call("getChats", limit=ЧАТОВ).get("chat_ids") or []:
            chat = self.chat(cid)
            if self.kind(chat) in ПРОПУСКАЕМ:
                continue
            seen = self.cursor.get(str(cid), 0)
            if ((chat.get("last_message") or {}).get("id") or 0) <= seen:
                continue
            n += self.history(cid, seen)
        self.log("догон: %d сообщений" % n)
        return n

    def history(self, cid, seen):
        got, frm = [], 0
        budget = None if seen else ГЛУБИНА
        while True:
            page = self.call("getChatHistory", chat_id=cid, from_message_id=frm, offset=0,
                             limit=СТРАНИЦА, only_local=False).get("messages") or []
            fresh = [m for m in page if m["id"] > seen]
            got += fresh
            if not page or len(fresh) < len(page) or (budget and len(got) >= budget):
                break
            frm = page[-1]["id"]
        got = got[:budget] if budget else got
        for m in reversed(got):          # старые раньше — курсор растёт монотонно
            self.message(m, revision=False)
        return len(got)

    def run(self, stop):
        """Главный поток: догон при старте и раз в час, сердцебиение раз в минуту."""
        last = 0
        while not stop.is_set():
            if time.time() - last >= ДОГОН_КАЖДЫЕ:
                try:
                    self.catch_up()
                except Exception as e:
                    self.log("догон не удался: %s: %s" % (type(e).__name__, e))
                last = time.time()
            if self.connected:
                self.beat()
            stop.wait(60)


def poster(url, token):
    def post(ev):
        req = urllib.request.Request(url + "/v1/ingest/message", method="POST",
                                     data=json.dumps(ev, ensure_ascii=False).encode("utf-8"))
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    return post


# ── TDLib: только здесь импортируется библиотека ───────────────────────────

class НетВхода(RuntimeError):
    pass


def _secret_file(path, make):
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(make())
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def client(home, api_id, api_hash, phone=None, daemon=True):
    from telegram.client import Telegram

    class Клиент(Telegram):
        """Демон не умеет спрашивать код: в состоянии «нужен телефон» он выходит,
        а не заказывает владельцу SMS на каждый перезапуск юнита."""
        def _send_phone_number_or_bot_token(self):
            if daemon:
                raise НетВхода("сессии нет: запусти tdlib_ingest.py --login")
            return super()._send_phone_number_or_bot_token()

    os.makedirs(home, mode=0o700, exist_ok=True)
    key = _secret_file(os.path.join(home, "key"), lambda: secrets.token_hex(32))
    phone_path = os.path.join(home, "phone")
    if phone:
        _secret_file(phone_path, lambda: phone)
    elif os.path.exists(phone_path):
        phone = _secret_file(phone_path, lambda: "")
    else:
        raise НетВхода("входа не было: запусти tdlib_ingest.py --login")
    return Клиент(api_id=int(api_id), api_hash=api_hash, database_encryption_key=key,
                  phone=phone, files_directory=os.path.join(home, "db"),
                  device_model="mara doctor", tdlib_verbosity=1,
                  default_workers_queue_size=10000)


def login(home, api_id, api_hash):
    from telegram.client import AuthorizationState as S
    phone = input("телефон (+7…): ").strip()
    tg = client(home, api_id, api_hash, phone=phone, daemon=False)
    st = tg.login(blocking=False)
    while st != S.READY:
        if st == S.WAIT_CODE:
            tg.send_code(input("код из Telegram: ").strip())
        elif st == S.WAIT_PASSWORD:
            tg.send_password(getpass.getpass("пароль двухфакторки: "))
        else:
            raise SystemExit("состояние %s: так войти не умею" % st.value)
        st = tg.login(blocking=False)
    me = tg.get_me()
    me.wait()
    print("вошёл как %s; сессия в %s" % ((me.update or {}).get("first_name", "?"),
                                          os.path.join(home, "db")))
    tg.stop()
    return 0


def _connect(home):
    api_id, api_hash = os.environ.get("TDLIB_API_ID"), os.environ.get("TDLIB_API_HASH")
    if not api_id or not api_hash:
        raise НетВхода("нет TDLIB_API_ID/TDLIB_API_HASH (my.telegram.org → /etc/mara/tdlib.env)")
    from telegram.client import AuthorizationState as S
    tg = client(home, api_id, api_hash)
    st = tg.login(blocking=False)
    if st != S.READY:
        tg.stop()
        raise НетВхода("состояние %s: запусти tdlib_ingest.py --login" % st.value)
    return tg


def serve(home, once=False):
    token = os.environ.get("MARA_CONTEXT_TOKEN")
    if not token:
        raise НетВхода("нет MARA_CONTEXT_TOKEN: contextd.py --pair tdlib → /etc/mara/tdlib.env")
    tg = _connect(home)
    d = Демон(tg, poster(URL, token), home)
    if once:
        n = d.catch_up()
        tg.stop()
        return 0
    for t in ("updateNewMessage", "updateMessageSendSucceeded", "updateMessageEdited",
              "updateDeleteMessages", "updateConnectionState"):
        tg.add_update_handler(t, d.handle)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    d.log("демон TDLib поднят, состояние в %s" % home)
    d.run(stop)
    tg.stop()
    return 0


# ── самопроверка на фальшивом клиенте ──────────────────────────────────────

class _Ответ:
    def __init__(self, update):
        self.update = update


class _Фальшивый:
    def __init__(self, chats, users, history):
        self.chats, self.users, self.history = chats, users, history

    def call_method(self, m, params, block=False):
        if m == "loadChats":
            self.loaded = getattr(self, "loaded", 0) + 1
            if self.loaded > 1:
                raise RuntimeError("Telegram error: 404 all chats loaded")
            return _Ответ({"@type": "ok"})
        if m == "getChat":
            return _Ответ(self.chats[params["chat_id"]])
        if m == "getUser":
            return _Ответ(self.users[params["user_id"]])
        if m == "getChats":
            return _Ответ({"chat_ids": list(self.chats)})
        if m == "getChatHistory":
            msgs = sorted(self.history.get(params["chat_id"], []), key=lambda x: -x["id"])
            if params["from_message_id"]:
                msgs = [x for x in msgs if x["id"] < params["from_message_id"]]
            return _Ответ({"messages": msgs[:params["limit"]]})
        if m == "getMessage":
            for x in self.history.get(params["chat_id"], []):
                if x["id"] == params["message_id"]:
                    return _Ответ(dict(x))
        raise AssertionError("фальшивый клиент не знает %s" % m)


def _msg(chat_id, mid, text, date=1756800000, **kw):
    d = {"@type": "message", "id": mid, "chat_id": chat_id, "date": date,
         "sender_id": {"@type": "messageSenderUser", "user_id": 42},
         "content": {"@type": "messageText", "text": {"@type": "formattedText", "text": text}}}
    d.update(kw)
    return d


def self_check():
    import tempfile
    home = tempfile.mkdtemp()
    chats = {7: {"id": 7, "title": "Анна", "type": {"@type": "chatTypePrivate", "user_id": 42},
                 "last_message": {"id": 3}},
             8: {"id": 8, "title": "Мара", "type": {"@type": "chatTypePrivate", "user_id": 99},
                 "last_message": {"id": 5}},
             9: {"id": 9, "title": "Новости", "type": {"@type": "chatTypeSupergroup", "is_channel": True},
                 "last_message": {"id": 1}}}
    users = {42: {"id": 42, "first_name": "Анна", "last_name": "Петрова", "type": {"@type": "userTypeRegular"}},
             99: {"id": 99, "first_name": "Мара", "type": {"@type": "userTypeBot"}}}
    history = {7: [_msg(7, 1, "привет"), _msg(7, 2, "пришлю смету в пятницу", edit_date=1756800600),
                   _msg(7, 3, "ок")],
               8: [_msg(8, 5, "бот")], 9: [_msg(9, 1, "канал")]}
    sent, fails = [], [2]

    def post(ev):
        if fails and fails[0] > 0:
            fails[0] -= 1
            raise ConnectionError("contextd лежит")
        sent.append(ev)
        return {"event_id": "message_x", "duplicate": ev["source_id"] in {s["source_id"] for s in sent[:-1]}}

    d = Демон(_Фальшивый(chats, users, history), post, home, log=lambda s: None, sleep=lambda s: None)
    # живое сообщение: две неудачи сети не роняют и не теряют
    d.handle({"@type": "updateNewMessage", "message": _msg(7, 1, "привет")})
    assert len(sent) == 1 and sent[0]["source_id"] == "7/1", sent
    assert sent[0]["payload"]["sender_name"] == "Анна Петрова"
    assert sent[0]["payload"]["chat_type"] == "private" and sent[0]["classification"] == "personal"
    assert d.cursor == {"7": 1}, d.cursor
    assert os.path.exists(os.path.join(home, "heartbeat"))
    # бот и канал — мимо
    d.handle({"@type": "updateNewMessage", "message": _msg(8, 5, "бот")})
    d.handle({"@type": "updateNewMessage", "message": _msg(9, 1, "канал")})
    assert len(sent) == 1, "чат с ботом и канал не должны уезжать"
    # правка — ревизия, а не тот же ключ
    d.handle({"@type": "updateMessageEdited", "chat_id": 7, "message_id": 2, "edit_date": 1756800600})
    assert sent[-1]["source_id"] == "7/2/edit/1756800600" and sent[-1]["payload"]["revision_of"] == "7/2"
    assert d.cursor == {"7": 1}, "ревизия курсор не двигает"
    # удаление из кэша — не удаление; настоящее — надгробие
    d.handle({"@type": "updateDeleteMessages", "chat_id": 7, "message_ids": [1], "is_permanent": True, "from_cache": True})
    assert sent[-1]["source_id"] != "7/1/deleted"
    d.handle({"@type": "updateDeleteMessages", "chat_id": 7, "message_ids": [1], "is_permanent": True, "from_cache": False})
    assert sent[-1]["payload"]["tombstone_of"] == "7/1"
    # своё неотправленное — ждём настоящий id
    d.handle({"@type": "updateNewMessage", "message": _msg(7, 77, "черновик", sending_state={"@type": "messageSendingStatePending"})})
    assert sent[-1]["source_id"] != "7/77"
    # догон: в апдейтах были 1 и правка 2, история дотягивает 2 и 3; бот и канал мимо
    n = d.catch_up()
    assert n == 2, n
    assert [s["source_id"] for s in sent[-2:]] == ["7/2", "7/3"], "старые раньше"
    assert d.cursor == {"7": 3}
    assert d.catch_up() == 0, "второй догон пуст"
    # новый клиент с тем же каталогом читает курсор
    assert Демон(_Фальшивый(chats, users, history), post, home, log=lambda s: None).cursor == {"7": 3}
    # сырой поток дописан построчно
    raw = open(os.path.join(home, "raw", time.strftime("%Y-%m-%d") + ".jsonl"), encoding="utf-8").read().splitlines()
    assert len(raw) == 5 and json.loads(raw[0])["@type"] == "updateNewMessage", \
        "бот и канал не должны попасть даже в сырой поток"
    assert d.c.loaded == 3, "loadChats крутится до 404: два вызова в первом догоне, один во втором"
    # 4xx — наша ошибка, не крутимся вечно
    def post400(ev):
        raise urllib.error.HTTPError("u", 400, "bad", {}, None)
    d2 = Демон(_Фальшивый(chats, users, history), post400, home, log=lambda s: None, sleep=lambda s: None)
    assert d2.send({"source_id": "x"}) is None
    # вложение: метаданные, не тело
    ph = {"@type": "messagePhoto", "caption": {"@type": "formattedText", "text": "смотри"},
          "photo": {"@type": "photo", "sizes": [
              {"@type": "photoSize", "type": "s", "photo": {"@type": "file", "id": 1, "size": 100}},
              {"@type": "photoSize", "type": "x", "photo": {"@type": "file", "id": 2, "size": 5000}}]}}
    ev = message_event(_msg(7, 4, "", content=ph), chats[7], "private", "Анна")
    assert ev["payload"]["text"] == "смотри" and ev["payload"]["media"] == [{"type": "photo", "size": 5000}], ev["payload"]
    print("ok: tdlib_ingest")
    return 0


def main():
    ap = argparse.ArgumentParser(description="личный Telegram через TDLib → contextd")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--catch-up", action="store_true", dest="catch_up")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    os.umask(0o077)
    home = state_dir(a.root)
    try:
        if a.login:
            api_id, api_hash = os.environ.get("TDLIB_API_ID"), os.environ.get("TDLIB_API_HASH")
            if not api_id or not api_hash:
                raise НетВхода("нет TDLIB_API_ID/TDLIB_API_HASH: set -a; . /etc/mara/tdlib.env")
            return login(home, api_id, api_hash)
        if a.serve or a.catch_up:
            return serve(home, once=a.catch_up)
    except НетВхода as e:
        print("нет входа: %s" % e, flush=True)
        return 2
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
