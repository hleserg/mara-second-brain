# Ранбук: выкат на doctor

Право мерджа не есть право деплоя — так было написано в ТЗ §14.2а, и до
2026-09-11 выкат оставался владельцу. С 2026-09-11 он передан агенту
(ответ на мешок М5 п.1 в `docs/PLAN-to-done.md`), и этот ранбук — то, что
исполняется вместо владельца. Гейт Г4 не снят: **миграция схемы реестра
по-прежнему требует отдельного разрешения**.

Ни один cron на doctor не делает `git pull`. Checkout там обновляется
только этим ранбуком.

## 0. Почему нельзя просто `git pull`

Checkout на doctor годами стоял на ветке выката с локальными коммитами:
кто-то правил код на месте, а потом ту же работу вливали в `main` через PR.
`git pull` на такой ветке даёт merge-коммит и расхождение, которое потом
никто не разберёт. Поэтому выкат — это `reset --hard origin/main`, а
локальные коммиты перед этим **доказываются избыточными**, а не
осматриваются на глаз.

## 1. Что имеем до выката

```bash
ssh doctor 'cd ~/mara-second-brain
  git branch --show-current
  git rev-parse --short HEAD
  git status --short
  git fetch -q origin
  git rev-list --count HEAD..origin/main      # насколько отстали
  git log --oneline origin/main..HEAD'        # чем разошлись
```

Прогон 2026-09-11: ветка `deploy/codex-usage-20260909`, HEAD `54f8464`,
отставание **53 коммита**, два локальных коммита, из незакоммиченного —
только сгенерированный `config/r2-filters.txt.md5` (не отслеживается,
выкату не мешает).

## 2. Доказать, что локальные коммиты избыточны

Сравнивать надо **именно те пути, которые локальные коммиты трогают**.
Пустой вывод — доказательство, что та же работа уже в `main`.

```bash
ssh doctor 'cd ~/mara-second-brain
  git diff --stat origin/main HEAD -- \
    config/r2-filters.txt docs/codex-usage.md install/mara.cron \
    scripts/codex-usage.py tests/test_codex_usage.py'
```

Прогон 2026-09-11: пусто. Работа локальных коммитов ушла в `main` как PR
#83 и #84.

**Если вывод не пуст — остановиться.** Это незалитая работа; её надо
оформить PR-ом, а не затирать.

## 3. Поставить точку возврата и выкатить

Тег дешевле сожалений: он держит старый HEAD достижимым после `reset`.

```bash
ssh doctor 'cd ~/mara-second-brain
  git tag deploy-before-$(date +%Y%m%d) $(git rev-parse HEAD)
  git checkout main -q
  git reset --hard origin/main -q
  git rev-parse --short HEAD'
```

Прогон 2026-09-11: тег `deploy-before-20260911` на `54f8464`, HEAD стал
`dca824f`.

**Откат:** `git reset --hard deploy-before-<дата>` и рестарт сервиса.

## 4. Крон

`install/mara.cron` — единственный источник правды. Руками `crontab -e` не
править никогда: правка переживёт до первого `--apply` и исчезнет.

```bash
ssh doctor 'cd ~/mara-second-brain && bash install/install-cron.sh --check'
# расхождение → bash install/install-cron.sh --apply
```

Прогон 2026-09-11: «блок в crontab совпадает с install/mara.cron», rc=0 —
`--apply` не понадобился.

## 5. Рестарт сервиса

```bash
ssh doctor 'sudo systemctl restart contextd'
ssh doctor 'systemctl is-active contextd
  systemctl show contextd -p ActiveEnterTimestamp --value
  curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8788/metrics'
```

Ждём `active`, свежую отметку времени и `200`. `/metrics` отвечает только с
loopback — снаружи он закрыт, и это проверка заодно.

Рестарт рвёт заливки в полёте. Телефон повторит сам: 413 у него
терминальный, а обрыв — нет.

## 6. Проверить, что выкатилось именно то

```bash
ssh doctor 'cd ~/mara-second-brain
  journalctl -u contextd -n 12 --no-pager | tail -8
  python3 -c "import sys; sys.path.insert(0,\"scripts\")
import contextd, mara_ingest, contextd_reconcile
print(\"импорт ок\", sys.version.split()[0])"'
```

Импорт трёх модулей — самая дешёвая проверка того, что выкаченное дерево
хотя бы синтаксически живо на той версии Python, что стоит на doctor
(3.12.3). Гейт целиком на doctor не гоняем: он идёт в CI и на BetaPi.

## 7. Чего этот ранбук НЕ делает

- **Не мигрирует схему реестра.** Это Т2.8 и гейт Г4: отдельное разрешение
  владельца, свежая резервная копия, пройденное учение восстановления.
- **Не логинит TDLib и Gmail.** Разовый `--login` требует браузера и
  аккаунтов владельца — мешок М2.
- **Не ставит APK.** Телефон — мешок М3.
- **Не удаляет ветку выката и её тег.** Пусть висят: место они не занимают,
  а точку возврата дают.

## 8. Что нашлось при выкате 2026-09-11 (не чинить здесь, вынести в issue)

- `~/.hermes` на doctor **нет вовсе** — плагин `mara-context` там не
  установлен. В журнале `contextd` видны `GET /v1/context/bootstrap -> 401
  from=127.0.0.1` от 07.09 и 09.09: локальный потребитель ходит без токена.
- В `devices` восемь строк, среди них **два** `gmail` (уже записано в #39),
  **два** `pura70` и одна с именем «Agent OS accepted engineering
  decisions» — похоже на ошибочную пару. Ни одну не отзывать наугад:
  телефон держит токен одной из двух строк `pura70`.
- `events` — 2 строки, `jobs` — пусто. Поток данных мёртв, и оживляют его
  мешки М2 и М3, а не выкат.
