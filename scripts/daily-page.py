#!/usr/bin/env python3
"""Дневные страницы (ТЗ §9, §6.4, §6.5): один файл на день со ссылками на карточки.

Карточек за год накопилось больше тысячи, и лежат они россыпью по kb/. Чтобы
день читался, `daily/YYYY-MM-DD.md` собирает его оглавление: что коммитил, о
чём были разговоры, какие заметки завелись — ссылками, а не пересказом. Пересказ
своими словами делает daily-summary.py, и он уезжает в телеграм; здесь только
навигация по волту.

В этот же файл Мара дописывает дневник (§7.2), поэтому машинный кусок живёт
между маркерами и в самом верху: всё, что снаружи, переписывается как есть, а
её `append` всегда падает ниже.

Крон гоняет `--all`, а не «за вчера»: карточки приезжают задним числом —
сессия за 27 июля разобралась 1 сентября, — а дистилляция очереди в 3:40 может
не успеть до 3:55, и день застыл бы с механическим заголовком. Пересборка всех
363 дней занимает секунду и ничего не переписывает, если ничего не менялось.

    python3 scripts/daily-page.py --vault /srv/vault              # за вчера
    python3 scripts/daily-page.py --all                           # все дни
    python3 scripts/daily-page.py --since 2024-01-01 --date 2026-09-01
"""
import os, re, sys, argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vault_common import canon_map, linkify, locked, unlink

FM = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.S)
AUTO = re.compile(r"[ \t]*<!-- mara:auto -->.*?<!-- /mara:auto -->[ \t]*\n?", re.S)
DAY = re.compile(r"\d{4}-\d\d-\d\d$")
MONTHS = ("января февраля марта апреля мая июня июля августа сентября октября "
          "ноября декабря").split()
SECTIONS = ("Код", "Разговоры", "Заметки")
# Заголовок карточки уезжает внутрь `[[файл|…]]`, где `]` и `|` — разметка.
BAD = str.maketrans({"]": ")", "[": "(", "|": "/"})


def read(path):
    return open(path, encoding="utf-8", errors="replace").read()


def field(fm, key):
    m = re.search(r"(?m)^%s:[ \t]*(.+)$" % key, fm)
    return m.group(1).strip().strip("'\"") if m else ""


def human(day):
    d = datetime.fromisoformat(day).date()
    return "%d %s %d" % (d.day, MONTHS[d.month - 1], d.year)


def scan(vault):
    """{день: [(секция, файл, заголовок, проект)]} по всем карточкам kb/.

    Дата — `occurred`, а если её нет (ручные заметки Мары её не ставят) — день
    из `created`. Иначе такая заметка не попадёт вообще ни в один день."""
    days = {}
    for sub in ("notes", "sessions", "howto", "decisions"):
        d = os.path.join(vault, "kb", sub)
        if not os.path.isdir(d): continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".md"): continue
            m = FM.match(read(os.path.join(d, name)))
            if not m: continue
            fm, body = m.groups()
            day = field(fm, "occurred") or field(fm, "created")[:10]
            if not DAY.match(day): continue
            h = re.search(r"(?m)^#[ \t]+(.+)$", body)
            # У недистиллированной карточки заголовок в теле механический
            # («repo — дата»), а в шапке хотя бы со счётчиком коммитов.
            title = field(fm, "title") if field(fm, "distilled") == "false" or not h \
                else h.group(1)
            src = field(fm, "source")
            sec = "Код" if src == "git" else "Разговоры" if sub == "sessions" else "Заметки"
            days.setdefault(day, []).append(
                (SECTIONS.index(sec), name[:-3], title.strip(), unlink(field(fm, "project"))))
    return days


def block(items, canon):
    out = ["<!-- mara:auto -->",
           "*Собрано само: правки внутри блока пропадут, пиши ниже.*", ""]
    for i, sec in enumerate(SECTIONS):
        rows = sorted(r for r in items if r[0] == i)
        if not rows: continue
        out.append("## " + sec)
        for _, fn, title, proj in rows:
            # Проект линкуем только через реестр: в карточках он сплошь и рядом
            # текст, которому сущности нет («workspace» у Claude Code — это
            # каталог, а не проект), и голая `[[…]]` плодила бы фантомы (§5.6).
            p = (linkify([proj], canon) or [""])[0]
            out.append("- %s[[%s|%s]]" % (p + " — " if p else "", fn,
                                          title.translate(BAD)))
        out.append("")
    out.append("<!-- /mara:auto -->")
    return "\n".join(out)


def head(day):
    return "\n".join(["---",
                      'title: "%s"' % human(day),
                      "type: daily",
                      "source: manual",
                      "source_id: daily@" + day,
                      "created: " + datetime.now().astimezone().isoformat(timespec="seconds"),
                      "occurred: " + day,
                      "tags: [daily]",
                      "sensitive: false",
                      "---", ""])


def put(body, blk):
    """Блок — сразу после заголовка дня, остальное не трогаем.

    Заголовок только первого уровня: секции дневника Мары — это `## 22:14`
    (§7.2), и вставить блок между её заголовком и её же текстом значило бы
    разорвать запись пополам."""
    if AUTO.search(body): return AUTO.sub(lambda _: blk + "\n", body, count=1)
    m = re.match(r"\s*#[ \t][^\n]*\n+", body)
    i = m.end() if m else 0
    return body[:i] + blk + "\n\n" + body[i:]


def write(vault, day, items, canon):
    """True, если файл изменился. Шапку заводим один раз: `sensitive` в ней
    могла стать true из-за дневника (§7.2), и затирать это нельзя."""
    path = os.path.join(vault, "daily", day + ".md")
    old = read(path) if os.path.exists(path) else ""
    m = FM.match(old)
    fm, body = (old[:m.start(2)], m.group(2)) if m else \
               (head(day), old or "# %s\n\n" % human(day))
    new = (fm + put(body, block(items, canon))).rstrip("\n") + "\n"
    # Basic Memory переписывает файл следом за нами и срезает последний перевод
    # строки. Сравнивать байт в байт значит переписывать все 363 страницы каждый
    # прогон и драться с ним за один байт (§13.8).
    if new.rstrip("\n") == old.rstrip("\n"): return False
    d = os.path.dirname(path)
    if not os.path.isdir(d): os.makedirs(d)
    tmp = path + ".tmp"                       # рядом крутятся автокоммит и bisync
    with open(tmp, "w", encoding="utf-8") as fh: fh.write(new)
    os.replace(tmp, path)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--date", help="день в ISO; по умолчанию вчерашний")
    ap.add_argument("--since", help="бэкфилл: с этого дня по --date включительно")
    ap.add_argument("--all", action="store_true", help="все дни, где есть карточки")
    a = ap.parse_args(argv)

    end = a.date or (datetime.now().date() - timedelta(days=1)).isoformat()
    days = scan(a.vault)
    want = sorted(days) if a.all else \
        [d for d in sorted(days) if (a.since or end) <= d <= end]
    canon = canon_map(a.vault)
    n = 0
    with locked(a.vault):
        for day in want:
            n += write(a.vault, day, days[day], canon)
    print("daily-page: дней %d, переписано %d" % (len(want), n))
    return 0


def self_check():
    import tempfile
    v = tempfile.mkdtemp()
    for s in ("kb/notes", "kb/sessions", "daily", ".git"): os.makedirs(os.path.join(v, s))
    open(os.path.join(v, "kb/notes/git-atman-2026-08-31.md"), "w", encoding="utf-8").write(
        "---\ntitle: 'atman: 8 коммитов за 2026-08-31'\nsource: git\noccurred: 2026-08-31\n"
        'project: "[[atman]]"\ndistilled: true\n---\n\n# Починил синк\nтело\n')
    open(os.path.join(v, "kb/notes/git-mara-2026-08-31.md"), "w", encoding="utf-8").write(
        "---\ntitle: 'mara: 1 коммит за 2026-08-31'\nsource: git\noccurred: 2026-08-31\n"
        'project: "[[mara]]"\ndistilled: false\n---\n\n# mara — 2026-08-31\nждёт\n')
    open(os.path.join(v, "kb/sessions/s1.md"), "w", encoding="utf-8").write(
        "---\ntype: session\noccurred: 2026-08-31\nproject: workspace\n---\n\n"
        "# Разбирал [очередь|всю]\n")
    open(os.path.join(v, "kb/notes/manual.md"), "w", encoding="utf-8").write(
        "---\ntitle: без occurred\nsource: manual\ncreated: 2026-08-31T21:58:32+03:00\n---\n\nтело\n")
    open(os.path.join(v, "kb/notes/old.md"), "w", encoding="utf-8").write(
        "---\ntitle: другой день\noccurred: 2026-08-30\n---\n\n# Старое\n")

    os.makedirs(os.path.join(v, "_system"))
    open(os.path.join(v, "_system/entity-index.json"), "w", encoding="utf-8").write(
        '[{"canonical": "atman", "title": "atman", "aliases": []}]')
    assert main(["--vault", v, "--date", "2026-08-31"]) == 0
    CANON = canon_map(v)
    p = os.path.join(v, "daily/2026-08-31.md")
    got = read(p)
    assert "title: \"31 августа 2026\"" in got and "occurred: 2026-08-31" in got
    assert "## Код\n- [[atman]] — [[git-atman-2026-08-31|Починил синк]]" in got, got
    # недистиллированная берёт заголовок из шапки, а не механический из тела
    assert "[[git-mara-2026-08-31|mara: 1 коммит за 2026-08-31]]" in got
    assert "## Разговоры" in got and "## Заметки" in got
    # проекта mara в реестре нет — ссылку на него не рисуем, это был бы фантом
    assert "- mara — [[git-mara-2026-08-31|" in got and "[[mara]]" not in got, got
    assert "[[workspace]]" not in got
    # скобки и палка в заголовке не ломают ссылку
    assert "[[s1|Разбирал (очередь/всю)]]" in got, got
    # заметка без occurred попала по дате создания
    assert "[[manual|без occurred]]" in got
    assert "Старое" not in got
    # идемпотентность
    assert not write(v, "2026-08-31", scan(v)["2026-08-31"], CANON)

    # дневник Мары дописан снизу — переживает пересборку, шапку не трогаем
    open(p, "a", encoding="utf-8").write("\n## 22:14\nзаебался с усилителем\n")
    txt = read(p).replace("sensitive: false", "sensitive: true")
    open(p, "w", encoding="utf-8").write(txt)
    open(os.path.join(v, "kb/sessions/s2.md"), "w", encoding="utf-8").write(
        "---\ntype: session\noccurred: 2026-08-31\n---\n\n# Ещё сессия\n")
    assert write(v, "2026-08-31", scan(v)["2026-08-31"], CANON)
    got = read(p)
    assert "заебался с усилителем" in got and "sensitive: true" in got, got
    assert "[[s2|Ещё сессия]]" in got
    assert got.count("<!-- mara:auto -->") == 1 and got.count("## Код") == 1
    assert got.index("mara:auto") < got.index("заебался")

    # хвостовой перевод строки, срезанный Basic Memory, — не повод переписывать
    cut = read(p).rstrip("\n")
    open(p, "w", encoding="utf-8").write(cut)
    assert not write(v, "2026-08-31", scan(v)["2026-08-31"], CANON)

    # файл, который Мара завела первой: её секция начинается с `##`, и блок
    # должен встать над ней целиком, а не влезть между заголовком и текстом
    d3 = os.path.join(v, "daily/2026-08-30.md")
    open(d3, "w", encoding="utf-8").write("## 09:12\nвыспался наконец\n")
    assert write(v, "2026-08-30", scan(v)["2026-08-30"], CANON)
    got = read(d3)
    assert got.index("mara:auto") < got.index("## 09:12"), got
    assert "## 09:12\nвыспался наконец" in got, got

    # --all забирает и день, которого нет в диапазоне
    assert main(["--vault", v, "--all"]) == 0
    assert "[[old|Старое]]" in read(d3)
    # пустой день страницы не заводит
    assert not os.path.exists(os.path.join(v, "daily/2026-08-29.md"))

    # daily-summary не должен утаскивать машинный блок в телеграм
    import importlib.util
    sp = importlib.util.spec_from_file_location(
        "ds", os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily-summary.py"))
    ds = importlib.util.module_from_spec(sp); sp.loader.exec_module(ds)
    d = ds.diary(v, "2026-08-31")
    assert "заебался" in d and "mara:auto" not in d and "git-atman" not in d, d
    print("daily-page: самопроверка ок")
    return 0


if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
