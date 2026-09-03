# Mara — управляющее техническое задание и манифест

> Версия: 1.0  
> Дата фиксации: 2026-09-03  
> Основной исполнитель: Claude Code  
> Независимый ревьюер: Codex  
> Статус документа: единый источник требований для следующего этапа разработки

---

## 0. Назначение документа

Этот документ одновременно является:

1. продуктовым манифестом Mara;
2. архитектурным решением;
3. техническим заданием для Claude Code;
4. перечнем уже реализованного и известных рисков;
5. программой hardening;
6. планом полевых испытаний;
7. набором обязательных критериев приёмки;
8. правилами разработки и управления агентами.

Claude Code должен сначала изучить живой репозиторий и сопоставить его с этим документом. Этот документ задаёт направление и инварианты, но не разрешает слепо переписывать рабочий код. Если названия модулей, таблиц или API в репозитории отличаются, следует сохранить удачные существующие границы и составить явную карту соответствия.

При противоречии между старой документацией проекта и этим документом приоритет имеет этот документ. При противоречии между документом и фактическими данными/полевым экспериментом приоритет имеет воспроизводимый факт; расхождение нужно зафиксировать в PR и запросить решение, не маскируя его предположением.

### 0.1. Правила интерпретации

Ключевые слова `MUST`, `MUST NOT`, `SHOULD`, `MAY` имеют нормативный смысл:

- **MUST / ОБЯЗАН** — обязательное требование;
- **MUST NOT / ЗАПРЕЩЕНО** — недопустимое поведение;
- **SHOULD / СЛЕДУЕТ** — стандартное решение, отклонение требует объяснения;
- **MAY / МОЖЕТ** — опциональная возможность.

Нельзя выдавать гипотезу, обнаруженный пункт интерфейса или неподтверждённую возможность стороннего приложения за готовую интеграцию.

---

## 1. Видение: Mara как local-first Personal Memory OS

Mara — не чат-бот, не заметочник, не очередной task manager и не оболочка вокруг LLM. Это **local-first Personal Memory OS**: личная операционная система памяти, которая непрерывно принимает события из жизни пользователя, сохраняет доказательства, выделяет сущности, решения и обязательства, поддерживает их актуальное состояние и предоставляет один согласованный контекст людям, интерфейсам и агентам.

Mara должна отвечать на четыре вопроса:

1. **Что произошло?** — исходное событие и его evidence.
2. **Что это значит?** — структурированные факты, участники, решения, обязательства и связи.
3. **Что актуально сейчас?** — текущее состояние с учётом исправлений, отмен, завершений и новых данных.
4. **Почему Mara так считает?** — происхождение, версия, история изменения и ссылка на доказательство.

### 1.1. Продуктовые принципы

1. **Local-first, not local-only.** Канонические личные данные и вычисления по умолчанию находятся на инфраструктуре пользователя. Удалённые сервисы допустимы только как явно выбранные транспортные или интеграционные адаптеры, с минимизацией данных.
2. **Evidence before inference.** Любой важный вывод должен быть связан с проверяемым источником.
3. **Structured objects, not only chunks.** Люди, проекты, разговоры, сообщения, решения и обязательства являются объектами с идентичностью и жизненным циклом, а не только текстом для embedding-поиска.
4. **Corrections are first-class.** Исправление пользователя — новая ревизия канонического объекта с аудируемой причиной, а не тихое редактирование файла.
5. **One authority.** Каноническое состояние существует в одном месте. Проекции, индексы и кэши можно удалить и пересобрать.
6. **Offline-tolerant and idempotent.** Потеря сети и повторная доставка не должны терять или дублировать события.
7. **Observable by default.** Ошибка источника, очереди, worker-а или проекции должна быть видна до того, как пользователь случайно обнаружит пропажу данных.
8. **Replaceable adapters.** Источники, ASR, extraction, retrieval, UI и агенты подключаются через стабильные границы.
9. **Progressive complexity.** FTS5 и явные связи — базовый путь. Vector DB, graph DB, Meilisearch, Graphiti, Langfuse и подобные компоненты добавляются только после измеренного недостатка текущей системы.
10. **Human sovereignty.** Пользователь может просмотреть, исправить, экспортировать, восстановить и удалить свои данные без зависимости от конкретной модели или облака.

### 1.2. Что Mara не должна становиться

- монолитным «магическим агентом», у которого память хранится только в prompt/history;
- Markdown-only системой со скрытым split-brain между файлами, UI и агентами;
- vector-first хранилищем без стабильных объектов и причинно-следственной истории;
- набором источников, каждый из которых напрямую пишет собственный формат в vault;
- облачной транскрипцией личных разговоров по умолчанию;
- самописным аналогом Notion, Jira или Grafana без доказанной необходимости;
- системой, где агент может без лимита плодить агентов, расходовать бюджет и напрямую менять `main`.

---

## 2. Текущее состояние проекта после ambient-memory реализации

Перед изменениями Claude Code ОБЯЗАН подтвердить этот раздел по текущему `HEAD`, тестам и документации. Ниже зафиксировано состояние, установленное последним аудитом; это baseline, а не повод доверять старому отчёту вместо кода.

### 2.1. Уже реализовано или продемонстрировано в коде

- `contextd` как локальный сервис ядра;
- durable queue с lease, retry и DLQ;
- отдельное blob-хранилище звонков вне Markdown vault;
- ingestion аудио, проверка SHA-256 и последующая обработка;
- локальный ASR pipeline;
- локальный extraction через Ollama;
- карточки conversations и commitments;
- post-call digest;
- retention/reconciliation механизмы;
- Android-приложение Mara Capture;
- личный Telegram через TDLib;
- личный Gmail;
- WhatsApp через NotificationListener и импорт экспортов;
- SMS ingestion;
- Context Broker / `context_pack` для Hermes;
- Hermes `pre_llm_call`, получение времени от Doctor, кэширование и fail-open;
- `mara_correction` для пользовательских команд наподобие «сделано», «отмени», «срок пятница»;
- защита токенов устройств хешированием;
- loopback-only привязка `contextd`;
- исключение тел сообщений и транскриптов из обычных логов;
- потоковая отправка файла со стороны Android-клиента;
- набор Python/JVM/self-check тестов, который на момент прошлого аудита насчитывал примерно 184 Python-теста и 32 JVM-теста, но эти числа не являются текущим acceptance criterion.

### 2.2. Подтверждённые слабые места baseline

1. Возможны коллизии имён Markdown-карточек: одинаковый slug в один день или два звонка одному контакту в одну минуту могут привести к перезаписи.
2. Идентичность местами не отделена от filename.
3. Нет полноценной диаризации; сегменты могли получать условного `unknown-A`, что делает атрибуцию обещаний ненадёжной.
4. Context Broker фактически покрывает прежде всего открытые commitments; people/project packs, decisions, blockers и changed instructions ещё не являются доказанно полноценным контуром.
5. Старые context packs могут оставаться в истории длинной Hermes-сессии и конфликтовать с более новой версией.
6. Evidence проверяется слабее необходимого: timestamp сам по себе не доказывает существование соответствующего ASR-сегмента.
7. WhatsApp dedupe по `package + chat + sender + text + minute` способен поглотить два реальных одинаковых сообщения в одну минуту.
8. Серверная сторона upload ранее читала request body целиком в память, потенциально до 512 MiB, несмотря на streaming со стороны Android.
9. Полный реальный E2E-контур не был доказан для каждого источника и всех отказов.
10. Большой объём изменений поступал напрямую в `main`; обязательные branch protection/CI checks и PR-only governance не были доказаны.

### 2.3. Обязательное первое действие Claude Code

До реализации новых функций создать `docs/current-state-audit.md` либо эквивалентный раздел в первом PR, где для каждого пункта 2.1–2.2 указать:

- `confirmed`, `partially confirmed`, `not found` или `already fixed`;
- точные файлы/модули;
- соответствующие тесты;
- найденные расхождения;
- миграционный риск.

Нельзя начинать с Control Plane или новых коннекторов, не закрыв критические риски целостности и восстановления.

---

## 3. Целевая архитектура верхнего уровня

```text
Sources / Capture
  Phone calls | Telegram | Gmail | WhatsApp | SMS | future adapters
                         |
                         v
              Ingest API + Blob Store
                         |
                         v
              Normalization / Event Log
                         |
                         v
         SQLite Memory Ledger (AUTHORITY)
              |          |          |
              |          |          +--> Job Queue / Compute Scheduler
              |          |                         |
              |          |                 Big PC / GTR workers
              |          |
              |          +--> Search indexes / derived relations
              |
              +--> Markdown Projector --> human-readable vault
              |
              +--> Context Broker --> MCP Gateway --> agents
              |
              +--> Control Plane / PWA / Reports / Widgets
```

### 3.1. Основные bounded contexts

| Контекст | Ответственность | Не владеет |
|---|---|---|
| Capture adapters | получить событие/файл и минимальные метаданные | канонической памятью |
| Ingest | аутентификация, streaming, hash, staging, idempotency | смысловой интерпретацией |
| Memory Ledger | канонические сущности, ревизии, связи, provenance | UI-представлением |
| Processing | ASR, diarization, extraction, enrichment | прямым редактированием проекций |
| Projector | детерминированный Markdown и другие read models | собственной authority |
| Context Broker | свежий bounded context для агентов | скрытым изменением памяти |
| MCP Gateway | стабильный agent-facing контракт и политики | обходом доменной логики |
| Control Plane | управление, аудит, health, review queues | отдельной БД истины |
| Agent Observatory | трассы, расходы, изменения, approvals | содержимым личных данных сверх необходимости |
| Compute Scheduler | маршрутизация тяжёлых задач | каноническим состоянием задач памяти |

---

## 4. Source of truth: SQLite Memory Ledger

### 4.1. Архитектурное решение

**SQLite Memory Ledger является единственным authority для структурированной памяти Mara. Markdown — обязательная человекочитаемая, версионируемая, полностью перестраиваемая проекция.**

Запрещено:

- считать filename идентификатором объекта;
- напрямую редактировать Markdown и считать это каноническим изменением без явного import/correction flow;
- позволять UI, MCP, агенты или source adapters писать в обход доменного write API;
- хранить часть актуального состояния только в Markdown frontmatter, а часть только в SQLite;
- исправлять проекцию вручную вместо устранения причины и rebuild.

### 4.2. Минимальные сущности ledger

Физическая схема может адаптироваться к репозиторию, но обязана поддерживать как минимум:

- `source_events` — неизменяемые нормализованные входные события;
- `blobs` — метаданные бинарных объектов, hash, размер, MIME, storage location;
- `conversations` — звонки и диалоги;
- `messages` — сообщения с source-native identity;
- `transcripts` и `transcript_segments`;
- `entities` — person, organization, project, place и расширяемые типы;
- `entity_aliases`;
- `commitments`;
- `decisions`;
- `facts` / `claims` с confidence и validity;
- `evidence_refs`;
- `relations`;
- `revisions` или эквивалентная история версий;
- `corrections`;
- `projection_state`;
- `ingest_attempts` / idempotency receipts;
- `jobs`, `job_attempts`, DLQ;
- `audit_events`;
- `provider_health` и `alerts`;
- `compute_nodes` и capability/heartbeat state.

### 4.3. Stable IDs

Каждый канонический объект ОБЯЗАН иметь immutable stable ID, например UUIDv7/ULID. Выбор единого формата нужно оформить ADR; формат должен быть сортируемым по времени или сопровождаться отдельным `created_at`.

Правила:

- `id` не меняется при rename, merge, correction, перемещении файла или rebuild;
- source-native ID хранится отдельно вместе с `source_system` и `source_account`;
- для источника без стабильного ID создаётся детерминированный fingerprint из достаточного набора полей плюс collision discriminator;
- content hash не заменяет object ID;
- blob hash идентифицирует содержимое, но не бизнес-событие;
- короткий stable ID MAY входить в filename проекции, например `2026-09-03-send-estimate--01J...md`;
- две разные записи с одинаковым текстом не дедуплицируются без доказательства, что это одно событие.

### 4.4. Идемпотентность и дедупликация

Каждая ingest-операция ОБЯЗАНА принимать `idempotency_key`. Сервер сохраняет итог операции и при повторе возвращает тот же результат.

Дедупликация строится слоями:

1. точный source-native ID, если он существует;
2. exact blob SHA-256 для того же provider/context;
3. детерминированный source fingerprint;
4. probabilistic near-duplicate detection только как review suggestion, не как автоматическое удаление.

Нельзя дедуплицировать два WhatsApp-сообщения только потому, что совпали sender, text и минута. Для NotificationListener и export import нужно хранить оба наблюдения, а связь `same_as` подтверждать точным правилом или review.

### 4.5. Revisions и optimistic concurrency

Каждая изменяемая сущность ОБЯЗАНА иметь монотонную `version` и историю ревизий. Любой update/transition принимает `expected_version`.

Пример контракта:

```json
{
  "entity_id": "01J...",
  "expected_version": 7,
  "operation": "reschedule_commitment",
  "payload": {"due_at": "2026-09-05T15:00:00+03:00"},
  "reason": "user_correction",
  "actor": {"type": "human", "id": "owner"}
}
```

Если текущая версия не равна `expected_version`, сервер возвращает структурированный `409 Conflict`:

```json
{
  "error": "version_conflict",
  "entity_id": "01J...",
  "expected_version": 7,
  "current_version": 8,
  "current": {},
  "attempted_patch": {},
  "conflict_id": "01J..."
}
```

Конфликты:

- не разрешаются last-write-wins для смысловых полей;
- повторяемая idempotent-команда может быть автоматически признана уже применённой;
- независимые commutative изменения MAY автоматически объединяться по чётким правилам;
- смысловой конфликт попадает в review queue Control Plane;
- агент не имеет права самовольно выбирать победителя при неоднозначности;
- каждая резолюция фиксирует actor, timestamp, before/after, reason и evidence.

### 4.6. Жизненный цикл обязательства

Минимальные состояния: `proposed`, `open`, `blocked`, `done`, `cancelled`, `superseded`. Переходы выполняются доменными командами, а не произвольной заменой строки `status`.

Commitment должен хранить:

- immutable ID;
- actor/owner;
- beneficiary/requester, если известен;
- action;
- due date/time и исходную формулировку срока;
- status;
- source conversation/message;
- evidence refs;
- confidence;
- created/updated timestamps;
- current version;
- supersedes/superseded_by;
- extraction model/version и rule/prompt version;
- историю corrections.

### 4.7. Evidence и provenance

Evidence — не декоративная ссылка. `evidence_ref` ОБЯЗАН ссылаться на существующий объект и валидный диапазон.

Для аудио:

- `blob_id`;
- `transcript_id`;
- существующий `segment_id` или набор segment IDs;
- `start_ms`, `end_ms`, проверенные относительно duration и сегмента;
- optional speaker ID/role;
- hash/version транскрипта.

Для сообщений:

- `source_event_id` / `message_id`;
- source system/account/chat;
- timestamp;
- immutable raw-observation reference.

Для derived claim:

- producer (`rule`, `model`, `human`);
- producer version/model identifier;
- prompt/rule version;
- input IDs и их revisions;
- `created_at`;
- confidence;
- validation status.

Если модель ссылается на несуществующий segment ID или диапазон вне записи, derived object отклоняется либо отправляется на review; он не становится каноническим commitment.

### 4.8. Markdown projection

Markdown остаётся важной частью продукта:

- читается человеком без Mara;
- удобно просматривается и версионируется;
- служит переносимым экспортом;
- пригоден как bounded input для инструментов.

Но Markdown генерируется projector-ом из ledger. Требования:

- deterministic ordering и stable paths;
- stable ID во frontmatter и, где полезно, в filename;
- `ledger_version`, `projected_at`, schema/projector version;
- atomic temp-write + rename;
- manifest с hash проекций;
- incremental rebuild и full rebuild;
- удаление/переименование проекции только на основании ledger;
- drift detector: ручные изменения выявляются, не затираются молча;
- документированный import/correction flow, если пользователь хочет внести правку из Markdown;
- тест: пустой vault полностью восстанавливается из backup ledger + blobs.

Search indexes, embeddings, entity graph и context packs также являются rebuildable derived state.

---

## 5. SQLite hardening

SQLite выбран осознанно как authority одного пользователя на Doctor. Это не означает «один файл без эксплуатационной дисциплины».

### 5.1. Обязательные настройки и инварианты

- WAL mode, если filesystem и backup strategy это безопасно поддерживают;
- `foreign_keys=ON` на каждом соединении;
- разумный `busy_timeout`;
- явные транзакции;
- один контролируемый write path / writer discipline;
- migrations с номером schema version и transactional safety;
- запрет destructive auto-migration без backup;
- prepared statements;
- ограничения `NOT NULL`, `UNIQUE`, `CHECK`, FK там, где инвариант известен;
- timestamps в UTC плюс сохранение исходной timezone/offset, где это семантически важно;
- `PRAGMA integrity_check`/`quick_check` по расписанию;
- мониторинг WAL size, disk free, lock contention и migration state;
- корректный shutdown, но восстановление не должно зависеть от clean shutdown;
- тесты power-loss/kill/retry вокруг критических транзакций.

Конкретные PRAGMA и режим синхронизации нужно выбрать по измерениям и документировать ADR. Для канонического ledger приоритет — durability, а не максимальный throughput.

### 5.2. Транзакционные границы

Следующие операции должны быть атомарны:

- регистрация source event + idempotency receipt;
- создание/обновление объекта + revision + audit event;
- commitment transition + correction provenance;
- успешное завершение job + запись результата/outbox event;
- принятие blob metadata после fsync/rename staging file;
- обновление projection checkpoint только после успешной записи проекций.

Для внешних side effects использовать transactional outbox либо эквивалентный механизм. Нельзя отмечать upload/job успешным до durable commit результата.

### 5.3. Backup и restore

Backup считается рабочим только после проверенного restore.

Обязательный состав:

- согласованный online backup SQLite через backup API или корректную snapshot-процедуру;
- blob store либо инкрементальный manifest всех blobs с hash;
- secrets/config отдельно, зашифрованно и с документированным восстановлением;
- schema/app version;
- backup manifest с hash, временем, host и retention class;
- копия за пределами Doctor; желательно versioned/offsite encrypted copy.

Минимальная политика должна быть настраиваемой, но baseline:

- частый локальный snapshot ledger;
- ежедневный полный проверяемый backup;
- grandfather-father-son либо эквивалентная retention;
- автоматическая проверка checksum;
- регулярная restore drill в изолированную директорию;
- отчёт о последнем успешном backup и последнем успешном restore test в Control Plane.

Restore runbook ОБЯЗАН описывать:

1. остановку writers;
2. проверку manifest/hash;
3. восстановление ledger и blobs;
4. migrations при необходимости;
5. integrity check;
6. rebuild Markdown/search/context projections;
7. reconciliation blob↔ledger;
8. проверку stable IDs и sample evidence;
9. безопасный возврат сервиса.

RPO/RTO нужно измерить и зафиксировать после первого restore drill; до этого нельзя заявлять production backup readiness.

---

## 6. Ingest и потоковая загрузка

### 6.1. Streaming end-to-end

Сервер ОБЯЗАН принимать большие аудиофайлы потоково:

- читать request body bounded chunks;
- писать в уникальный staging `.part` файл;
- одновременно считать SHA-256 и количество байт;
- проверять declared size/hash и server limits;
- не держать файл целиком в RAM;
- выполнять fsync и atomic rename только после полной валидации;
- регистрировать blob и source event транзакционно;
- удалять или quarantining незавершённые части по TTL;
- возвращать стабильный receipt;
- поддерживать безопасный retry с тем же idempotency key.

Для нестабильной мобильной сети SHOULD быть resumable upload либо chunk protocol с authenticated upload session. Если это откладывается, клиент обязан надёжно повторять полный upload, а сервер — не создавать дубликаты.

### 6.2. Безопасность ingest

- TLS или защищённый overlay network, например Tailscale;
- per-device credentials, хранящиеся на сервере только в безопасном виде;
- rotation/revocation;
- request authentication и replay protection;
- лимиты размера, времени, concurrent uploads и MIME allowlist;
- content sniffing; расширению файла доверять нельзя;
- quarantine для повреждённых/неподдерживаемых файлов;
- никаких transcript/body/token в обычных логах;
- audit metadata без утечки содержимого;
- rate limiting и backpressure.

### 6.3. Reconciliation

Периодический reconciler проверяет:

- blob в ledger есть на диске и совпадает по hash;
- orphan blob не теряется молча;
- source event имеет ожидаемый pipeline state;
- зависшие lease возвращаются в очередь;
- exhausted retries попадают в DLQ и видимый alert;
- projection checkpoint соответствует ledger revision.

---

## 7. Recorder Provider Layer

### 7.1. Факты полевого исследования — не переинтерпретировать

Целевое устройство:

- **Huawei Pura 70 Pro**;
- модель **HBN-LX9**;
- **EMUI 14.2**;
- в данной прошивке **нет системной записи звонков**.

Следовательно, Huawei OEM recorder не является доступным источником на этом устройстве. Mara Capture не должна предполагать наличие папки OEM-записей.

Подтверждённые результаты:

| Provider | Обычная трубка | Громкая связь | Текущий вывод |
|---|---|---|---|
| Cube ACR | собеседник слышен плохо/очень тихо | обе стороны слышны хорошо | технически пишет, но не годится как reliable default для обычных звонков |
| ACR Phone + APH | обе стороны записываются нормально | требуется включить в матрицу повторных тестов | временно рабочий provider |
| Huawei system recorder | отсутствует | отсутствует | использовать нельзя |

### 7.2. ACR Phone + APH: зафиксированная конфигурация

- automatic call recording: ON;
- delay: **500 ms**;
- quality: **Critical**;
- bitrate: **192 kb/s**;
- sample rate: **48 kHz**;
- increase volume during call: ON;
- auto-enable speakerphone: OFF;
- autodelete: OFF;
- storage: **`/Documents/ACR/ACRPhone`**;
- APH/helper участвует в рабочей схеме.

Главное ограничение: **после reboot механизм записи слетает**. Это известное свойство текущей схемы. Поэтому ACR Phone + APH — не финальный reliable source, а временно рабочий provider со статусом `degraded / reboot-unsafe`.

Нельзя закрывать recorder milestone на основании одного успешного звонка до перезагрузки.

### 7.3. Неизвестные возможности ACR Phone

В интерфейсе виден раздел **«Отчёт на удалённый сервер»** с полями **Server** и **Secret**. На текущем этапе:

- протокол неизвестен;
- payload неизвестен;
- authentication/signature semantics неизвестны;
- доставка recording file не подтверждена;
- retry/idempotency/TLS behavior не подтверждены.

**Запрещено называть это webhook или строить production-архитектуру вокруг него до исследования.** Создать отдельную research task: изучить официальную документацию, легально снять сетевой trace в контролируемом тесте, определить protocol/payload/security/retry и оформить ADR с verdict `adopt`, `experiment only` или `reject`.

Также интерфейс показывает варианты передачи/интеграции:

- SFTP;
- WebDAV;
- FTP;
- FTPS;
- Box;
- Dropbox;
- OneDrive;
- email;
- device;
- варианты транскрипции.

Все они — только кандидаты на отдельную проверку. Наличие пункта меню не доказывает, что он работает автоматически после звонка, после reboot, в фоне, через Tailscale, с retry или с нужным форматом.

Production policy:

- встроенную cloud transcription не использовать;
- аудио и полные транскрипты не отправлять в стороннее облако без отдельного явного решения;
- plain FTP не принимать как production transport;
- cloud storage providers не делать authority;
- SFTP/WebDAV/FTPS MAY быть приняты только после security и reliability tests.

### 7.4. Provider abstraction

Mara Capture должен работать через интерфейс `RecorderProvider`, а не через hardcoded ACR Phone path.

Концептуальный контракт:

```text
provider_id
provider_version
capabilities
health()
list_candidates(since_cursor)
open_recording(candidate)
extract_metadata(candidate)
acknowledge_ingested(candidate, receipt)
diagnostics()
```

Capability manifest должен описывать, не обещая неподтверждённого:

```json
{
  "provider_id": "acr_phone_aph",
  "status": "degraded",
  "capture_modes_tested": ["cellular_earpiece"],
  "storage_mode": "folder",
  "storage_path": "/Documents/ACR/ACRPhone",
  "requires_helper": true,
  "requires_post_reboot_recovery": true,
  "remote_report_protocol": "unknown",
  "production_ready": false
}
```

Provider discovery не должен автоматически доверять пути. Требуются folder permission, readable probe и реальные post-call observations.

### 7.5. Сопоставление CallLog ↔ recording

Для каждого звонка хранить отдельное call observation. Matching использует:

- direction;
- normalized phone number / contact identity;
- call start/end/duration;
- file creation/modification time;
- duration audio;
- provider metadata/filename;
- tolerance window с настраиваемыми пределами.

Результат matching: `matched`, `ambiguous`, `missing`, `orphan_recording`. Ambiguous никогда не связывается молча; он идёт на review. Нельзя считать отсутствие записи нормальным только потому, что provider folder доступен.

---

## 8. Recorder Health Monitor

### 8.1. Цель

Проблема должна обнаруживаться сразу после reboot или первого пропущенного вызова, а не через неделю. Health — это не только «процесс запущен» и «папка существует». Главный synthetic business signal: **реальный завершённый звонок породил читаемый аудиофайл ожидаемого качества и он дошёл до Doctor**.

### 8.2. Триггеры проверок

Mara Capture ОБЯЗАН запускать health workflow:

- после `BOOT_COMPLETED` и, при необходимости, `LOCKED_BOOT_COMPLETED`/первого unlock;
- после обновления приложения;
- при старте/возврате Mara Capture;
- регулярно через поддерживаемый Android scheduler;
- после каждого обнаруженного завершённого звонка;
- после изменения permissions/provider configuration;
- при длительном отсутствии heartbeat/upload.

Нельзя обещать мгновенный background execution там, где Android/EMUI его не гарантирует. Реальные ограничения и задержки измерить, а UI должен показывать timestamp последней проверки.

### 8.3. Что проверять

Где Android API или provider позволяют, проверять:

- выбранный provider и его declared status;
- доступ к `/Documents/ACR/ACRPhone` через актуальный Android storage mechanism;
- сохранённый и действующий URI permission;
- наличие папки и возможность читать новые файлы;
- время последнего CallLog event;
- время последней найденной записи;
- время последнего успешного upload/ingest;
- очередь pending uploads и oldest age;
- permissions телефона/CallLog/notifications/storage, необходимые конкретной реализации;
- Accessibility/helper/APH state, если доступно легальным публичным API;
- Shizuku state, только если он фактически используется выбранным provider и статус можно получить надёжно;
- battery optimization exemption;
- EMUI app launch/background settings, насколько их можно диагностировать;
- свободное место;
- provider package installed/enabled/version;
- worker scheduling heartbeat.

Если состояние APH/helper/Shizuku нельзя программно прочитать, UI обязан показывать `unknown`, а не `healthy`, и дать короткий guided check. Нельзя использовать недокументированные опасные обходы лишь ради зелёной иконки.

### 8.4. Правило `last call vs last recording`

После завершённого реального звонка стартует bounded observation window. Если в допустимое время нет подходящей записи:

1. создать durable alert `recording_missing_after_call`;
2. сохранить call observation и диагностический snapshot;
3. повторить scan с backoff;
4. не создавать дубликаты одного alert;
5. показать warning на телефоне и в Control Plane;
6. не помечать звонок успешно захваченным;
7. после появления файла закрыть warning как recovered, сохранив историю.

Таймаут должен учитывать delay рекордера и завершение записи файла, но быть измеренным и ограниченным. Значение вынести в конфигурацию; baseline предложить по результатам field tests.

### 8.5. Health state model

- `healthy` — есть свежий успешный synthetic/real evidence;
- `degraded` — capture работает, но есть известный риск (например reboot-unsafe) или часть состояния неизвестна;
- `unhealthy` — звонок был, записи нет; permission/path/helper сломан; очередь критически просрочена;
- `unknown` — доказательств недостаточно;
- `recovering` — устранение/повторная доставка в процессе.

Статус не может быть `healthy` только из-за последней записи недельной давности, если после неё были звонки.

### 8.6. Post-reboot UX

После reboot Mara Capture должен:

1. восстановить собственный scheduler/queue;
2. проверить provider, permissions и folder access;
3. показать persistent warning, что ACR Phone + APH имеет известный reboot risk;
4. провести доступные automatic checks;
5. предложить минимальную пошаговую инструкцию ручного восстановления только для того, что нельзя сделать программно;
6. считать provider `degraded/unknown`, пока контрольный реальный звонок не создаст запись;
7. зафиксировать результат post-reboot контрольного звонка.

Отдельная P0 research/engineering задача: найти более надёжный и воспроизводимый способ восстановления/автозапуска записи после reboot. Решение оценивается по полевым данным, безопасности, поддерживаемости и отсутствию постоянного ручного шаманства. До результата нельзя заявлять «поставил и забыл».

---

## 9. Processing pipeline: ASR, diarization, extraction

### 9.1. Общий pipeline

```text
accepted blob
  -> media validation
  -> audio normalization (derived, original preserved)
  -> ASR with timestamps
  -> diarization / speaker attribution
  -> extraction
  -> evidence validation
  -> canonical proposals or auto-commit by policy
  -> projections/context invalidation
```

Каждый результат хранит model name/version, configuration, input hash и pipeline version. Reprocessing создаёт новую derived revision; original blob не меняется.

### 9.2. Speaker separation

Цель — различать как минимум `owner` и `other`, а где возможно — конкретных людей. Нельзя интерпретировать фразы «Анна попросила Сергея» и «Сергей обещал Анне» без учёта speaker/evidence.

Требования:

- сегменты с timestamps;
- diarization confidence;
- возможность `unknown` без выдумывания личности;
- ручная коррекция speaker labels;
- сохранение correction для будущего reprocessing;
- benchmark на реальных тестовых звонках разных режимов;
- extraction policy снижает confidence или отправляет на review при неясной атрибуции.

### 9.3. Extraction

- локальное выполнение по умолчанию;
- schema-constrained output;
- строгая валидация IDs/evidence/timestamps;
- никаких канонических объектов из невалидного output;
- versioned prompts/rules;
- regression corpus без раскрытия личных данных;
- явное различие между `fact`, `inference`, `suggestion` и `commitment`;
- negative examples для отрицаний, условных обещаний, цитирования чужой речи и отмен.

---

## 10. Context Broker и свежесть контекста

Context Broker создаёт bounded, актуальные и объяснимые context packs. Он не является source of truth.

Минимальные packs:

- `now`: открытые commitments, blockers, ближайшие сроки, recent decisions, changed instructions, критические alerts;
- `person/<id>`: aliases, актуальные отношения/обязательства/последние релевантные события;
- `project/<id>`: состояние, решения, blockers, commitments;
- query-specific retrieval pack.

Каждый pack содержит:

- `pack_id`;
- `pack_type`;
- `generated_at`;
- `ledger_revision` или high-water mark;
- `version`;
- `supersedes`;
- expiry/staleness policy;
- source object IDs и versions;
- privacy classification;
- token/size budget.

### 10.1. Stale-context policy

- при correction/transition затронутые packs инвалидируются;
- Hermes и другие агенты получают свежий overlay для каждого meaningful turn;
- старый pack не должен конкурировать с новым как равноправная инструкция;
- если клиент не умеет удалить старый context из history, новый pack явно содержит `supersedes` и компактный current-state override;
- destructive/important action агент выполняет после чтения актуальной версии через MCP, а не по старому chat context;
- stale pack должен быть обнаружим по ledger high-water mark.

---

## 11. MCP Gateway

MCP — основной agent-facing слой Mara. Claude, Codex, Hermes и будущие клиенты используют один доменный backend, что и Control Plane.

Минимальные capabilities:

- query current context;
- search entities/conversations/messages;
- read object с revision/provenance/evidence;
- propose correction;
- execute version-checked domain command;
- inspect pending conflicts/review items;
- inspect health/queues/compute state в пределах политики;
- generate report/widget data через read-only contracts.

Требования:

- explicit scopes и least privilege;
- read-only по умолчанию;
- mutation tools требуют `expected_version` и actor/session identity;
- high-risk операции требуют human approval;
- полная audit trail;
- pagination, bounded payloads, timeouts;
- schema/version negotiation;
- prompt injection не превращается в авторизацию;
- raw evidence выдаётся только при разрешённом scope;
- MCP Gateway не имеет обходного SQL API для агентов.

---

## 12. Control Plane / PWA

Control Plane — web-first PWA для desktop/mobile browser. На этом этапе отдельное Android-приложение управления не требуется; Mara Capture остаётся специализированным capture companion.

UI не создаёт собственный source of truth. Все изменения идут через тот же domain API и concurrency rules, что MCP/corrections.

### 12.1. Основные разделы

**Dashboard**

- overall health;
- recorder/provider health;
- latest calls vs recordings;
- ingestion/processing queues и DLQ;
- disk/backup/restore status;
- compute nodes;
- unresolved conflicts/review items;
- recent events без утечки чувствительного текста на общий экран.

**Memory**

- people, projects, conversations, messages, decisions, commitments;
- search/filter;
- current revision и history;
- evidence viewer;
- provenance/model version;
- merge/split aliases через безопасные команды.

**Commitments**

- list/kanban/calendar views по необходимости;
- create, edit, reschedule, complete, cancel, supersede;
- conflict UX;
- evidence and requester/owner visibility;
- все mutation через `expected_version`.

**Recorder & Capture**

- provider manifest;
- last boot/check/call/recording/upload;
- permission/helper state или `unknown`;
- missing recording alerts;
- guided post-reboot recovery;
- field-test results.

**Pipelines**

- job status, retries, DLQ;
- reprocess с явным version/policy;
- quarantine;
- stage durations и errors.

**Backups**

- last successful backup;
- last verified restore;
- RPO/RTO evidence;
- manifest validation;
- запуск безопасной проверки без destructive restore поверх production.

**Agent Observatory**

- sessions/runs;
- agent/model/tool;
- token/cost/time budgets;
- changes proposed/applied;
- PR/review status;
- approvals и failures;
- correlation IDs до domain commands;
- redacted by default.

### 12.2. Report/widget extensibility

Control Plane должен поддерживать расширение отчётами и widgets без превращения каждого расширения в доступ к базе.

Минимальный manifest:

```json
{
  "id": "recorder-health",
  "version": "1.0.0",
  "title": "Recorder health",
  "data_contract": "mara.report.recorder_health.v1",
  "permissions": ["health:read"],
  "refresh_policy": "event_or_60s"
}
```

Правила:

- versioned read-only data contracts;
- permission declaration;
- server-enforced queries;
- bounded response/time;
- no arbitrary SQL;
- widgets не могут менять каноническое состояние без отдельной domain action;
- graceful degradation при недоступном widget;
- в будущем возможен plugin SDK, но MVP начинается со встроенного registry.

### 12.3. Технологический ориентир

React + Vite/Next-equivalent, TanStack Query и OpenTelemetry допустимы, если соответствуют текущему стеку. Выбор обязан учитывать простоту self-hosting, PWA, типобезопасные API и невысокую операционную стоимость. Не добавлять крупный framework только ради моды.

---

## 13. Compute architecture

### 13.1. Роли узлов

**Doctor — всегда доступное ядро**

- SQLite Memory Ledger;
- API/MCP;
- queues/scheduler;
- blob metadata и локальное хранилище согласно фактической конфигурации;
- Context Broker;
- projector;
- lightweight ingestion/reconciliation/health;
- Home Assistant/MQTT и существующие лёгкие сервисы;
- никакого обязательного тяжёлого inference, который делает core недоступным.

**Big PC — основной heavy compute worker**

- NVIDIA RTX 5070 Ti 16 GB;
- Whisper/ASR;
- diarization;
- embeddings/reranking;
- локальные LLM extraction/inference;
- batch reprocessing, evaluation и эксперименты;
- default priority для GPU-heavy jobs.

**GTR — optional low-priority/fallback worker**

- на GTR работает Minecraft;
- AI workload не должен ухудшать Minecraft;
- только small/bounded jobs, fallback или opportunistic обработка;
- scheduler учитывает Minecraft/activity/resource guard;
- тяжёлые задачи не назначаются на GTR по умолчанию;
- пользователь может полностью отключить AI worker без нарушения core.

### 13.2. Scheduler requirements

Job declaration содержит:

- type;
- priority/deadline;
- CPU/RAM/GPU/VRAM/disk estimates;
- required capabilities/model;
- privacy/location constraint;
- retry/idempotency semantics;
- maximum runtime;
- preemptibility;
- input/output IDs.

Node heartbeat содержит capabilities, load, available resources, versions и policy flags. Scheduler:

- предпочитает Big PC для heavy compute;
- не ждёт бесконечно выключенный Big PC: показывает queued reason;
- MAY выполнить допустимый fallback на GTR только в рамках resource guard;
- не запускает две задачи, совместно превышающие VRAM/RAM budget;
- поддерживает cancellation/checkpoint там, где возможно;
- сохраняет историю назначения и фактическое потребление;
- core Doctor остаётся работоспособным при отсутствии обоих workers.

---

## 14. Agent Observatory и governance

### 14.1. Роли

- **Claude Code — primary builder:** анализ, реализация, тесты, документация, PR.
- **Codex — independent reviewer:** проверка diff, инвариантов, миграций, тестов, безопасности и соответствия ТЗ. Codex не должен просто подтверждать выводы Claude.
- **Human owner — final authority:** одобряет значимые архитектурные решения, опасные миграции, privacy changes и merge.

### 14.2. PR-only governance

- никакой прямой разработки в `main`;
- каждая логическая порция работы — отдельная branch + PR;
- обязательный CI до merge;
- независимое review Codex для security/data-model/migration/recorder changes;
- migrations, rollback/roll-forward notes и backup impact в PR;
- generated artifacts и secrets не коммитить;
- branch protection REQUIRED;
- emergency fix всё равно оформляется PR с последующим review;
- commit history должен быть понятен, без бессмысленных массовых коммитов от swarm.

### 14.3. Ограничения субагентов и бюджета

По умолчанию Claude Code:

- использует максимум **3 субагента одновременно**;
- не запускает рекурсивный swarm без разрешения;
- каждому субагенту задаёт bounded scope, deliverable и stop condition;
- не дублирует одинаковое исследование несколькими агентами без причины;
- перед новым fan-out суммирует уже полученное;
- устанавливает time/token/cost budget на run, если платформа позволяет;
- при 80% бюджета прекращает новые исследования, сохраняет промежуточный результат и эскалирует;
- не обходит лимит созданием цепочки новых сессий;
- Agent Observatory получает run/agent/budget/PR metadata.

Предлагаемые guardrails для одного этапа должны быть зафиксированы перед запуском: max agents, max wall time, max token/cost, допустимые tools, write scope и approval points.

---

## 15. Observability, security и privacy

### 15.1. Observability

Использовать structured events, correlation IDs и метрики по стадиям:

- capture detected;
- upload started/completed/retried;
- blob accepted/quarantined;
- ASR/diarization/extraction duration/status;
- entity/revision created;
- projection lag;
- context pack age;
- queue depth/oldest job/DLQ;
- recorder health and alert transitions;
- backup/restore verification;
- compute utilization;
- MCP/domain mutations and conflicts.

Логи не должны содержать transcript, message body, email body, phone number или auth token по умолчанию. Для диагностики нужен gated, time-limited sensitive mode с явным предупреждением и redaction.

OpenTelemetry допустим как стандарт инструментирования. Langfuse или аналог не является обязательным компонентом; подключать только после privacy review и доказанной потребности, предпочтительно self-hosted.

### 15.2. Threat model minimum

Учесть:

- кражу телефона;
- компрометацию device token;
- MITM/replay upload;
- вредоносный файл/неверный MIME;
- prompt injection в сообщении/письме/транскрипте;
- агент с избыточными MCP правами;
- утечку через logs/backups/widgets;
- повреждение/rollback SQLite;
- rogue compute worker;
- supply-chain dependency;
- случайный публичный доступ Control Plane;
- опасную массовую correction/migration.

Нужны secrets inventory, rotation runbook, dependency scanning, least privilege, authenticated nodes, encrypted backups и document retention/deletion policy.

---

## 16. Field-test matrix

Каждый тест хранит: device/build/provider/app versions, настройки, время boot, network state, call metadata, expected/actual result, file hash, durations, health snapshot, screenshots/log bundle без лишних персональных данных и verdict.

### 16.1. Recorder capture matrix

| ID | Provider | Scenario | Direction/mode | Preconditions | Expected |
|---|---|---|---|---|---|
| R01 | ACR Phone + APH | baseline | outgoing, earpiece | fresh configured state | both sides intelligible, file created |
| R02 | ACR Phone + APH | baseline | incoming, earpiece | screen locked | auto recording, both sides intelligible |
| R03 | ACR Phone + APH | speakerphone | outgoing | auto-speaker remains OFF | recording valid without forced speaker |
| R04 | ACR Phone + APH | long call | 30–60 min | sufficient disk | complete playable file, no truncation |
| R05 | ACR Phone + APH | immediate reboot test | first call after reboot | no manual recovery | failure is detected; pass for reliability only if recording works |
| R06 | ACR Phone + APH | manual recovery | first call after documented recovery | post-reboot broken state | recovery reproducible, health returns only after evidence |
| R07 | ACR Phone + APH | app killed | incoming/outgoing | force-stop/background pressure | either records or raises timely warning |
| R08 | ACR Phone + APH | battery saver | incoming/outgoing | saver enabled | behavior measured, no silent false-green |
| R09 | ACR Phone + APH | storage permission revoked | call | permission removed | pre/post-call unhealthy alert |
| R10 | ACR Phone + APH | low disk | call | threshold reached | warning before corruption; safe failure |
| R11 | ACR Phone + APH | Bluetooth headset | incoming/outgoing | common headset | both sides/quality measured; explicit capability verdict |
| R12 | ACR Phone + APH | repeated calls | same contact within minute | two distinct calls | two stable events/files; no overwrite/dedupe loss |
| R13 | Cube ACR | control | earpiece | known best config | confirms weak remote side; not promoted |
| R14 | Cube ACR | control | speakerphone | known best config | both sides good; documents fallback only |

### 16.2. Delivery and pipeline matrix

| ID | Scenario | Expected |
|---|---|---|
| P01 | Wi‑Fi/Tailscale available | file streams to Doctor once, hashes match |
| P02 | network drops mid-upload | partial not accepted; retry/recovery succeeds without duplicate |
| P03 | phone offline after call | durable local queue; upload after reconnect |
| P04 | duplicate request/idempotency key | same receipt/object IDs returned |
| P05 | same content, two distinct calls | distinct call events retained |
| P06 | corrupted/truncated file | quarantine + visible error, no downstream extraction |
| P07 | 512 MiB-class upload | bounded server memory, no whole-body read |
| P08 | Doctor restarts mid-job | lease recovery; at-least-once processing with idempotent result |
| P09 | Big PC offline | job remains visible/queued, Doctor healthy |
| P10 | Big PC returns | eligible jobs execute and report versions/resources |
| P11 | GTR with Minecraft active | heavy job not scheduled; no gameplay resource breach |
| P12 | invalid evidence from model | canonical write rejected/reviewed |
| P13 | two simultaneous edits | one succeeds, second receives 409 conflict; no lost update |
| P14 | projector directory deleted | complete deterministic rebuild from ledger |
| P15 | stale Hermes session | current pack supersedes old state; completed task not presented as open |

### 16.3. Backup/restore matrix

| ID | Scenario | Expected |
|---|---|---|
| B01 | scheduled online backup under writes | consistent snapshot with manifest |
| B02 | checksum tampered | restore validation refuses snapshot |
| B03 | isolated full restore | ledger integrity passes, blobs reconcile, projections rebuild |
| B04 | missing blob | discrepancy visible; affected evidence marked unavailable, not fabricated |
| B05 | migration + restore old version | documented forward path works or fails safely before mutation |

### 16.4. Quality assessment for calls

Для каждого representative call измерить/оценить:

- обе стороны присутствуют;
- remote/local loudness balance;
- clipping/noise;
- duration mismatch;
- ASR WER/CER на размеченном фрагменте либо human intelligibility rubric;
- diarization error / speaker confusion;
- правильность определения requester/owner/action/deadline;
- evidence link opens нужный фрагмент.

Личные записи не включать в публичный test corpus. Создать согласованный synthetic/consented regression corpus.

---

## 17. Acceptance criteria

### 17.1. P0 — целостность и authority

- [ ] SQLite ledger документирован и фактически является единственным write authority.
- [ ] Все канонические сущности имеют immutable stable IDs, не зависящие от filename.
- [ ] Коллизии двух одинаково названных commitments/двух звонков в минуту покрыты regression tests.
- [ ] Updates используют revisions и `expected_version`.
- [ ] Потерянное обновление невозможно; конфликт возвращается и виден пользователю.
- [ ] Evidence валидируется по существующему object/segment ID и диапазону.
- [ ] Markdown полностью перестраивается и детерминированно соответствует ledger.
- [ ] WhatsApp и другие source dedupe не теряют два реальных одинаковых события.

### 17.2. P0 — recorder reliability

- [ ] Device/provider facts из раздела 7 отражены в документации и UI.
- [ ] ACR Phone + APH помечен временным reboot-unsafe provider, пока тест не докажет иное.
- [ ] После `BOOT_COMPLETED` запускается health workflow.
- [ ] Проверяются folder access, permissions и доступные helper/provider signals.
- [ ] Реальный `last call` сопоставляется с `last recording`.
- [ ] Если после звонка записи нет, durable warning возникает в измеренный срок и не дублируется.
- [ ] False-green после reboot невозможен: до успешного контрольного звонка статус не `healthy`.
- [ ] Есть воспроизводимая инструкция восстановления и отдельный research verdict по надёжному способу.
- [ ] Раздел `Server + Secret` не назван webhook без доказательства; protocol research оформлен отдельно.
- [ ] Встроенная cloud transcription не используется в production.

### 17.3. P0 — upload/SQLite/restore

- [ ] Server upload потоковый и имеет доказанный bounded memory profile.
- [ ] Interrupted/repeated uploads безопасны и идемпотентны.
- [ ] SQLite constraints, transactions, migrations и integrity checks покрыты тестами.
- [ ] Backup manifest проверяется.
- [ ] Выполнен и задокументирован хотя бы один isolated restore drill.
- [ ] После restore проекции rebuild, blobs reconcile, sample evidence открывается.

### 17.4. P1 — memory intelligence

- [ ] ASR segments имеют timestamps и stable IDs.
- [ ] Speaker attribution не выдумывает личность и поддерживает correction.
- [ ] Extraction различает просьбу/обещание и owner/requester на regression corpus.
- [ ] Context packs versioned, contain source versions и stale-detectable.
- [ ] Completion/cancellation немедленно отражаются в новом `now` pack.

### 17.5. P1 — compute/control plane

- [ ] Doctor работает при выключенных Big PC и GTR.
- [ ] Heavy jobs по умолчанию назначаются Big PC RTX 5070 Ti 16 GB.
- [ ] GTR имеет low-priority/resource guard и не получает heavy job при активном Minecraft.
- [ ] Control Plane использует общий domain API, не отдельную authority DB.
- [ ] Dashboard показывает recorder, queues, backups, conflicts и compute state.
- [ ] Mutations из UI/MCP используют `expected_version` и audit trail.
- [ ] Agent Observatory показывает run/budget/change/PR metadata с redaction.
- [ ] Widget/report contract versioned и не допускает arbitrary SQL.

### 17.6. Governance

- [ ] Branch protection и обязательный CI включены.
- [ ] Работа идёт PR-only.
- [ ] Каждый data model/migration/security/recorder PR получил независимый review Codex.
- [ ] Максимум три субагента по умолчанию и budget guardrails зафиксированы в contributor/agent docs.
- [ ] Ни один acceptance пункт не закрыт только ссылкой на unit test, если он требует полевого E2E.

---

## 18. План реализации и приоритеты

### Phase 0 — Audit and safety gate

1. Инвентаризация репозитория и current-state audit.
2. Карта данных/источников истины/write paths.
3. Threat model и backup до migrations.
4. CI, branch protection, PR template, CODEOWNERS/reviewer policy.
5. Baseline tests и воспроизводимое окружение.

**Выход:** подтверждённый baseline, risk register, migration plan. Новые функции ещё не добавляются.

### Phase 1 — Memory Ledger hardening

1. Stable ID strategy/ADR.
2. SQLite schema/migrations.
3. Revisions, `expected_version`, conflicts.
4. Idempotency receipts и safe dedupe.
5. Evidence/provenance model.
6. Atomic domain commands/audit/outbox.
7. Markdown projector + full rebuild/drift checks.
8. Regression tests по коллизиям и stale state.

**Выход:** один authority без silent overwrite/lost update.

### Phase 2 — Operational hardening

1. End-to-end streaming upload.
2. SQLite operational settings and integrity jobs.
3. Backup manifest, encrypted copy, restore runbook.
4. Isolated restore drill.
5. Reconciliation/DLQ/alerts.

**Выход:** доказанная восстановимость и bounded resource behavior.

### Phase 3 — Recorder reliability

1. `RecorderProvider` abstraction.
2. ACR Phone + APH adapter/config manifest.
3. BOOT_COMPLETED + periodic health checks.
4. CallLog↔recording matching и missing-recording alerts.
5. Field matrix R01–R14.
6. Исследование reboot recovery/autostart.
7. Исследование `Server + Secret` и transport candidates без преждевременного внедрения.

**Выход:** либо доказанный reliable provider, либо честно работающий degraded provider с быстрым обнаружением и воспроизводимым recovery. Не маскировать второй результат как первый.

### Phase 4 — Processing correctness

1. ASR segment identity/versioning.
2. Diarization benchmark.
3. Speaker correction.
4. Schema-constrained extraction.
5. Evidence validation.
6. Realistic consented/synthetic regression corpus.

**Выход:** commitments с проверяемой атрибуцией и evidence.

### Phase 5 — Context/MCP

1. Versioned packs and stale detection.
2. `now`, person, project и query packs.
3. Stable MCP read/query/correction/domain command contracts.
4. Scopes, approvals, audit.
5. Hermes long-session tests.

**Выход:** разные агенты получают одинаковое актуальное состояние.

### Phase 6 — Compute pool

1. Job capability/resource schema.
2. Worker protocol/heartbeats.
3. Big PC default routing.
4. GTR Minecraft-aware low-priority guard.
5. Scheduling observability/cancellation/retry.

**Выход:** heavy compute управляем и не ломает Doctor/GTR workloads.

### Phase 7 — Control Plane/PWA

1. Read-only dashboard/health first.
2. Memory/evidence/history.
3. Version-checked mutations/conflict UX.
4. Recorder recovery UI.
5. Backups/restore evidence.
6. Agent Observatory.
7. Internal report/widget registry.

**Выход:** единая панель управления без второй базы истины.

### Phase 8 — Measured extensions

Только после профилирования и реальной потребности оценивать:

- embeddings/reranking at scale;
- external full-text search;
- temporal graph database;
- advanced observability products;
- third-party report/widget SDK;
- дополнительные recorder providers и source adapters.

Для каждого — benchmark, privacy/ops cost, exit strategy и ADR.

---

## 19. Требования к каждому PR Claude Code

PR description должен содержать:

1. цель и связанные пункты ТЗ;
2. что было в baseline;
3. архитектурное решение и альтернативы;
4. schema/API changes;
5. migration/rollback или roll-forward procedure;
6. privacy/security impact;
7. resource impact на Doctor/Big PC/GTR/phone;
8. тесты и их фактические результаты;
9. field test needed / performed;
10. observability и failure behavior;
11. документацию;
12. известные ограничения;
13. checklist независимого review Codex.

Размер PR должен позволять содержательное review. Нельзя смешивать migration ledger, recorder experiments и новый UI в один гигантский diff.

### 19.1. Definition of Done

Работа не завершена, пока:

- код не соответствует инвариантам;
- тесты реально запущены и результаты приложены;
- migration/recovery проверены соразмерно риску;
- ошибки наблюдаемы;
- документация обновлена;
- нет заявлений о неподтверждённых возможностях;
- проведено независимое review;
- для аппаратно-зависимого поведения выполнен полевой тест пользователем или явно записан блокер.

---

## 20. Конкретные исследовательские задачи

### RES-REC-001 — ACR Phone remote report protocol

**Вопрос:** что реально делает «Отчёт на удалённый сервер» с `Server` и `Secret`?

**Метод:** официальная документация/поддержка, контролируемый тестовый endpoint, разрешённый network trace, negative/security cases.

**Результат:** документ с protocol, method, payload, auth/signature, TLS, retries, file inclusion, timeout, idempotency, background/reboot behavior и verdict. До этого слово `webhook` не использовать как установленный факт.

### RES-REC-002 — ACR Phone transfer options

Проверить по отдельности SFTP/WebDAV/FTPS/device и только при необходимости облачные хранилища. FTP допускается лишь для лабораторного понимания, не production. Проверить auto-trigger, metadata, filename stability, partial/retry, network loss, Tailscale, deletion policy, reboot.

### RES-REC-003 — Reliable post-reboot capture

Изучить документированные настройки EMUI/app launch, APH lifecycle, Accessibility/Shizuku requirements и допустимые альтернативные providers/аппаратные пути. Критерий — не «однажды заработало», а повторяемость серии reboot→call tests.

### RES-ASR-001 — Diarization on RTX 5070 Ti 16 GB

Сравнить подходящие локальные pipelines на representative corpus по speaker error, speed, VRAM, лицензии, offline usability и integration cost. Выбрать минимально достаточный.

### RES-OSS-001 — Reuse decision log

Для React/TanStack/OTel/MCP SDK и любых Open Walnut/second-brain inspirations проверить license, maintenance, security, architectural fit. Не копировать код только из-за похожего UI. Graphiti/Meilisearch/Langfuse/vector/graph DB требуют отдельного benchmark-driven ADR.

---

## 21. Обязательные ADR

Создать или обновить ADR по темам:

1. SQLite ledger as authority; Markdown as projection.
2. Stable ID format and source identity.
3. Revision/conflict semantics.
4. Evidence/provenance model.
5. SQLite durability and backup strategy.
6. Blob store and streaming upload protocol.
7. Recorder Provider contract and current ACR Phone degraded status.
8. Context pack versioning/staleness.
9. MCP authorization/mutation policy.
10. Compute scheduling policy Doctor/Big PC/GTR.
11. UI/report/widget extension boundary.
12. Any adopted external component.

ADR обязан фиксировать context, decision, alternatives, consequences и reversal/migration path.

---

## 22. Команды Claude Code на старт работы

Claude Code должен выполнить следующую последовательность:

1. Прочитать репозиторий, историю, документацию, migrations, CI и tests.
2. Не доверять старому summary вместо живого кода.
3. Создать current-state audit и traceability matrix `requirement → code → test → status`.
4. Выделить P0 integrity/recovery/recorder risks.
5. Предложить небольшие последовательные PR, начиная с safety gate и ledger hardening.
6. Перед destructive migration создать и проверить backup/rollback path.
7. Не строить Control Plane поверх нестабильной authority model.
8. Не добавлять новые source connectors до закрытия silent data loss risks.
9. Не объявлять ACR Phone reliable до post-reboot field matrix.
10. Не реализовывать выдуманный webhook; сначала `RES-REC-001`.
11. Для действий на физическом телефоне сформировать короткий human checklist: только то, что Claude не может сделать сам.
12. Каждый этап вести PR-only и отдавать Codex на независимый review.
13. По умолчанию не превышать три субагента и заранее фиксировать budget guardrails.
14. После каждого phase обновлять этот документ/traceability, ADR и operational runbooks.

---

## 23. Финальная продуктовая проверка

Mara можно считать достигшей первой надёжной версии Personal Memory OS только когда воспроизводимо проходит следующий реальный цикл:

```text
Телефон перезагружен
  -> recorder health не врёт о состоянии
  -> совершается обычный звонок без ручного запуска записи
  -> обе стороны разборчиво записаны
  -> файл найден и однозначно связан со звонком
  -> сеть может пропасть и вернуться
  -> upload завершается ровно один раз логически
  -> ASR и speaker attribution дают проверяемые сегменты
  -> commitment извлечён с правильными owner/requester/deadline
  -> evidence открывает нужный фрагмент
  -> пользователь исправляет/закрывает commitment
  -> revision и audit trail сохраняются без lost update
  -> новый Context Pack немедленно supersedes старый
  -> Claude, Codex, Hermes и PWA видят одно текущее состояние
  -> backup восстанавливает ledger/blobs, а Markdown пересобирается
```

Если запись после reboot не запускается автоматически, текущая система всё ещё может быть полезной, но её статус — **degraded prototype with monitored recovery**, а не autonomous reliable memory capture.

---

## 24. Северная звезда

Главная ценность Mara не в количестве интеграций и не в эффектной панели. Она в доверии:

> Mara ничего важного не теряет молча, не путает догадку с фактом, не хранит две несовместимые версии истины, показывает происхождение каждого вывода и остаётся управляемой владельцем даже без конкретной модели, облака или интерфейса.

Все технические решения следует оценивать относительно этой цели.
