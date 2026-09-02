# Личный Gmail — письма в память

Спека 5 по ТЗ «Mara Ambient Memory» (§12, §17 «Gmail history cursor»,
порядок §22 пункт 7). Предыдущие: [ядро](2026-09-02-ambient-memory-design.md),
[контекст-брокер](2026-09-02-context-broker-design.md),
[Mara Capture](2026-09-02-mara-capture-design.md),
[Telegram](2026-09-02-telegram-tdlib-design.md).

## Зачем

Второй источник переписки после Telegram. Контракт тот же: письмо — событие
в contextd, удаление — надгробие, корзина — ревизия. Тела писем остаются на
doctor; наружу, как и у звонков, едет только то, что пройдёт через границу
облака (`cloud_allowed:false` по умолчанию).

## Что это НЕ делает

**Рабочую почту не подключает — вообще.** `docs/decisions.md` §15.2 когда-то
метил в корпоративный ящик; ТЗ §12 это отменяет, запись там помечена. Код
закрывает границу не запиской: `--login` принимает только `@gmail.com` и
`@googlemail.com`. Аккаунт Workspace с доменом компании отбрасывается до
того, как refresh token попадёт на диск.

Не ходит в Pub/Sub. ТЗ §12 разрешает periodic history sync, если push
неоправданно тяжёл для личного ящика — он тяжёл: проект в Google Cloud,
топик, подписка, открытый эндпоинт. Раз в десять минут по крону хватает.

Не извлекает обязательства из писем: сначала посмотреть на живой поток,
как и с Telegram. Не качает вложения: только метаданные (§12 «attachments:
metadata всегда; текстовое извлечение — отдельным policy/job»).

Не тянет клиентскую библиотеку Google. Три REST-вызова и обновление токена —
это `urllib`, а `google-api-python-client` тащит с собой полсотни пакетов.

## Где что лежит

| Что | Где | Почему |
|---|---|---|
| client_id / client_secret, токен contextd | `/etc/mara/gmail.env`, 600 | как `tdlib.env`; в репо только `install/gmail.env.example` |
| refresh token, адрес ящика | `/srv/mara-blobs/gmail/token.json`, 600 | §12 «никакие refresh tokens не класть в vault/Git/R2»; каталог вне волта и вне бэкапов, тест проверяет |
| курсор `historyId` | `/srv/mara-blobs/gmail/cursor.json` | двигается только после 200 от contextd |
| полные письма | `/srv/mara-blobs/gmail/raw/ГГГГ-ММ-ДД.jsonl` | §12 «локальный raw store»; retention — спека 10 |
| сердцебиение | `/srv/mara-blobs/gmail/heartbeat` | метрика `mara_gmail_lag_seconds`, находка сверки при молчании дольше часа |
| замок | `/srv/mara-blobs/gmail/lock` | второй прогон при незакончившемся первом выходит сразу |

## Транспорт: тот же contextd

`POST /v1/ingest/email` уже есть в contextd (kind `email`), новых
эндпоинтов нет. Устройство `gmail` спарено, токен в `gmail.env`.

## Контракт события

Новое письмо: `source: gmail`, `source_id: <message id>`, `occurred_at` из
`internalDate`, `classification: personal`, payload:

```
message_id, thread_id, labels, outgoing (SENT в labels),
from, to, cc, subject, date, rfc_message_id, in_reply_to,
snippet, text, has_html, attachments [{name, mime, size, attachment_id}], size
```

`text` — `text/plain` часть, если она есть и не пуста; иначе `text/html`
через `html.parser`: script/style/head выброшены, блочные теги — переводы
строк, сущности раскрыты. Кодировка берётся из `charset=` части, письмо в
windows-1251 не превращается в кашу. Тело в событии режется на 100 000
символов, полное письмо в `raw/`.

Удаление навсегда (`messagesDeleted` в history): `source_id <id>/deleted`,
payload `tombstone_of`. Корзина и возврат из неё (`labelsAdded`/
`labelsRemoved` с `TRASH`): письмо перечитывается и уходит ревизией
`<id>/labels/<historyId>` с полным payload, `revision_of` и `trashed`.
Остальные ярлыки не ревизии: прочитано/непрочитано — не состояние письма.
`mara_ingest.message_state(con, "gmail", id)` складывает это так же, как
Telegram.

Пропускаются письма с ярлыками SPAM, TRASH, DRAFT. Промо и соцсети
(`CATEGORY_*`) сейчас берутся — вопрос к утру.

## Курсор и догон

Первый запуск и `--backfill --days N`: сначала `historyId` из профиля, потом
`messages.list` с `after:ГГГГ/ММ/ДД`, письма старые→новые, потом курсор =
тот `historyId`. Порядок важен: письмо, пришедшее во время догона, окажется
в history после курсора и не потеряется. Догон ограничен 3000 писем за
прогон, дальше — ещё раз.

Обычный прогон: `users.history.list` от курсора с `historyTypes`
messageAdded/messageDeleted/labelAdded/labelRemoved, страницами по 500.
Курсор сохраняется после того, как все события прогона приняты. Повтор
после падения безопасен: contextd дедупит по `(source, source_id)`.

Курсор протух — Google отвечает 404 (§17): не падаем в цикл, а берём свежий
`historyId` и догоняем последние 7 дней.

Ошибка сети к contextd — пять попыток с задержкой до 16 с, потом прогон
выходит с ошибкой, не двигая курсор. 4xx — письмо пишется в журнал по id и
пропускается: contextd его не примет и завтра.

## Одноразовый вход

Google убрал OOB-редирект в 2022; на безголовом doctor остаётся loopback.
Клиент OAuth типа «Desktop app», redirect `http://127.0.0.1:8765/`.
Владелец с ноутбука:

```
ssh -L 8765:127.0.0.1:8765 doctor
~/mara-second-brain/scripts/gmail_ingest.py --login
```

Скрипт читает `/etc/mara/gmail.env` сам (`--env`): у крона нет
`EnvironmentFile`, а «экспортируй перед запуском» забывается.

Скрипт печатает ссылку, слушает 8765 на doctor, браузер ноутбука через
проброс возвращает код. PKCE S256, `access_type=offline`, `prompt=consent`
— иначе Google не отдаёт refresh token повторно. Scope только
`gmail.readonly`.

## Наблюдаемость

Журнал — `~/.local/state/mara/gmail-ingest.log`: `<id> ok`, `догон: N`,
`нет входа`, без тем и тел. `mara_gmail_lag_seconds` в `/metrics` (−1, пока
не подключали). Сверка раз в час: сердцебиение старше часа — warn `gmail`.

## Как это тестируется без Google

Разбор письма, html→текст, кодировки, надгробие, ревизия корзины, пропуск
спама, проверка домена, каталог вне волта — `tests/test_gmail_ingest.py`.
Синк целиком — `--self-check` на фальшивом API и фальшивом contextd: догон,
history с добавлением/удалением/корзиной, два обрыва сети, 4xx, протухший
курсор, курсор и сырой поток на диске, сердцебиение.

## Порядок нарезки

1. Разбор письма и контракт события, тесты.
2. Синк с курсором, самопроверка.
3. Вход, env, устройство, крон, метрика и сверка.
4. Шаги владельца в `USER-MANUAL-STEPS.md`, пометка в `decisions.md`.
5. Извлечение из писем — отдельная спека после живого потока.

## Вопросы к утру

1. Клиент OAuth: https://console.cloud.google.com → APIs & Services →
   Credentials → OAuth client ID → Desktop app; включить Gmail API; в
   `/etc/mara/gmail.env` вписать `GMAIL_CLIENT_ID` и `GMAIL_CLIENT_SECRET`.
   Consent screen перевести в Production (Publish app): в режиме Testing
   refresh token живёт 7 дней. Проверку Google проходить не нужно; если
   для restricted scope `gmail.readonly` публикацию не дадут — остаться в
   Testing и повторять `--login` еженедельно.
2. `--login` через `ssh -L`, как выше.
3. Брать ли `CATEGORY_PROMOTIONS` и `CATEGORY_SOCIAL`. Сейчас берутся.
4. Глубина первого забора: 30 дней по умолчанию, `--backfill --days N`
   для другого.
5. Retention `raw/` — спека 10.
