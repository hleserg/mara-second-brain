"""Мелочи, общие для сборщиков карточек (session-note, git-ingest).

Живёт отдельно не ради красоты, а потому что правило «линковать только то, что
есть в реестре» должно быть одно на всех: разъехавшись, две копии тихо начнут
плодить фантомные узлы в графе, и заметит это только линтер §5.6.
"""
import json, os, sys, fcntl, contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from redact import SECRETS          # слой 1 §8.3, чистые регэкспы

def canon_map(vault):
    """Алиас или каноническое имя → каноническое (§5.2). Голый `[[алиас]]`
    Obsidian не резолвит — он вставляет `[[Каноническое|алиас]]`, — поэтому
    имя приводим к канону, а незнакомое не линкуем вовсе. Индекса нет
    (клиентский спул) — не линкуем ничего, и это правильно."""
    try:
        idx = json.load(open(os.path.join(vault or "", "_system/entity-index.json"),
                             encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return {n.lower(): e["canonical"] for e in idx
            for n in [e["canonical"]] + list(e.get("aliases") or [])}

def link(name, canon):
    """Значение для frontmatter: ссылка, если сущность известна, иначе текст."""
    c = (canon or {}).get((name or "").lower())
    return "[[%s]]" % c if c else name

def unlink(s):
    """`[[канон|Текст]]` → `Текст`, `[[канон]]` → `канон`. Модель иногда кладёт в
    people уже готовую ссылку — обернуть её второй раз нельзя."""
    s = (s or "").strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2].split("|")[-1].strip()
    return s

def linkify(names, canon):
    """Имена из карточки → `[[канон|как написано]]`; незнакомое остаётся текстом.

    Одна на всех: дистиллятор линкует карточку в момент записи, entity-link
    догоняет старые, когда сущность завелась позже. Разъедься эти двое — часть
    графа была бы связана, часть нет, и по какому принципу — не понял бы никто.
    """
    out = []
    for n in names:
        n = unlink(str(n))
        if not n: continue
        c = (canon or {}).get(n.lower())
        out.append(n if not c else "[[%s]]" % c if c == n else "[[%s|%s]]" % (c, n))
    return out

@contextlib.contextmanager
def locked(vault):
    """Общий с автокоммитом и bisync флок (§13.8). Берём его только вокруг
    записи в волт: качать зеркала под ним — значит держать синк все те минуты,
    что идёт clone."""
    fh = open(os.path.join(vault, ".git/vault-git.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fh.close()

def scrub(s):
    """Текст человека уезжает в волт как есть, а волт синкается в R2 и
    коммитится. Ключ, вставленный в реплику или в сообщение коммита, уехал бы
    вместе с ним — §11, «секреты не в волте, никогда»."""
    for rx, repl in SECRETS: s = rx.sub(repl, s)
    return s

def yaml_str(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')
