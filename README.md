# mara-second-brain

Инфраструктура «второго мозга»: markdown-волт, который читается человеком в Obsidian,
служит памятью ассистенту Маре и даёт агентам контекст проектов.

**Это репозиторий инструментов, а не волта.** Волт — отдельный git-репозиторий на `doctor`
в `/srv/vault`, пушится только в bare-зеркало `/srv/backup/vault.git`. В GitHub он не уезжает
никогда: там `sensitive: true` и личный дневник.

ТЗ: [`docs/TZ.md`](docs/TZ.md).

## Топология

| Что | Где |
|---|---|
| Волт (source of truth) | `doctor:/srv/vault` |
| Bare-зеркало | `doctor:/srv/backup/vault.git` |
| Транспорт на устройства | Cloudflare R2 (Remotely Save / rclone) |
| Obsidian | Mac mini, Windows, телефон |
| Мара (Hermes) | Mac mini M1 |
| Тяжёлые батчи | GTR (WSL, GPU), по требованию |
| `beta-pi` | только разработка. Ничто в рантайме на неё не завязано (§13.6) |

## Установка на doctor

```bash
git clone git@github.com:hleserg/mara-second-brain.git ~/mara-second-brain
~/mara-second-brain/install/stage0-doctor.sh          # волт, git, зеркало, крон
~/mara-second-brain/install/basic-memory-doctor.sh    # Basic Memory + MCP
~/mara-second-brain/scripts/stage0-selftest.sh        # приёмка
```

Оба установщика идемпотентны. Креды R2 живут в `~/.config/rclone/rclone.conf`
на doctor и в репозиторий не попадают (ТЗ §11).

## Что крутится на doctor

| Что | Как |
|---|---|
| Синк с R2 | крон `*/5`, `scripts/vault-r2-sync.sh` (rclone bisync) |
| Автокоммит | крон `*/15`, `scripts/vault-git.sh commit` |
| Пуш в зеркало | крон `0 * * * *`, `scripts/vault-git.sh push` |
| Basic Memory MCP | systemd `basic-memory-mcp.service`, `127.0.0.1:8787` |

## Отступления от ТЗ и почему

- **Remotely Save → rclone bisync.** Remotely Save это GUI-плагин Obsidian,
  на headless doctor его не запустить. Тот же бакет, исключения из §2.1
  в `config/r2-filters.txt`.
- **MCP на 8787, а не 8765.** 8765 на doctor занят docker-proxy контейнера
  `caddy-letheclaw`.
- **Свой rclone в `/opt/rclone`.** Системный 1.60 (2022), его `bisync`
  разваливается. Общий бинарь не трогаем — на нём чужие бэкапы.
- **Семантический поиск Basic Memory выключен.** Он тянет fastembed с
  `bge-small-en-v1.5` — английская модель на русском волте. `bge-m3` из §8.2
  в fastembed нет; молча подменять модель не стал. Включится на этапе 1
  вместе с Ollama. Полнотекстовый поиск работает.
- **Структура §3 добавлена рядом с существующими папками волта**
  (`atman/`, `MeshCore/`, `Умный дом/`…), а не вместо них.

## Статус этапов

- [x] Этап 0 — фундамент (волт, git, зеркало, крон, Basic Memory). Приёмка пройдена.
- [ ] Этап 1 — Obsidian и человеческий слой
- [ ] Этап 2 — ingest сессий
- [ ] Этап 3 — Мара
- [ ] Этап 4 — автосвязи
- [ ] Этап 5 — остальные источники
- [ ] Этап 6 — гигиена
