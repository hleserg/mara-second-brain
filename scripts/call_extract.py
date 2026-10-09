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
import os, sys, re, json, hashlib, argparse, urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi
import call_asr
import vault_common

# Файл читаем на импорте, а не лениво: ниже из окружения берутся ручки
# настройки, и после ленивого перехода они молча перестали читаться из файла —
# `нужен_адрес` вызывается уже после того, как константы вычислены. Тестовый
# процесс от этого больше не страдает: `run-tests.sh` уводит MARA_ENV_FILE в
# несуществующий файл.
vault_common.load_env()

OLLAMA = os.environ.get("MARA_LLM_URL") or None
MODEL = os.environ.get("MARA_EXTRACT_MODEL", "qwen3.5:9b")
TASK_MIN = float(os.environ.get("MARA_TASK_MIN", 0.85))
REVIEW_MIN = float(os.environ.get("MARA_REVIEW_MIN", 0.60))
HTTP_TIMEOUT = 900
# Параметры запроса к модели. Температура ноль — ответ воспроизводим; окно
# контекста — сколько расшифровки модель видит целиком.
OPTIONS = {"temperature": 0, "num_ctx": 8192}
# Версия правил после модели (Т5.0, ТЗ §9.3 «versioned prompts/rules»):
# `normalize`, `сверить_evidence`, `parse_deadline`, пороги `disposition`.
# Поднимать при любой правке их поведения, как `PROMPT_VERSION` — при правке
# промпта; иначе два извлечения с одной моделью и одним промптом, но разными
# правилами выглядят одинаково, и корпус Т5.5 сравнивает несравнимое.
RULES_VERSION = 1


def конфигурация():
    """Ручки прогона извлечения, которые ложатся в `extractions/<event>.json`
    (Т5.0, ТЗ §9): модель, параметры запроса, пороги. Таймаут HTTP результат
    не меняет — его тут нет."""
    return {"model": MODEL, "options": dict(OPTIONS),
            "task_min": TASK_MIN, "review_min": REVIEW_MIN}

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
        # ADR-0004 п.2: модель называет сегмент, а не миллисекунды — аудит
        # показал, что секунды из промпта она пересчитывает, а не копирует.
        # Подынтервала в схеме нет нарочно: ollama с `format` склоняет модель
        # заполнять и необязательные ключи, и «00:00–00:25» вернулось бы
        # как 0–25 мс (ревью PR #121). Подынтервал придёт от пословных меток
        # или от человека (п.1 ADR); `сверить_evidence` его проверяет.
        "evidence": {"type": "array", "items": {
            "type": "object",
            "properties": {"segment": {"type": "string"}},
            "required": ["segment"]}},
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

# ADR-0004 п.4: версия промпта поднимается при любой правке его текста —
# иначе регрессионный корпус (Т5.5) сравнивает несравнимое. Ложится в
# извлечение и в карточку рядом с именем модели.
PROMPT_VERSION = 2
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
- у каждого пункта обязателен evidence — список сегментов, где это сказано,
  вида {"segment": "s0003"}: идентификатор ровно тот, что в квадратных
  скобках; времена не пересчитывай и не выдумывай; без evidence пункт не
  нужен;
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


МЕТКА = re.compile(r"s(\d{4})")


def сегменты_из(segs):
    """Сегменты файла расшифровки → `{seq: {start_ms, end_ms, id}}` — форма,
    в которой их ждёт `normalize`; `id` у файла нет (он в реестре)."""
    out = {}
    for s in segs:
        m = МЕТКА.fullmatch(str(s.get("segment_id") or ""))
        if m:
            out[int(m.group(1))] = {"start_ms": s["start_ms"], "end_ms": s["end_ms"],
                                    "id": s.get("id")}
    return out


def сверить_evidence(item, сегменты):
    """ADR-0004 п.3: ссылка сохраняется, только если сегмент существует, а
    подынтервал, если задан, лежит внутри его границ. Иначе — не сохраняется,
    никакого «подтянуть к ближайшему». Возвращает `(валидные, отклонённые)`;
    у валидной — `segment`, `segment_id` (из реестра, если есть), `start_ms`,
    `end_ms` в координатах записи (без подынтервала — границы сегмента)."""
    валидные, отклонённые = [], []
    for e in item.get("evidence") or []:
        if not isinstance(e, dict):
            отклонённые.append({"evidence": e, "why": "не объект"})
            continue
        m = МЕТКА.fullmatch(str(e.get("segment") or ""))
        seg = сегменты.get(int(m.group(1))) if m else None
        if seg is None:
            отклонённые.append({"evidence": e, "why": "нет такого сегмента"})
            continue
        a, b = e.get("start_ms"), e.get("end_ms")
        if a is None and b is None:
            a, b = seg["start_ms"], seg["end_ms"]
        else:
            # `type is int`, не `isinstance`: `True`/`False` — тоже int
            if not (type(a) is int and type(b) is int
                    and seg["start_ms"] <= a <= b <= seg["end_ms"]):
                отклонённые.append({"evidence": e, "why": "подынтервал за границами сегмента"})
                continue
        валидные.append({"segment": m.group(0), "segment_id": seg.get("id"),
                         "start_ms": a, "end_ms": b})
    return валидные, отклонённые


def has_evidence(item):
    """Есть ли у пункта evidence вообще — до сверки с сегментами."""
    return bool(item.get("evidence"))


def normalize(raw, occurred_at, сегменты=None):
    """Ответ модели → то, с чем работает проекция. Правила ТЗ §9.

    `сегменты` — `{seq: {start_ms, end_ms, id}}` расшифровки, по которой
    модель отвечала (`сегменты_из` или `call_asr.сегменты_события`). Пункт
    без единой валидной ссылки не создаётся; пункт, у которого часть ссылок
    отклонена, создаётся, но идёт в `needs-review` (ADR-0004 п.3, P12).
    Отклонённые ссылки собираются в `out["evidence_rejected"]` — вызывающий
    кладёт их в `audit_events`."""
    сегменты = сегменты or {}
    out = {"evidence_rejected": []}
    for key in LISTS:
        items = []
        for номер, it in enumerate(raw.get(key) or []):
            it = dict(it)
            if not has_evidence(it):
                continue
            валидные, отклонённые = сверить_evidence(it, сегменты)
            for о in отклонённые:
                # в аудит — только адрес и причина (ТЗ §6.2): ни `action`
                # (формулировка модели о сказанном), ни сырого объекта —
                # модель кладёт в него и лишние ключи вроде цитаты
                e = о["evidence"] if isinstance(о["evidence"], dict) else {}
                # метка — только если она метка: строка не по шаблону может
                # оказаться цитатой из разговора (Codex по #121)
                метка = МЕТКА.fullmatch(str(e.get("segment") or ""))
                # `item` — номер пункта в ответе модели, не среди принятых:
                # иначе после выброшенного пункта индексы съезжали бы и
                # несколько отказов указывали бы на один (Codex, круг 4)
                out["evidence_rejected"].append({
                    "list": key, "item": номер, "why": о["why"],
                    "segment": метка.group(0) if метка else None,
                    "start_ms": e.get("start_ms") if type(e.get("start_ms")) is int else None,
                    "end_ms": e.get("end_ms") if type(e.get("end_ms")) is int else None})
            if not валидные:
                continue
            it["evidence"] = валидные
            try:
                conf = float(it.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < REVIEW_MIN:
                continue
            it["confidence"] = conf
            it["disposition"] = ("task" if conf >= TASK_MIN and it.get("explicit")
                                 and not отклонённые else "needs-review")
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
        "options": OPTIONS,
    }, ensure_ascii=False).encode("utf-8")
    адрес = base_url or OLLAMA or vault_common.нужен_адрес(
        "MARA_LLM_URL", "коробка с ollama")
    req = urllib.request.Request(адрес + "/api/generate", data=body,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        d = json.loads(r.read())
    return json.loads(strip_fence(d.get("response") or "{}"))


def run(event_id, root=None):
    root = root or mi.ROOT
    con = mi.connect(root)
    ev = mi.event_row(con, event_id)
    occurred = ev["occurred"] or mi.now_iso()
    # Промпт и сверка — из одного источника. Есть расшифровка в реестре
    # (Т5.1) — модели показываются её строки, по ним же сверяется evidence;
    # файл `transcripts/<event>.jsonl` при этом не читается: ASR, умерший
    # между записью файла и фиксацией строк, оставлял бы файл одного прогона
    # и строки другого, и метка `s0001` цеплялась бы к чужому тексту (Codex
    # по #121). Файл — только для расшифровок, сделанных до Т5.1: строк нет,
    # сверка по нему, без segment_id.
    tid, в_реестре = call_asr.сегменты_события(con, event_id)
    # по `tid`, не по числу строк: расшифровка из одной тишины — тоже
    # расшифровка, и файл при ней не авторитет (Codex по #121, круг 2).
    # Файл без строки — только legacy: `call_asr.run` пишет файл после
    # фиксации строк, так что свежий прогон без строк файла не оставляет
    # (Codex, круг 3)
    if tid is not None:
        segs = [{"segment_id": "s%04d" % seq, "start_ms": r["start_ms"],
                 "end_ms": r["end_ms"], "speaker": r["speaker"], "text": r["text"]}
                for seq, r in в_реестре.items()]
        сегменты = {seq: {"start_ms": r["start_ms"], "end_ms": r["end_ms"], "id": r["id"]}
                    for seq, r in в_реестре.items()}
    else:
        tpath = mi.transcript_path(root, event_id)
        if not os.path.exists(tpath):
            raise RuntimeError("нет транскрипта %s" % tpath)
        segs = call_asr.read_jsonl(tpath)
        сегменты = сегменты_из(segs)
    текст = transcript_text(segs)
    raw = ask_model(текст)
    data = normalize(raw, occurred, сегменты)
    отклонено = data.pop("evidence_rejected")
    data["event_id"] = event_id
    data["occurred_at"] = occurred
    data["pipeline_version"] = mi.PIPELINE_VERSION
    data["transcript_id"] = tid
    data["extractor"] = MODEL          # ADR-0004 п.4: чем и по какой версии
    data["prompt_version"] = PROMPT_VERSION
    # Т5.0, ТЗ §9: правила — версией, конфигурация прогона и хеш входа —
    # того текста, который ушёл модели (у legacy-расшифровки без строк
    # `transcript_id` пустой, и хеш — единственный след входа)
    data["rules_version"] = RULES_VERSION
    data["config"] = конфигурация()
    data["input_sha256"] = hashlib.sha256(текст.encode("utf-8")).hexdigest()
    out = mi.write_json(mi.extraction_path(root, event_id), data)
    # отказ по evidence — строка аудита (ADR-0004 п.3): что прислала модель,
    # без текста расшифровки; одной транзакцией с переходом события
    with mi.транзакция(con):
        for о in отклонено:
            mi.audit(con, "evidence_rejected", ("model", MODEL), "event", event_id,
                     dict(о, transcript_id=tid, prompt_version=PROMPT_VERSION,
                          rules_version=RULES_VERSION))
        con.execute("update events set state='extracted' where id=?", (event_id,))
    if отклонено:
        print("call_extract: %s — отклонено ссылок evidence: %d" % (event_id, len(отклонено)),
              file=sys.stderr)
    print("call_extract: %s — просьб %d, обещаний %d, изменений %d"
          % (event_id, len(data["requests"]), len(data["commitments"]),
             len(data["changed_instructions"])))
    return out


def self_check():
    occ = "2026-09-02T14:05:00+03:00"       # среда
    span = [{"segment": "s0001"}]
    сег = {1: {"start_ms": 0, "end_ms": 1000, "id": None}}
    r = normalize({"requests": [
        {"action": "явная", "explicit": True, "confidence": 0.93, "evidence": span},
        {"action": "намёк", "explicit": False, "confidence": 0.93, "evidence": span},
        {"action": "слабая", "explicit": True, "confidence": 0.3, "evidence": span},
        {"action": "без спана", "explicit": True, "confidence": 0.99, "evidence": []},
        {"action": "чужой сегмент", "explicit": True, "confidence": 0.99,
         "evidence": [{"segment": "s0009"}]},
    ]}, occ, сег)
    assert [x["disposition"] for x in r["requests"]] == ["task", "needs-review"], \
        "пороги или отсев спанов сломаны"
    assert r["requests"][0]["evidence"] == [{"segment": "s0001", "segment_id": None,
                                             "start_ms": 0, "end_ms": 1000}]
    assert len(r["evidence_rejected"]) == 1, "ссылка в несуществующий сегмент не отклонена"
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
