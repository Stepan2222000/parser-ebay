# Спецификация: парсер eBay (`parser_ebay`) — v2

## 1. Назначение

Оркестратор парсинга eBay по смарт-деталям: держит актуальный список того,
что надо обработать (смарт-детали для каталогов, item'ы для PDP), и пул
браузерных воркеров, которые это обрабатывают. Сам парсинг, конвертация цен
в USD и запись фактов в `ebay_data` — внутри библиотеки `ebaylib`; решения
«какие item'ы достойны PDP» — внешний валидатор (`ebay_validation_catalog`).
Задача этой системы — только сказать воркерам, ЧТО парсить, и держать это
знание актуальным. Принципы платформы `server_logic` соблюдены — детали
стыковки в §10.

## 2. Окружение и границы

Все базы — Postgres-контейнеры на `194.164.245.107`, в docker-сети
`db_default`. Изнутри сети — по имени контейнера (порт 5432), снаружи — по
IP и внешнему порту. Учётка везде `admin`, пароли в `.env`.

| Роль | База (контейнер) | Внешний порт | Кто ходит |
|---|---|---|---|
| Наша: runs, tasks, пул, FDW-вьюхи | `parser_ebay` | 5420 | координатор, воркеры, CLI, платформа |
| Цели закупки: `purchase_feed(...)`, `effective_season_months()` | `ebay_to_buy` | 5406 | координатор (чтение) |
| Артикулы смарт-деталей (`part_articles`) | `smart` | 5402 | координатор (чтение) |
| Факты eBay | `ebay_data` | 5415 | воркеры (через `ebaylib.Store`), у нас FDW |
| Валидатор каталога | `ebay_validation_catalog` | 5421 | у нас FDW |
| fx-микросервис (валюты → USD) | — | 8092 (HTTP) | библиотека сама (`FX_API_URL`) |

**FDW.** В `parser_ebay` создаётся `postgres_fdw`: схема `ebay_fdw`
(`catalog_fetches`, `items`, `contexts`, `search_profiles` из `ebay_data`) и
`validation_fdw` (`validated_items`, `reparse_tasks` из базы валидатора).
Зачем: координатору — потребности одним SQL; платформе — вьюхи «done за
интервал» по чужим фактам. Правило: чужие данные не копируем, вьюхи — можно.
`purchase_feed` / `effective_season_months` — функции, через FDW не зовутся:
к `ebay_to_buy` и `smart` координатор ходит обычными соединениями.

**Распределение ролей с библиотекой `ebaylib` (v2):**

| Делает библиотека | Делает парсер |
|---|---|
| цикл `run_worker` («задача → парсинг → запись → task_done») | поставка задач (`next_task`) и подтверждение (`task_done`) |
| `EbaySession`: warmup, пагинация SRP (до 5 страниц/запрос), PDP+iframe, ZIP-флоу | `get_page`: браузер, контексты, Xvfb (позже — прокси) |
| замена страницы при Access Denied / смерти транспорта (без лимита) | пул воркер-процессов и их перезапуск после смерти |
| конвертация цен в USD (fx) | список потребностей и его актуальность (координатор) |
| запись в `ebay_data` (`Store` → `apply_catalog_fetch` / `apply_item_snapshot`) | runs (вход), CLI, FDW-вьюхи статистики |
| `e.task` — задача-виновница на исключении | сохранение HTML из `ParseError` в `parse_errors` |

## 3. Модель: tasks = потребности

Классической очереди со статусами нет. `tasks` — **текущий список того, что
должно быть обработано**; координатор циклически синхронизирует его с
реальностью, воркеры забирают и по факту записи удаляют. Факт выполнения
живёт не у нас, а в `ebay_data` (`catalog_fetches.fetched_at`,
`items.pdp_seen_at`) — поэтому упавшая задача не теряется: её потребность не
исчезла, координатор вставит её снова. Семантика at-least-once; повторная
запись в `ebay_data` идемпотентна (нулевые диффы).

```
runs (вход: параметры) ──▶ координатор ──▶ tasks (потребности) ──▶ воркеры
                                ▲                                      │
        purchase_feed × smart   │            ebaylib.run_worker:       │
        свежесть: ebay_fdw      │            парсинг → Store → ebay_data
        approved: validation_fdw└──────────── task_done → DELETE задачи
```

### Виды потребностей

- **catalog** — смарт-деталь: «по артикулам детали нет прогона каталога
  свежее порога в профиле run (zip+condition+цены)». Одна задача = одна
  деталь со ВСЕМИ её артикулами (`articles[]`) — валидатор видит деталь
  целиком, переотдача атомарна.
- **item** (source `validator`) — «item одобрен (`status='approved'`), а PDP
  ещё не было (`pdp_seen_at IS NULL`)». PDP делается один раз; повторные —
  только через reparse. К run не привязан.
- **item** (source `reparse`) — «строка `reparse_tasks` валидатора с
  `done_at IS NULL`». После записи PDP воркер ставит `done_at` — потребность
  гаснет. `taken_at` не используем: дубль безвреден.

### Откуда берутся детали: фид и сезонное окно

Список деталей — живой: координатор раз в `feed_refresh_sec` перечитывает
`purchase_feed(p_months, include-флаги, p_only_need)` в `ebay_to_buy` и
артикулы из `smart.part_articles`. Деталь ушла из фида (закупили) →
потребность исчезает; появилась — добавляется. Это работает в обоих режимах.

`p_months` — глобальное **сезонное окно** `ebay_to_buy` (`app_settings`:
season-filter / season-months-ahead, та же логика, что в UI закупки):
координатор берёт его свежим вызовом `effective_season_months()` при каждом
пересеве — окно скользит само, настройка правится в `ebay_to_buy` без
перезапуска парсера. Run с `season='ignore'` (CLI `--ignore-season`) сезон
не применяет (полный фид).

### Свежесть каталогов и режимы run

- `mode=once`: порог свежести = `runs.created_at` — каждая деталь парсится
  один раз за запуск; всё спарсили → потребностей нет (run активен до
  `stop-run`: новые детали фида будут обработаны).
- `mode=continuous`: порог = `now() − catalog_refresh_sec` — каталоги
  устаревают по скользящему окну и переотправляются сами; парсер крутится
  сутками.

Деталь «свежая» ⇔ **каждый** её артикул имеет в `ebay_data.catalog_fetches`
прогон с `fetched_at ≥ порога` в профиле run.

## 4. Схема базы `parser_ebay`

Миграции — SQL в `migrations/`, применяются при старте (учёт в
`schema_migrations`). FDW-объекты — отдельным идемпотентным скриптом
(паттерн `setup_fdw.py` из ebay_data).

### `runs` — вход системы

| Колонка | Тип | Описание |
|---|---|---|
| `run_id` | bigserial PK | |
| `params` | jsonb | zip, condition, min/max_price, season ('auto'/'ignore'), include-флаги и only_need (purchase_feed), mode, catalog_refresh_sec |
| `is_active` | boolean not null default true | false = остановлен |
| `created_at` | timestamptz | порог свежести для `once` |

`start-run` = INSERT, `stop-run` = `UPDATE is_active = false`. Параллельные
активные run допустимы (разные профили); пересечение потребностей гасит
fingerprint.

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
| `dispatched_at` | timestamptz | NULL = доступна; иначе выдана воркеру |
| `dispatched_to` | text | диагностика: host:slot |

Выполнена (`task_done`) → DELETE. Индексы: UNIQUE (fingerprint);
частичный `(type, task_id) WHERE dispatched_at IS NULL` (забор).

### `worker_hosts` — целевой размер пула на сервер

`host PK, desired_workers int, updated_at`. Меняется CLI `set-workers`
(нет строки для хоста → супервизор создаёт с `desired_workers_default`).

### `parse_errors` — сырьё для починки селекторов

`id PK, task_fingerprint text, kind text, error text, html_gz bytea,
created_at`. Retention `parse_errors_retention_days`.

### Вьюхи статистики (для `status` и адаптера платформы)

`stats_catalog_done` — по `ebay_fdw.catalog_fetches.fetched_at`;
`stats_items_done` — по `ebay_fdw.items.pdp_seen_at`. Backlog — count по
`tasks` (`dispatched_at IS NULL`, по типам).

## 5. Координатор

Один активный на систему: каждый контейнер — кандидат, лидер держит
advisory lock в `parser_ebay`; упал — лок перехватывает другой. Цикл раз в
`coordinator_poll_sec`:

1. **Каталоги.** Для активных run: детали фида (§3, кэш `feed_refresh_sec`)
   × несвежие по `ebay_fdw.catalog_fetches` (профиль резолвится join'ом
   `ebay_fdw.search_profiles` по zip+condition+ценам) и отсутствующие в
   `tasks` → INSERT `ON CONFLICT (fingerprint) DO NOTHING`.
2. **Items.** `validation_fdw.validated_items` (`status='approved'`) LEFT
   JOIN `ebay_fdw.items` WHERE `pdp_seen_at IS NULL` → INSERT (zip — из
   `validated_items.context_id` → `ebay_fdw.contexts`).
3. **Reparse.** `validation_fdw.reparse_tasks WHERE done_at IS NULL` →
   INSERT (source `reparse`, zip = `zip_default`).
4. **Чистка.** Невыданные задачи (`dispatched_at IS NULL`), чья потребность
   исчезла (деталь посвежела / ушла из фида / run остановлен; item получил
   `pdp_seen_at`; reparse закрыт) → DELETE. Выданные не трогаем: воркер
   доделает, лишняя запись безвредна.
5. **Перевыдача.** `dispatched_at < now() − dispatch_timeout_sec` → сброс
   `dispatched_at/dispatched_to` (воркер умер или завис).
6. **Retention** `parse_errors`.

Смерть воркера координатора не интересует: упавшие задачи возвращаются
потребностями (п.1–3) и перевыдачей (п.5).

## 6. Воркер

Один процесс = один Xvfb-дисплей (`:100+slot`) = один браузер cloakbrowser
(`headless=False`) = одна задача за раз. Процесс запускает
`ebaylib.run_worker(get_page, next_task, Store(EBAY_DATA_DSN), task_done)`
и реализует три колбека:

- **`get_page`** — закрыть предыдущий context → новый context → новая page.
  Библиотека зовёт лениво и при заменах страниц. Будущая точка прокси:
  новый context = новая аренда из proxy-manager.
- **`next_task`** — получен SIGTERM → вернуть `None` (библиотека допишет
  хвост записи и штатно выйдет); иначе забор, пусто → sleep
  `poll_interval_sec` и снова:

  ```sql
  UPDATE tasks SET dispatched_at = now(), dispatched_to = $me
  WHERE task_id = (SELECT task_id FROM tasks
                   WHERE dispatched_at IS NULL
                   ORDER BY (type = 'item') DESC, task_id
                   LIMIT 1 FOR UPDATE SKIP LOCKED)
  RETURNING ...
  ```

  Item'ы всегда раньше каталогов (абсолютный приоритет: «сразу после
  каталога — его item'ы», как только валидатор их пропустит), FIFO внутри
  типа. Строка → задача формата библиотеки: `{"type": "catalog", "params":
  {"articles": [...], "zip", "condition", "min_price", "max_price"},
  "task_id": N, "reparse_task_id": ...}` / `{"type": "item", "params":
  {"item_id", "zip"}, ...}` — всё вне `params` библиотека вернёт в
  `task_done` как есть.
- **`task_done(task, stats)`** — зовётся библиотекой строго после записи в
  `ebay_data`: DELETE задачи по `task_id`; у reparse — ещё `UPDATE
  validation_fdw.reparse_tasks SET done_at = now()`. Stats — в лог.

### Смерти

Любая критическая ошибка (`ParseError`, `ErrorPageError`, Pardon/iframe
таймауты, сбой fx, сбой записи, `TaskFormatError`) валит `run_worker`.
Обёртка процесса: читает `e.task` (виновница), для `ParseError` пишет HTML в
`parse_errors`, логирует и умирает. Битая задача будет реинкарнироваться и
убивать воркеров, громко крася лог, пока не починим селекторы — осознанно
жёсткая политика, dead-letter'а нет.

## 7. Процессы контейнера

Контейнер один на сервер. Внутри:

- **Супервизор** (PID 1): приводит пул к `worker_hosts.desired_workers` —
  поднять Xvfb+воркер на свободный слот / SIGTERM лишнему (дорабатывает
  задачу и гаснет). Умерших перезапускает через `restart_delay_sec`.
  SIGTERM контейнера → SIGTERM всем, ожидание до `shutdown_grace_sec`.
- **Координатор-кандидат** (§5).
- **Воркеры** (§6).

## 8. Запуск и CLI

```
start-run [--zip 19701] [--condition new] [--mode continuous|once]
          [--refresh-sec 86400] [--ignore-season]
          [--no-include-personal ... --no-only-need]   # флаги purchase_feed
stop-run <run_id>
set-workers --host HOST N
status        # активные run, потребности/выдано по типам, темп из вьюх §4
```

Дефолты zip/condition/mode/refresh — из `config.yaml`; `start-run` печатает
`run_id`.

## 9. Конфигурация

- **`.env`** (не коммитится): `PARSER_DSN`, `EBAY_TO_BUY_DSN`, `SMART_DSN`,
  `EBAY_DATA_DSN` (уходит в `ebaylib.Store`), опц. `FX_API_URL`.
  FDW-скрипт берёт пароли из тех же env.
- **`config.yaml`** (коммитится, читается при старте):

```yaml
zip_default: "19701"
condition_default: "new"
mode_default: "continuous"
catalog_refresh_sec_default: 86400   # continuous: окно свежести каталогов

coordinator_poll_sec: 5     # цикл координатора
feed_refresh_sec: 60        # кэш purchase_feed + сезонного окна
poll_interval_sec: 1        # сон воркера при пустых tasks
dispatch_timeout_sec: 1800  # перевыдача зависшей выдачи
restart_delay_sec: 5        # пауза перед перезапуском умершего воркера
shutdown_grace_sec: 120
desired_workers_default: 1
parse_errors_retention_days: 14
```

## 10. Интеграция с server_logic

- **Вход** — INSERT/UPDATE в `runs` (§4): платформенный `POST /run` и кнопка
  «Стоп» делают то же; «режим без конца» = `mode=continuous`.
- **Единица мощности — воркер** (один из пула на сервере); платформа будет
  управлять их числом, ресурсный ориентир — из замеров §11.
- **Адаптер-SQL** — готов из коробки: backlog и done за интервал — вьюхи §4.
- **Lease/reaper из её соглашений** покрыты перевыдачей §5.5 при той же
  семантике at-least-once. Осознанное отступление: dead-letter по attempts
  нет (§6 «Смерти»).
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

- **Прокси** (proxy-manager, scope `ebay`) — точка входа: `get_page`;
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
