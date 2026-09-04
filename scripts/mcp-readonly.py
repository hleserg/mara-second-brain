#!/usr/bin/env python3
"""Read-only Basic Memory для агентских клиентов (ADR-0009 решение 3).

Сервер прав не различает: `tools/list` отдаёт все 23 инструмента кому угодно,
серверной ручки read-only у basic-memory 0.22.1 нет (`basic-memory mcp --help`
знает только транспорт, хост, порт, путь и проект). Значит отказ живёт в
конфигурации клиента, и у каждого клиента он свой:

  Claude Code — `permissions.deny` в ~/.claude/settings.json, имена вида
                `mcp__basic-memory__write_note`; формат подсказывает сам
                клиент: «Use 'mcp__<server>' to deny one server's tools
                ('mcp__<server>__<tool>' for one tool)».
  Codex       — `disabled_tools` в секции [mcp_servers.basic-memory]
                файла ~/.codex/config.toml, имена голые; проверено
                `codex mcp get basic-memory -c 'mcp_servers.basic-memory.
                disabled_tools=["delete_note"]'` — ключ читается.

Флага у установщиков нет ни у того, ни у другого (`claude mcp add --help`,
`codex mcp add --help`), поэтому правим конфиги сами. Список только
расширяется: чужие правила не трогаем.

  python3 scripts/mcp-readonly.py              # применить
  python3 scripts/mcp-readonly.py --check      # только сказать, чего нет
  python3 scripts/mcp-readonly.py --self-check
"""
import argparse, json, os, sys, tomllib

SERVER = "basic-memory"
# Всё, что пишет в волт. `canvas` тут не по недосмотру: он создаёт .canvas
# файл в проекте, то есть такой же писатель, как write_note.
WRITE_TOOLS = ("canvas", "create_memory_project", "delete_note",
               "delete_project", "edit_note", "move_note", "write_note")

CLAUDE = "~/.claude/settings.json"
CODEX = "~/.codex/config.toml"


def claude_deny(path, apply=True):
    """Возвращает те инструменты, которые ещё не запрещены."""
    if not os.path.exists(path):
        return []                      # клиента тут нет — запрещать нечего
    with open(path, encoding="utf-8") as fh:
        s = json.load(fh)
    deny = s.setdefault("permissions", {}).setdefault("deny", [])
    if "mcp__%s" % SERVER in deny:      # запрещён весь сервер целиком
        return []
    want = ["mcp__%s__%s" % (SERVER, t) for t in WRITE_TOOLS]
    нет = [t for t in want if t not in deny]
    if нет and apply:
        deny.extend(нет)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False, indent=2)
    return нет


def codex_disable(path, apply=True):
    """То же для Codex. Правка текстовая, потому что tomlkit в системном
    питоне нет, а tomllib только читает: переписать файл целиком значило бы
    потерять комментарии и порядок. Меняем одну строку и проверяем, что файл
    остался разбираемым, иначе откатываем."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    with open(path, "rb") as fh:
        было = tomllib.load(fh).get("mcp_servers", {}).get(SERVER)
    if было is None:
        return []                      # сервер тут не подключён
    нет = [t for t in WRITE_TOOLS if t not in было.get("disabled_tools", [])]
    if not нет or not apply:
        return нет

    lines = src.splitlines(keepends=True)
    шапки = ("[mcp_servers.%s]" % SERVER, '[mcp_servers."%s"]' % SERVER)
    i = next((k for k, l in enumerate(lines) if l.strip() in шапки), None)
    if i is None:
        # Секция задана иначе (inline table, точечные ключи) — молча
        # переписывать такое опаснее, чем сказать вслух.
        print("codex: секции %s в тексте нет, допишите disabled_tools руками"
              % шапки[0], file=sys.stderr)
        return нет
    полный = sorted(set(было.get("disabled_tools", [])) | set(WRITE_TOOLS))
    строка = "disabled_tools = [%s]\n" % ", ".join('"%s"' % t for t in полный)
    j = next((k for k in range(i + 1, len(lines))
              if lines[k].lstrip().startswith("[")), len(lines))
    for k in range(i + 1, j):
        if lines[k].lstrip().startswith("disabled_tools"):
            lines[k] = строка
            break
    else:
        lines.insert(i + 1, строка)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))
    try:
        with open(path, "rb") as fh:
            стало = tomllib.load(fh)["mcp_servers"][SERVER]["disabled_tools"]
        assert sorted(стало) == полный, стало
    except Exception:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        raise
    return нет


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="не править, только сказать")
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--claude", default=CLAUDE)
    ap.add_argument("--codex", default=CODEX)
    a = ap.parse_args(argv)
    if a.self_check:
        return self_check()
    осталось = 0
    for имя, fn, path in (("claude", claude_deny, a.claude),
                          ("codex", codex_disable, a.codex)):
        p = os.path.expanduser(path)
        нет = fn(p, apply=not a.check)
        if not os.path.exists(p):
            print("%s: конфига нет (%s) — пропуск" % (имя, path))
        elif not нет:
            print("%s: запись уже закрыта" % имя)
        elif a.check:
            print("%s: не запрещено %d — %s" % (имя, len(нет), ", ".join(нет)))
            осталось += len(нет)
        else:
            print("%s: запрещено %d — %s" % (имя, len(нет), ", ".join(нет)))
    return 1 if осталось else 0


def self_check():
    import tempfile
    d = tempfile.mkdtemp()
    cl = os.path.join(d, "settings.json")
    cx = os.path.join(d, "config.toml")

    assert claude_deny(cl) == [] and codex_disable(cx) == [], "выдумал конфиг"

    with open(cl, "w", encoding="utf-8") as fh:
        json.dump({"permissions": {"allow": ["Bash"],
                                   "deny": ["Bash(rm *)"]}}, fh)
    нет = claude_deny(cl)
    assert len(нет) == len(WRITE_TOOLS), нет
    s = json.load(open(cl, encoding="utf-8"))
    assert s["permissions"]["deny"][0] == "Bash(rm *)", "чужое правило потеряно"
    assert s["permissions"]["allow"] == ["Bash"], "allow пострадал"
    assert "mcp__basic-memory__write_note" in s["permissions"]["deny"]
    assert claude_deny(cl) == [], "повтор добавил ещё раз"

    with open(cl, "w", encoding="utf-8") as fh:
        json.dump({"permissions": {"deny": ["mcp__basic-memory"]}}, fh)
    assert claude_deny(cl) == [], "запрет сервера целиком не учтён"

    with open(cx, "w", encoding="utf-8") as fh:
        fh.write('model = "x"\n\n[mcp_servers.basic-memory]\n'
                 'url = "http://127.0.0.1:8787/mcp"  # туннель\n'
                 'disabled_tools = ["своё"]\n\n[mcp_servers.graft]\n'
                 'command = "graft"\n')
    нет = codex_disable(cx)
    assert len(нет) == len(WRITE_TOOLS), нет
    t = tomllib.load(open(cx, "rb"))
    assert (t["mcp_servers"][SERVER]["disabled_tools"]
            == sorted(("своё",) + WRITE_TOOLS))
    assert t["mcp_servers"]["graft"]["command"] == "graft", "соседняя цела"
    assert t["model"] == "x", "шапка сломана"
    assert "# туннель" in open(cx, encoding="utf-8").read(), "комментарий цел"
    assert codex_disable(cx) == [], "повтор запретил ещё раз"

    with open(cx, "w", encoding="utf-8") as fh:
        fh.write('[mcp_servers.graft]\ncommand = "graft"\n')
    assert codex_disable(cx) == [], "запретил у неподключённого сервера"

    # Секция есть, но задана inline: файл не трогаем и говорим вслух.
    with open(cx, "w", encoding="utf-8") as fh:
        fh.write('[mcp_servers]\nbasic-memory = { url = "u" }\n')
    было = open(cx, encoding="utf-8").read()
    assert len(codex_disable(cx)) == len(WRITE_TOOLS)
    assert open(cx, encoding="utf-8").read() == было, "переписал inline-секцию"

    print("mcp-readonly self-check: ок")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
