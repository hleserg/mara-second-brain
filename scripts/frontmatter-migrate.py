#!/usr/bin/env python3
"""Доводит фронтматтер заметок до схемы ТЗ §4.

Сейчас там то, что проставил Basic Memory: title / type / permalink. Схема
требует ещё source, created, occurred, project, sensitive, distilled.

Существующие строки не переписываются, только добавляются недостающие и
исправляется type — так диф остаётся маленьким, а чужие поля (permalink,
на нём висит граф Basic Memory) не теряются.

Про даты. `occurred` заполняется ТОЛЬКО если реальная дата известна из
индекса Copilot (см. copilot-dates.py). Проставить всем дату импорта было бы
проще, но это навсегда сломало бы вьюху «что я делал за неделю»: 101 заметка
с occurred: 2026-09-01. Нет даты — нет поля.

Пропускаются copilot/ (шаблоны промптов, их читает плагин) и Excalidraw/.
"""
import json, os, re, subprocess, sys
from datetime import datetime

VAULT = os.environ.get("VAULT", "/srv/vault")
# коммит реструктуризации: до него пути совпадают с теми, что в индексе Copilot
RESTRUCTURE = os.environ.get("RESTRUCTURE", "f3e9766")
SKIP_TOP = {".git", ".obsidian", "raw", "copilot", "Excalidraw", "_system"}

# порядок ключей из §4; всё незнакомое (permalink) уезжает в хвост
ORDER = ["title", "type", "source", "source_id", "created", "occurred",
         "learned", "project", "tags", "sensitive", "distilled", "relations"]

TYPE_BY_DIR = {
    "daily": "daily", "timeline": "event",
    "entities/people": "person", "entities/projects": "project",
    "entities/tools": "tool", "entities/places": "place",
    "entities/concepts": "concept",
    "kb/sessions": "session", "kb/decisions": "decision",
    "kb/howto": "howto", "kb/notes": "note",
}


def renames():
    """старый путь -> новый, из коммита реструктуризации."""
    out = subprocess.run(
        ["git", "-C", VAULT, "diff", "-M", "--find-renames=40%", "-z",
         "--name-status", RESTRUCTURE + "^", "HEAD"],
        capture_output=True, text=True, check=True).stdout
    parts = out.split("\0")
    m, i = {}, 0
    while i < len(parts) - 1:
        st = parts[i]
        if st.startswith("R"):
            m[parts[i + 1]] = parts[i + 2]
            i += 3
        else:
            i += 2
    return m


def note_type(rel):
    d = os.path.dirname(rel)
    while d:
        if d in TYPE_BY_DIR:
            return TYPE_BY_DIR[d]
        d = os.path.dirname(d)
    return "note"


def project_of(rel):
    p = rel.split("/")
    if len(p) > 3 and p[0] == "kb" and p[1] == "howto":
        return p[2]
    return None


def split_fm(text):
    """-> (список верхнеуровневых блоков [(ключ, сырые строки)], тело)"""
    if not text.startswith("---\n"):
        return [], text
    end = text.find("\n---\n", 3)
    if end < 0:
        return [], text
    body = text[end + 5:]
    blocks, cur = [], None
    for line in text[4:end + 1].splitlines():
        m = re.match(r"([A-Za-z_][\w-]*):", line)
        if m:
            cur = [m.group(1), [line]]
            blocks.append(cur)
        elif cur:
            cur[1].append(line)
    return blocks, body


def main(dry=False):
    ren = renames()
    old_of = {v: k for k, v in ren.items()}
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dates = json.load(open(args[0], encoding="utf-8")) if args else {}
    changed = 0

    for root, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs
                   if os.path.relpath(os.path.join(root, d), VAULT) not in SKIP_TOP]
        for fn in files:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, VAULT)
            text = open(full, encoding="utf-8").read()
            blocks, body = split_fm(text)
            have = {k for k, _ in blocks}

            d = dates.get(old_of.get(rel, rel))
            ntype = note_type(rel)
            add = {
                "type": ntype,
                "source": "manual",
                "created": d["ctime"] if d else datetime.fromtimestamp(
                    os.path.getmtime(full)).astimezone().isoformat(),
                "sensitive": "true" if "Контакты" in rel else "false",
                "distilled": "true",
            }
            if d:
                add["occurred"] = d["ctime"][:10]
            proj = project_of(rel)
            if proj:
                add["project"] = '"[[%s]]"' % proj
            if "title" not in have:
                add["title"] = json.dumps(os.path.splitext(fn)[0], ensure_ascii=False)

            for k in have:
                add.pop(k, None)
            merged = [(k, ["type: " + ntype] if k == "type" and ntype != "note" else lines)
                      for k, lines in blocks]
            merged += [(k, ["%s: %s" % (k, v)]) for k, v in add.items()]

            rank = {k: i for i, k in enumerate(ORDER)}
            merged.sort(key=lambda kv: rank.get(kv[0], len(ORDER)))
            out = "---\n" + "\n".join(
                l for _, ls in merged for l in ls) + "\n---\n" + body
            if out != text:
                changed += 1
                if dry:
                    print(rel, "->", ", ".join(sorted(add)))
                else:
                    open(full, "w", encoding="utf-8").write(out)
    print("изменено: %d" % changed)


def demo():
    b, body = split_fm("---\ntitle: X\ntags:\n  - a\n---\nтело\n")
    assert [k for k, _ in b] == ["title", "tags"], b
    assert b[1][1] == ["tags:", "  - a"], b
    assert body == "тело\n", repr(body)
    assert split_fm("без фронтматтера") == ([], "без фронтматтера")
    assert note_type("kb/howto/atman/X.md") == "howto"
    assert note_type("entities/people/vanya.md") == "person"
    assert note_type("archive/scraps/X.md") == "note"
    assert project_of("kb/howto/atman/X.md") == "atman"
    assert project_of("kb/howto/X.md") is None      # файл прямо в howto — не проект
    print("ok")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        demo()
    else:
        main("--dry-run" in sys.argv)
