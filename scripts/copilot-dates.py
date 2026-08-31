#!/usr/bin/env python3
"""Достаёт реальные даты файлов из индекса плагина Copilot.

Единственный сохранившийся источник: mtime самих файлов затёрт (Basic Memory
переписал все заметки 2026-08-31), git волта начинается тогда же, R2 хранит
время загрузки. Индекс Copilot — снимок от 3 июля с ctime/mtime каждой
заметки, снятыми ещё на машине пользователя.

Файл — 88 МБ одной строкой, json.load его не переживёт, поэтому читаем
кусками с перехлёстом и вынимаем записи регэкспом.

Вывод: JSON {старый_путь: {"ctime": iso, "mtime": iso}} на stdout.
"""
import json, re, sys
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=3))
REC = re.compile(r'"path":"((?:[^"\\]|\\.)*)","embeddingModel":"[^"]*",'
                 r'"ctime":(\d+),"mtime":(\d+)')
CHUNK = 8_000_000
OVERLAP = 4096          # длиннее самой длинной записи, которую ищем

def main(path):
    out = {}
    tail = ""
    with open(path, encoding="utf-8") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            buf = tail + chunk
            for m in REC.finditer(buf):
                p = json.loads('"%s"' % m.group(1))
                c, t = int(m.group(2)), int(m.group(3))
                prev = out.get(p)
                # у заметки много чанков с одинаковыми датами; на всякий
                # случай берём самый ранний ctime и самый поздний mtime
                out[p] = {"c": min(c, prev["c"]) if prev else c,
                          "m": max(t, prev["m"]) if prev else t}
            tail = buf[-OVERLAP:]
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, TZ).isoformat()
    json.dump({p: {"ctime": iso(v["c"]), "mtime": iso(v["m"])}
               for p, v in sorted(out.items())},
              sys.stdout, ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main(sys.argv[1])
