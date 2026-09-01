"""Мелочи, общие для сборщиков карточек (session-note, git-ingest).

Живёт отдельно не ради красоты, а потому что правило «линковать только то, что
есть в реестре» должно быть одно на всех: разъехавшись, две копии тихо начнут
плодить фантомные узлы в графе, и заметит это только линтер §5.6.
"""
import json, os, sys

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

def scrub(s):
    """Текст человека уезжает в волт как есть, а волт синкается в R2 и
    коммитится. Ключ, вставленный в реплику или в сообщение коммита, уехал бы
    вместе с ним — §11, «секреты не в волте, никогда»."""
    for rx, repl in SECRETS: s = rx.sub(repl, s)
    return s

def yaml_str(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')
