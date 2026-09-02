#!/usr/bin/env python3
"""Транскрипт → просьбы, обязательства, решения (ТЗ §9).

Модель локальная: ollama на bigpc по локалке. Ни один байт транскрипта не
уходит во внешний API — это условие ТЗ §9 и §18, а не предпочтение вкуса.

Модель предлагает, правила решают. Порог 0.85 при явной формулировке делает
задачу, 0.60–0.85 — строку «возможно задача» в дайджесте, ниже — ничего.
Дедлайн берётся только из произнесённой фразы: «побыстрее» датой не
становится никогда, а исходная фраза сохраняется рядом с разобранной датой.

Пункт без спана выбрасывается: утверждение, которое нельзя показать в записи,
для этой системы не существует.

    python3 scripts/call_extract.py --event call_<uuid>
    python3 scripts/call_extract.py --self-check
"""
import os, sys, re, json, argparse, urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import call_asr

OLLAMA = os.environ.get("MARA_LLM_URL", "http://192.168.1.10:11434")
MODEL = os.environ.get("MARA_EXTRACT_MODEL", "qwen3.5:9b")
TASK_MIN = float(os.environ.get("MARA_TASK_MIN", 0.85))
REVIEW_MIN = float(os.environ.get("MARA_REVIEW_MIN", 0.60))
HTTP_TIMEOUT = 900

LISTS = ("requests", "commitments", "decisions", "constraints",
         "open_questions", "changed_instructions", "followups")
NAMES = ("people_mentioned", "projects_mentioned")

# Дни недели во всех падежах, которые реально звучат в речи.
DAYS = {"понедельник": 0, "понедельника": 0,
        "вторник": 1, "вторника": 1,
        "среду": 2, "среда": 2, "среды": 2,
        "четверг": 3, "четверга": 3,
        "пятницу": 4, "пятница": 4, "пятницы": 4,
        "субботу": 5, "суббота": 5, "субботы": 5,
        "воскресенье": 6, "воскресенья": 6}

ITEM = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "requester": {"type": "string"},
        "owner": {"type": "string"},
        "promised_to": {"type": "string"},
        "explicit": {"type": "boolean"},
        "deadline_phrase": {"type": "string"},
        "success_criteria": {"type": "string"},
        "confidence": {"type": "number"},
        "supersedes": {"type": "string"},
        "new_state": {"type": "string"},
        "evidence": {"type": "array", "items": {
            "type": "object",
            "properties": {"start_ms": {"type": "integer"},
                           "end_ms": {"type": "integer"}},
            "required": ["start_ms", "end_ms"]}},
    },
    # explicit и deadline_phrase в обязательных не для красоты: необязательное
    # поле модель просто не заполняет, и «до пятницы» теряется вместе с
    # различием «попросили» и «подумали вслух». Пустая строка = срока не было.
    "required": ["action", "explicit", "deadline_phrase", "confidence", "evidence"],
}

SCHEMA = {"type": "object",
          "properties": dict([(k, {"type": "array", "items": ITEM}) for k in LISTS] +
                             [(k, {"type": "array", "items": {"type": "string"}})
                              for k in NAMES])}

PROMPT = """Ты разбираешь расшифровку телефонного разговора Сергея.

Куда что класть:
- requests — то, что собеседник попросил у Сергея;
- commitments — то, что Сергей пообещал сам («пришлю», «перезвоню»);
- decisions — то, о чём договорились;
- open_questions — что осталось нерешённым;
- changed_instructions — прежняя договорённость отменена или заменена;
- people_mentioned — имена людей, прозвучавшие в разговоре, включая того, кто
  представился;
- projects_mentioned — темы и проекты, о которых шла речь.

Верни JSON по схеме. Правила, нарушать нельзя:
- явная просьба и предположение — разные вещи; explicit: true только если
  собеседник прямо попросил или Сергей прямо пообещал;
- deadline_phrase — ровно те слова о сроке, которые прозвучали («до пятницы»,
  «побыстрее»); срока не было — пустая строка, даты не выдумывай;
- explicit: true, если прозвучала прямая просьба или прямое обещание;
  false для мыслей вслух вроде «может быть, потом покрасим»;
- у каждого пункта обязателен evidence со start_ms и end_ms того сегмента,
  где это сказано; без него пункт не нужен;
- если новое указание отменяет прежнее, положи его в changed_instructions с
  supersedes (что отменено) и new_state (как теперь);
- confidence — твоя честная уверенность от 0 до 1;
- пиши по-русски, коротко, инфинитивом: «прислать смету», не «Сергей должен».

Расшифровка (в квадратных скобках — идентификатор сегмента и его время):
"""


def transcript_text(segs):
    """Транскрипт для модели: со спанами, чтобы ей было чем заполнить evidence."""
    out = []
    for s in segs:
        a, b = s.get("start_ms", 0), s.get("end_ms", 0)
        out.append("[%s %02d:%02d–%02d:%02d] %s"
                   % (s.get("segment_id", "?"), a // 60000, (a % 60000) // 1000,
                      b // 60000, (b % 60000) // 1000, s.get("text", "")))
    return "\n".join(out)


def parse_deadline(phrase, occurred_at):
    """Дата только из произнесённого. Возвращает (iso или None, явный ли)."""
    if not phrase:
        return None, False
    p = str(phrase).lower().strip()
    try:
        base = datetime.fromisoformat(occurred_at)
    except (TypeError, ValueError):
        return None, False
    if "послезавтра" in p:
        return (base + timedelta(days=2)).date().isoformat(), True
    if "завтра" in p:
        return (base + timedelta(days=1)).date().isoformat(), True
    if "сегодня" in p:
        return base.date().isoformat(), True
    for name, idx in DAYS.items():
        if name in p:
            delta = (idx - base.weekday()) % 7 or 7
            return (base + timedelta(days=delta)).date().isoformat(), True
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b", p)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3) or base.year)
        year += 2000 if year < 100 else 0
        try:
            return base.replace(year=year, month=month, day=day).date().isoformat(), True
        except ValueError:
            return None, False
    return None, False          # «побыстрее», «на днях», «как получится»


def has_evidence(item):
    ev = item.get("evidence") or []
    return bool(ev) and all("start_ms" in e for e in ev)


def normalize(raw, occurred_at):
    """Ответ модели → то, с чем работает проекция. Правила ТЗ §9."""
    out = {}
    for key in LISTS:
        items = []
        for it in (raw.get(key) or []):
            it = dict(it)
            if not has_evidence(it):
                continue
            try:
                conf = float(it.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < REVIEW_MIN:
                continue
            it["confidence"] = conf
            it["disposition"] = ("task" if conf >= TASK_MIN and it.get("explicit")
                                 else "needs-review")
            # Срок разбираем у любого пункта, а не только у просьб: схема
            # требует deadline_phrase везде, и «побыстрее» в открытом вопросе
            # так же не должно становиться датой.
            phrase = it.get("deadline_phrase") or it.get("deadline")
            it["deadline_phrase"] = phrase
            it["due_at"], it["deadline_explicit"] = parse_deadline(phrase, occurred_at)
            items.append(it)
        out[key] = items
    for key in NAMES:
        out[key] = [str(x) for x in (raw.get(key) or [])]
    return out


def strip_fence(text):
    """Снять ```json ... ``` вокруг ответа. Схема такого не допускает, но
    страховка стоит четырёх строк, а разбор без неё падает целиком."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        t = t.rsplit("```", 1)[0]
    return t.strip() or "{}"


def ask_model(text, base_url=None, model=None):
    """Один запрос к локальной ollama со схемой ответа. Наружу не ходим.

    Именно /api/generate и именно с `think: false`. Проверено руками на
    qwen3.5:9b и ollama 0.23.2 второго сентября 2026:
      - /api/chat с `think: false` тихо перестаёт соблюдать схему и возвращает
        markdown-забор с выдуманными ключами;
      - /api/chat с включённым думаньем схему соблюдает, но на трёх сегментах
        размышляет дольше десяти минут, а звонки бывают двадцатиминутные;
      - /api/generate с `think: false` даёт схему и укладывается в секунды.
    """
    body = json.dumps({
        "model": model or MODEL,
        "prompt": PROMPT + text,
        "format": SCHEMA,
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_ctx": 8192},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request((base_url or OLLAMA) + "/api/generate", data=body,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        d = json.loads(r.read())
    return json.loads(strip_fence(d.get("response") or "{}"))


def run(event_id, root=None):
    root = root or mi.ROOT
    con = mi.connect(root)
    ev = mi.event_row(con, event_id)
    tpath = mi.transcript_path(root, event_id)
    if not os.path.exists(tpath):
        raise RuntimeError("нет транскрипта %s" % tpath)
    segs = call_asr.read_jsonl(tpath)
    occurred = ev["occurred"] or mi.now_iso()
    raw = ask_model(transcript_text(segs))
    data = normalize(raw, occurred)
    data["event_id"] = event_id
    data["occurred_at"] = occurred
    data["pipeline_version"] = mi.PIPELINE_VERSION
    out = mi.write_json(mi.extraction_path(root, event_id), data)
    con.execute("update events set state='extracted' where id=?", (event_id,))
    print("call_extract: %s — просьб %d, обещаний %d, изменений %d"
          % (event_id, len(data["requests"]), len(data["commitments"]),
             len(data["changed_instructions"])))
    return out


def self_check():
    occ = "2026-09-02T14:05:00+03:00"       # среда
    span = [{"start_ms": 0, "end_ms": 1000}]
    r = normalize({"requests": [
        {"action": "явная", "explicit": True, "confidence": 0.93, "evidence": span},
        {"action": "намёк", "explicit": False, "confidence": 0.93, "evidence": span},
        {"action": "слабая", "explicit": True, "confidence": 0.3, "evidence": span},
        {"action": "без спана", "explicit": True, "confidence": 0.99, "evidence": []},
    ]}, occ)
    assert [x["disposition"] for x in r["requests"]] == ["task", "needs-review"], \
        "пороги или отсев спанов сломаны"
    assert parse_deadline("до пятницы", occ) == ("2026-09-04", True)
    assert parse_deadline("побыстрее", occ) == (None, False), "дедлайн выдуман"
    assert parse_deadline(None, occ) == (None, False)
    assert "s0001" in transcript_text([{"segment_id": "s0001", "start_ms": 0,
                                        "end_ms": 1000, "text": "а"}])
    assert json.loads(strip_fence('```json\n{"a": 1}\n```')) == {"a": 1}
    print("call_extract self-check: ок")
    return 0


def main():
    ap = argparse.ArgumentParser(description="извлечение смысла звонка локальной моделью")
    ap.add_argument("--event")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    if not a.event:
        ap.error("нужен --event")
    mi.ROOT = a.root
    run(a.event, a.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
