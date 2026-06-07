# Спецификация: парсер eBay (`parser_ebay`)

## 1. Назначение

Сервис парсит eBay по артикулам запчастей: сначала каталоги (поисковая выдача, SRP),
затем — после внешней валидации — страницы конкретных объявлений (PDP). Все факты
складываются в базу `ebay_data` через её штатные функции; собственная база парсера
хранит только очередь задач и служебное состояние. Парсер ничего не решает — какие
объявления достойны детального парсинга, определяет валидатор каталога
(`ebay_validation_catalog`); задача этой логики — только парсить.

Масштабирование свободное: воркеры универсальны (любой воркер берёт и каталоги,
и items), их число меняется на лету, контейнер с воркерами разворачивается на любом
числе серверов. Архитектура следует конвенциям платформы `server_logic`
(очередь в своей БД, seed-задачи, адаптер-SQL, SIGTERM) — при появлении платформы
парсер регистрируется в ней без переделки.

## 2. Окружение и базы

Все базы — Postgres-контейнеры на `194.164.245.107`, в docker-сети `db_default`.
Изнутри сети видны по имени контейнера (порт 5432), снаружи — по IP и внешнему порту.
Учётка везде `admin`, пароли в `.env`.

| Роль | База (контейнер) | Внешний порт | Доступ |
|---|---|---|---|
| Очередь и состояние парсера | `parser_ebay` | 5420 | своя, читает-пишет |
| Источник целей закупки | `ebay_to_buy` | 5406 | только чтение (`purchase_feed`) |
| Артикулы смарт-деталей | `smart` | 5402 | только чтение (`part_articles`) |
| Факты eBay (каталоги, items) | `ebay_data` | 5415 | запись через функции + чтение `pdp_seen_at`, `contexts` |
| Валидатор каталога | `ebay_validation_catalog` | 5421 | чтение `validated_items`, протокол `reparse_tasks` |

Что именно используется у соседей:

- **`ebay_to_buy.purchase_feed(...)`** — функция «что закупать»: возвращает
  `smart_part_id`, `product_type`, `need_qty` и пр. Параметры запуска парсера
  пробрасываются в неё как есть: `p_product_types text[]` (фильтр категорий,
  NULL = все), include-флаги (`p_include_personal`, `p_include_in_transit`,
  `p_include_ebay_pending`, `p_include_kit_breakdown`, `p_include_virtual_kit`,
  `p_include_defect`), `p_only_need` (по умолчанию true).
- **`smart.part_articles`** — `article → part_id`: канонический список артикулов
  смарт-детали. Берутся только собственные артикулы детали; компоненты китов
  не разворачиваются (они попадают в работу, только если сами есть в фиде).
- **`ebay_data`** — контракт записи, вся дедупликация диффов на её стороне:
  - `apply_catalog_fetch(p_article text, p_zip text, p jsonb)` — весь прогон
    каталога одного артикула; payload = `dataclasses.asdict(Catalog)` из
    `ebay-library`. Не-USD позиции парсер выкидывает из payload до вызова
    (контракт базы: любая валюта кроме USD — исключение), их количество логируется.
  - `apply_item_snapshot(p_zip text, p jsonb)` — один PDP-снимок; payload =
    `asdict(ItemPage)`.
  - `items.pdp_seen_at` — признак «PDP уже парсился» (критерий постановки item-задач).
  - `contexts` — маппинг `context_id ↔ (marketplace, zip)`.
  - Нарушение контракта (артикул не из smart, дубль item_id, не-USD) — громкое
    исключение, задача уходит в `failed`, мусор в базу не пишется.
- **`ebay_validation_catalog`** — см. её SPEC.md; парсер использует два контракта,
  описанных там как «контракт потребителя» и «контракт парсера» (§5 настоящей спеки).

Прямой связи с `ebay_validation_item` (5422) у парсера нет: тот сам читает
`validated_items` и `ebay_data`.

## 3. Конвейер

```
запуск (CLI / платформа)
   └─ seed-задача {zip, product_types[], include-флаги}
        └─ координатор: purchase_feed → part_articles → catalog-задачи (по артикулу)
             └─ воркер: fetch_catalog → apply_catalog_fetch → ebay_data
                  └─ валидатор каталога (внешний, сам видит новое в ebay_data)
                       └─ координатор: курсор по validated_items → item-задачи
                            └─ воркер: fetch_item → apply_item_snapshot → ebay_data
                                 └─ валидатор item (внешний, сам видит новое)
```

Стадии самоорганизуются в одной очереди `tasks`; формального «закрытия запуска» нет:
items текут от валидатора непрерывно, состояние видно по счётчикам очереди
(`pending`/`done` по типам). `run_id` несут только seed- и catalog-задачи —
для трассировки.

### Типы задач

- **`seed`** — параметры запуска. Обрабатывает координатор: дергает
  `purchase_feed(...)` в `ebay_to_buy`, по полученным `smart_part_id` читает
  артикулы из `smart.part_articles`, вставляет catalog-задачи (идемпотентно
  по fingerprint — повторный запуск при недоработанном прошлом безвреден:
  активные дубли не плодятся).
- **`catalog`** — один артикул. Воркер собирает весь каталог
  (`fetch_catalog(page, article)` — пагинация и дедуп внутри библиотеки),
  фильтрует не-USD, вызывает `apply_catalog_fetch`. Воркер может взять пачку
  catalog-задач (до `catalog_batch_size`) на одну браузер-сессию —
  внутри используется `fetch_catalogs`, упавшая подзадача не валит пачку.
- **`item`** — один `item_id` + zip. Воркер парсит PDP (`fetch_item`),
  вызывает `apply_item_snapshot`. Если у задачи проставлен `reparse_task_id` —
  после успешной записи ставит `done_at` в `reparse_tasks` валидатора.

## 4. Запуск

Запуск — по команде. «Запустить» = вставить seed-задачу; кто вставил — не важно:

- сейчас: CLI `start_run` (точка входа в образе):
  `start_run --zip 19701 [--product-types "Для мототехники" ...] [--no-only-need] [...]`
  → строка в `runs` + seed-задача;
- потом: платформа `server_logic` делает тот же INSERT из параметров запроса
  (её штатный механизм seed), расписание — её cron.

Другой zip — другой запуск. Все задачи каскада наследуют zip из seed
(item-задачи — из контекста `validated_items`).

## 5. Интеграция с валидатором каталога

### 5.1 Новые PDP — курсор по `validated_items`

Координатор хранит закладку в своей таблице `cursors` (имя `validator_validated_at`)
и раз в `coordinator_poll_sec` читает:

```sql
SELECT item_id, part_id, context_id, status, validated_at
FROM validated_items
WHERE validated_at > %(cursor)s - interval '%(overlap)s seconds'
ORDER BY validated_at
```

Из пришедших берутся строки `status = 'approved'`; для них батч-запросом в
`ebay_data` проверяется `items.pdp_seen_at IS NULL` — **PDP делается один раз,
при первом одобрении**. Прошедшие проверку становятся item-задачами
(`ON CONFLICT (fingerprint) DO NOTHING`), закладка двигается на максимальный
`validated_at` обработанного батча. Повторное чтение из-за перекрытия безвредно
(fingerprint + проверка `pdp_seen_at`).

`validated_at` бампается валидатором только при смене отпечатка данных — холостые
прогоны каталога лавину повторов не создают (заложено в его дизайн). Все повторные
PDP — только явные, через `reparse_tasks`. Отзыв одобрения (`revoked_at`) парсер
не отслеживает: уже поставленная задача выполняется вхолостую — это дешевле логики
отмены. Re-approve после отзыва нового PDP не порождает (pdp уже есть).

### 5.2 Перепарсы — `reparse_tasks`

По протоколу из спеки валидатора, раз в `reparse_poll_sec`:

```sql
UPDATE reparse_tasks SET taken_at = now()
WHERE task_id IN (SELECT task_id FROM reparse_tasks
                  WHERE taken_at IS NULL
                  ORDER BY task_id LIMIT %(n)s
                  FOR UPDATE SKIP LOCKED)
RETURNING task_id, item_id;
```

Каждая взятая строка — item-задача с `source='reparse'` и ссылкой
`reparse_task_id`. Если на тот же item уже есть активная задача — fingerprint
конфликтует, тогда ссылка прикрепляется к существующей:
`ON CONFLICT (fingerprint) WHERE active DO UPDATE
SET reparse_task_id = COALESCE(tasks.reparse_task_id, EXCLUDED.reparse_task_id)`
(двух активных reparse по одному item валидатор не создаёт — у него частичный
индекс). После `apply_item_snapshot` воркер ставит `done_at = now()`.
Потеря воркера не подвешивает задачу: наша очередь вернёт её по lease,
`done_at` проставится при повторном выполнении.

## 6. Балансировка: каталоги не убегают от items

Требование: items обрабатываются как можно скорее после своего каталога;
число обработанных каталогов не должно сильно отрываться от обработки items.

Механика — приоритет при заборе задач:

1. item-задачи всегда берутся раньше catalog-задач (внутри типа — FIFO по `task_id`,
   поэтому items свежеотвалидированного каталога уходят в работу сразу);
2. если item-долг (`count pending type='item'`) превышает
   `catalog_throttle_threshold` — catalog-задачи не берутся вообще, воркеры
   дренируют items.

Реальная задержка «каталог → его items» определяется тиком валидатора
(30 сек + NOTIFY на его стороне); поллинг парсера в 1 сек узким местом не является.

## 7. Воркер

Один воркер = один процесс = один Xvfb-дисплей = один браузер cloakbrowser =
одна задача за раз. Параллельность наращивается числом воркеров, не контекстами
внутри браузера (изоляция сессий, простая ротация прокси в будущем).

- Браузер: `cloakbrowser.launch_async(headless=False)` на своём `DISPLAY`
  (`:100 + slot`). Headless не используется принципиально — только headed
  под виртуальным дисплеем.
- Сессия живёт между задачами: `warmup(page)` один раз после запуска браузера,
  дальше задачи идут подряд.
- Цикл: взять задачу (§6) → выполнить → `done`/`failed` → следующая.
  Пустая очередь — сон `poll_interval_sec`, процесс не завершается
  (scale-to-zero — забота платформы потом).

### Ошибки

- `ErrorPageError`, `TimeoutError`, сетевые — транзиентные: задача возвращается
  в `pending` (attempts++), браузер живёт дальше.
- `AccessDeniedError` — жёсткий блок: задача в `pending`, браузер и контекст
  пересоздаются с нуля (с прокси — это же точка `ban + rotate`).
- `ParseError` — обязательное поле не распарсилось: задача в `failed`, HTML из
  исключения сохраняется в `parse_errors` (gzip) для разбора; retention
  `parse_errors_retention_days`.
- Нарушение контракта `ebay_data` — `failed` сразу, без ретраев.
- `attempts > max_attempts` → `failed` c `last_error`; конвейер продолжается.

## 8. Схема базы `parser_ebay`

Схема `public`, миграции — SQL-файлы в `migrations/`, применяются при старте
(учёт в `schema_migrations`).

### `tasks` — единая очередь

| Колонка | Тип | Описание |
|---|---|---|
| `task_id` | bigserial PK | FIFO-порядок внутри типа |
| `type` | text | `seed` / `catalog` / `item` |
| `article` | text | для catalog |
| `item_id` | bigint | для item |
| `zip` | text | контекст eBay |
| `params` | jsonb | для seed: параметры purchase_feed |
| `source` | text | `cli` / `seed` / `validator` / `reparse` |
| `reparse_task_id` | bigint | ссылка на `reparse_tasks` валидатора (item) |
| `run_id` | bigint | трассировка (seed, catalog) |
| `status` | text | `pending` / `processing` / `done` / `failed` |
| `attempts` | smallint | инкремент при каждом взятии |
| `fingerprint` | text | `md5(type:ключ:zip)`; уникальный среди активных |
| `leased_by` | text | `host:slot` воркера |
| `leased_at` | timestamptz | для reaper |
| `created_at` / `started_at` / `finished_at` | timestamptz | |
| `last_error` | text | |

Индексы: частичный `UNIQUE (fingerprint) WHERE status IN ('pending','processing')`;
частичный `(type, task_id) WHERE status = 'pending'` (выбор задач);
`(status, finished_at)` (адаптер-SQL и очистка).

Забор: `UPDATE … WHERE task_id = (SELECT … FOR UPDATE SKIP LOCKED) RETURNING` —
семантика at-least-once, задачи идемпотентны (вся запись фактов идемпотентна
на стороне `ebay_data`).

Reaper (координатор): `processing` с `leased_at` старше `lease_timeout_sec` →
`pending`. Задачи `done`/`failed` старше `tasks_retention_days` удаляются
(вместе со сверкой-очисткой; партиционирование — при реальных объёмах, не сейчас).

### `runs` — запуски

`run_id bigserial PK`, `params jsonb` (zip, product_types, флаги),
`articles_total int` (сколько catalog-задач породил seed), `created_at`.

### `cursors` — закладки

`name text PK`, `pos timestamptz`, `updated_at`. Используется:
`validator_validated_at`.

### `worker_hosts` — управление пулом

| Колонка | Тип | Описание |
|---|---|---|
| `host` | text PK | hostname контейнера-носителя |
| `desired_workers` | int | целевое число воркеров на хосте |
| `updated_at` | timestamptz | |

Число воркеров выставляется вручную (SQL или CLI `set_workers --host X N`);
позже эту же таблицу будет крутить платформа. Супервизор нового хоста при
отсутствии своей строки создаёт её с `desired_workers_default`.

### `parse_errors` — HTML непарсящихся страниц

`id bigserial PK`, `task_id bigint`, `kind text`, `html_gz bytea`, `created_at`.

### `schema_migrations` — `name`, `applied_at`.

## 9. Процессы контейнера

Контейнер один на сервер-носитель, внутри:

- **Супервизор** (PID 1): раз в несколько секунд читает `worker_hosts` по своему
  hostname и приводит пул к целевому: добавить — поднять Xvfb на свободном слоте
  и воркер-процесс на нём; убавить — послать воркеру SIGTERM (тот дорабатывает
  текущую задачу и гаснет вместе со своим Xvfb). SIGTERM контейнера — то же самое
  для всех, ожидание до `shutdown_grace_sec`.
- **Координатор** — кандидат в каждом контейнере, активен один на всю систему
  (advisory lock в `parser_ebay`; упал носитель — лок подхватывает другой).
  Обязанности: разворачивание seed, курсор `validated_items`, забор
  `reparse_tasks`, reaper, очистка (`tasks`, `parse_errors`).
- **Воркеры** — §7.

## 10. Замеры-основания (прогоны 2026-06-07, сервер 2 vCPU / 3.3 GiB, без прокси)

- cloakbrowser 0.3.31 (chromium 146) + Xvfb + ebay-library: антибот не сработал
  ни разу; warmup 11–18 с; каталог (1 страница, 50 items) 35–45 с; PDP ~34 с.
- Сквозной контур подтверждён вживую: `apply_catalog_fetch` →
  `{"appeared": 50, "items_new": 50}`; валидатор подхватил каталог своим тиком
  сам (27 approved / 23 rejected по артикулу 866148); `apply_item_snapshot`
  принял PDP.
- Два параллельных браузера на 2 ядрах — деградация ~×2 (упор в CPU).
  Норма ёмкости: **1 воркер ≈ 1 ядро CPU и ~1–1.2 GiB RAM**.
- `playwright install-deps` не знает Ubuntu 26 — системные библиотеки хромиума
  ставятся явным списком в Dockerfile.

## 11. Конфигурация

Два источника, не пересекаются:

- **`.env`** — только подключения (не коммитится): `PARSER_DSN`, `EBAY_TO_BUY_DSN`,
  `SMART_DSN`, `EBAY_DATA_DSN`, `VALIDATOR_DSN`. Локально — IP и внешние порты,
  в проде — имена контейнеров и 5432 (`.env.example` с обоими вариантами).
- **`config.yaml`** — параметры (коммитится), читается при старте процесса:

```yaml
zip_default: "19701"

poll_interval_sec: 1            # сон воркера при пустой очереди
coordinator_poll_sec: 1         # курсор validated_items
reparse_poll_sec: 5             # забор reparse_tasks
cursor_overlap_sec: 60          # перекрытие курсорного запроса назад

catalog_batch_size: 5           # catalog-задач на одну браузер-сессию
catalog_throttle_threshold: 200 # item-долг, выше которого каталоги не берутся

max_attempts: 3
lease_timeout_sec: 1800
tasks_retention_days: 14        # done/failed
parse_errors_retention_days: 7

desired_workers_default: 1      # для нового хоста без строки в worker_hosts
shutdown_grace_sec: 120

proxy:
  enabled: false                # включается, когда в proxy-manager появится пул
  server_url: "http://194.164.245.107:8099"
  scope: "ebay"
```

## 12. Деплой

По общему шаблону `/Users/stepan/Desktop/projects/DEPLOY_TEMPLATE.md`:
`git push main` → GHA + Docker Build Cloud → `ghcr.io/stepan2222000/parser-ebay`
→ SSH-деплой. Каталог на сервере: `/root/parser_ebay_app`.

Образ: python 3.13+, `cloakbrowser` (бинарь хромиума скачивается на этапе build —
`ensure_binary()`, кэшируется слоем), `ebay-library` (git), `psycopg[binary]`,
Xvfb + системные библиотеки хромиума + шрифты. Воркеры-носители — любые серверы
(сейчас тест: `144.31.167.227`); базы доступны по внешним IP:порт, на
`194.164.245.107` — через сеть `db_default`.

## 13. Интеграция с платформой `server_logic`

Парсер уже соблюдает её конвенции, регистрация потом сводится к конфигу:

- очередь в собственной БД, забор `FOR UPDATE SKIP LOCKED`, lease + reaper,
  идемпотентная вставка стадий по fingerprint;
- seed-задача как универсальный вход (INSERT из параметров запроса);
- адаптер-SQL: backlog — `SELECT count(*) FROM tasks WHERE status='pending'`;
  done за интервал — `SELECT count(*) FROM tasks WHERE status='done' AND
  finished_at > now() - $1`;
- корректный SIGTERM, образ на публичном registry;
- управление мощностью: платформа пишет в `worker_hosts` (вместо ручного CLI)
  и/или крутит replicas контейнеров.

## 14. Вне рамок (задел на будущее)

- **Прокси** — интерфейс `proxymgr` заложен (acquire на сессию воркера,
  `report()` после каждой задачи, `ban + rotate` на `AccessDeniedError`,
  `LeaseLost` → новая аренда); включается флагом конфига, когда пул scope `ebay`
  будет наполнен.
- **Фильтрация трафика / кэш (`ebay_filtering`)** — в воркере предусмотрен хук
  route-handler на контекст браузера; жёсткий слой и адаптивный кэш подключаются
  отдельным шагом по его спеке.
- **Несколько zip-контекстов** — zip уже в ключах задач и параметрах запуска;
  добавление контекста = ещё один запуск с другим zip.
- **Принудительный refresh старых PDP** — отдельный тип задач, когда понадобится.
- **Дашборд/метрики** — пока счётчики SQL по очереди + docker logs; метрики
  платформы появятся с её стороны.
