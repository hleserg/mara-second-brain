#!/usr/bin/env python3
"""Реестр сущностей: §5.2 индекс для промптов и §5.6 линтер битых ссылок.

Индекс нужен дистиллятору: без него модель проставляет [[wikilinks]] на
выдуманные заметки. Правило §5.3 — линковать только то, что в списке.

    python3 scripts/entity-index.py           # перестроить _system/entity-index.json
    python3 scripts/entity-index.py --lint    # + отчёт в _system/broken-links.md
"""
import os, re, sys, json, glob

VAULT = os.environ.get("VAULT", "/srv/vault")
FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)
# Обязательно с закрывающими ]]: в доках atman есть `[[ru](README-ru.md)]` —
# это markdown-ссылка в квадратных скобках, а не викилинк.
LINK = re.compile(r"\[\[([^\]|#\n]+?)(?:\|[^\]\n]*)?\]\]")

def frontmatter(text):
    m = FM.match(text)
    if not m: return {}
    out, key = {}, None
    for line in m.group(1).split("\n"):
        if line.startswith("- ") and key:            # список из предыдущего ключа
            out.setdefault(key, []).append(line[2:].strip().strip("'\""))
        elif ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip().strip("'\"")
            if val.startswith("[") and val.endswith("]"):
                out[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            elif val:
                out[key] = val
    return out

def entities():
    for p in sorted(glob.glob(os.path.join(VAULT, "entities", "*", "*.md"))):
        fm = frontmatter(open(p, encoding="utf-8", errors="replace").read())
        stem = os.path.splitext(os.path.basename(p))[0]
        al = fm.get("aliases") or []
        yield {"canonical": stem, "type": fm.get("type", os.path.basename(os.path.dirname(p))),
               "aliases": [a for a in ([al] if isinstance(al, str) else al) if a],
               "title": fm.get("title", stem)}

def write(path, text):
    tmp = path + ".tmp"                # рядом крутятся автокоммит и bisync
    with open(tmp, "w", encoding="utf-8") as fh: fh.write(text)
    os.replace(tmp, path)

def lint(known):
    """§5.6: [[X]] без файла X. Ссылка на несуществующую заметку хуже, чем её
    отсутствие — граф в Obsidian копит фантомные узлы."""
    # Целью ссылки может быть любая заметка волта, не только сущность.
    notes = {os.path.splitext(os.path.basename(p))[0]
             for p in glob.glob(os.path.join(VAULT, "**", "*.md"), recursive=True)}
    broken = {}
    for p in glob.glob(os.path.join(VAULT, "**", "*.md"), recursive=True):
        # prompts — инструкции модели, там [[wikilinks]] упоминаются как слово.
        # broken-links.md — свой же прошлый отчёт: без этого линтер
        # находит ссылки, которые сам напечатал, и они не кончаются.
        if any(x in p for x in ("/raw/", "/archive/", "/_system/prompts/",
                                "/_system/broken-links.md")): continue
        for t in LINK.findall(open(p, encoding="utf-8", errors="replace").read()):
            t = t.strip()
            if t and t not in notes and t not in known:
                broken.setdefault(t, []).append(os.path.relpath(p, VAULT))
    out = ["# Битые ссылки", "",
           "Автоотчёт `scripts/entity-index.py --lint` (ТЗ §5.6). Правится либо",
           "созданием сущности в `entities/`, либо снятием ссылки.", ""]
    if not broken:
        out.append("Битых ссылок нет.")
    else:
        out.append("| Цель | Упоминаний | Где |")
        out.append("|---|---:|---|")
        for t, files in sorted(broken.items(), key=lambda kv: -len(kv[1])):
            out.append("| `[[%s]]` | %d | %s |" % (t, len(files), ", ".join(
                "`%s`" % f for f in sorted(set(files))[:3])))
    write(os.path.join(VAULT, "_system", "broken-links.md"), "\n".join(out) + "\n")
    return sum(len(v) for v in broken.values()), len(broken)

def main():
    idx = list(entities())
    write(os.path.join(VAULT, "_system", "entity-index.json"),
          json.dumps(idx, ensure_ascii=False, indent=1) + "\n")
    print("сущностей: %d, алиасов: %d" % (idx.__len__(), sum(len(e["aliases"]) for e in idx)))
    if "--lint" in sys.argv:
        known = {e["canonical"] for e in idx} | {a for e in idx for a in e["aliases"]}
        n, uniq = lint(known)
        print("битых ссылок: %d упоминаний, %d разных целей" % (n, uniq))

if __name__ == "__main__":
    main()
