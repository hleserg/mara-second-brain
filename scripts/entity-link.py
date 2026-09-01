#!/usr/bin/env python3
"""Автосвязи (ТЗ §5.4, §5.5): из строк «Люди:/Проекты:» — сущности и ссылки.

Дистиллятор перечисляет имена простым текстом и правильно делает: §5.3 запрещает
ему линковать то, чего нет в реестре. Но дальше этого никто не шёл, и в волте
лежали сотни карточек с текстовыми именами и ноль рёбер между ними.

Здесь второй проход §5.4. Он детерминированный, без модели: сверка со списком, а
не догадка. Модель во втором проходе могла бы придумать сущность, которой нет, —
ровно то, от чего §5 и защищается.

Что делает и чего НЕ делает:
  - проекты заводит сам, начиная с --min упоминаний; людей — никогда. Людей в
    волте ровно один живой (владелец), остальное — боты и ассистенты, и заводить
    на каждого карточку значит получить граф из мусора;
  - «слить похожие» (§5.5) только предлагает, в `_system/entity-review.md`.
    Сливать автоматически нельзя: mara, mara-second-brain и mara-arena — три
    разных репозитория, а не опечатки друг друга.

    python3 scripts/entity-link.py --vault /srv/vault --dry-run
"""
import os, re, sys, glob, json, difflib, argparse, subprocess, collections
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from vault_common import canon_map, linkify, unlink, yaml_str

FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)
KINDS = {"Люди": "people", "Проекты": "projects"}
TYPE = {"people": "person", "projects": "project"}   # папка → type: во фронтматтере
LINE = re.compile(r"(?m)^(%s):[ \t]*(.+)$" % "|".join(KINDS))
# Поле шапки, куда git-ingest кладёт проект. Старые карточки писались до того,
# как завелись сущности, и держат его текстом — их и догоняем.
PROJ = re.compile(r"(?m)^project:[ \t]*(.+)$")
# Гит-коммиты подписаны ботами, и дистиллятор честно перечисляет их в людях.
# dependabot[bot] — не человек, карточка ему не нужна ни в каком виде.
DENY = re.compile(r"\[bot\]$|^(unknown|n/a|-|—|none|нет|не указан\w*"
                  r"|dependabot|renovate|github-actions|blocks task runner)$", re.I)

def ignored(vault):
    """Имена, про которые владелец уже сказал «не заводить». Без этого отчёт
    вечно показывает один и тот же отвергнутый десяток, и в нём тонет новое."""
    p = os.path.join(vault, "_system", "entity-ignore.txt")
    try: lines = open(p, encoding="utf-8").read().split("\n")
    except OSError: return set()
    return {norm(l.split("#")[0].strip()) for l in lines if l.split("#")[0].strip()}

def norm(s):
    """Ключ склейки: регистр, ё и разделители. attadipa/Attadipa и
    make_style_dataset/make-style-dataset — одно и то же, а не разное."""
    return re.sub(r"[-_ ]+", "", s.casefold().replace("ё", "е"))

def stem(name):
    """Имя файла сущности — латиницей в нижнем регистре, как у девяти уже
    заведённых. Кириллическое имя файлом не делаем: на маке NFC/NFD расходятся,
    и синк начинает возить туда-сюда «одинаковые» файлы. Такое — в review."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def cards(vault):
    """Карточки волта: путь, дата события, тело."""
    for p in sorted(glob.glob(os.path.join(vault, "kb", "**", "*.md"), recursive=True)):
        text = open(p, encoding="utf-8", errors="replace").read()
        m = FM.match(text)
        fm = m.group(1) if m else ""
        d = re.search(r"(?m)^occurred:[ \t]*['\"]?(\d{4}-\d\d-\d\d)", fm)
        yield p, (d.group(1) if d else "9999-99-99"), text

def harvest(vault):
    """{kind: {ключ: {"forms": Counter, "n": int, "first": (дата, файл)}}}"""
    out = {k: {} for k in KINDS.values()}
    for p, day, text in cards(vault):
        body = text[FM.match(text).end():] if FM.match(text) else text
        for head, names in LINE.findall(body):
            for raw in names.split(","):
                name = unlink(raw)
                if not name or DENY.search(name): continue
                c = out[KINDS[head]].setdefault(norm(name),
                        {"forms": collections.Counter(), "n": 0, "first": (day, p)})
                c["forms"][name] += 1
                c["n"] += 1
                if day < c["first"][0]: c["first"] = (day, p)
    return out

def card_text(name, kind, c, vault):
    first_day, first_path = c["first"]
    forms = [f for f, _ in c["forms"].most_common() if f != name]
    fm = ["---", "title: " + name, "type: " + TYPE[kind],
          "source: auto", "source_id: %s-%s" % (TYPE[kind], stem(name)),
          "created: " + datetime.now().astimezone().isoformat(timespec="seconds"),
          "occurred: " + (first_day if first_day[0] != "9" else
                          datetime.now().date().isoformat())]
    if forms: fm += ["aliases:"] + ["- " + f for f in forms]
    fm += ["sensitive: false", "distilled: true", "---", "",
           "# " + name, "",
           "Заведено автоматически (§5.4): упоминаний в карточках — %d." % c["n"],
           "Первое — [[%s]], %s." % (
               os.path.splitext(os.path.basename(first_path))[0], first_day),
           "Описание допишите руками — из перечислений его не собрать.", ""]
    return "\n".join(fm)

def create(vault, found, canon, min_n, skip=()):
    """Новые карточки сущностей. Люди сюда не попадают вовсе — см. шапку."""
    made, review = [], []
    for kind, cands in found.items():
        known = {norm(k) for k in canon}
        for key, c in sorted(cands.items(), key=lambda kv: -kv[1]["n"]):
            name = c["forms"].most_common(1)[0][0]
            why = ("есть в реестре" if key in known else
                   "человек — заводим только руками" if kind == "people" else
                   "упоминаний %d, порог %d" % (c["n"], min_n) if c["n"] < min_n else
                   "имя не латиницей" if not stem(name) else None)
            if why:
                if why != "есть в реестре" and key not in skip:
                    review.append((kind, name, c, why))
                continue
            made.append((os.path.join(vault, "entities", kind, stem(name) + ".md"),
                         card_text(name, kind, c, vault), name, c["n"]))
    return made, review

def relink(text, canon):
    """Только строки «Люди:/Проекты:» в теле и ничего больше. Фронтматтер не
    трогаем: Basic Memory нормализует YAML по-своему, эту войну мы уже вели."""
    m = FM.match(text)
    head, body = (text[:m.end()], text[m.end():]) if m else ("", text)
    n = [0]
    def one(mo):
        items = linkify(mo.group(2).split(","), canon)
        n[0] += sum(1 for i in items if i.startswith("[["))
        return "%s: %s" % (mo.group(1), ", ".join(items))
    def proj(mo):
        v = unlink(mo.group(1).strip().strip("'\""))
        c = canon.get(v.lower())
        if not c: return mo.group(0)
        n[0] += 1
        return "project: " + yaml_str("[[%s]]" % c)
    return PROJ.sub(proj, head) + LINE.sub(one, body), n[0]

def review_text(review, canon, min_n):
    """§5.5: похожее — в очередь на подтверждение, а не сливать молча."""
    out = ["# Кандидаты в сущности", "",
           "Автоотчёт `scripts/entity-link.py` (ТЗ §5.4, §5.5). Сюда попадает то,",
           "что скрипт сам заводить не стал. Согласны — заведите файл в `entities/`",
           "(или допишите имя в `aliases:` существующей сущности), и следующий",
           "прогон проставит ссылки задним числом. Боты (`…[bot]`) отброшены совсем.",
           "Не надо заводить — впишите имя в `_system/entity-ignore.txt`, и оно",
           "перестанет тут появляться.", ""]
    rows = ["| Имя | Тип | Упом. | Первое | Почему не завели | Похоже на |", "|---|---|---:|---|---|---|"]
    for kind, name, c, why in sorted(review, key=lambda r: (-r[2]["n"], r[1])):
        near = difflib.get_close_matches(norm(name), [norm(k) for k in canon], 1, 0.8)
        near = next((k for k in canon if norm(k) == near[0]), "") if near else ""
        rows.append("| %s | %s | %d | `%s` | %s | %s |" % (
            name, TYPE[kind], c["n"], c["first"][0], why,
            "`%s`?" % near if near else ""))
    out += rows if review else ["Кандидатов нет."]
    return "\n".join(out) + "\n"

def write(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d): os.makedirs(d)
    tmp = path + ".tmp"                # рядом крутятся автокоммит и bisync
    with open(tmp, "w", encoding="utf-8") as fh: fh.write(text)
    os.replace(tmp, path)

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    # Три — это «встречается регулярно», а не «мелькнуло в одном коммите».
    # Ниже порога граф зарастает одноразовыми узлами, ради которых §5 и писался.
    ap.add_argument("--min", type=int, default=3, help="порог упоминаний для проекта")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    from vault_common import locked
    canon = canon_map(a.vault)
    made, review = create(a.vault, harvest(a.vault), canon, a.min, ignored(a.vault))
    if a.dry_run:
        for _, _, name, n in made: print("завёл бы: %s (%d упом.)" % (name, n))
        files = links = 0
        for p, _, text in cards(a.vault):
            new, n = relink(text, canon)
            links += n
            files += new != text
        print("entity-link (вхолостую): сущностей %d, карточек переписал бы %d, "
              "ссылок %d, кандидатов в review %d" % (len(made), files, links, len(review)))
        return 0

    with locked(a.vault):
        for path, text, _, _ in made:
            if not os.path.exists(path): write(path, text)
    # Индекс перестраиваем до простановки ссылок: только что заведённые сущности
    # должны попасть в тот же прогон, иначе они ждали бы завтрашнего.
    if made:
        subprocess.run([sys.executable, os.path.join(HERE, "entity-index.py")],
                       env=dict(os.environ, VAULT=a.vault), check=True)
        canon = canon_map(a.vault)

    files = links = 0
    with locked(a.vault):
        for p, _, text in cards(a.vault):
            new, n = relink(text, canon)
            links += n
            if new != text: write(p, new); files += 1
        write(os.path.join(a.vault, "_system", "entity-review.md"),
              review_text(review, canon, a.min))
    print("entity-link: сущностей заведено %d, карточек переписано %d, ссылок %d, "
          "кандидатов в review %d" % (len(made), files, links, len(review)))
    return 0

def self_check():
    import tempfile
    assert norm("Attadipa") == norm("attadipa") == "attadipa"
    assert norm("make_style_dataset") == norm("make-style-dataset")
    assert norm("Ёлка") == norm("елка")
    assert stem("Smart Home") == "smart-home" and stem("Сергей") == ""
    # бот и «неизвестно» отсекаются, живое имя — нет
    assert DENY.search("dependabot[bot]") and DENY.search("unknown")
    assert not DENY.search("Сергей") and not DENY.search("Robot Framework")
    assert DENY.search("dependabot") and DENY.search("Blocks Task Runner")
    canon = {"сергей": "sergey", "sergey khlebnikov": "sergey", "sergey": "sergey",
             "attadipa": "attadipa", "meshcore": "meshcore"}
    # ссылка всегда на канон: голый [[алиас]] Obsidian не резолвит
    assert linkify(["Сергей", "attadipa", "Вася"], canon) == \
           ["[[sergey|Сергей]]", "[[attadipa]]", "Вася"]
    # готовую ссылку из дистиллятора не оборачиваем второй раз
    assert linkify(["[[meshcore]]", "[[sergey|Сергей]]"], canon) == \
           ["[[meshcore]]", "[[sergey|Сергей]]"]
    txt = ("---\ntitle: x\noccurred: 2026-08-31\nproject: \'attadipa\'\n---\n"
           "\n# Т\n\nЛюди: Сергей, dependabot[bot]\n"
           "Проекты: attadipa\n")
    new, n = relink(txt, canon)
    assert "Люди: [[sergey|Сергей]], dependabot[bot]" in new, new
    assert "Проекты: [[attadipa]]" in new and n == 3, (new, n)
    # шапку догоняем тоже: старые карточки писались до того, как завелись сущности
    assert 'project: "[[attadipa]]"' in new, new
    # фронтматтер не тронут, и повторный прогон ничего не меняет
    assert new.startswith("---\ntitle: x\noccurred: 2026-08-31\n")
    assert relink(new, canon)[0] == new, "проход не идемпотентен"
    # harvest: склейка регистра, счёт, первое упоминание — по самой ранней дате
    v = tempfile.mkdtemp(); os.makedirs(os.path.join(v, "kb"))
    open(os.path.join(v, "kb/a.md"), "w", encoding="utf-8").write(txt)
    open(os.path.join(v, "kb/b.md"), "w", encoding="utf-8").write(
        "---\noccurred: 2026-08-01\n---\n\nПроекты: Attadipa, Ponytail\n")
    h = harvest(v)
    assert h["projects"]["attadipa"]["n"] == 2, h
    assert h["projects"]["attadipa"]["first"][0] == "2026-08-01"
    assert "dependabot[bot]" not in str(h["people"])
    # порог и «людей не заводим»
    # отвергнутое владельцем в отчёт больше не лезет
    assert not [r for r in create(v, h, {}, 2, {norm("Ponytail")})[1] if r[1] == "Ponytail"]
    made, review = create(v, h, {}, 2)
    # имя берётся в самой частой написанной форме; при равенстве — первая
    assert [m[2] for m in made] == ["attadipa"], made
    assert ("Сергей", "человек — заводим только руками") in [(r[1], r[3]) for r in review]
    assert ("Ponytail", "упоминаний 1, порог 2") in [(r[1], r[3]) for r in review]
    # у заведённой сущности алиас второй формы и ссылка на первое упоминание
    assert "type: project" in made[0][1] and "aliases:\n- Attadipa" in made[0][1] and "[[b]], 2026-08-01" in made[0][1]
    # опечатка предлагается, а не сливается молча
    rt = review_text([("projects", "atttadipa", h["projects"]["ponytail"], "упоминаний 1")],
                     {"attadipa": "attadipa"}, 3)
    assert "`attadipa`?" in rt, rt
    # а вот вложенность не предлагается вовсе: mara, mara-arena и
    # mara-second-brain — три разных репозитория, а не опечатки друг друга
    assert "mara" not in review_text(
        [("projects", "mara-arena", h["projects"]["ponytail"], "упоминаний 1")],
        {"mara": "mara"}, 3).split("| project |")[1]
    print("entity-link: самопроверка ок")
    return 0

if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
