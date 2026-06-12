# Спецификация: парсер eBay (`parser_ebay`) — v2

## 1. Назначение

Оркестратор парсинга eBay по смарт-деталям: держит список того, что надо
обработать (смарт-детали для каталогов, item'ы для PDP), и пул браузерных
воркеров, которые это обрабатывают. Сам парсинг, конвертация цен в USD и
запись фактов в `ebay_data` — целиком внутри библиотеки `ebaylib`
(`run_worker` + `EbaySession` + `Store`); решения «какие item'ы достойны PDP»
принимает внешний валидатор (`ebay_validation_catalog`). Задача этой системы —
только сказать воркерам, ЧТО парсить, и держать это знание актуальным.

Архитектура следует принципам платформы `server_logic` (DESIGN.md там):
своя БД, вход через INSERT, воркер = будущий под, адаптер-SQL для
backlog/скорости. До появления платформы всё управляется CLI и тонким
супервизором.

## 2. Окружение и базы

Все базы — Postgres-контейнеры на `194.164.245.107`, в docker-сети
`db_default`. Изнутри сети — по имени контейнера (порт 5432), снаружи — по IP
и внешнему порту. Учётка везде `admin`, пароли в `.env`.

| Роль | База (контейнер) | Внешний порт | Кто ходит |
|---|---|---|---|
| Наша: runs, tasks, пул, FDW-вьюхи | `parser_ebay` | 5420 | координатор, воркеры, CLI, платформа |
| Цели закупки (`purchase_feed(...)`) | `ebay_to_buy` | 5406 | координатор (чтение) |
| Артикулы смарт-деталей (`part_articles`) | `smart` | 5402 | координатор (чтение) |
| Факты eBay (пишет `ebaylib.Store`) | `ebay_data` | 5415 | воркеры (через Store), у нас FDW |
| Валидатор каталога | `ebay_validation_catalog` | 5421 | у нас FDW (чтение `validated_items`, `reparse_tasks` + UPDATE `done_at`) |
| fx-микросервис (валюты → USD) | — | 8092 (HTTP) | библиотека сама (`FX_API_URL`) |

**FDW.** В `parser_ebay` создаётся `postgres_fdw` на `ebay_data` (схема
`ebay_fdw`: `catalog_fetches`, `items`, `contexts`) и на
`ebay_validation_catalog` (схема `validation_fdw`: `validated_items`,
`reparse_tasks`). Это даёт: координатору — потребности одним SQL без
клиентских пересечений; платформе — вьюхи «done за интервал» без
дублирования данных (правило: чужие данные не копируем, вьюхи — можно).
`purchase_feed` — функция, через FDW не зовётся: координатор читает
`ebay_to_buy` и `smart` обычными соединениями.

**Распределение ролей с библиотекой `ebaylib` (v2):**

| Делает библиотека | Делает парсер |
|---|---|
| цикл `run_worker` («задача → парсинг → запись → task_done») | поставка задач (`next_task`) и подтверждение (`task_done`) |
| `EbaySession`: warmup, пагинация SRP, PDP+iframe, ZIP-флоу | `get_page`: браузер, контексты, Xvfb (позже — прокси) |
| замена страницы при Access Denied / смерти транспорта | пул воркер-процессов и их перезапуск после смерти |
| конвертация цен в USD (fx) | список потребностей и его актуальность (координатор) |
| запись в `ebay_data` (`Store` → `apply_catalog_fetch`/`apply_item_snapshot`) | runs (вход), CLI, FDW-вьюхи статистики |
| `e.task` — задача-виновница на исключении при смерти | сохранение HTML из `ParseError` в `parse_errors` |

## 3. Модель: tasks = потребности

Никакой классической очереди со статусами нет. `tasks` — это **текущий список
того, что должно быть обработано**; координатор циклически синхронизирует его
с реальностью, воркеры забирают и по факту записи удаляют. Факт выполнения
живёт не у нас, а в `ebay_data` (`catalog_fetches.fetched_at`,
`items.pdp_seen_at`) — поэтому упавшая задача никуда не теряется: её
потребность не исчезла, координатор её снова вставит.

```
runs (вход: параметры) ──▶ координатор ──▶ tasks (потребности) ──▶ воркеры
                                ▲                                      │
        purchase_feed × smart   │            ebaylib.run_worker:       │
        свежесть: ebay_fdw      │            парсинг → Store → ebay_data
        approved: validation_fdw└──────────── task_done → DELETE задачи
```

### Виды потребностей

- **catalog** — смарт-деталь: «по всем артикулам детали нет прогона каталога
  свежее порога (run-профиль: zip+condition+цены)». Одна задача = одна деталь
  со ВСЕМИ её артикулами (`articles[]`) — так валидатор видит деталь целиком,
  а переотдача атомарна.
- **item** (source `validator`) — «item одобрен валидатором
  (`status='approved'`), а PDP ещё не было (`pdp_seen_at IS NULL`)».
  PDP делается один раз; повторные — только через reparse. Не привязан к run.
- **item** (source `reparse`) — «в `reparse_tasks` валидатора есть строка с
  `done_at IS NULL`». После записи PDP воркер ставит `done_at` (через FDW) —
  потребность гаснет. `taken_at` не используем (дубль безвреден, запись
  идемпотентна).

### Свежесть каталогов и режимы

Порог свежести задаётся режимом run:

- `mode=once`: порог = `runs.created_at` — каждая деталь парсится один раз
  за запуск; все спарсили → потребностей нет, run исчерпан (но активен до
  `stop-run` — если деталь появится в фиде, она будет обработана).
- `mode=continuous`: порог = `now() − catalog_refresh_sec` — каталоги
  устаревают по скользящему окну и переотправляются сами; парсер крутится
  сутками. Это «режим без конца» из server_logic §8.6.

Деталь «свежая» ⇔ **каждый** её артикул имеет `catalog_fetches` с
`fetched_at ≥ порога` в профиле run. Список деталей — живой: координатор
перечитывает `purchase_feed` (раз в `feed_refresh_sec`), ушедшие из фида
детали перестают быть потребностью в обоих режимах (закупили — парсить
незачем), новые — появляются.

## 4. Схема базы `parser_ebay`

Миграции — SQL в `migrations/`, применяются при старте (учёт в
`schema_migrations`). FDW-объекты — отдельным идемпотентным скриптом
(паттерн `setup_fdw.py` из ebay_data).

### `runs` — вход системы

| Колонка | Тип | Описание |
|---|---|---|
| `run_id` | bigserial PK | |
| `params` | jsonb | zip, condition, min/max_price, product_types, include-флаги и only_need (purchase_feed), mode, catalog_refresh_sec |
| `is_active` | boolean not null default true | false = остановлен |
| `created_at` | timestamptz | порог свежести для `once` |

`start-run` = INSERT, `stop-run` = `UPDATE is_active=false` — ровно то, что
потом будет делать платформа из `POST /run` и кнопки «Стоп» (server_logic
§8.6). Параллельные активные run допустимы (разные профили/категории);
пересечение потребностей гасится fingerprint'ом.

### `tasks` — потребности

| Колонка | Тип | Описание |
|---|---|---|
| `task_id` | bigserial PK | FIFO внутри типа |
| `type` | text | `catalog` / `item` |
| `part_id` | text | catalog: смарт-деталь |
| `articles` | text[] | catalog: все её артикулы |
| `item_id` | bigint | item |
| `zip` | text | контекст |
| `condition` | text | catalog: фильтр профиля (`all`/`new`/`used`) |
| `min_price`, `max_price` | numeric | catalog: границы профиля (сейчас NULL) |
| `run_id` | bigint | catalog: чей профиль; item: NULL |
| `source` | text | `feed` / `validator` / `reparse` |
| `reparse_task_id` | bigint | item-reparse: строка в `reparse_tasks` валидатора |
| `fingerprint` | text UNIQUE | catalog: `part_id+zip+condition+цены`; item: `item_id+zip` |
| `created_at` | timestamptz | |
| `dispatched_at` | timestamptz | NULL = доступна; выдана воркеру |
| `dispatched_to` | text | диагностика: host:slot |

Статусов нет: выполнена (task_done) → DELETE. Индексы: UNIQUE (fingerprint);
`(type, task_id) WHERE dispatched_at IS NULL` (забор).

### `worker_hosts` — пул (до платформы)

`host PK, desired_workers int, updated_at`. Меняется CLI `set-workers`;
с приходом платформы — replicas подов, таблица отомрёт.

### `parse_errors` — сырьё для починки селекторов

`id PK, task_fingerprint text, kind text, error text, html_gz bytea,
created_at`. Retention `parse_errors_retention_days`.

### FDW-вьюхи для адаптера платформы

- pending: `SELECT count(*) FROM tasks WHERE type=$1 AND dispatched_at IS NULL`
- done за интервал: `stats_catalog_done` (view на
  `ebay_fdw.catalog_fetches.fetched_at`), `stats_items_done` (view на
  `ebay_fdw.items.pdp_seen_at`).

## 5. Координатор

Один активный на систему: каждый контейнер — кандидат, лидер берёт
advisory lock в `parser_ebay`; упал — лок перехватывается. Цикл раз в
`coordinator_poll_sec`:

1. **Каталоги.** Для активных run: список деталей из `purchase_feed(params)`
   (кэш на `feed_refresh_sec`) + артикулы из `smart.part_articles`; деталь
   несвежая (см. §3) и нет строки в `tasks` → INSERT
   (`ON CONFLICT (fingerprint) DO NOTHING`). Свежесть — одним SQL через
   `ebay_fdw.catalog_fetches` (профиль run → `profile_id` через
   `ebay_fdw.search_profiles`... профиль резолвится по (context, condition,
   цены); упрощение — join по этим полям).
2. **Items.** `validation_fdw.validated_items (status='approved')` LEFT JOIN
   `ebay_fdw.items` ON item_id WHERE `pdp_seen_at IS NULL` → INSERT item-задач
   (zip — из контекста `validated_items.context_id` → `ebay_fdw.contexts`).
3. **Reparse.** `validation_fdw.reparse_tasks WHERE done_at IS NULL` →
   INSERT item-задач (source `reparse`, zip = `zip_default`).
4. **Чистка.** Задачи с `dispatched_at IS NULL`, чья потребность исчезла
   (деталь стала свежей / ушла из фида / run остановлен; item получил
   `pdp_seen_at`; reparse получил `done_at`) → DELETE. Выданные не трогаем —
   воркер доделает, лишняя запись идемпотентна и безвредна.
5. **Перевыдача.** `dispatched_at < now() − dispatch_timeout_sec` → сброс
   `dispatched_at/dispatched_to` в NULL (воркер умер или завис; если он
   всё-таки допишет — не страшно, см. выше).
6. **Retention** `parse_errors`.

Смерть воркера координатора не интересует — упавшие задачи возвращаются
через потребности (п.1–3) и перевыдачу (п.5). Это замена lease+reaper из
server_logic §9.2 с той же семантикой at-least-once.

## 6. Воркер

Один воркер = один процесс = один Xvfb-дисплей (`:100+slot`) = один браузер
cloakbrowser (`headless=False`) = одна задача за раз. Это будущий под
платформы (единица мощности — воркер); ресурсный ориентир из замеров:
~1 CPU, ~1.2 GiB RAM.

Процесс запускает `ebaylib.run_worker(get_page, next_task, Store(dsn),
task_done=...)` и реализует три колбека:

- **`get_page`** — закрыть предыдущий context → новый context → новая page.
  Зовётся библиотекой лениво и при заменах страниц (Access Denied / смерть
  транспорта — без лимита, политика «жёстко»: темп пауз задаёт библиотека).
  Будущая точка прокси: новый context = новая аренда из proxy-manager.
- **`next_task`** — цикл: SIGTERM получен → вернуть `None` (библиотека
  допишет хвост записи и штатно выйдет); иначе забор:

  ```sql
  UPDATE tasks SET dispatched_at = now(), dispatched_to = $me
  WHERE task_id = (SELECT task_id FROM tasks
                   WHERE dispatched_at IS NULL
                   ORDER BY (type = 'item') DESC, task_id
                   LIMIT 1 FOR UPDATE SKIP LOCKED)
  RETURNING ...
  ```

  Items всегда раньше каталогов (абсолютный приоритет — «сразу после
  каталога его item'ы», как только валидатор их пропустит), FIFO внутри
  типа. Пусто → sleep `poll_interval_sec`, снова. Задача → формат библиотеки:
  `{"type": "catalog", "params": {"articles": [...], "zip", "condition",
  "min_price", "max_price"}, "task_id": N, ...}` /
  `{"type": "item", "params": {"item_id", "zip"}, ...}` — всё вне `params`
  библиотека вернёт в `task_done` как есть.
- **`task_done(task, stats)`** — строго после записи в ebay_data:
  DELETE задачи по `task_id`; для reparse — `UPDATE
  validation_fdw.reparse_tasks SET done_at = now()`. Stats — в лог.

### Смерти

Любая критическая ошибка (`ParseError`, `ErrorPageError`, Pardon/iframe
таймауты, сбой fx, сбой записи, `TaskFormatError`) валит `run_worker` —
обёртка процесса: читает `e.task` (виновница), для `ParseError` пишет HTML в
`parse_errors`, логирует и умирает. Супервизор поднимает новый процесс через
`restart_delay_sec`. Висящая выдача вернётся перевыдачей (§5.5); битая задача
будет реинкарнироваться и убивать воркеров, громко крася лог, пока не починим
селекторы — осознанно жёсткая политика, dead-letter'а нет.

## 7. Процессы контейнера

Образ один; режим — env `WORKERS`:

- `WORKERS=N` (под платформу: N=1) — ровно N воркер-процессов, без таблицы;
- без `WORKERS` (дефолт, до платформы) — супервизор: читает `worker_hosts`
  по hostname (нет строки → создаёт с `desired_workers_default`), приводит
  пул: поднять Xvfb+воркер на свободный слот / SIGTERM лишнему (доработка
  задачи, гашение). Перезапуск умерших — через `restart_delay_sec`.

Координатор-кандидат — в каждом контейнере (лок выберет одного). SIGTERM
контейнера → SIGTERM всем воркерам → ожидание до `shutdown_grace_sec`.

## 8. Запуск и CLI

```
start-run [--zip 19701] [--condition new] [--product-types ...]
          [--mode continuous|once] [--refresh-sec 86400]
          [--no-include-personal ... --no-only-need]   # флаги purchase_feed
stop-run <run_id>
set-workers --host HOST N
status        # активные run, потребности по типам, выданное, темп из FDW-вьюх
```

`start-run` валидирует категории по `smart.product_types` и печатает
`run_id`. Дефолты: zip/condition/mode/refresh из `config.yaml`.

## 9. Конфигурация

- **`.env`** (не коммитится): `PARSER_DSN`, `EBAY_TO_BUY_DSN`, `SMART_DSN`,
  `EBAY_DATA_DSN` (уходит в `ebaylib.Store`), опц. `FX_API_URL`.
  FDW-пароли — в `setup_fdw`-скрипте из тех же env.
- **`config.yaml`** (коммитится, читается при старте):

```yaml
zip_default: "19701"
condition_default: "new"
mode_default: "continuous"
catalog_refresh_sec_default: 86400   # continuous: окно свежести каталогов

coordinator_poll_sec: 5     # цикл координатора
feed_refresh_sec: 60        # кэш purchase_feed
poll_interval_sec: 1        # сон воркера при пустых tasks
dispatch_timeout_sec: 1800  # перевыдача зависшей выдачи
restart_delay_sec: 5        # пауза перед перезапуском умершего воркера
shutdown_grace_sec: 120
desired_workers_default: 1
parse_errors_retention_days: 14
```

## 10. Интеграция с server_logic

- **Вход**: INSERT в `runs` / UPDATE `is_active` — платформенный `POST /run`,
  «Стоп»; режим «без конца» = `mode=continuous` (§8.6 DESIGN).
- **Единица мощности — воркер**: контейнер с `WORKERS=1` = под; контроллер
  ставит replicas, шедулер пакует по requests (~1 CPU / 1.2 GiB).
- **Адаптер-SQL**: pending — count по `tasks` (по типам); done за интервал —
  FDW-вьюхи `stats_catalog_done` / `stats_items_done` (факты в `ebay_data`,
  без дублей).
- **Идемпотентность** at-least-once: повторная запись в `ebay_data` — нулевые
  диффы; перевыдача = аналог lease+reaper. Отступление от §9.2: dead-letter
  по attempts нет — критические ошибки чинятся, а не прячутся.
- Корректный SIGTERM, публичный образ — соблюдены.

## 11. Замеры-основания (прогоны 2026-06-07, 2 vCPU / 3.3 GiB, без прокси)

- cloakbrowser 0.3.31 + Xvfb + библиотека: антибот не сработал; warmup
  11–18 с; каталог (страница, 50 карточек) 35–45 с; PDP ~34 с.
- Сквозной контур жив: каталог → `ebay_data` → валидатор сам вынес вердикты
  (27 approved / 23 rejected) → PDP записан.
- 2 браузера на 2 ядрах — деградация ×2: **1 воркер ≈ 1 ядро + ~1.2 GiB**.
- Ubuntu 26: системные библиотеки хромиума ставить явным списком
  (`playwright install-deps` её не знает).

## 12. Деплой

По `/Users/stepan/Desktop/projects/DEPLOY_TEMPLATE.md`: GHA + Docker Build
Cloud → `ghcr.io/stepan2222000/parser-ebay` → SSH. Каталог
`/root/parser_ebay_app`. Образ: python 3.13+, Xvfb + библиотеки хромиума +
шрифты, `cloakbrowser` (бинарь в build-слой через `ensure_binary()`),
`ebay-library` (git), `asyncpg`, `pyyaml`. Тестовый сервер воркеров:
`144.31.167.227`.

## 13. Вне рамок (заделы)

- **Прокси** (proxy-manager, scope `ebay`) — точка входа готова: `get_page`;
  включение конфигом, когда нальют пул.
- **Фильтрация трафика / кэш** (`ebay_filtering`) — route-handler на context
  в `get_page`, отдельным шагом.
- **Фото** (`fetch_images` + S3) — не наша зона, ждёт фотохранилище.
- **Сужение выдачи ценой** (`min/max_price` per-артикул из parts_prices) —
  колонки и проброс готовы, логика потом.
- **Несколько zip** — zip в ключах задач и параметрах run.
- **Refresh PDP** (повторные снапшоты живых item'ов) — отдельный вид
  потребности, когда понадобится.
- **Дашборд** — пока `status` + SQL; далее платформа.
