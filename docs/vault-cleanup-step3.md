# Шаг 3: волт против репозиториев — разбор закончен

Считалка: `scripts/vault-vs-repo.py`. Прогнать заново — `python3 scripts/vault-vs-repo.py`.

**Прошлый вывод «байт-в-байт не совпадает ни один файл» был неверен.** Он мерил
шум, а не содержание: Basic Memory дописал во все заметки волта свой
фронтматтер, а какой-то линтер проставил язык у безымянных ` ``` ` → ` ```text `.
Если это снять, картина другая.

Второе, что ломало прошлый счёт: сравнивать надо с `origin/main`, а не с
клоном на doctor. Клон отставал на **933 коммита**, и на нём выходило, будто
волт новее репы. Он не новее.

## Что сравнивали

| | |
|---|---|
| `kb/howto/atman/**` | против `hleserg/atman`, `origin/main` |
| `kb/howto/smart-home/iot stack/**` | против `hleserg/ha-mqtt-sensor-hub`, `origin/main` |
| Всего файлов в волте | 64 |

Репа `atman` за это время сама убрала майские доки в `docs/archive/2026-05/` —
искали и там.

## Ответ

Вопрос шага 3 был один: **не потеряются ли правки, сделанные в Obsidian и не
вернувшиеся в репу.** Он решается механически — сверить волтовскую копию не с
сегодняшней репой, а с **каждой** исторической версией файла. Если хоть на
одной сходится в ноль, волт просто снимок того дня.

**45 файлов из 64 — чистые копии репы, терять нечего.**

| Категория | Файлов | Что делать |
|---|---:|---|
| Совпадают с сегодняшней репой | 28 | удалить из волта |
| Снимок конкретного коммита репы, своих правок ноль | 17 | удалить из волта |
| Есть строки, которых в репе не было никогда | 8 | в `archive/`, не удалять |
| В репе нет вовсе | 11 | оставить, это содержимое волта |

## Удалить: снимки репы (45)

Восстанавливаются из репы одной командой, плюс лежат в истории git волта.

<details>
<summary>Список</summary>

- `atman/GITHUB_AUTOMATIONS.md` — совпадает с `docs/archive/2026-05/GITHUB_AUTOMATIONS.md`
- `atman/MANIFEST-ru.md` — совпадает с `MANIFEST-ru.md`
- `atman/MANIFEST.md` — совпадает с `MANIFEST.md`
- `atman/architecture/DATADOG-LLM-OBSERVABILITY.md` — совпадает с `docs/archive/2026-05/DATADOG-LLM-OBSERVABILITY.md`
- `atman/deploy/atman-deploy/deploy/README.md` — совпадает с `deploy/atman-deploy/deploy/README.md`
- `atman/development/README_FACTUAL_MEMORY.md` — совпадает с `docs/archive/2026-05/README_FACTUAL_MEMORY.md`
- `atman/development/TEST_COVERAGE_PLAN.md` — совпадает с `docs/archive/2026-05/TEST_COVERAGE_PLAN.md`
- `atman/development/work-packages/01-factual-memory-adapter.md` — совпадает с `docs/archive/2026-05/01-factual-memory-adapter.md`
- `atman/development/work-packages/02-experience-store.md` — совпадает с `docs/archive/2026-05/02-experience-store.md`
- `atman/development/work-packages/03-identity-and-narrative.md` — совпадает с `docs/archive/2026-05/03-identity-and-narrative.md`
- `atman/development/work-packages/04-reflection-engine.md` — совпадает с `docs/archive/2026-05/04-reflection-engine.md`
- `atman/development/work-packages/05-session-manager.md` — совпадает с `docs/archive/2026-05/05-session-manager.md`
- `atman/development/work-packages/06-reality-and-affect.md` — совпадает с `docs/archive/2026-05/06-reality-and-affect.md`
- `atman/development/work-packages/07-ambient-and-proactive.md` — совпадает с `docs/archive/2026-05/07-ambient-and-proactive.md`
- `atman/development/work-packages/08-skill-manager.md` — совпадает с `docs/archive/2026-05/08-skill-manager.md`
- `atman/development/work-packages/09-background-agent.md` — совпадает с `docs/archive/2026-05/09-background-agent.md`
- `atman/development/work-packages/ISSUE_BACKLOG.md` — совпадает с `docs/archive/2026-05/ISSUE_BACKLOG.md`
- `atman/features/full-corpus-demo/README-ru.md` — совпадает с `docs/features/full-corpus-demo/README-ru.md`
- `atman/features/full-corpus-demo/README.md` — совпадает с `docs/features/full-corpus-demo/README.md`
- `atman/features/web-dashboard/README-ru.md` — совпадает с `docs/features/web-dashboard/README-ru.md`
- `atman/features/web-dashboard/README.md` — совпадает с `docs/features/web-dashboard/README.md`
- `atman/research/ATMAN_INTEGRATION_RESEARCH.md` — совпадает с `docs/archive/2026-05/ATMAN_INTEGRATION_RESEARCH.md`
- `atman/research/RESEARCH_FACTUAL_MEMORY.md` — совпадает с `docs/archive/2026-05/RESEARCH_FACTUAL_MEMORY.md`
- `atman/research/agent-thinking-comparison-ru.md` — совпадает с `docs/archive/2026-05/agent-thinking-comparison-ru.md`
- `atman/research/agent-thinking-comparison.md` — совпадает с `docs/archive/2026-05/agent-thinking-comparison.md`
- `atman/research/agent-ui-research.md` — совпадает с `docs/archive/2026-05/agent-ui-research.md`
- `atman/research/dashboard-research.md` — совпадает с `docs/archive/2026-05/dashboard-research.md`
- `smart-home/iot stack/BACKUP_RESTORE.md` — совпадает с `BACKUP_RESTORE.md`
- `atman/README-ru.md` — снимок `README-ru.md` от 2026-05-05 (`1943eee1`), проверено по 43 версиям
- `atman/README.md` — снимок `README.md` от 2026-05-05 (`1943eee1`), проверено по 47 версиям
- `atman/architecture/SYSTEM-ru.md` — снимок `docs/architecture/SYSTEM-ru.md` от 2026-05-14 (`744e08c3`), проверено по 8 версиям
- `atman/architecture/SYSTEM.md` — снимок `docs/architecture/SYSTEM.md` от 2026-05-14 (`744e08c3`), проверено по 7 версиям
- `atman/architecture/SYSTEM_MAP-ru.md` — снимок `docs/architecture/SYSTEM_MAP-ru.md` от 2026-05-05 (`13ec58a4`), проверено по 120 версиям
- `atman/architecture/SYSTEM_MAP.md` — снимок `docs/architecture/SYSTEM_MAP.md` от 2026-05-05 (`13ec58a4`), проверено по 124 версиям
- `atman/development/DEVELOPMENT_STANDARD.md` — снимок `docs/development/DEVELOPMENT_STANDARD.md` от 2026-05-05 (`69d70562`), проверено по 22 версиям
- `atman/features/experience-store/README-ru.md` — снимок `docs/features/experience-store/README-ru.md` от 2026-05-05 (`69d70562`), проверено по 8 версиям
- `atman/features/experience-store/README.md` — снимок `docs/features/experience-store/README.md` от 2026-05-05 (`69d70562`), проверено по 10 версиям
- `atman/features/factual-memory/README-ru.md` — снимок `docs/features/factual-memory/README-ru.md` от 2026-05-05 (`4b89f070`), проверено по 13 версиям
- `atman/features/factual-memory/README.md` — снимок `docs/features/factual-memory/README.md` от 2026-05-05 (`4b89f070`), проверено по 10 версиям
- `atman/features/identity-store/README-ru.md` — снимок `docs/features/identity-store/README-ru.md` от 2026-05-05 (`4b89f070`), проверено по 6 версиям
- `atman/features/identity-store/README.md` — снимок `docs/features/identity-store/README.md` от 2026-05-05 (`4b89f070`), проверено по 6 версиям
- `atman/features/reflection-engine/README-ru.md` — снимок `docs/features/reflection-engine/README-ru.md` от 2026-05-04 (`c3891267`), проверено по 9 версиям
- `atman/features/reflection-engine/README.md` — снимок `docs/features/reflection-engine/README.md` от 2026-05-04 (`c3891267`), проверено по 9 версиям
- `atman/features/session-manager/README-ru.md` — снимок `docs/features/session-manager/README-ru.md` от 2026-05-05 (`c5b7340f`), проверено по 7 версиям
- `atman/features/session-manager/README.md` — снимок `docs/features/session-manager/README.md` от 2026-05-05 (`c5b7340f`), проверено по 7 версиям

</details>

## В `archive/`: свои строки есть (8)

Не удалять. Эти строки не существуют ни в одной версии репы — единственная
копия в волте.

| Файл | Своих строк | Что там |
|---|---:|---|
| `smart-home/iot stack/MESHCORE.md` | 60 | «железо не подключено» + готовая автоматизация `meshcore_mirror_to_mqtt` целиком |
| `smart-home/iot stack/ACCEPTANCE.md` | 37 | живые цифры аудита: 70 сущностей в реестре, 38 от стека, почему 14 в `unknown` |
| `smart-home/iot stack/ARCHITECTURE.md` | 19 | разбор сети doctor: наружу открыт только 443 на рабочий стенд, VPN нет, `cf tunnel` временный |
| `smart-home/iot stack/README.md` | 10 | десять строк ранней редакции |
| `atman/development/ROADMAP.md` | 7 | то же: пакеты работ под локальный qwen3 |
| `atman/MEMORY-ARCHITECTURE.md` | 6 | план на локальные Ollama-модели `qwen3-embedding:1.5b` (768d) и `qwen3:14b` — репа этот выбор не сохранила |
| `atman/ENDPOINTS.md` | 4 | адреса стенда: WSL `<адрес стенда>:3000`, LAN |
| `smart-home/iot stack/TROUBLESHOOTING.md` | 1 | одна строка, скорее всего шум |

По пяти файлам `iot stack` репа **длиннее** волта (например `MESHCORE.md` — 345
строк против 185) и написана позже: у неё один-единственный коммит от 20 августа,
«Initial commit: iot-stack as deployed on doctor». То есть волт держит более
раннюю редакцию, которую при заливке в репу переписали. Канон — репа, но
переезд в `archive/` ничего не стоит и эти строки сохраняет.

## Оставить в волте: в репе нет вовсе (11)

- `atman/benchmarks/BENCHMARKS.md`
- `atman/benchmarks/EXTERNAL_BENCHMARKS.md`
- `atman/deploy/УСТАНОВКА.md`
- `atman/development/README.md`
- `atman/development/work-packages/README.md`
- `atman/digest-prompt.md`
- `atman/ideas/project-blocks-after-manifest-and-system.md`
- `atman/Клянчим кредитки на атман.md`
- `atman/Форма подачи на подачки.md`
- `smart-home/iot stack/ADDING_SENSOR.md`
- `smart-home/iot stack/MQTT_TOPICS.md`

Из них `atman/development/README.md` и `atman/development/work-packages/README.md`
попали сюда только потому, что считалка отказывается матчить неоднозначное имя:
в репе десяток файлов `README.md`, и брать первый попавшийся хуже, чем не брать
ничего. Скорее всего это архивные доки репы. Проверить глазами — две минуты.

Остальное — настоящее содержимое волта: `Клянчим кредитки на атман.md`,
`Форма подачи на подачки.md`, `digest-prompt.md`, бенчмарки, `УСТАНОВКА.md`,
`MQTT_TOPICS.md`, `ADDING_SENSOR.md`.

## ⚠️ Перед удалением

`vault-r2-sync.sh` стоит с `--max-delete 25`. Удаление 45 файлов разом уронит
прогон синка. Либо партиями по 20, либо один надзорный прогон с поднятым
потолком.

Удаление уезжает через R2 на телефон и мак — поэтому ничего не сделано, жду
отмашки.
