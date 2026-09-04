#!/usr/bin/env python3
"""Read-only Basic Memory для агентских клиентов (ADR-0009 решение 3).

Сервер прав не различает: `tools/list` отдаёт все 23 инструмента кому угодно,
серверной ручки read-only у basic-memory 0.22.1 нет (`basic-memory mcp --help`
знает только транспорт, хост, порт, путь и проект). Значит отказ живёт в
конфигурации клиента, и у каждого клиента он свой:

  Claude Code — `permissions.deny` в ~/.claude/settings.json, имена вида
                `mcp__basic-memory__write_note`; так их и разбирает сам
                клиент: «MCP permission rules match on the
                `mcp__<server>__<tool>` name string alone».
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
    """Возвращает (состояние, ещё не запрещённые инструменты).

    Состояния общие у обоих клиентов: «нет-конфига», «уже», «надо» — и
    только у Codex ещё «нет-сервера» и «не-смог». Врозь их держим затем,
    чтобы `main` не печатал «запрещено 7» там, где ничего не записано.
    """
    if not os.path.exists(path):
        return "нет-конфига", []       # клиента тут нет — запрещать нечего
    with open(path, encoding="utf-8") as fh:
        s = json.load(fh)
    deny = s.setdefault("permissions", {}).setdefault("deny", [])
    # Клиент понимает и запрет сервера целиком, и звёздочку на все MCP:
    # «or 'mcp__*' to deny every MCP server's tools».
    if "mcp__%s" % SERVER in deny or "mcp__*" in deny:
        return "уже", []
    want = ["mcp__%s__%s" % (SERVER, t) for t in WRITE_TOOLS]
    нет = [t for t in want if t not in deny]
    if not нет:
        return "уже", []
    if apply:
        deny.extend(нет)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False, indent=2)
    return "надо", нет


def руками(почему, нет):
    """Случай, который текстовой правкой не берётся: не трогаем и говорим."""
    print("codex: %s — допишите руками" % почему, file=sys.stderr)
    return "не-смог", нет


def codex_disable(path, apply=True):
    """То же для Codex. Правка текстовая, потому что tomlkit в системном
    питоне нет, а tomllib только читает: переписать файл целиком значило бы
    потерять комментарии и порядок. Меняем одну строку и проверяем, что файл
    остался разбираемым, иначе откатываем."""
    if not os.path.exists(path):
        return "нет-конфига", []
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    with open(path, "rb") as fh:
        было = tomllib.load(fh).get("mcp_servers", {}).get(SERVER)
    if было is None:
        return "нет-сервера", []
    нет = [t for t in WRITE_TOOLS if t not in было.get("disabled_tools", [])]
    if not нет:
        return "уже", []
    if not apply:
        return "надо", нет

    lines = src.splitlines(keepends=True)
    шапки = ("[mcp_servers.%s]" % SERVER, '[mcp_servers."%s"]' % SERVER,
             "[mcp_servers.'%s']" % SERVER)
    i = next((k for k, l in enumerate(lines) if l.strip() in шапки), None)
    if i is None:
        # Секция задана иначе (inline table, точечные ключи) — молча
        # переписывать такое опаснее, чем сказать вслух.
        return руками("секции %s в тексте нет" % шапки[0], нет)
    полный = sorted(set(было.get("disabled_tools", [])) | set(WRITE_TOOLS))
    строка = "disabled_tools = [%s]\n" % ", ".join('"%s"' % t for t in полный)
    j = next((k for k in range(i + 1, len(lines))
              if lines[k].lstrip().startswith("[")), len(lines))
    k = next((k for k in range(i + 1, j)
              if lines[k].lstrip().startswith("disabled_tools")), None)
    if k is None:
        lines.insert(i + 1, строка)
    elif "]" in lines[k]:
        lines[k] = строка
    else:
        # Массив расписан на несколько строк: заменив первую, получим
        # хвост от старого массива и невалидный TOML.
        return руками("disabled_tools расписан в несколько строк", нет)
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
    return "надо", нет


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
        код, нет = fn(os.path.expanduser(path), apply=not a.check)
        if код == "нет-конфига":
            print("%s: конфига нет (%s) — пропуск" % (имя, path))
        elif код == "нет-сервера":
            print("%s: сервер %s не подключён — пропуск" % (имя, SERVER))
        elif код == "уже":
            print("%s: запись уже закрыта" % имя)
        elif код == "не-смог":
            print("%s: не записал ничего, осталось %d" % (имя, len(нет)))
            осталось += len(нет)
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

    assert claude_deny(cl)[0] == "нет-конфига", "выдумал конфиг Claude Code"
    assert codex_disable(cx)[0] == "нет-конфига", "выдумал конфиг Codex"

    with open(cl, "w", encoding="utf-8") as fh:
        json.dump({"permissions": {"allow": ["Bash"],
                                   "deny": ["Bash(rm *)"]}}, fh)
    код, нет = claude_deny(cl)
    assert код == "надо" and len(нет) == len(WRITE_TOOLS), (код, нет)
    s = json.load(open(cl, encoding="utf-8"))
    assert s["permissions"]["deny"][0] == "Bash(rm *)", "чужое правило потеряно"
    assert s["permissions"]["allow"] == ["Bash"], "allow пострадал"
    assert "mcp__basic-memory__write_note" in s["permissions"]["deny"]
    assert claude_deny(cl)[0] == "уже", "повтор добавил ещё раз"

    # Оба широких правила клиента считаются закрытой записью.
    for правило in ("mcp__basic-memory", "mcp__*"):
        with open(cl, "w", encoding="utf-8") as fh:
            json.dump({"permissions": {"deny": [правило]}}, fh)
        assert claude_deny(cl)[0] == "уже", правило

    with open(cx, "w", encoding="utf-8") as fh:
        fh.write('model = "x"\n\n[mcp_servers.basic-memory]\n'
                 'url = "http://127.0.0.1:8787/mcp"  # туннель\n'
                 'disabled_tools = ["своё"]\n\n[mcp_servers.graft]\n'
                 'command = "graft"\n')
    код, нет = codex_disable(cx)
    assert код == "надо" and len(нет) == len(WRITE_TOOLS), (код, нет)
    t = tomllib.load(open(cx, "rb"))
    assert (t["mcp_servers"][SERVER]["disabled_tools"]
            == sorted(("своё",) + WRITE_TOOLS))
    assert t["mcp_servers"]["graft"]["command"] == "graft", "соседняя цела"
    assert t["model"] == "x", "шапка сломана"
    assert "# туннель" in open(cx, encoding="utf-8").read(), "комментарий цел"
    assert codex_disable(cx)[0] == "уже", "повтор запретил ещё раз"

    with open(cx, "w", encoding="utf-8") as fh:
        fh.write('[mcp_servers.graft]\ncommand = "graft"\n')
    assert codex_disable(cx)[0] == "нет-сервера", "запретил у чужого сервера"

    # Заголовок в кавычках — тоже наша секция, обе формы валидны в TOML.
    формы = ('[mcp_servers."basic-memory"]', "[mcp_servers.'basic-memory']")
    for шапка in формы:
        with open(cx, "w", encoding="utf-8") as fh:
            fh.write("%s\nurl = \"u\"\n" % шапка)
        assert codex_disable(cx)[0] == "надо", шапка
        стало = tomllib.load(open(cx, "rb"))["mcp_servers"][SERVER]
        assert стало["disabled_tools"] == sorted(WRITE_TOOLS), шапка

    # Текстовой правкой не берётся: файл цел, состояние отличимо от успеха.
    for текст in ('[mcp_servers]\nbasic-memory = { url = "u" }\n',
                  '[mcp_servers.basic-memory]\nurl = "u"\n'
                  'disabled_tools = [\n  "своё",\n]\n'):
        with open(cx, "w", encoding="utf-8") as fh:
            fh.write(текст)
        assert codex_disable(cx)[0] == "не-смог", текст
        assert open(cx, encoding="utf-8").read() == текст, "переписал, а обещал"

    print("mcp-readonly self-check: ок")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
