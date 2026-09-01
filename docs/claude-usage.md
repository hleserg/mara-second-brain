# Учёт расхода лимитов Claude Code

Состояние на 2 сентября 2026. Сделаны этапы 1–3 из брифа (`разведка`,
`коллектор`, `импорт истории`); этапы 4–7 (агрегатор, отчёты, крон, приёмка)
ещё нет.

## Что выяснила разведка

Бриф исходил из того, что данные придётся собирать хуками. Оказалось, почти
всё уже лежит в транскриптах `~/.claude/projects/<проект>/<сессия>.jsonl`:

| Запись | Что несёт |
|---|---|
| `assistant` | `message.usage` (вход, выход, запись и чтение кэша, `output_tokens_details.thinking_tokens`, `cache_creation.ephemeral_1h/5m`), `message.model`, `message.id` (ключ дедупа), `timestamp`, `effort`, `version`, `cwd`, `gitBranch`, `isSidechain` (подагент), `advisorModel`, `attributionSkill` |
| `assistant.message.content[]` | блоки `tool_use` — имя инструмента, MCP-сервер по префиксу `mcp__<сервер>__` |
| `user.toolUseResult` | результат инструмента целиком, отсюда его размер в байтах |
| `system.compactMetadata` | компакции: `trigger`, `preTokens`, `postTokens`, `cumulativeDroppedTokens`, `durationMs` |
| `cost-state` | `totalCostUSD`, `modelUsage` с токенами и `costUSD` **по каждой модели**, `hasUnknownModelCost`, длительности, строки кода |

Отсюда два вывода, которые сократили работу:

- **Хуки не нужны вовсе.** Ни `PostToolUse`, ни `SessionStart`, ни `PreCompact`.
  Всё, что бриф просил собирать хуками, уже в транскрипте, а транскрипты на
  doctor приносит существующий `SessionEnd`-хук — 535 файлов в
  `raw/claude-code/`, включая мак. Меньше движущихся частей в `settings.json`.
- **Прайс-таблица не нужна.** Claude Code сам считает деньги по каждой модели,
  включая модель советника и подагентов. `api_usd_equiv` берём как есть.

Чего в транскриптах нет вовсе — **процентов лимита подписки**. Их отдаёт
только statusline, и это единственное, ради чего пришлось трогать
`~/.claude/`.

### Реальный JSON statusline (версия 2.1.252)

Проверено на живой сессии, а не по документации:

```
session_id, transcript_path, cwd, prompt_id, session_name, version
effort.level                      model.id / model.display_name
workspace.current_dir / project_dir / repo.{host,owner,name}
cost.total_cost_usd / total_duration_ms / total_api_duration_ms
    / total_lines_added / total_lines_removed
context_window.total_input_tokens / used_percentage / context_window_size
    / current_usage.{input,output,cache_creation,cache_read}_tokens
exceeds_200k_tokens               fast_mode        thinking.enabled
prompt_cache.{warm,ttl,requests,misses,hit_ratio,cache_write_tokens,
              expected_rebuilds,recache_tokens_if_cold,expires_at}
rate_limits.five_hour.{used_percentage,resets_at}
rate_limits.seven_day.{used_percentage,resets_at}
```

`resets_at` — epoch в секундах. Отдельного ведра под Fable, как и предполагал
бриф, нет: долю Fable придётся оценивать косвенно.

**Времени в JSON нет.** Штампует statusline (`$EPOCHREALTIME`), иначе тик
получил бы время шиппера — на пять минут позже, и интервалы поехали бы.

## Что уже работает

### `scripts/claude-usage.py` — сканер транскриптов

Разбирает транскрипты в записи сессий и в лог тяжёлых результатов
инструментов. Дедуп по `message.id`: одно сообщение встречается в файле не раз
(резюме компакции, ретраи), без дедупа токены задваиваются.

```
python3 scripts/claude-usage.py status                       # сводка на экран
python3 scripts/claude-usage.py scan --out "…/Claude Usage/_data"
```

Первый прогон на 535 транскриптах из волта — полторы секунды.

Две находки на живых данных:

- **506 «сессий» с мака — пустышки.** `model: "<synthetic>"`, ноль токенов:
  OpenClaw запускает Claude Code, тот сразу отваливается. Отсеиваются, иначе
  любой счёт сессий утонул бы в них.
- Модель `claude-opus-5[1m]` — отдельный ключ: миллионное окно тарифицируется
  своей строкой. `advisorModel: claude-fable-5` объясняет, откуда в расходе
  Fable при работе на Opus.

### `scripts/claude-usage-ship.py` — тики и тулсет (**не запускался**)

Statusline дописывает сырой блоб в `~/.local/state/mara/ticks.raw` одним
`printf` — без `jq` и без сети, чтобы не тормозить строку состояния. Шиппер
разбирает накопленное, ужимает в строки тиков, снимает тулсет и везёт всё на
doctor.

Сырой файл забирается **переименованием**: statusline после этого откроет по
имени новый файл, и гонки за строку не будет. Один писатель на файл — сырьё
лежит в папке своего хоста.

Тулсет (§4 брифа) снимается из конфигов, а не хуком: `~/.claude.json`
(MCP по проектам), `~/.claude/settings.json` (плагины), `~/.claude/skills/`,
размер `CLAUDE.md`. Строка пишется, только когда меняется хэш.

### Правка в `~/.claude/statusline-command.sh`

Одна строка после `input=$(cat)`, бэкап рядом (`.bak-usage`):

```bash
printf '%s %s\n' "$EPOCHREALTIME" "$input" >> "${CLAUDE_USAGE_TICKS:-$HOME/.local/state/mara/ticks.raw}" 2>/dev/null || true
```

Работает: тики копятся. Ошибка записи строку состояния не роняет.

## Что осталось

1. **Прогнать самопроверку шиппера и поставить его в крон** (см. ниже).
2. **Агрегатор** (§4 брифа): глобальная лента тиков всех сессий, интервалы,
   калибровка весов на «чистых» интервалах (где активна ровно одна сессия),
   атрибуция Δ7d% по взвешенным токенам, дневные и недельные агрегаты.
3. **Отчёты** (§7): `Dashboard.md`, `Daily/`, `Weekly/`, `Models.md`,
   `Tools & MCP.md`, `Alerts.md`.
4. **Крон** на doctor и приёмка (§11).

Пункты 2–3 всё равно упираются в данные: калибровать веса не на чем, пока не
накопится хотя бы несколько дней тиков. Сейчас их десятки минут.

## Отступления от брифа

- **Хуков нет.** Обоснование выше. Если Claude Code перестанет писать что-то в
  транскрипт, вернуть хук — работа на полчаса.
- **SQLite-кэша нет** и, похоже, не понадобится: 535 транскриптов
  разбираются за полторы секунды, а сессий за год меньше тысячи. Заводить
  бинарник, который нельзя синкать и надо уметь пересобирать, ради такого
  объёма — лишняя деталь. Если разбор перевалит за десяток секунд, вернуться
  к этому.
- **Сырьё не копируется в `_data`.** Бриф просит складывать туда JSONL. Но
  транскрипты — уже append-only и уже в волте (`raw/claude-code/`), а это
  123 МБ; вторая копия удвоила бы синк ни за чем. Сырой слой — это они и тики,
  всё остальное пересобирается из них.
- **Вызовы инструментов не пишутся поштучно.** 535 сессий дали бы под
  миллион строк. Поимённо логируются только результаты тяжелее 20 КБ — из
  таких и состоит «куда ушёл контекст», — остальные учитываются суммой в
  записи сессии.

## Как проверить и доставить (Bash был заблокирован, руками)

```bash
cd ~/mara-second-brain
python3 scripts/claude-usage.py --self-check
python3 scripts/claude-usage-ship.py --self-check
python3 scripts/claude-usage-ship.py --no-push      # разобрать накопленные тики
head -1 ~/.local/state/mara/usage-spool/"Claude Usage"/_data/*/ticks-*.jsonl

git add -A && git commit -m "учёт лимитов: тики и снимок тулсета" && git push
```

Крон на машине с Claude Code (не на doctor — там Claude Code нет):

```
*/5 * * * * /usr/bin/python3 ~/mara-second-brain/scripts/claude-usage-ship.py >> ~/.local/state/mara/usage-ship.log 2>&1
```
