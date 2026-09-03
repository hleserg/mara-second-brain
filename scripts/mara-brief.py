#!/usr/bin/env python3
"""Сводка сущностей для Мары — «штырь в затылок» (ТЗ §7, этап 3).

Мара знает Серёгу не потому, что каждый раз ищет, а потому что имена проектов,
людей, мест и инструментов лежат у неё в системном промпте. Сводка собирается из
фронтматтера и первого абзаца карточек entities/ — без модели, детерминированно
(ТЗ §13: сбор и агрегация без вызовов модели). Пишется в два места:

  - _system/mara-brief.md в волте — человеку видно, что именно она «знает»;
  - ~/.hermes/SOUL.md на маке, между <!-- mara:brief --> и <!-- /mara:brief -->,
    атомарно, по ssh с doctor: своего крона в GUI-домене мака нет (README).

Системный промпт Hermes собирает один раз на сессию и хранит в state.db по хэшу
(sessions.system_prompt_hash → system_prompts); рестарт gateway его не трогает.
Поэтому после записи нового блока скрипт делает то же, что /model в телеграме:
обнуляет хэш живым телеграм-сессиям — на следующей реплике промпт собирается
заново, история остаётся. Блок без даты: меняется только с карточками, и хэш
сбрасывается (а кэш префикса у провайдера рвётся) только когда есть что менять.
Карточки с sensitive: true в сводку не идут: SOUL.md уезжает провайдеру модели.
Один писатель на файл: _system/mara-brief.md пишет только этот скрипт, в SOUL.md
он трогает только свой блок между маркерами.
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vault_common

vault_common.load_env()          # адрес мака — не в репозитории

MARK_OPEN, MARK_CLOSE = "<!-- mara:brief -->", "<!-- /mara:brief -->"
ANCHOR = "\n## Когда он изливает душу"      # блок знаний идёт перед блоками поведения
KIND = [("projects", "Проекты"), ("people", "Люди"), ("places", "Места и машины"),
        ("tools", "Инструменты"), ("concepts", "Понятия")]
MAX_DESC, MAX_STATUS, MAX_ALIASES = 180, 120, 3
MAX_BYTES = 14000   # ponytail: потолок блока, он едет в каждый ход; вырос — резать MAX_DESC

INTRO = """## Что ты уже знаешь о Серёге

Ниже карточки из волта (`entities/`): проекты, люди, места, инструменты. Узнавай
имена без поиска. Подробности, историю и дневник ищи basic-memory (search_notes,
read_note, build_context, recent_activity) — молча, до того как сказать «не знаю».
Нашла — отвечай по делу, без permalink'ов и пересказа frontmatter. Не нашла — так
и скажи, не выдумывай."""


def frontmatter(text):
    """Плоский YAML наших карточек: скаляры и списки через «- ». Без PyYAML —
    его нет на doctor, а карточки пишут свои же скрипты одним форматом."""
    m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
    if not m:
        return {}, text
    fm, key = {}, None
    for line in m.group(1).splitlines():
        if line.startswith("- ") and key is not None:
            if not isinstance(fm[key], list):
                fm[key] = []
            fm[key].append(line[2:].strip().strip("'\""))
        elif line and not line[0].isspace() and ":" in line:
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip().strip("'\"")
            fm[key] = val
    return fm, m.group(2)


def clean(s):
    s = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", s)   # [[a|b]] -> b
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)              # [[a]] -> a
    s = s.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", s).strip()


def cut(s, n):
    if len(s) <= n:
        return s
    s = s[:n]
    return s[:s.rfind(" ")].rstrip(",;:—-") + "…" if " " in s else s + "…"


def describe(body):
    """Первый абзац после заголовка и строка «Статус:», если есть."""
    lines = body.strip().splitlines()
    para, status, started = [], "", False
    for line in lines:
        if line.startswith("#"):
            continue
        if line.startswith("**Статус:**"):
            status = clean(line[len("**Статус:**"):])
            continue
        if not line.strip():
            if started:
                break
            continue
        if line.startswith("**"):      # другие жирные поля вроде «Где живёт»
            if started:
                break
            continue
        started = True
        para.append(line)
    return cut(clean(" ".join(para)), MAX_DESC), cut(status, MAX_STATUS)


def контакт(alias):
    """Похож ли алиас на способ связи, а не на имя.

    Номер телефона из карточки, заведённой по журналу звонков, — контактные
    данные, а не знание. SOUL.md уезжает провайдеру модели (ТЗ §11), и вместе
    с ним уехал бы личный номер. В самой карточке номер остаётся: он нужен
    локально, чтобы узнать входящий звонок.
    """
    if "@" in alias:
        return True
    цифры = re.sub(r"[\s()\-+.]", "", alias)
    return цифры.isdigit() and len(цифры) >= 7


def card(path):
    fm, body = frontmatter(open(path, encoding="utf-8").read())
    if str(fm.get("sensitive", "false")).lower() == "true":
        return None
    title = fm.get("title") or os.path.splitext(os.path.basename(path))[0]
    aliases = fm.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    seen, keep = {title.lower()}, []
    for a in aliases:
        if (a.lower() not in seen and "/" not in a and not контакт(a)
                and len(keep) < MAX_ALIASES):
            keep.append(a)
            seen.add(a.lower())
    desc, status = describe(body)
    line = f"- **{title}**"
    if keep:
        line += " (" + ", ".join(keep) + ")"
    if desc:
        line += " — " + desc
    if status:
        line += f" Статус: {status}"
    return line


def build(vault):
    parts = [MARK_OPEN, INTRO, ""]
    total = 0
    for folder, name in KIND:
        lines = [card(p) for p in sorted(glob.glob(os.path.join(vault, "entities", folder, "*.md")))]
        lines = [l for l in lines if l]
        if not lines:
            continue
        total += len(lines)
        parts += [f"### {name}", *lines, ""]
    parts.append(MARK_CLOSE)
    block = "\n".join(parts)
    if len(block.encode()) > MAX_BYTES:
        print(f"mara-brief: блок {len(block.encode())} байт > {MAX_BYTES}, режь MAX_DESC", file=sys.stderr)
    return block, total


def splice(soul, block):
    if MARK_OPEN in soul and MARK_CLOSE in soul:
        a, b = soul.index(MARK_OPEN), soul.index(MARK_CLOSE) + len(MARK_CLOSE)
        return soul[:a] + block + soul[b:]
    if ANCHOR in soul:
        i = soul.index(ANCHOR)
        return soul[:i].rstrip("\n") + "\n\n" + block + "\n" + soul[i:]
    return soul.rstrip("\n") + "\n\n" + block + "\n"


def write_atomic(path, text):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".brief-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def push_mac(mac, soul_path, block):
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", mac]
    cur = subprocess.run(ssh + [f"cat {soul_path}"], capture_output=True, text=True)
    if cur.returncode != 0:
        print(f"mara-brief: мак недоступен или нет {soul_path}: {cur.stderr.strip()}", file=sys.stderr)
        return False
    new = splice(cur.stdout, block)
    if new == cur.stdout:
        return True
    # Первый раз — копия персоны до врезки; дальше блок только свой, копий не плодим.
    cmd = (f"[ -f {soul_path}.bak-before-brief ] || cp {soul_path} {soul_path}.bak-before-brief; "
           f"cat > {soul_path}.tmp && mv {soul_path}.tmp {soul_path}")
    r = subprocess.run(ssh + [cmd], input=new, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"mara-brief: не записал SOUL.md: {r.stderr.strip()}", file=sys.stderr)
        return False
    # Как /model: живая телеграм-сессия пересоберёт промпт на следующей реплике.
    # ponytail: если агент этой сессии ещё в кэше gateway (Серёга писал меньше
    # часа назад), новый промпт придёт после выселения агента; крон в 04:20 — ок.
    db = os.path.dirname(soul_path) + "/state.db"
    q = "UPDATE sessions SET system_prompt=NULL, system_prompt_hash=NULL WHERE source='telegram' AND ended_at IS NULL"
    r = subprocess.run(ssh + [f"sqlite3 {db} \"{q}\""], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"mara-brief: SOUL.md записан, но хэш промпта не сброшен: {r.stderr.strip()}", file=sys.stderr)
        return False
    return True


def self_check():
    import shutil
    v = tempfile.mkdtemp(prefix="brief-")
    try:
        os.makedirs(os.path.join(v, "entities/projects"))
        os.makedirs(os.path.join(v, "entities/people"))
        open(os.path.join(v, "entities/projects/atta.md"), "w", encoding="utf-8").write(
            "---\ntitle: Atta-dipa\ntype: project\naliases:\n- Attadipa\n- Atta-Dipa\n- hleserg/Attadipa\n"
            "sensitive: false\n---\n# Atta-dipa\nПрошивка для часов на [[esp32|ESP32]], **меш** и навигация.\n"
            "Вторая строка абзаца.\n**Статус:** ранняя реализация.\n**Где живёт:** репа.\n")
        open(os.path.join(v, "entities/people/x.md"), "w", encoding="utf-8").write(
            "---\ntitle: Тайный Человек\ntype: person\nsensitive: true\n---\n# Тайный\nне для облака\n")
        open(os.path.join(v, "entities/people/y.md"), "w", encoding="utf-8").write(
            "---\ntitle: Катя\ntype: person\naliases:\n- Катюша\n- +79990000000\n"
            "sensitive: false\n---\n# Катя\nПодруга.\n")
        block, n = build(v)
        assert n == 2, n
        assert "+79990000000" not in block and "79990000000" not in block, \
            "номер телефона уехал бы провайдеру модели вместе с SOUL.md"
        assert "Катюша" in block, "обычный алиас отбрасывать не надо"
        assert контакт("+7 (999) 000-00-00") and контакт("anna@example.com")
        assert not контакт("Катюша") and not контакт("Atta-Dipa")
        assert "Atta-dipa** (Attadipa)" in block, block          # алиас-дубль и repo-slug отброшены
        assert "ESP32, меш и навигация. Вторая строка абзаца." in block, block
        assert "Статус: ранняя реализация." in block and "Где живёт" not in block
        assert "Тайный" not in block and "**Катя**" in block
        assert block.startswith(MARK_OPEN) and block.endswith(MARK_CLOSE)
        soul = "# SOUL\n\n## Суть\nдемон\n\n## Когда он изливает душу\nслушать\n"
        s1 = splice(soul, block)
        assert s1.count(MARK_OPEN) == 1 and s1.index(MARK_OPEN) < s1.index("## Когда он")
        s2 = splice(s1, block.replace("Подруга", "Психолог"))
        assert s2.count(MARK_OPEN) == 1 and "Психолог" in s2 and "Подруга" not in s2
        assert s2.replace("Психолог", "Подруга") == s1                # врезка идемпотентна
        assert splice("без якоря\n", block).endswith(MARK_CLOSE + "\n")
        assert cut("а б в г", 4) == "а б…" and cut("абв", 10) == "абв"
        print("mara-brief --self-check: ок")
    finally:
        shutil.rmtree(v)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--mac", default=os.environ.get("MARA_MAC"))
    ap.add_argument("--soul", default="~/.hermes/SOUL.md", help="путь на маке")
    ap.add_argument("--no-mac", action="store_true", help="только файл в волте")
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    block, n = build(a.vault)
    out = os.path.join(a.vault, "_system", "mara-brief.md")
    body = ("---\ntitle: Что знает Мара\ntype: note\nsource: manual\nsource_id: mara-brief\n"
            "tags:\n- system\nsensitive: false\n---\n# Что знает Мара\n"
            "Этот блок лежит в её SOUL.md и пересобирается кроном из `entities/`. "
            "Карточки с `sensitive: true` сюда не попадают.\n\n" + block + "\n")
    if not os.path.exists(out) or open(out, encoding="utf-8").read() != body:
        write_atomic(out, body)
    ok = True if a.no_mac else push_mac(
        a.mac or vault_common.нужен_адрес("MARA_MAC", "мак с Марой"),
        a.soul, block)
    print(f"mara-brief: карточек {n}, блок {len(block.encode())} байт, мак {'ок' if ok else 'НЕТ'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
