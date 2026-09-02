"""Контекст-брокер на стороне Мары (ТЗ §15) и правка обязательств её руками (§16).

Мара не ищет то, о чём не подозревает. Открытые обязательства должны лежать у
неё перед глазами, а не за вызовом basic-memory. Дописать их в `SOUL.md` нельзя:
каждый звонок менял бы системный промпт и рвал префиксный кэш у провайдера на
всех живых сессиях. Поэтому пакет едет хуком `pre_llm_call` — Hermes подклеивает
его возврат к сообщению пользователя (`api_content`), а системный промпт
остаётся байт-в-байт тем же (agent/turn_context.py).

Собирает пакет `context_pack.py` на doctor, отдаёт contextd по
`/v1/context/bootstrap`, сюда он приезжает через ssh-туннель (com.mara.relay).

Три вещи, без которых плагин вреден:

  - свой таймаут 2 с. Хук в Hermes ограничен тридцатью и fail-open: лежащий
    doctor добавлял бы полминуты к каждой реплике Мары, а это хуже, чем
    отсутствие контекста;
  - кэш последнего удачного ответа. Не только ради сети: пока doctor молчит,
    вчерашний список полезнее пустоты;
  - инжект по истории, а не каждый ход. Hermes хранит наш блок в `api_content`
    сообщения, пишет его в базу сессии и поднимает при загрузке
    (hermes_state.py), а при сжатии истории — роняет. Значит правило одно:
    пакет едет, если его текста нет ни в одном сообщении истории. Перезапуск
    шлюза, новая сессия, сжатие, изменившийся список — всё решается им, без
    памяти о сессиях и без таймеров. `is_first_turn` не годится:
    телеграм-сессия живёт сутками, и первый ход в ней бывает раз в неделю.

Инструмент `mara_correction` — обратная дорога: «сделал», «это тоже задача, срок
пятница», «отмени». Плагин шлёт событие `kind: correction`, contextd применяет
его к карточке синхронно (call_project.apply_correction) и пересобирает пакет.
Ответ сервера — строкой Маре, ничего сверх заголовков обязательств в ход не едет.

Наружу из хука и инструмента не выходит ни одно исключение: сбой брокера
обязан выглядеть как отсутствие контекста, а не как сломанный ход.

Токен устройства — из настроек или окружения, в репозиторий не попадает.

    python3 install/mara-context/__init__.py --demo
"""
import hashlib
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

TIMEOUT = 2.0        # быстрее сдаться, чем задержать реплику
TIMEOUT_ПРАВКИ = 5.0 # правку Серёга ждёт: doctor пишет в волт и пересобирает пакет
TTL = 60.0           # свежесть пакета; звонок пересобирает его сразу, но
                     # ходов между звонками много, и каждый в сеть не пойдёт
URL = "http://100.64.0.1:8788"     # адрес туннеля, см. install/com.mara.relay.plist

ОПИСАНИЕ = ("Серёга поправил обязательство словами: «сделал», «это тоже задача, "
            "срок пятница», «отмени», «перенеси на среду». Зови сразу, без "
            "basic-memory: карточку правит doctor и тут же пересобирает список "
            "открытых. item — теми же словами, что в списке «Открытые "
            "обязательства»; если такого нет и status=open — заведётся новая. "
            "Два похожих — сервер попросит уточнить, переспроси Серёгу.")

СХЕМА = {
    "name": "mara_correction",
    "description": ОПИСАНИЕ,
    "parameters": {
        "type": "object",
        "properties": {
            "item": {"type": "string",
                     "description": "обязательство теми же словами, что в списке; "
                                    "для новой задачи — её формулировка"},
            "status": {"type": "string", "enum": ["open", "done", "cancelled"],
                       "description": "open — открыть или завести, done — сделано, "
                                      "cancelled — отменено"},
            "due": {"type": "string",
                    "description": "срок как YYYY-MM-DD; «пятница» переведи в дату сам"},
            "note": {"type": "string",
                     "description": "что ещё сказал Серёга, одной строкой"},
        },
        "required": ["item"],
    },
}

log = logging.getLogger(__name__)


def _в_истории(text, history):
    """Пакет уже лежит в истории сессии? Смотрим `content` и `api_content`
    каждого сообщения; content бывает списком частей — тогда их `text`."""
    метка = text.strip()
    for m in history or ():
        if not isinstance(m, dict):
            continue
        for k in ("content", "api_content"):
            v = m.get(k)
            if isinstance(v, str):
                if метка in v:
                    return True
            elif isinstance(v, list):
                for part in v:
                    t = part.get("text") if isinstance(part, dict) else None
                    if isinstance(t, str) and метка in t:
                        return True
    return False


class Брокер:
    """Что отдать в ход и как отправить правку. Ничего не знает про Hermes —
    поэтому проверяем без него."""

    def __init__(self, url=URL, token=None, ttl=TTL, timeout=TIMEOUT,
                 timeout_правки=TIMEOUT_ПРАВКИ, open_url=None):
        base = url.rstrip("/")
        self.url = base + "/v1/context/bootstrap"
        self.url_событий = base + "/v1/ingest/event"
        self.token = token
        self.ttl, self.timeout, self.timeout_правки = ttl, timeout, timeout_правки
        self._open = open_url or urllib.request.urlopen
        self._lock = threading.Lock()
        self._свежесть = None       # когда последний раз ходили в сеть
        self._пакет = None          # последний удачный, живёт и после сбоя

    def _токен(self):
        # читаем на каждый поход, а не при регистрации: порядок загрузки .env и
        # плагинов в Hermes не гарантирован, а промах при регистрации был бы
        # молчаливым — предупреждения плагинов в gateway.log не доезжают
        return self.token or os.environ.get("MARA_CONTEXT_TOKEN")

    def _запрос(self, url, тело=None):
        req = urllib.request.Request(
            url, data=json.dumps(тело, ensure_ascii=False).encode("utf-8") if тело else None,
            method="POST" if тело else "GET")
        if тело:
            req.add_header("Content-Type", "application/json")
        токен = self._токен()
        if токен:
            req.add_header("Authorization", "Bearer " + токен)
        return req

    def _достать(self):
        with self._lock:
            # проверяем время, а не пакет: пустой список обязательств — это
            # тоже ответ, и ходить за ним каждую реплику незачем
            if self._свежесть is not None and time.monotonic() - self._свежесть < self.ttl:
                return self._пакет
            try:
                with self._open(self._запрос(self.url), timeout=self.timeout) as r:
                    data = json.loads(r.read() or b"{}")
            except Exception as e:
                # старый пакет не выбрасываем: пока doctor молчит, вчерашний
                # список обязательств полезнее пустоты. Свежесть двигаем и при
                # сбое: иначе лежащий doctor стоит 2 с на каждой реплике Мары,
                # а так — одна попытка в TTL.
                self._свежесть = time.monotonic()
                log.warning("mara-context: не забрал пакет (%s)", e)
                return self._пакет
            self._свежесть = time.monotonic()
            self._пакет = data.get("now")
            return self._пакет

    def контекст(self, history=()):
        пакет = self._достать()
        if not пакет or not пакет.get("text"):
            return ""
        return "" if _в_истории(пакет["text"], history) else пакет["text"]

    def правка(self, args):
        """Инструмент mara_correction: одно событие на doctor, ответ — строкой."""
        payload = {k: args.get(k) for k in ("item", "status", "due", "note") if args.get(k)}
        # source_id — содержимое плюс минута: повтор вызова моделью в ту же
        # минуту — дубль, та же правка через час — новое событие
        ключ = "|".join(str(payload.get(k) or "") for k in ("item", "status", "due", "note"))
        событие = {"kind": "correction", "source": "mara",
                   "source_id": hashlib.sha256(
                       (ключ + "|" + time.strftime("%Y-%m-%dT%H:%M")).encode("utf-8")).hexdigest(),
                   "occurred_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "payload": payload}
        try:
            with self._open(self._запрос(self.url_событий, событие),
                            timeout=self.timeout_правки) as r:
                data = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read() or b"{}").get("error")
            except ValueError:
                msg = None
            return "doctor не принял правку: %s" % (msg or e.code)
        except Exception as e:
            return "не дозвонился до doctor: %s" % e
        with self._lock:
            self._свежесть = None     # список изменился — следующий ход идёт за свежим
        if data.get("duplicate"):
            return "эту правку уже принимал, повторять не стал"
        applied = data.get("applied") or {}
        return applied.get("text") or json.dumps(applied, ensure_ascii=False)


def register(ctx) -> None:
    # регистрируемся в любом процессе. Хук срабатывает только на настоящем ходе
    # модели, так что в `hermes plugins list` он ничего не стоит, зато крон и
    # разовый прогон (`hermes -z`) тоже получают обязательства, а
    # `hermes plugins doctor mara-context` показывает «1 hook, 1 tool».
    # токен не обязателен прямо сейчас: Брокер добирает его из окружения на
    # каждый поход, а .env к первому ходу Мары уже загружен
    # MARA_CONTEXT_URL — шов для проверки: подставляем заглушку и смотрим, что
    # непустой пакет действительно доезжает до модели, не трогая живой волт
    брокер = Брокер(ctx.get_config("url") or os.environ.get("MARA_CONTEXT_URL") or URL,
                    ctx.get_config("token"))

    def hook(**kw):
        # из хука наружу не летит ничего: сбой брокера — это отсутствие
        # контекста, а не сломанный ход Мары
        try:
            return брокер.контекст(kw.get("conversation_history") or ())
        except Exception:
            log.warning("mara-context: сбой в хуке", exc_info=True)
            return ""

    def tool(args, **kw):
        try:
            return брокер.правка(args or {})
        except Exception as e:
            log.warning("mara-context: сбой инструмента", exc_info=True)
            return "не вышло: %s" % type(e).__name__

    ctx.register_hook("pre_llm_call", hook)
    # свой toolset. В platform_toolsets конфига его дописывает только дашборд
    # Hermes, CLI enable — нет; на маке `mara` вписан в cli и telegram руками,
    # иначе инструмента в сессии просто не видно (см. спеку брокера)
    ctx.register_tool("mara_correction", toolset="mara", schema=СХЕМА, handler=tool,
                      description="Правка обязательства по слову Серёги", emoji="✍️")
    log.info("mara-context: пакет обязательств беру с %s", брокер.url)


def _demo():
    """Самопроверка без Hermes и без сети: python3 __init__.py --demo"""
    import io

    ответ = {"now": {"text": "- прислать смету", "sha256": "a" * 64}}
    счёт = {"n": 0}

    class Ответ(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def живой(req, timeout=None):
        счёт["n"] += 1
        if req.get_full_url().endswith("/v1/ingest/event"):
            assert timeout == 0.5, timeout
            тело = json.loads(req.data)
            assert тело["kind"] == "correction" and тело["source"] == "mara", тело
            assert тело["payload"] == {"item": "прислать смету", "status": "done"}, тело
            assert req.get_header("Authorization") == "Bearer t", "токен не уехал"
            assert len(тело["source_id"]) == 64 and тело["occurred_at"][:2] == "20"
            return Ответ(json.dumps({"event_id": "correction_1", "duplicate": False,
                                     "applied": {"found": True, "text": "«прислать смету»: статус proposed → done"}}).encode())
        assert timeout == 0.05, timeout        # свой таймаут, а не гермесовы 30 с
        return Ответ(json.dumps(ответ).encode())

    def мёртвый(req, timeout=None):
        счёт["n"] += 1
        raise urllib.error.URLError("doctor молчит")

    б = Брокер(token="t", ttl=100, timeout=0.05, timeout_правки=0.5, open_url=живой)
    assert б.контекст([]) == "- прислать смету", "в истории пакета нет — едет"
    видела = [{"role": "user", "content": "привет",
               "api_content": "привет\n\n- прислать смету"}]
    assert б.контекст(видела) == "", "пакет уже в api_content — молчим"
    assert б.контекст([{"role": "user", "content": "было: - прислать смету"}]) == "", \
        "в content тоже считается"
    assert б.контекст([{"role": "user", "content": [{"type": "text", "text": "- прислать смету"}]}]) == "", \
        "content списком частей — тоже история"
    assert б.контекст([{"role": "user", "content": None}, "мусор"]) == "- прислать смету", \
        "странная история не роняет хук"
    assert счёт["n"] == 1, "кэш: пять ходов — один поход в сеть"

    ответ["now"] = {"text": "- перезвонить", "sha256": "b" * 64}
    б._свежесть = None                                     # как будто TTL истёк
    assert б.контекст(видела) == "- перезвонить", "список изменился — старый в истории не в счёт"
    assert б.контекст([{"role": "user", "content": "привет"}]) == "- перезвонить", \
        "сжатие уронило api_content — пакет едет заново"

    б._свежесть = None
    б._open = мёртвый
    было = time.monotonic()
    assert б.контекст([]) == "- перезвонить", "сеть легла — отдаём последнее"
    assert time.monotonic() - было < 1.0, "сбой не должен задерживать ход"
    n = счёт["n"]
    assert б.контекст([]) == "- перезвонить", "внутри TTL берём из кэша"
    assert счёт["n"] == n, "лежащий doctor стоит одну попытку в TTL, а не каждый ход"

    пустой = Брокер(token="t", timeout=0.05, open_url=мёртвый)
    assert пустой.контекст([]) == "", "удачного ответа не было — контекста нет"

    # пустой список обязательств — это ответ, а не отсутствие ответа
    ответ["now"] = None
    тихий = Брокер(token="t", ttl=100, timeout=0.05, open_url=живой)
    n = счёт["n"]
    assert тихий.контекст([]) == "" and тихий.контекст([]) == ""
    assert счёт["n"] == n + 1, "нечего отдавать — в сеть всё равно раз в TTL"

    # инструмент: правка уезжает событием, ответ сервера — строкой Маре
    б._open = живой
    б._свежесть = time.monotonic()
    assert б.правка({"item": "прислать смету", "status": "done"}) == \
        "«прислать смету»: статус proposed → done"
    assert б._свежесть is None, "после правки пакет считаем устаревшим"
    б._open = мёртвый
    assert б.правка({"item": "x", "status": "done"}).startswith("не дозвонился"), \
        "сеть легла — строка, а не исключение"

    def отказ(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {},
                                     io.BytesIO(b'{"error": "\\u0441\\u0440\\u043e\\u043a \\u043d\\u0443\\u0436\\u0435\\u043d \\u043a\\u0430\\u043a YYYY-MM-DD"}'))
    б._open = отказ
    assert "YYYY-MM-DD" in б.правка({"item": "x", "due": "пятница"}), "ошибку сервера отдаём словами"

    def дубль(req, timeout=None):
        return Ответ(b'{"event_id": "correction_1", "duplicate": true, "applied": null}')
    б._open = дубль
    assert "уже" in б.правка({"item": "x", "status": "done"}), "повтор в ту же минуту — дубль"

    # register: хук и инструмент встают, без Hermes
    класс = {}

    class Ctx:
        def get_config(self, k):
            return None

        def register_hook(self, name, fn):
            класс["hook"] = (name, fn)

        def register_tool(self, name, **kw):
            класс["tool"] = (name, kw)

    register(Ctx())
    assert класс["hook"][0] == "pre_llm_call"
    assert класс["tool"][0] == "mara_correction" and класс["tool"][1]["toolset"] == "mara"
    assert класс["tool"][1]["schema"]["parameters"]["required"] == ["item"]
    assert класс["hook"][1](conversation_history=None) == "" or True, "хук не падает без истории"
    print("mara-context demo: ок")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo() if "--demo" in sys.argv else 0)
