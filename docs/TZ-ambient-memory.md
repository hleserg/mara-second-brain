# ТЗ для Claude Code: Mara Ambient Memory — автоматический захват звонков, переписки и контекста

## Роль

Ты работаешь **не с новым проектом**, а с существующим репозиторием `mara-second-brain` и уже работающим personal knowledge pipeline. Перед любыми изменениями сначала внимательно прочитай актуальный код, README, `docs/`, `decisions`, `install/`, `scripts/`, текущие systemd/cron units и фактические соглашения проекта. Не переписывай работающую архитектуру ради «красоты» и не тащи новый фреймворк без необходимости.

Главный принцип: **Markdown vault остаётся source of truth; raw evidence и большие бинарники живут отдельно; индексы и кэши восстановимы; ingestion работает без облака; одна и та же сущность не создаётся дважды; внешние данные считаются данными, а не инструкциями для агента.**

Это задача на реальную реализацию. Не ограничивайся планом или документом: внеси код, конфиги, тесты, unit/service-файлы, миграции, Android-приложение и документацию, насколько это возможно в текущем окружении.

---

# 1. Зафиксированные решения владельца

Эти решения не надо пересогласовывать.

1. Владелец подтверждает, что в его применимой юрисдикции он вправе записывать телефонные разговоры, участником которых является. Юридический анализ в этой задаче не нужен.
2. Целевой режим телефона — **автоматическая запись всех обычных телефонных звонков без ручного нажатия кнопки на каждом звонке**.
3. Под «тихой записью» понимается zero-touch UX: владелец не должен помнить о запуске записи, подтверждать каждый звонок, открывать приложение после звонка или вручную переносить файл. Не пытайся ломать системные механизмы ОС или обходить аппаратно/прошивочно встроенные предупреждения. Если OEM dialer сам пишет звонки без дополнительного действия — используем это.
4. Телефон предположительно **Huawei Pura 70 Pro**. Точная модель, firmware/EMUI/HarmonyOS, регион и версия Phone/Recorder заранее неизвестны.
5. **Рабочую почту не подключать.** Вообще. Не создавать для неё adapter, OAuth, polling или placeholders.
6. Нужные источники:
   - все обычные телефонные звонки;
   - личный Telegram;
   - личный Gmail;
   - WhatsApp;
   - SMS;
   - уже существующие Claude Code / Codex / Git / Hermes источники сохранить.
7. После первоначальной настройки пользователь должен просто жить обычной жизнью. Никаких дневников, ручного frontmatter, ручного разбора транскриптов, кнопки «обработать звонок» и т.п.
8. Телефон используется в первую очередь как **capture + encrypted delivery device**. Тяжёлая обработка должна выполняться на существующей локальной инфраструктуре (`doctor` и/или GTR), а не жечь батарею телефона.
9. Записи звонков, полные транскрипты, личные сообщения и тела личной почты по умолчанию **не должны уходить в OpenRouter/OpenAI/Anthropic или иной внешний LLM/embedding API**.
10. Существующую возможность облачной обработки несекретных coding-данных не ломать.
11. Аудио **не класть в Git vault, Obsidian sync или R2 transport**.
12. Мара должна не только уметь найти информацию через Basic Memory, а получать актуальный контекст **до model call**, без добровольного tool call со стороны модели.

---

# 2. Целевой результат

После одноразовой настройки система должна работать так:

```text
Обычный входящий/исходящий звонок
    ↓
Huawei/OEM автоматически создаёт запись
    ↓
Mara Capture замечает готовый файл
    ↓
hash + metadata + local durable queue
    ↓
encrypted upload в trusted memory plane
    ↓
ASR → diarization → speaker resolution
    ↓
transcript with timestamps
    ↓
requests / promises / decisions / deadlines / changed instructions
    ↓
conversation + commitments + entities + project state
    ↓
Basic Memory reindex + Context Broker rebuild
    ↓
короткий Telegram digest от Мары
```

Параллельно:

```text
Telegram → TDLib on trusted host → ingest
Personal Gmail → Gmail API push/history → ingest
WhatsApp → phone notification capture (+ optional official exports for backfill) → ingest
SMS → direct SMS read if granted, otherwise notification fallback → ingest
```

И затем:

```text
новая реплика Сергея Маре
    ↓
Context Broker / Hermes pre_llm_call
    ↓
core + now + relevant people/projects/commitments
    ↓
model call
    ↓
Basic Memory только для deep retrieval
```

---

# 3. Сначала провести реальный аудит текущего репозитория

До написания кода:

- определить фактическую структуру vault и tooling repo;
- найти текущие ingestion contracts, `source_id`, frontmatter conventions, queue/spool, entity index/linker, daily generation, `mara-brief`, Hermes integration, Basic Memory integration, R2 filters, backup/restore scripts;
- найти существующую Whisper/speech инфраструктуру и **переиспользовать её**, а не создавать второй независимый ASR stack;
- проверить, где сейчас живут секреты и какие path уже исключены из Git/R2;
- проверить текущую версию Hermes и точный поддерживаемый контракт `pre_llm_call`/context injection;
- проверить текущие локальные GPU/CPU execution paths и выбрать существующий preferred path для тяжёлых batch jobs;
- сохранить backward compatibility с уже созданными карточками и `sensitive: true`.

Если фактический код отличается от описания ниже — адаптируй дизайн к реальному коду, но сохрани инварианты и целевое поведение.

---

# 4. Архитектура: один memory plane, без микросервисного зоопарка

Нужен один небольшой локальный service boundary, условно `contextd`/`mara-ingest`. Название можешь подобрать по стилю репозитория.

Он отвечает за:

- ingestion API;
- durable job queue/status;
- device authentication;
- blob metadata;
- normalization/classification;
- task/commitment projections;
- Context Broker/bootstrap context;
- health/metrics;
- retention reconciliation.

Markdown vault остаётся source of truth для человечески читаемых нормализованных знаний. SQLite допустим для очереди, jobs, dedupe, projection/cache и mobile delivery state, но не как замена vault.

Предпочтительно не добавлять Redis/Postgres/Kafka, если текущий масштаб не требует их.

Минимальная API-поверхность:

```text
POST /v1/ingest/event
POST /v1/ingest/audio
POST /v1/ingest/message
POST /v1/ingest/email
GET  /v1/jobs/{id}
GET  /v1/context/bootstrap
POST /v1/context/query
GET  /healthz
GET  /metrics
```

API должен слушать private interface/loopback/VPN path в соответствии с фактической инфраструктурой. Не выставлять Basic Memory или raw blob storage напрямую в интернет.

---

# 5. Android-приложение `Mara Capture`

Создай минимальное внутреннее Android-приложение, предназначенное для sideload, а не для Google Play.

## 5.1. Что приложение должно делать

### A. Забор OEM call recordings

Приложение **не должно считать себя универсальным call recorder**. Его основной путь — подхватывать файлы, которые уже создал системный Huawei Phone/Recorder.

Поддержать несколько способов обнаружения, потому что точный путь на Pura 70 Pro неизвестен:

1. MediaStore audio query/content observer, если OEM запись публикуется туда.
2. User-selected directory через Storage Access Framework с persisted URI permission.
3. FileObserver/периодическая reconciliation scan там, где это реально доступно.

Нельзя hardcode'ить один путь вроде `/Record/CallRecord`. На onboarding приложение должно уметь:

- показать найденные candidate directories/files;
- дать пользователю один раз выбрать папку, если auto-detection не сработала;
- запомнить доступ;
- после этого работать без участия пользователя.

Файл считать завершённым только после проверки стабильного размера/mtime, чтобы не загрузить запись, которую OEM ещё пишет.

### B. Корреляция с call log

Если пользователь выдал разрешение, читать local call log и contacts, чтобы сопоставить запись с:

- incoming/outgoing/missed;
- номером;
- contact display name;
- start/end/duration;
- ближайшей записью по timestamp.

Не отправлять всю адресную книгу как отдельный dump. Передавать только данные, необходимые для конкретного события/разрешения сущности, либо синхронизировать контакты в отдельном явном локальном adapter с минимальными полями.

### C. Notification capture

Использовать штатный `NotificationListenerService` для WhatsApp и как fallback для SMS/Telegram, без Accessibility scraping, screen scraping и keylogging.

Хранить только полезные поля события:

```json
{
  "package": "...",
  "posted_at": "...",
  "title": "...",
  "text": "...",
  "subtext": "...",
  "conversation_id_hint": "...",
  "notification_key_hash": "..."
}
```

Не сохранять изображения уведомлений и лишние extras без необходимости.

### D. SMS

Сделать два режима:

1. preferred — прямое чтение SMS provider при выданном пользователем runtime permission, включая начальный backfill и incremental sync;
2. fallback — NotificationListener, если прошивка не даёт требуемый доступ.

Не заставлять приложение становиться default SMS client, если без этого чтение работает на данной конфигурации. Если конкретная прошивка требует default role, не менять её молча: выдать понятный diagnostic и оставить fallback.

### E. Durable encrypted delivery

Перед upload:

```text
sha256
→ create local job
→ persist metadata
→ encrypt transport payload / TLS
→ upload
→ verify server acknowledgement/hash
→ mark delivered
```

Если сеть отсутствует — ничего не терять. Очередь должна переживать reboot и force-stop. Использовать Room/SQLite + WorkManager/foreground service только там, где это действительно нужно.

Секрет/credential устройства хранить через Android Keystore, а не в SharedPreferences plain text.

### F. Huawei background survival

Учесть агрессивное battery/background management Huawei:

- приложение должно корректно восстанавливаться после reboot;
- использовать минимально необходимые foreground/background механизмы;
- иметь health/status screen: последняя запись, последний upload, queue depth, notification listener state, выбранная recording directory, last server contact;
- иметь self-test, который пользователь запускает один раз после установки.

Не делать тяжёлый ASR/LLM на телефоне в нормальном режиме.

## 5.2. Чего приложение НЕ должно делать

- не использовать Accessibility Service для чтения мессенджеров или попытки «вытащить» telephony audio;
- не использовать VPN service как перехватчик приложений;
- не отправлять raw данные сторонним SaaS;
- не класть токены/ключи в APK/resources/repo;
- не удалять локальный call recording до подтверждённой серверной копии и истечения retention policy;
- не требовать ручного открытия после каждого звонка.

---

# 6. Автоматическая запись всех телефонных звонков

Capture стратегии разделить на tiers.

## Tier 1 — Huawei OEM automatic call recording

Это основной и желаемый путь.

Система должна быть готова к варианту `all calls → automatic recording`. Пользователь вручную включает этот переключатель один раз, если он есть в его build. Дальше приложение только подхватывает результат.

При первом запуске `Mara Capture` должно показать диагностический wizard:

```text
Device model/build
Phone app version
Recorder app version
Есть ли call recordings в MediaStore
Найдена ли папка
Удаётся ли прочитать последний файл
Удаётся ли сопоставить его с call log
Кодек/каналы/частота
```

## Tier 2 — сторонний recorder как внешний producer

Если OEM auto-record отсутствует, не пытайся притворяться, что обычный APK гарантированно запишет обе стороны GSM-звонка.

Архитектура ingestion должна принимать запись **из любого producer**, поэтому владелец сможет отдельно протестировать ACR Phone/Cube ACR/иной recorder. `Mara Capture` должен уметь подхватить их output directory так же, как OEM.

## Tier 3 — unsupported

Если ни OEM, ни выбранный recorder не дают качественного двустороннего звука, честно зафиксировать, что **software ingestion готов, а audio capture ограничен конкретной прошивкой**. Не городить root/bootloader/exploit обходы внутри этой задачи.

---

# 7. Call ingestion и blob store

Аудио хранить отдельно от vault, например по фактической архитектуре проекта:

```text
/srv/mara-blobs/calls/YYYY/MM/<sha256>.<ext>
```

Не использовать оригинальное имя файла как ключ доверия. Canonical identity — content hash + source event id.

Для каждого звонка создавать immutable manifest с минимумом:

```yaml
id: call_<uuid>
type: call
source: phone
source_id: <stable mobile event id>
started_at: ...
ended_at: ...
direction: incoming|outgoing|unknown
participants:
  - person:sergey
  - person:<resolved-or-unknown>
classification: personal|work-confidential|third-party
sensitive: true
cloud_allowed: false
recording:
  producer: huawei-native|external-recorder|unknown
  audio_sha256: ...
  blob_ref: local://calls/...
  codec: ...
  channels: ...
processing:
  pipeline_version: ...
retention:
  audio_until: ...
  transcript_until: ...
```

Default retention сделать конфигурируемым. Без отдельного решения владельца разумный старт:

- full audio: 90 дней;
- transcript: long-term/local-only;
- distilled facts/commitments: long-term;
- `pin: true` отменяет auto-delete конкретного audio.

Retention purge должен быть idempotent и оставлять manifest с фактом удаления blob.

---

# 8. ASR, diarization, speaker resolution

Сначала найди и переиспользуй существующий speech stack.

Целевой режим:

- primary ASR на GTR GPU: `faster-whisper`, предпочтительно существующая в проекте Whisper-модель/конфигурация;
- fallback на `doctor`: существующий локальный Whisper/`whisper.cpp`/доступный CPU path;
- VAD/normalization до ASR;
- word/segment timestamps;
- diarization через локально выполняемый pyannote/WhisperX-подобный pipeline, если зависимости и model weights доступны.

Критически важно **не блокировать весь pipeline отсутствием diarization model/token**. В таком случае сохранить transcript с `speaker: unknown-A/B` и обработать позже.

Для определения `self` vs `other`:

1. сначала проверить, не пишет ли конкретный recorder каналы раздельно;
2. если нет — diarization;
3. поддержать optional local voice enrollment Сергея и хранить voice embedding **только локально**;
4. если identity не уверена — не выдумывать, а помечать ambiguity.

Transcript хранить JSONL/JSON с evidence spans:

```json
{"segment_id":"s0001","start_ms":0,"end_ms":4920,"speaker":"person:boss","text":"...","asr_confidence":0.94,"speaker_confidence":0.91}
```

Readable Markdown conversation — отдельная projection, а не замена raw transcript.

---

# 9. Извлечение смысла звонка: не summary, а commitments

Для всех call transcripts нужен локальный extraction stage. Нельзя отправлять confidential/raw transcript в OpenRouter.

Schema должна различать как минимум:

```yaml
requests:
  - action: ...
    requester: ...
    owner: ...
    explicit: true|false
    deadline: ...
    deadline_explicit: true|false
    success_criteria: ...
    confidence: ...
    evidence:
      - start_ms: ...
        end_ms: ...

commitments:
  - action: ...
    owner: ...
    promised_to: ...
    due_at: ...
    explicit: true|false
    confidence: ...
    evidence: ...

decisions: ...
constraints: ...
open_questions: ...
changed_instructions:
  - supersedes: ...
    new_state: ...
people_mentioned: ...
projects_mentioned: ...
followups: ...
```

Правила:

- explicit request != inferred suggestion;
- не придумывать deadline;
- «до пятницы» парсить относительно `occurred_at`, но сохранять original phrase + parsed value;
- «побыстрее» не превращать в дату;
- каждое действие/решение/обещание обязано иметь evidence span;
- новый conflicting instruction не удаляет старый, а создаёт `supersedes`/`valid_until`;
- история не переписывается, меняется только current-state projection.

Рекомендуемый initial threshold, конфигурируемый тестами:

```text
>= 0.85    proposed/open task
0.60–0.85  "возможно задача" / needs-review
< 0.60     не создавать task
```

Если extraction model локально недоступна, сохранить transcript и поставить job в retry/DLQ — данные не терять.

---

# 10. First-class conversations и commitments

Добавить/адаптировать структуру vault без разрушительной миграции текущих заметок.

Целевая семантика:

```text
kb/conversations/   # звонки и важные диалоги
kb/commitments/     # кто кому что обещал/должен
kb/decisions/       # существующий слой сохранить
```

Если текущий layout отличается — используй существующий namespace, но типы должны стать first-class.

Frontmatter расширить backward-compatible полями:

```yaml
domain: personal|work|technical
classification: personal|work-confidential|third-party|secret
storage_scope: local-only|trusted-devices|vault-sync
model_scope: local-only|redacted-cloud|cloud-ok
cloud_allowed: false
audience:
  - mara
retention: ...
content_sha256: ...
source_revision: ...
pipeline_version: ...
valid_from: ...
valid_until: ...
supersedes: ...
```

`sensitive: true/false` сохранить для совместимости, но enforcement строить не только на нём.

---

# 11. Telegram: полный личный поток через TDLib

Для личного Telegram использовать TDLib-клиент на trusted host (`doctor` или фактический подходящий узел), авторизованный как пользователь, а не Bot API.

Требования:

- one-time interactive auth, затем daemon;
- локальная TDLib state DB вне vault и вне R2;
- credentials/session файлы denylisted из Git/R2/backups, если backup policy их явно не шифрует;
- incremental update ingestion;
- idempotency по `(chat_id, message_id)`;
- edits/deletes учитывать как revisions/tombstones, а не создавать ложные независимые сообщения;
- reply/thread relations сохранять;
- attachments сначала metadata + local ref; скачивание по policy, не бездумно всего подряд;
- private chats/group messages классифицировать local-only по умолчанию;
- не тащить весь Telegram в Boot Pack, только distilled/relevant facts.

Сырой stream допустимо держать append-only JSONL/SQLite вне синка, нормализованные факты — в vault.

---

# 12. Personal Gmail: подключить, рабочую почту не трогать

Нужен adapter **только для личного Gmail владельца**.

Предпочтительный путь — Gmail API `watch` + history delta sync; если инфраструктура Pub/Sub неоправданно тяжёлая для personal account, допустим аккуратный periodic history sync, но не полный повторный crawl каждого раза.

После one-time OAuth:

- backfill разумного диапазона или всей доступной истории отдельным batch job;
- ingest headers + text/plain/text/html body в локальный raw store;
- HTML sanitize → readable text;
- threads/replies/labels сохранять;
- attachments: metadata всегда; текстовое извлечение — отдельным policy/job;
- dedupe по Gmail message/thread IDs;
- delete/trash events отражать как state changes;
- `cloud_allowed:false` по умолчанию для body и attachments;
- никакие refresh tokens не класть в vault/Git/R2.

**Не подключать корпоративный/рабочий Gmail даже если его адрес найден в старых docs/config.** Если существующий незавершённый work-mail adapter есть — не активировать его; при необходимости явно пометить deprecated/disabled, не ломая историю.

---

# 13. WhatsApp

Не использовать unofficial web scraping/Accessibility automation как основу.

Реалистичный постоянный поток:

```text
NotificationListener on phone
→ dedupe
→ conversation windowing
→ local raw
→ facts/entities/commitments
```

Ограничение признать явно: notification stream может пропускать сообщения, для которых не было уведомления/preview.

Добавить importer официального `Export Chat` для **опционального ручного backfill/reconciliation**. Он должен:

- принимать текстовый export/zip;
- нормализовать timestamp/sender/text;
- дедуплицировать с уже пойманными notifications;
- импортировать missing history;
- не требовать повторной ручной обработки уже известных сообщений.

Периодический экспорт не должен быть обязательным для ежедневной работы; это только способ повысить полноту истории.

---

# 14. SMS

При доступном permission — прямой local provider sync:

- initial backfill;
- incremental scan by message id/date;
- incoming/outgoing;
- sender/recipient;
- body;
- delivery/read state при наличии;
- dedupe stable ids.

Если OS/permission policy не даёт чтение — fallback на NotificationListener без поломки всей системы.

SMS по умолчанию `cloud_allowed:false`.

---

# 15. Context Broker: Мара должна знать до tool call

Это обязательная часть задачи, а не «потом».

Не увеличивать бесконечно dynamic dump в `SOUL.md`.

Оставить:

- `SOUL.md` — persona и стабильные правила;
- `USER.md`/аналог — медленно меняющаяся identity/world info;
- динамическое `now/relevant` — через Hermes API-call-time context injection (`pre_llm_call` или фактический эквивалент текущей версии).

Сгенерировать materialized context packs, например:

```text
_system/context/
  core.md
  now.md
  aliases.json
  manifest.json
  people/*.md
  projects/*.md
```

`now.md` строится автоматически из:

- open commitments;
- deadlines;
- active blockers;
- recent decisions;
- changed instructions;
- последних значимых разговоров/сообщений;
- ближайших событий, если calendar уже существует/появится позже.

Per-turn pipeline:

```text
user message
→ exact alias/entity resolution
→ current-session entities
→ local hybrid retrieval if needed
→ now.md + 1–3 relevant packs
→ strict token budget
→ pre_llm_call context
→ model
```

Raw transcript/email/message **никогда не инжектить напрямую** в prompt автоматически. Только normalized/distilled context pack. Deep details поднимаются через Basic Memory/retrieval.

После внедрения `mara-brief.py` либо уменьшить до compact aliases/core, либо превратить в generator новых pack'ов. Убрать необходимость постоянно переписывать огромный `SOUL.md` и инвалидировать prompt cache ради каждого изменения статуса.

---

# 16. Post-call UX

После успешной обработки звонка Мара автоматически присылает в Telegram короткий digest, а не транскрипт.

Формат примерно такой:

```text
Звонок · <контакт> · 14:05–14:23

Попросили
• ...

Ты обещал
• ...

Создано
• 2 задачи

Возможно задача
• ...

Изменилось
• старое поручение X отменено/заменено на Y

Неясно
• ...
```

Каждый пункт должен быть связан с evidence span, чтобы по команде Мара могла показать точную цитируемую часть transcript или открыть соответствующий timestamp.

Пользователь может ответить обычной фразой вроде «это тоже задача, срок пятница». Такой ответ должен стать correction event и обновить commitment projection, а не требовать правки YAML.

---

# 17. Queue, retries, DLQ

Для event-driven ingestion использовать durable jobs.

Пример retry policy:

```text
immediate
+1m
+5m
+30m
+2h
+12h
then DLQ
```

С jitter.

DLQ не должен превращаться в ручную админку. Раз в день Мара может кратко сообщить владельцу только о реальных проблемах: сколько событий не обработано и почему.

Обязательная reconciliation логика:

- mobile upload queue vs server manifests;
- blob exists vs manifest exists;
- transcript exists vs extraction job exists;
- vault projection vs Basic Memory index lag;
- Telegram TDLib cursor/history;
- Gmail history cursor;
- SMS cursor;
- retention.

---

# 18. Безопасность

До подключения приватных источников провести P0-проверку текущих секретов.

Если в истории проекта ранее были раскрыты API keys/credentials и нет доказательства ротации — не печатай их и не пытайся использовать; добавь explicit checklist/report владельцу.

Обязательно:

- secret scanning tooling repo;
- deny-by-default для `.env`, token caches, TDLib DB, Gmail refresh tokens, Android pairing secrets, voice embeddings;
- никаких secrets в Markdown vault;
- никаких raw calls/messages/email bodies в R2/Obsidian sync;
- local-only embeddings для private sources;
- cloud egress tests;
- sanitized logs: не логировать полные message bodies/transcripts/tokens;
- device revoke mechanism;
- per-source classification enforcement до prompt/context layer.

Добавь тест, который ломает сборку/CI, если private fixture случайно уходит в cloud adapter path.

---

# 19. Наблюдаемость

Минимальные метрики/health state:

```text
mara_ingest_queue_depth
mara_ingest_lag_seconds
mara_dlq_count
mara_context_pack_age_seconds
mara_context_pack_bytes
mara_basic_memory_index_lag_seconds
mara_transcription_queue_depth
mara_transcription_realtime_factor
mara_task_extraction_failures_total
mara_mobile_last_seen_seconds
mara_mobile_pending_uploads
mara_tdlib_lag_seconds
mara_gmail_history_lag_seconds
mara_sms_lag_seconds
```

Не нужен Grafana-проект ради графиков, если существующие средства проще. Главное — диагностируемость и короткий human-readable health report.

---

# 20. Тесты

Нужны unit + integration + fixture-based regression tests.

## Call ingest

- один и тот же audio повторно не создаёт второй call;
- partial file не загружается;
- offline mobile queue переживает reboot/retry;
- server hash mismatch не считается успехом;
- manifest → ASR → transcript → extraction → vault projection;
- audio не попадает в Git/R2 path;
- cloud adapter не вызывается для `cloud_allowed:false`.

## Extraction

- explicit request создаёт proposed task;
- suggestion не становится обязательством;
- explicit deadline парсится;
- vague deadline не выдумывается;
- каждое действие имеет evidence span;
- new instruction supersedes old current state, но history сохраняется.

## Context

Фиксированный regression set минимум из вопросов:

```text
Что у меня сейчас горит?
Что я обещал <человеку>?
Что <человек> просил вчера?
Что изменилось в последнем разговоре?
Что сейчас с <активным проектом>?
Почему полгода назад выбрали X?
```

Ожидание:

- первые 5 должны обычно решаться без model-initiated memory tool call;
- глубокий исторический вопрос может вызвать retrieval;
- private call не виден coding agent несвязанного проекта;
- stale superseded instruction не выдаётся как current truth.

## Mobile source tests

Сделать диагностическую test matrix, которую пользователь сможет пройти на реальном телефоне:

- outgoing call, earpiece;
- incoming call, earpiece;
- speakerphone;
- Bluetooth headset;
- locked screen;
- 20–30 minute call;
- два звонка подряд;
- no network during call, network returns later;
- reboot before upload;
- WhatsApp notification;
- SMS direct/fallback;
- Telegram TDLib new/edit/delete;
- Gmail new/reply/delete.

---

# 21. Acceptance criteria

Работа считается действительно готовой, когда выполняются условия:

1. После первоначальной настройки пользователь не нажимает «запись» и не запускает обработку вручную.
2. Если Huawei OEM auto-record доступен и включён, каждый тестовый обычный звонок появляется в server ingest автоматически.
3. При отсутствии сети запись гарантированно догружается позже и не дублируется.
4. После обработки есть timestamped transcript, conversation card и структурированные commitments/requests/decisions.
5. Post-call digest автоматически приходит в Telegram.
6. Мара в новой сессии знает актуальные open commitments без Basic Memory tool call.
7. Personal Telegram автоматически ingest'ится через TDLib после одноразовой авторизации.
8. Personal Gmail автоматически ingest'ится после одноразового OAuth; рабочий email не подключён.
9. WhatsApp новые сообщения best-effort ingest'ятся через notifications; official export importer умеет backfill/reconcile.
10. SMS ingest'ится напрямую при доступном permission или через fallback.
11. Ни call audio, ни full transcript, ни Gmail body, ни private messages не уходят во внешние LLM/embedding сервисы по умолчанию.
12. Raw audio не находится в Git vault/R2/Obsidian sync.
13. Secrets не находятся в Git/vault/R2.
14. Есть install/update/rollback документация и self-test.
15. Есть один короткий файл `docs/USER-MANUAL-STEPS.md`, в котором перечислено **только то, что физически должен сделать владелец и чего агент не может сделать сам**.

---

# 22. Порядок реализации

Не делай гигантский rewrite. Работай вертикальными срезами, сохраняя работоспособность после каждого этапа:

1. audit + tests around current invariants/security;
2. normalized event contract + `contextd`/ingest boundary + blob store;
3. call ingest from sample files + ASR + transcript + extraction + digest;
4. Context Broker/Hermes injection;
5. Android `Mara Capture` для recordings/upload + device pairing;
6. Telegram TDLib;
7. personal Gmail;
8. WhatsApp notifications/export importer;
9. SMS direct/fallback;
10. end-to-end reconciliation, retention, metrics, regression suite.

Если возможно, параллельно сохраняй понятные commits по вертикальным этапам, но не оставляй main в сломанном состоянии.

---

# 23. Что не делать

- не заменять Markdown vault на PostgreSQL/vector DB;
- не вводить Qdrant/Milvus/Pinecone без доказанной необходимости;
- не строить новый web UI — Telegram + Obsidian достаточно для MVP;
- не подключать рабочую почту;
- не отправлять private data в cloud LLM «временно»;
- не требовать от пользователя ручных ежедневных действий;
- не требовать от пользователя копировать логи/JSON руками, если это можно диагностировать программно;
- не использовать Accessibility scraping как основной WhatsApp/call capture;
- не считать, что Android APK может гарантированно писать telephony downlink;
- не hardcode'ить модель/регион/recording path Huawei;
- не хранить raw audio в Git/R2/Obsidian;
- не превращать `SOUL.md` в постоянно растущую энциклопедию.

---

# 24. Итоговый отчёт Claude

После фактической реализации дай владельцу короткий отчёт на русском:

- что изменено;
- что уже работает end-to-end;
- какие тесты реально запущены и их результат;
- что не удалось проверить без физического телефона/аккаунтного подтверждения;
- где лежит собранный APK;
- какие **ровно** ручные действия остались владельцу;
- какие секреты/авторизации нужны, но не проси присылать секреты в чат;
- какие ограничения конкретной Huawei прошивки обнаружились после полевого теста.

Если что-то невозможно проверить из твоего окружения, не выдумывай результат. Подготовь self-test так, чтобы владелец сделал минимум действий, а система сама собрала диагностический отчёт.
