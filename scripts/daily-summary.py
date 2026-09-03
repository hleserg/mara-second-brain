#!/usr/bin/env python3
"""Дневная сводка (ТЗ §7.4, §10 этап 3): что сделано за день, простым языком.

Гоняется script-only cron-ом Мары (`--no-agent --script`) в 9:00: стдаут
уезжает в телеграм как есть.

Раньше сводка была оглавлением карточек — «Сессии (4): PR #338 доведён до
pass, триаж issues…». Заголовок карточки написан для поиска в волте, а не
для чтения за завтраком, и человеку из такого списка непонятно ничего.
Поэтому карточки за день уходят одним куском в модель и возвращаются
человеческим текстом. Без ключа или если облако не ответило — печатаем
старую механику, молчать хуже.

Пустой вывод = молчание: Hermes ничего не отправит, если день был пустой.

    python3 scripts/daily-summary.py                  # за вчера
    python3 scripts/daily-summary.py --date 2026-08-31 --raw   # без облака
"""
import os, re, sys, json, argparse, urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vault_common                                          # noqa: E402

FM = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.S)
API = "https://openrouter.ai/api/v1/chat/completions"
MAX_CHARS = 60000

PROMPT = """Ты пишешь Серёге сводку за вчера: что сделано.

На входе — карточки из его базы знаний за один день. Это разборы его рабочих
сессий с ИИ-помощниками и разборы коммитов в его репозиториях.

Главное правило: живой русский язык. Читать это будет человек за завтраком с
телефона, а не программист в рабочем чате. Английских слов, жаргона и
сокращений быть не должно вовсе. Не «смерджен PR #338 после ревью», а
«доделал и влил правки по такой-то задаче». Не «триаж 32 issues», а «разобрал
три десятка накопившихся задач». Не «настроен CI», а «сборка проекта теперь
проверяется сама». Не «keyring», а «хранилище паролей». Не «эмбеддинги» и не
«индексация» — «поиск по заметкам». Если без английского названия никак
(имя программы, название репозитория), назови его и тут же объясни по-русски,
что это.

Ещё:
- Только результат: что в итоге получилось, а не о чём разговаривали и не
  как шли к этому.
- Автоматику не упоминай вовсе: обновления библиотек, правки от ботов,
  мелочь по сборке. Если за день в проекте только это — проект не называй.
- Ничего не выдумывай. Чего в карточках нет — того не пиши.
- Одно и то же дело — один пункт, а не по пункту на карточку.
- От пяти до девяти пунктов, каждый одной строкой, не длиннее двух строк.
- Без вступления и без вывода. Не начинай строку с двоеточия и названия
  проекта — пиши сразу по делу, глаголом.

Ответ — просто строки, каждая начинается с «- ».
Делать было нечего — ответь одним словом: пусто"""


def cards(d, day):
    """(заголовок, проект, тело) из карточек, у которых occurred == день.

    Карточки с sensitive: true пропускаем: их содержимое в облако не уходит
    ни при каких условиях (§8.3.3), а сводку пишет облако."""
    out = []
    if not os.path.isdir(d): return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"): continue
        m = FM.match(open(os.path.join(d, name), encoding="utf-8", errors="replace").read())
        if not m: continue
        fm, body = m.groups()
        if not re.search(r"(?m)^occurred:\s*'?%s" % re.escape(day), fm): continue
        # Карточки звонков и переписки помечены sensitive: true, и эта строка
        # — единственное, что не пускает их в OpenRouter вместе со сводкой дня
        # (ТЗ ambient memory §18). Захочется «показать звонки в сводке» —
        # делать это надо локальной моделью, а не ослаблением условия.
        if re.search(r"(?m)^sensitive:\s*['\"]?true", fm): continue
        h = re.search(r"(?m)^#\s+(.+)$", body)
        p = re.search(r"(?m)^project:\s*'?\"?\[?\[?([^'\"\]\n]+)", fm)
        out.append(((h.group(1).strip() if h else name[:-3]),
                    (p.group(1).strip() if p else ""),
                    nolinks(re.sub(r"(?m)^#.*$", "", body)).strip()))
    return out


def nolinks(text):
    """`[[sergey|Сергей]]` → `Сергей`. С автосвязей (§5.4) тело карточки
    размечено викилинками, а сводка уезжает в телеграм, где `[[…]]` — просто
    квадратные скобки в тексте. Модель охотно копирует их из источника."""
    return re.sub(r"\[\[([^\]|#\n]+?)(?:\|([^\]\n]*))?\]\]",
                  lambda m: (m.group(2) or m.group(1)).strip(), text)

# Оглавление дня, которое собрал daily-page.py. В сводку его пускать нельзя:
# те же карточки уже пересказаны выше, а ссылки в телеграме — мусор.
AUTO = re.compile(r"<!-- mara:auto -->.*?<!-- /mara:auto -->", re.S)

def diary(vault, day):
    """Дневник Мары — уже человеческий текст, в облако его не гоняем."""
    p = os.path.join(vault, "daily", day + ".md")
    if not os.path.exists(p): return ""
    m = FM.match(open(p, encoding="utf-8", errors="replace").read())
    text = m.group(2) if m else open(p, encoding="utf-8", errors="replace").read()
    return re.sub(r"(?m)^#.*$", "", AUTO.sub("", text)).strip()[:1500]


def plain(git, ses):
    """Механическая сводка: оглавление карточек. Запасной путь, когда облако
    недоступно — прислать список лучше, чем промолчать."""
    lines = []
    if git:
        lines.append("Код:")
        for t, p, _ in git: lines.append("- %s — %s" % (p, t))
    if ses:
        lines.append("Сессии (%d):" % len(ses))
        for t, _, _ in ses[:10]: lines.append("- " + t)
        if len(ses) > 10: lines.append("- …и ещё %d" % (len(ses) - 10))
    return "\n".join(lines)


def material(git, ses):
    """Карточки одним куском для модели."""
    parts = []
    for t, p, body in git:
        parts.append("## Коммиты, %s\n%s\n%s" % (p or "?", t, body))
    for t, _, body in ses:
        parts.append("## Рабочая сессия\n%s\n%s" % (t, body))
    return "\n\n".join(parts)[:MAX_CHARS]


def ask(text, key, model, redacted=None):
    body = json.dumps({"model": model, "temperature": 0.2,
                       "messages": [{"role": "system", "content": PROMPT},
                                    {"role": "user", "content": redacted or text}]}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/hleserg/mara-second-brain",
        "X-Title": "mara-second-brain"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def summary(vault, day, key=None, model=None, raw=False):
    git = [c for c in cards(os.path.join(vault, "kb/notes"), day) if c[1]]
    ses = cards(os.path.join(vault, "kb/sessions"), day)
    d = diary(vault, day)
    if not (git or ses or d): return ""          # пустой день — молчим

    text = ""
    if (git or ses) and key and not raw:
        try:
            import redact
            redact.require_chain()
            clean, _ = redact.redact(material(git, ses))
            got = ask(material(git, ses), key, model, clean)
            text = "" if got.strip().lower().startswith("пусто") else got
        except Exception as e:
            # Сводка обязана прийти. Не вышло по-человечески — шлём механику
            # и говорим почему, чтобы поломка не выглядела нормой.
            text = plain(git, ses) + "\n\n(сводку своими словами собрать не вышло: %s)" % e
    elif git or ses:
        text = plain(git, ses)

    if d: text += ("\n\n" if text else "") + "Из дневника:\n" + d
    return ("Сводка за %s\n\n" % day + text.strip()) if text.strip() else ""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--date", help="день в ISO; по умолчанию вчерашний")
    ap.add_argument("--raw", action="store_true", help="без облака, оглавление карточек")
    a = ap.parse_args(argv)
    vault_common.load_env()
    day = a.date or (datetime.now().date() - timedelta(days=1)).isoformat()
    text = summary(a.vault, day, os.environ.get("OPENROUTER_API_KEY"),
                   os.environ.get("MARA_DIGEST_MODEL", "deepseek/deepseek-v4-pro"), a.raw)
    if text: print(text)
    return 0


def self_check():
    import tempfile
    v = tempfile.mkdtemp()
    for sub in ("kb/notes", "kb/sessions", "daily"): os.makedirs(os.path.join(v, sub))
    open(os.path.join(v, "kb/notes/g.md"), "w", encoding="utf-8").write(
        "---\noccurred: 2026-08-31\nproject: '[[atman]]'\nsensitive: false\n---\n"
        "\n# Починил синк\nтело\nЛюди: [[sergey|Сергей]]\n")
    open(os.path.join(v, "kb/notes/old.md"), "w", encoding="utf-8").write(
        "---\noccurred: 2026-08-30\nproject: x\n---\n\n# Не тот день\n")
    open(os.path.join(v, "kb/notes/secret.md"), "w", encoding="utf-8").write(
        "---\noccurred: 2026-08-31\nproject: work\nsensitive: true\n---\n\n# Рабочее\n")
    open(os.path.join(v, "kb/sessions/s.md"), "w", encoding="utf-8").write(
        "---\noccurred: 2026-08-31\n---\n\n# Разбирал очередь\n")
    got = summary(v, "2026-08-31", raw=True)
    assert "atman — Починил синк" in got and "Разбирал очередь" in got, got
    assert "Не тот день" not in got
    # §8.3.3: секретная карточка не доезжает даже до материала для модели
    assert "Рабочее" not in got
    assert "Рабочее" not in material([c for c in cards(os.path.join(v, "kb/notes"), "2026-08-31")
                                      if c[1]], [])
    # пустой день — пустая строка, иначе Мара будет присылать «ничего не было»
    assert summary(v, "2026-08-29", raw=True) == ""
    # дневник приезжает без своего заголовка и в облако не уходит
    open(os.path.join(v, "daily/2026-08-31.md"), "w", encoding="utf-8").write(
        "---\ntype: daily\n---\n\n# 31 августа\nзаебался с усилителем\n")
    got = summary(v, "2026-08-31", raw=True)
    assert "заебался с усилителем" in got and "31 августа" not in got, got
    assert "усилителем" not in material(cards(os.path.join(v, "kb/notes"), "2026-08-31"), [])
    # день без карточек, но с дневником — сводка всё равно есть
    v2 = tempfile.mkdtemp(); os.makedirs(os.path.join(v2, "daily"))
    open(os.path.join(v2, "daily/2026-08-31.md"), "w", encoding="utf-8").write("# д\nтолько дневник\n")
    assert "только дневник" in summary(v2, "2026-08-31", raw=True)
    # викилинки из карточек в сводку не просачиваются
    assert nolinks("Люди: [[sergey|Сергей]], [[atman]]") == "Люди: Сергей, atman"
    assert "[[" not in cards(os.path.join(v, "kb/notes"), "2026-08-31")[0][2]
    print("daily-summary: самопроверка ок")
    return 0


if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
