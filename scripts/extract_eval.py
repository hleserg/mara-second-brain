#!/usr/bin/env python3
"""Регрессионный корпус извлечения (Т5.5, ТЗ §17.4 п.3).

§17.4 требует, чтобы извлечение различало просьбу и обещание, owner и
requester «на regression corpus». Корпус — синтетические разговоры в
`tests/corpus/extraction/*.json`: аудио в git не кладём (§7.3, §18), живых
расшифровок тоже — только придуманные реплики вымышленных людей.

Случай — сегменты расшифровки в той же форме, что у `call_asr`, золотая
разметка и записанный ответ модели:

    {"id", "about", "occurred",
     "segments": [{"segment_id": "s0001", "start_ms", "end_ms", "text"}],
     "gold": {"requests": [{"ключ": ["смет"], "requester": "Анна",
                            "due_at": "2026-09-04"}],
              "commitments": [{"ключ": [...], "promised_to": "...", "due_at": null}],
              "не_задача": [["покрас"]]},
     "recorded": {"model", "prompt_version", "rules_version", "response",
                  "ожидаемо": {счёт этого ответа}}}

Пункт узнаётся по `ключ` — все кусочки должны быть в `action` (без
регистра); `due_at`, если задан в золоте (и `null` тоже), сверяется; кто —
`requester` у просьбы, `promised_to` у обещания, по началу строки без
регистра («Анна» узнаёт «Анна Петрова»). `не_задача` — то, что задачей
стать не должно: мысль вслух, обещание собеседника, смена договорённости;
это пояснение для читателя корпуса — задача, которую золото списков не
узнало, и так считается `лишнее`.

Счёт идёт по `requests` и `commitments` после `call_extract.normalize`, то
есть вместе с порогами, отсевом evidence и разбором срока:

- `верно` — задача (`disposition: task`) в своём списке, узнанная золотом;
- `перепутано` — задача, узнанная золотом другого списка (просьба ↔ обещание);
- `лишнее` — задача, которую золото не узнало (в том числе `не_задача`);
- `на_ревью` — золото узнано только пунктом `needs-review`;
- `пропущено` — золото не узнано ничем;
- `кто_не_тот`, `срок_не_тот` — среди `верно`.

Точность — `верно / (верно + перепутано + лишнее)`: цель Т5.4 — точность, а
не полнота.

Режимы:

    python3 scripts/extract_eval.py --replay      # записанные ответы, в гейте
    python3 scripts/extract_eval.py --live        # модель (MARA_LLM_URL), на doctor
    python3 scripts/extract_eval.py --live --record   # и записать ответы в корпус

`--replay` — регресс правил: записанный ответ проходит нынешний `normalize`, и
счёт сравнивается с `ожидаемо`. Разошлось — правила изменили исход; это
либо ошибка, либо повод поднять `RULES_VERSION` и перезаписать `ожидаемо`.
Ответы, записанные в репозитории сейчас, — синтетические (`model:
synthetic`), с нарочными ошибками модели: они проверяют счёт и правила, а
не модель. Качество модели — `--live` на doctor.
"""
import argparse
import glob
import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import call_extract as ce

КОРПУС = os.path.join(HERE, "..", "tests", "corpus", "extraction")
СПИСКИ = ("requests", "commitments")
КТО = {"requests": "requester", "commitments": "promised_to"}
СЧЁТ = ("верно", "перепутано", "лишнее", "на_ревью", "пропущено",
        "кто_не_тот", "срок_не_тот")


def случаи(каталог=КОРПУС):
    """Случаи корпуса по имени файла: `[(путь, случай)]`."""
    out = []
    for p in sorted(glob.glob(os.path.join(каталог, "*.json"))):
        with open(p, encoding="utf-8") as fh:
            out.append((p, json.load(fh)))
    return out


def узнаёт(золото, пункт):
    """Все кусочки `ключ` есть в `action` пункта, без регистра."""
    текст = str(пункт.get("action") or пункт.get("new_state") or "").lower()
    return all(к.lower() in текст for к in золото["ключ"])


def тот_же(золото_кто, кто):
    """«Анна» узнаёт «Анна Петрова»; пустое в золоте — не сверяется."""
    if not золото_кто:
        return True
    return str(кто or "").strip().lower().startswith(золото_кто.strip().lower())


def оценить(случай, raw):
    """Ответ модели → счёт случая (словарь `СЧЁТ`)."""
    out = ce.normalize(raw, случай["occurred"], ce.сегменты_из(случай["segments"]))
    золото = случай.get("gold") or {}
    счёт = dict.fromkeys(СЧЁТ, 0)
    узнанные = set()                      # (список, номер золота)
    for список in СПИСКИ:
        чужой = СПИСКИ[1 - СПИСКИ.index(список)]
        for пункт in out.get(список) or []:
            if пункт.get("disposition") != "task":
                continue
            своё = [(i, з) for i, з in enumerate(золото.get(список) or [])
                    if узнаёт(з, пункт)]
            if своё:
                i, з = своё[0]
                узнанные.add((список, i))
                счёт["верно"] += 1
                if not тот_же(з.get(КТО[список]), пункт.get(КТО[список])):
                    счёт["кто_не_тот"] += 1
                if "due_at" in з and пункт.get("due_at") != з["due_at"]:
                    счёт["срок_не_тот"] += 1
            elif any(узнаёт(з, пункт) for з in золото.get(чужой) or []):
                счёт["перепутано"] += 1
            else:
                счёт["лишнее"] += 1
    for список in СПИСКИ:
        for i, з in enumerate(золото.get(список) or []):
            if (список, i) in узнанные:
                continue
            if any(узнаёт(з, п) for п in out.get(список) or []):
                счёт["на_ревью"] += 1
            else:
                счёт["пропущено"] += 1
    return счёт


def итог(счета):
    """Сумма счетов и точность (None, если задач не было вовсе)."""
    сумма = dict.fromkeys(СЧЁТ, 0)
    for с in счета:
        for k in СЧЁТ:
            сумма[k] += с.get(k, 0)
    задач = сумма["верно"] + сумма["перепутано"] + сумма["лишнее"]
    сумма["точность"] = round(сумма["верно"] / задач, 3) if задач else None
    return сумма


def replay(каталог=КОРПУС):
    """Записанные ответы через нынешние правила. Возвращает
    `(итог, расхождения)`; расхождение — `(id, ожидаемо, вышло)`."""
    счета, расхождения = [], []
    for _, с in случаи(каталог):
        rec = с["recorded"]
        вышло = оценить(с, rec["response"])
        счета.append(вышло)
        if вышло != rec["ожидаемо"]:
            расхождения.append((с["id"], rec["ожидаемо"], вышло))
    return итог(счета), расхождения


def live(каталог=КОРПУС, записать=False, спросить=None):
    """Каждый случай — модели, счёт по золоту. `записать` — ответ и его счёт
    ложатся в `recorded` случая (на doctor; синтетика заменяется живым
    ответом). `спросить` — подмена `ce.ask_model` для тестов."""
    спросить = спросить or ce.ask_model
    счета = []
    for путь, с in случаи(каталог):
        raw = спросить(ce.transcript_text(с["segments"]))
        счёт = оценить(с, raw)
        счета.append(счёт)
        print("%-28s %s" % (с["id"], " ".join("%s=%d" % (k, счёт[k]) for k in СЧЁТ if счёт[k])))
        if записать:
            с["recorded"] = {"model": ce.MODEL, "prompt_version": ce.PROMPT_VERSION,
                             "rules_version": ce.RULES_VERSION,
                             "date": date.today().isoformat(),
                             "response": raw, "ожидаемо": счёт}
            with open(путь, "w", encoding="utf-8") as fh:
                json.dump(с, fh, ensure_ascii=False, indent=1)
                fh.write("\n")
    return итог(счета)


def self_check():
    случай = {"id": "проба", "occurred": "2026-09-02T14:05:00+03:00",
              "segments": [{"segment_id": "s0001", "start_ms": 0, "end_ms": 1000,
                            "text": "пришли смету до пятницы"}],
              "gold": {"requests": [{"ключ": ["смет"], "requester": "Анна",
                                     "due_at": "2026-09-04"}],
                       "не_задача": [["покрас"]]}}
    ev = [{"segment": "s0001"}]
    raw = {"requests": [{"action": "прислать смету", "requester": "Анна Петрова",
                         "explicit": True, "deadline_phrase": "до пятницы",
                         "confidence": 0.95, "evidence": ev}],
           "commitments": [{"action": "покрасить стены", "explicit": True,
                            "deadline_phrase": "", "confidence": 0.9, "evidence": ev}]}
    с = оценить(случай, raw)
    assert (с["верно"], с["лишнее"], с["кто_не_тот"], с["срок_не_тот"]) == (1, 1, 0, 0), с
    raw["requests"][0]["confidence"] = 0.7
    с = оценить(случай, raw)
    assert (с["верно"], с["на_ревью"]) == (0, 1), с
    assert итог([{"верно": 3, "лишнее": 1}])["точность"] == 0.75
    сумма, расхождения = replay()
    assert not расхождения, расхождения
    print("extract_eval self-check: ок, случаев %d, точность записанных %s"
          % (len(случаи()), сумма["точность"]))
    return 0


def main():
    ap = argparse.ArgumentParser(description="регрессионный корпус извлечения (Т5.5)")
    ap.add_argument("--corpus", default=КОРПУС)
    ap.add_argument("--replay", action="store_true",
                    help="записанные ответы через нынешние правила")
    ap.add_argument("--live", action="store_true", help="спросить модель (MARA_LLM_URL)")
    ap.add_argument("--record", action="store_true",
                    help="с --live: записать ответы и счёт в корпус")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if a.record and not a.live:
        ap.error("--record только вместе с --live")
    if a.live:
        сумма = live(a.corpus, a.record)
        print(json.dumps(сумма, ensure_ascii=False, sort_keys=True))
        return 0
    сумма, расхождения = replay(a.corpus)
    for id_, ждали, вышло in расхождения:
        print("%s: ждали %s, вышло %s" % (id_, json.dumps(ждали, ensure_ascii=False),
                                          json.dumps(вышло, ensure_ascii=False)),
              file=sys.stderr)
    print(json.dumps(сумма, ensure_ascii=False, sort_keys=True))
    return 1 if расхождения else 0


if __name__ == "__main__":
    raise SystemExit(main())
