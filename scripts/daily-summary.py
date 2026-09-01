#!/usr/bin/env python3
"""Дневная сводка (ТЗ §7.4, §10 этап 3): что было за день, одним текстом.

Гоняется script-only cron-ом Мары (`--no-agent --script`): LLM не участвует,
стенограмма собирается из готовых карточек волта. Так и задумано в ТЗ —
cron-сессии идут с урезанным контекстом и в память не пишут, а токены жечь
на пересказ уже пересказанного незачем.

Пустой вывод = молчание: Hermes ничего не отправит, если день был пустой.

    python3 scripts/daily-summary.py                  # за вчера
    python3 scripts/daily-summary.py --date 2026-08-31
"""
import os, re, sys, argparse
from datetime import datetime, timedelta

FM = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.S)

def cards(d, day):
    """(заголовок, проект) из карточек, у которых occurred == день.

    Заголовок берём из тела, а не из фронтматтера: во фронтматтере лежит
    механическое «5 коммитов за …», а человеку нужен смысл, который написал
    дистиллятор. Недистиллированные так и отдают механику — это честно,
    видно, что выжимки ещё нет."""
    out = []
    if not os.path.isdir(d): return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"): continue
        m = FM.match(open(os.path.join(d, name), encoding="utf-8", errors="replace").read())
        if not m: continue
        fm, body = m.groups()
        if not re.search(r"(?m)^occurred:\s*'?%s" % re.escape(day), fm): continue
        h = re.search(r"(?m)^#\s+(.+)$", body)
        p = re.search(r"(?m)^project:\s*'?\"?\[?\[?([^'\"\]\n]+)", fm)
        out.append(((h.group(1).strip() if h else name[:-3]),
                    (p.group(1).strip() if p else "")))
    return out

def summary(vault, day):
    lines = []
    git = cards(os.path.join(vault, "kb/notes"), day)
    git = [(t, p) for t, p in git if p]          # git-карточки всегда с проектом
    ses = cards(os.path.join(vault, "kb/sessions"), day)
    diary = os.path.join(vault, "daily", day + ".md")

    if git:
        lines.append("Код:")
        for t, p in git: lines.append("- %s — %s" % (p, t))
    if ses:
        lines.append("Сессии (%d):" % len(ses))
        for t, _ in ses[:10]: lines.append("- " + t)
        if len(ses) > 10: lines.append("- …и ещё %d" % (len(ses) - 10))
    if os.path.exists(diary):
        m = FM.match(open(diary, encoding="utf-8", errors="replace").read())
        text = (m.group(2) if m else open(diary, encoding="utf-8", errors="replace").read())
        text = re.sub(r"(?m)^#.*$", "", text).strip()
        if text: lines += ["Дневник:", text[:1500]]
    if not lines: return ""                      # пустой день — молчим
    return "Сводка за %s\n\n" % day + "\n".join(lines)

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--date", help="день в ISO; по умолчанию вчерашний")
    a = ap.parse_args(argv)
    day = a.date or (datetime.now().date() - timedelta(days=1)).isoformat()
    text = summary(a.vault, day)
    if text: print(text)
    return 0

def self_check():
    import tempfile
    v = tempfile.mkdtemp()
    for sub in ("kb/notes", "kb/sessions", "daily"): os.makedirs(os.path.join(v, sub))
    open(os.path.join(v, "kb/notes/g.md"), "w", encoding="utf-8").write(
        "---\noccurred: 2026-08-31\nproject: '[[atman]]'\n---\n\n# Починил синк\nтело\n")
    open(os.path.join(v, "kb/notes/old.md"), "w", encoding="utf-8").write(
        "---\noccurred: 2026-08-30\nproject: x\n---\n\n# Не тот день\n")
    open(os.path.join(v, "kb/sessions/s.md"), "w", encoding="utf-8").write(
        "---\noccurred: 2026-08-31\n---\n\n# Разбирал очередь\n")
    got = summary(v, "2026-08-31")
    assert "atman — Починил синк" in got, got
    assert "Разбирал очередь" in got and "Не тот день" not in got
    # пустой день — пустая строка, иначе Мара будет присылать «ничего не было»
    assert summary(v, "2026-08-29") == ""
    # дневник приезжает без своего заголовка
    open(os.path.join(v, "daily/2026-08-31.md"), "w", encoding="utf-8").write(
        "---\ntype: daily\n---\n\n# 31 августа\nзаебался с усилителем\n")
    got = summary(v, "2026-08-31")
    assert "заебался с усилителем" in got and "31 августа" not in got, got
    print("daily-summary: самопроверка ок")
    return 0

if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
