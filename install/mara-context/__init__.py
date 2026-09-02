"""Контекст-брокер на стороне Мары: обязательства до первого tool call (ТЗ §15).

Мара не ищет то, о чём не подозревает. Открытые обязательства должны лежать у
неё перед глазами, а не за вызовом basic-memory. Дописать их в `SOUL.md` нельзя:
каждый звонок менял бы системный промпт и рвал префиксный кэш у провайдера на
всех живых сессиях. Поэтому пакет едет хуком `pre_llm_call` — Hermes подклеивает
его возврат к сообщению пользователя, а системный промпт остаётся байт-в-байт
тем же (agent/turn_context.py).

Собирает пакет `context_pack.py` на doctor, отдаёт contextd по
`/v1/context/bootstrap`, сюда он приезжает через ssh-туннель (com.mara.relay).

Три вещи, без которых плагин вреден:

  - свой таймаут 2 с. Хук в Hermes ограничен тридцатью и fail-open: лежащий
    doctor добавлял бы полминуты к каждой реплике Мары, а это хуже, чем
    отсутствие контекста;
  - кэш последнего удачного ответа. Не только ради сети: пока doctor молчит,
    вчерашний список полезнее пустоты;
  - инжект по изменению, а не каждый ход. Плагин помнит, какую подпись он в эту
    сессию уже отдал. `is_first_turn` тут не годится: телеграм-сессия живёт
    сутками, и первый ход в ней бывает раз в неделю.

Наружу из хука не выходит ни одно исключение: сбой брокера обязан выглядеть как
отсутствие контекста, а не как сломанный ход.

Токен устройства — из настроек или окружения, в репозиторий не попадает.

    python3 install/mara-context/__init__.py --demo
"""
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request

TIMEOUT = 2.0        # быстрее сдаться, чем задержать реплику
TTL = 60.0           # свежесть пакета; звонок пересобирает его сразу, но
                     # ходов между звонками много, и каждый в сеть не пойдёт
СЕССИЙ = 200         # потолок памяти о сессиях, чтобы словарь не рос вечно
URL = "http://100.64.0.1:8788"     # адрес туннеля, см. install/com.mara.relay.plist

log = logging.getLogger(__name__)


class Брокер:
    """Что отдать в ход. Ничего не знает про Hermes — поэтому проверяем без него."""

    def __init__(self, url=URL, token=None, ttl=TTL, timeout=TIMEOUT, open_url=None):
        self.url = url.rstrip("/") + "/v1/context/bootstrap"
        self.token = token
        self.ttl, self.timeout = ttl, timeout
        self._open = open_url or urllib.request.urlopen
        self._lock = threading.Lock()
        self._свежесть = 0.0        # когда пакет последний раз подтверждался
        self._пакет = None          # последний удачный, живёт и после сбоя
        self._отдано = {}           # сессия → подпись, которую она уже видела

    def _достать(self):
        with self._lock:
            if self._пакет and time.monotonic() - self._свежесть < self.ttl:
                return self._пакет
            req = urllib.request.Request(self.url)
            if self.token:
                req.add_header("Authorization", "Bearer " + self.token)
            try:
                with self._open(req, timeout=self.timeout) as r:
                    data = json.loads(r.read() or b"{}")
            except Exception as e:
                # старый пакет не выбрасываем: пока doctor молчит, вчерашний
                # список обязательств полезнее пустоты. Свежесть не двигаем —
                # значит следующий ход попробует снова.
                log.warning("mara-context: не забрал пакет (%s)", e)
                return self._пакет
            self._свежесть = time.monotonic()
            self._пакет = data.get("now")
            return self._пакет

    def контекст(self, session_id):
        пакет = self._достать()
        if not пакет or not пакет.get("text"):
            return ""
        sha = пакет.get("sha256")
        with self._lock:
            if self._отдано.get(session_id) == sha:
                return ""           # эта сессия уже видела ровно этот список
            if len(self._отдано) >= СЕССИЙ:
                self._отдано.clear()
            self._отдано[session_id] = sha
        return пакет["text"]


def register(ctx) -> None:
    if "gateway" not in sys.argv:          # в разовых командах CLI контекст ни к чему
        return
    token = ctx.get_config("token") or os.environ.get("MARA_CONTEXT_TOKEN")
    if not token:
        log.warning("mara-context: нет plugins.entries.mara-context.settings.token "
                    "— contextd ответит 401, контекст не поедет")
        return
    брокер = Брокер(ctx.get_config("url", URL), token)

    def hook(**kw):
        # из хука наружу не летит ничего: сбой брокера — это отсутствие
        # контекста, а не сломанный ход Мары
        try:
            return брокер.контекст(kw.get("session_id") or "?")
        except Exception:
            log.warning("mara-context: сбой в хуке", exc_info=True)
            return ""

    ctx.register_hook("pre_llm_call", hook)
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
        assert timeout == 0.05, timeout        # свой таймаут, а не гермесовы 30 с
        return Ответ(json.dumps(ответ).encode())

    def мёртвый(req, timeout=None):
        счёт["n"] += 1
        raise urllib.error.URLError("doctor молчит")

    б = Брокер(token="t", ttl=100, timeout=0.05, open_url=живой)
    assert б.контекст("s1") == "- прислать смету", "новая подпись едет в ход"
    assert б.контекст("s1") == "", "та же подпись — молчим, бюджет не жжём"
    assert б.контекст("s2") == "- прислать смету", "другая сессия его не видела"
    assert счёт["n"] == 1, "кэш: три хода — один поход в сеть"

    ответ["now"] = {"text": "- перезвонить", "sha256": "b" * 64}
    б._свежесть = 0                                        # как будто TTL истёк
    assert б.контекст("s1") == "- перезвонить", "список изменился — отдаём снова"

    б._свежесть = 0
    б._open = мёртвый
    было = время = time.monotonic()
    assert б.контекст("s3") == "- перезвонить", "сеть легла — отдаём последнее"
    assert time.monotonic() - было < 1.0, "сбой не должен задерживать ход"
    б._свежесть = 0
    assert б.контекст("s3") == "", "s3 это уже видела"

    пустой = Брокер(token="t", timeout=0.05, open_url=мёртвый)
    assert пустой.контекст("s1") == "", "удачного ответа не было — контекста нет"

    сессий = Брокер(token="t", ttl=100, timeout=0.05, open_url=живой)
    for i in range(СЕССИЙ + 5):
        сессий.контекст("s%d" % i)
    assert len(сессий._отдано) <= СЕССИЙ, "память о сессиях не растёт вечно"
    print("mara-context demo: ок")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo() if "--demo" in sys.argv else 0)
