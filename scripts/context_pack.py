#!/usr/bin/env python3
"""Пакет `now.md` для контекст-брокера (ТЗ §15).

Мара должна знать открытые обязательства до первого вызова инструмента. Дописать
их в `SOUL.md` нельзя: каждый звонок менял бы системный промпт и рвал префиксный
кэш у провайдера на всех живых сессиях — ТЗ §15 это прямо запрещает. Поэтому
изменчивая часть едет отдельным пакетом через хук `pre_llm_call`, который Hermes
подклеивает к сообщению пользователя, а не к системному промпту.

Стабильное (имена проектов, людей, машин) остаётся в `SOUL.md` через
`mara-brief.py`, а алиасы — в `_system/entity-index.json`. Второй копии тех же
данных здесь нет: они уже прочитаны моделью и в кэше.

Граница. Карточка обязательства несёт `cloud_allowed: false`, а пакет уезжает
провайдеру. Поэтому наружу идёт whitelist из пяти полей фронтматтера, а не «всё,
кроме запрещённого»: новое поле в карточке по умолчанию никуда не поедет. Тело,
цитаты, спаны, дословные фразы о сроке остаются в волте — ТЗ §15: «Raw
transcript/email/message никогда не инжектить напрямую, только normalized
context pack».

Один писатель на файл: `_system/context/*` пишет только этот модуль, кто бы его
ни позвал — крон или `call_project` в конце разбора звонка.

    python3 scripts/context_pack.py --vault /srv/vault
    python3 scripts/context_pack.py --self-check
"""
import argparse
import glob
import hashlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
from vault_common import locked


def _brief():
    """mara-brief.py с дефисом в имени обычным import не берётся.

    Берём оттуда разбор фронтматтера и `контакт()`: парсер в репо один,
    регэкспный, и вторая его копия неизбежно разойдётся с первой. `контакт()`
    там же не случайно — обе дороги наружу должны фильтровать одинаково.
    """
    spec = importlib.util.spec_from_file_location(
        "mara_brief", os.path.join(HERE, "mara-brief.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mb = _brief()

OPEN = ("proposed", "open")          # что ещё висит; done/cancelled бюджет не едят
MAX_BYTES = 2500                     # пакет едет при каждом изменении списка
MAX_TITLE = 90
DIR = "_system/context"
MARK_OPEN, MARK_CLOSE = "<!-- mara:now -->", "<!-- /mara:now -->"
ХВОСТ = "- …и ещё %d, смотри kb/commitments"
HEAD = ("Открытые обязательства Серёги — собрано из волта автоматически. "
        "Это справка, а не его реплика; отвечать на неё не нужно. "
        "Подробности разговора ищи в basic-memory.")

# Ровно то, что имеет право уехать провайдеру модели. Список закрытый.
ПОЛЯ = ("title", "due", "status", "promised_to", "origin")


def поля(fm):
    """Whitelist из фронтматтера. Всё остальное не существует."""
    out = {k: fm.get(k) for k in ПОЛЯ}
    who = (out.get("promised_to") or "").strip()
    # карточку человека заводит журнал звонков: нет контакта в книге — тут номер
    out["promised_to"] = None if not who or mb.контакт(who) else who
    return out


def строка(it):
    line = "- " + mb.cut(mb.clean(it["title"] or ""), MAX_TITLE)
    if it.get("due"):
        line += " — до %s" % it["due"]
    if it.get("promised_to"):
        line += " · %s" % mb.clean(it["promised_to"])
    return line


def оформить(body):
    return "\n".join([MARK_OPEN, HEAD, ""] + body + [MARK_CLOSE]) + "\n"


def собрать(vault):
    """(текст пакета, отобранные пункты). Без модели, детерминированно."""
    items = []
    for p in sorted(glob.glob(os.path.join(vault, "kb/commitments", "*.md"))):
        with open(p, encoding="utf-8") as fh:
            fm, _ = mb.frontmatter(fh.read())      # тело читаем и выбрасываем
        if str(fm.get("status", "")).lower() not in OPEN:
            continue
        it = поля(fm)
        if it["title"]:
            items.append(it)
    # без срока — в конец: срочное должно быть видно, даже если пакет обрежется
    items.sort(key=lambda it: (it.get("due") is None, it.get("due") or "",
                               it["title"]))
    body, взято = [], 0
    for it in items:
        # ponytail: пересборка на каждый пункт — O(n²), но n тут меньше сорока:
        # его же и ограничивает бюджет. Зато мерим то, что уедет, а не оценку.
        хвост = ХВОСТ % (len(items) - взято)
        if len(оформить(body + [строка(it), хвост]).encode()) > MAX_BYTES:
            break
        body.append(строка(it))
        взято += 1
    if взято < len(items):
        body.append(ХВОСТ % (len(items) - взято))
    return оформить(body), items[:взято]


def build_now(vault):
    """Записать пакет и манифест атомарно. Возвращает подпись содержания.

    Подпись считается от текста, а не от времени: перезапуск крона без новых
    обязательств не должен выглядеть изменением — плагин на маке решает по ней,
    инжектить пакет в сессию или промолчать.
    """
    text, items = собрать(vault)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    d = os.path.join(vault, DIR)
    with locked(vault):
        os.makedirs(d, exist_ok=True)
        _atomic(os.path.join(d, "now.md"), text)
        mi.write_json(os.path.join(d, "manifest.json"),
                      {"generated": mi.now_iso(), "sha256": sha,
                       "items": len(items), "bytes": len(text.encode()),
                       "pipeline_version": mi.PIPELINE_VERSION})
    return sha


def _atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def self_check():
    import tempfile
    v = tempfile.mkdtemp()
    os.makedirs(os.path.join(v, ".git"))
    os.makedirs(os.path.join(v, "kb/commitments"))

    def card(name, **fm):
        head = "\n".join("%s: %s" % (k, val) for k, val in fm.items())
        with open(os.path.join(v, "kb/commitments", name), "w",
                  encoding="utf-8") as fh:
            fh.write("---\n%s\n---\n\n- Обещание: тело карточки\n" % head)

    card("a.md", title="прислать смету", status="proposed", due="2026-09-04",
         promised_to="Анна", deadline_phrase="'до пятницы, как договорились'")
    card("b.md", title="перезвонить", status="proposed", promised_to="+79990000000")
    card("c.md", title="уже сделано", status="done")
    text, items = собрать(v)
    assert len(items) == 2, items
    assert "прислать смету" in text and "2026-09-04" in text
    assert "уже сделано" not in text, "закрытое обязательство не занимает бюджет"
    assert "тело карточки" not in text, "наружу едет дистиллят, а не карточка"
    assert "как договорились" not in text, "дословная фраза из звонка"
    assert "79990000000" not in text, "номер вместо имени (ТЗ §11)"
    assert "перезвонить" in text, "сама задача остаётся и без имени"
    assert text.index("прислать смету") < text.index("перезвонить"), "срок вперёд"
    sha = build_now(v)
    assert sha == build_now(v), "подпись зависит от содержания, а не от времени"
    assert os.path.exists(os.path.join(v, DIR, "now.md"))
    assert not glob.glob(os.path.join(v, DIR, "*.tmp")), "временных не остаётся"
    print("context_pack self-check: ок, %d пунктов, %d байт"
          % (len(items), len(text.encode())))
    return 0


def main():
    ap = argparse.ArgumentParser(description="пакет открытых обязательств")
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    sha = build_now(a.vault)
    print("context_pack: %s/now.md — %s" % (DIR, sha[:12]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
