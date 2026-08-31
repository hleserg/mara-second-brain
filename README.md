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
git clone <this-repo> ~/mara-second-brain
~/mara-second-brain/install/stage0-doctor.sh
```

Скрипт идемпотентен — можно гонять повторно.

## Статус этапов

- [x] Этап 0 — фундамент (волт, git, зеркало, крон, Basic Memory)
- [ ] Этап 1 — Obsidian и человеческий слой
- [ ] Этап 2 — ingest сессий
- [ ] Этап 3 — Мара
- [ ] Этап 4 — автосвязи
- [ ] Этап 5 — остальные источники
- [ ] Этап 6 — гигиена
