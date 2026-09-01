#!/usr/bin/env python3
"""Обработка очереди `_system/queue/` (ТЗ §8.1, §8.4 слой 1).

Читает задачу → достаёт реплики из сырья → редактирует (§8.3) → шлёт в
OpenRouter → переписывает тело карточки и ставит distilled: true.

Держит очередь, а не падает, если:
  - нет ключа (ещё не положили);
  - Presidio не поставлен — неполная редакция это НЕ повод отправить как есть;
  - у карточки sensitive: true — такое в облако не уходит вообще (§8.3.3),
    а локальной генеративной модели на doctor нет.
Это слой 2 §8.4, штатное состояние, а не авария.

Запускать питоном из venv с presidio:
  ~/.local/share/mara/venv/bin/python scripts/queue-worker.py
"""
import json, os, re, sys, argparse, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import redact
from session_note_compat import messages   # см. ниже

API = "https://openrouter.ai/api/v1/chat/completions"
MAX_CHARS = 100_000     # ~25k токенов; вход дешёвый, но лишний хвост только шумит
MAX_ATTEMPTS = 3

def load_env(path):
    """~/.config/mara/env: KEY=value. Секреты в волте не держим (§11)."""
    try: lines = open(os.path.expanduser(path)).read().splitlines()
    except OSError: return
    for l in lines:
        l = l.strip()
        if l and not l.startswith("#") and "=" in l:
            k, v = l.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

def payload(raw_path):
    parts, total = [], 0
    for role, text in messages(raw_path):
        who = "Сергей" if role == "user" else "Ассистент"
        piece = "## %s\n%s\n" % (who, text.strip())
        if total + len(piece) > MAX_CHARS:
            parts.append("\n[…сессия обрезана по объёму…]\n"); break
        parts.append(piece); total += len(piece)
    return "".join(parts)

def call_llm(prompt, text, model, key):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": text}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/hleserg/mara-second-brain",
        "X-Title": "mara-second-brain"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read())
    return json.loads(d["choices"][0]["message"]["content"])

def rewrite_card(path, out):
    """Меняем тело под фронтматтером и один флаг. Фронтматтер целиком не
    перегенерируем: Basic Memory нормализует YAML по-своему и дописывает
    permalink — переписав, мы бы каждый раз воевали с ним."""
    src = open(path, encoding="utf-8").read()
    m = re.match(r"(---\n.*?\n---\n)(.*)", src, re.S)
    if not m: return False
    fm, _ = m.groups()
    fm = re.sub(r"(?m)^distilled:\s*false\s*$", "distilled: true", fm)
    body = ["# " + (out.get("title") or "Сессия"), "", (out.get("summary") or "").strip(), ""]
    for key, head in (("facts", "## Факты и решения"), ("open", "## Осталось")):
        items = [i for i in (out.get(key) or []) if str(i).strip()]
        if items: body += [head] + ["- " + str(i).strip() for i in items] + [""]
    for key, head in (("people", "Люди"), ("projects", "Проекты")):
        items = [str(i).strip() for i in (out.get(key) or []) if str(i).strip()]
        if items: body.append("%s: %s" % (head, ", ".join(items)))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh: fh.write(fm + "\n".join(body).rstrip() + "\n")
    os.replace(tmp, path)     # атомарно: рядом крутятся автокоммит и bisync
    return True

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true",
                    help="показать, что именно уехало бы в облако, и не отправлять")
    a = ap.parse_args()

    load_env("~/.config/mara/env")
    key = os.environ.get("OPENROUTER_API_KEY")
    model = os.environ.get("MARA_DISTILL_MODEL", "deepseek/deepseek-v4-flash")
    if not key and not a.dry_run:
        print("queue-worker: нет OPENROUTER_API_KEY, очередь держим"); return 0
    try:
        redact.require_chain()
    except Exception as e:
        print("queue-worker: редакция §8.3 неполная (%s: %s), в облако не шлём, "
              "очередь держим" % (type(e).__name__, e)); return 0

    prompt = open(os.path.join(a.vault, "_system/prompts/session-distill.md"), encoding="utf-8").read()
    qdir = os.path.join(a.vault, "_system/queue")
    jobs = sorted(f for f in os.listdir(qdir) if f.endswith(".json"))
    done = held = 0
    for name in jobs:
        if done >= a.limit: break
        jp = os.path.join(qdir, name)
        try: job = json.load(open(jp, encoding="utf-8"))
        except ValueError: print("queue-worker: битая задача", name); continue
        card = os.path.join(a.vault, job.get("note", ""))
        raw = os.path.join(a.vault, job.get("raw", ""))
        if not os.path.exists(card) or not os.path.exists(raw):
            print("queue-worker: нет карточки или сырья, снимаю", name); os.unlink(jp); continue
        head = open(card, encoding="utf-8").read()
        if re.search(r"(?m)^sensitive:\s*['\"]?true", head):
            held += 1; continue                        # §8.3.3 — облако не трогает
        if re.search(r"(?m)^distilled:\s*['\"]?true", head):
            # Задача пережила карточку: bisync мог воскресить её с устройства,
            # которое отлежалось офлайн. Второй раз платить незачем.
            os.unlink(jp); continue
        if job.get("attempts", 0) >= MAX_ATTEMPTS:
            held += 1; continue

        text, stats = redact.redact(payload(raw))
        if a.dry_run:
            print("=" * 60, "\n%s  (вычищено: %s)\n" % (name, stats or "ничего"), text[:4000])
            done += 1; continue
        try:
            out = call_llm(prompt, text, model, key)
        except Exception as e:
            job["attempts"] = job.get("attempts", 0) + 1
            job["last_error"] = "%s: %s" % (type(e).__name__, e)
            with open(jp, "w", encoding="utf-8") as fh: json.dump(job, fh, ensure_ascii=False, indent=2)
            print("queue-worker: %s — %s (попытка %d)" % (name, job["last_error"], job["attempts"]))
            continue
        if rewrite_card(card, out):
            os.unlink(jp); done += 1
            print("queue-worker: дистиллировано %s (вычищено: %s)" % (job.get("source_id"), stats or "ничего"))
    print("queue-worker: обработано %d, придержано %d, в очереди осталось %d"
          % (done, held, len(os.listdir(qdir)) - 1))
    return 0

if __name__ == "__main__":
    sys.exit(main())
