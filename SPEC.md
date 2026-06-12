# Спецификация: парсер eBay (`parser_ebay`) — v2

## 1. Назначение

Оркестратор парсинга eBay по смарт-деталям: ведёт журнал задач (смарт-детали
для каталогов, item'ы для PDP), которые обрабатывают масштабируемые снаружи
браузерные воркеры. Сам парсинг, конвертация цен в USD и запись фактов в
`ebay_data` — внутри библиотеки `ebaylib`; решения «какие item'ы достойны
PDP» — внешний валидатор (`ebay_validation_catalog`). Задача этой системы —
сказать воркерам, ЧТО парсить, держать этот список актуальным и копить
историю обработки. Принципы платформы `server_logic` соблюдены — стыковка
в §10.

## 2. Окружение и границы

Все базы — Postgres-контейнеры на `194.164.245.107`, в docker-сети
`db_default`. Изнутри сети — по имени контейнера (порт 5432), снаружи — по
IP и внешнему порту. Учётка везде `admin`, пароли в `.env`.

| Роль | База (контейнер) | Внешний порт | Кто ходит |
|---|---|---|---|
| Наша: runs, tasks | `parser_ebay` | 5420 | координатор, воркеры, CLI, платформа |
| Цели закупки: `purchase_feed(...)`, `effective_season_months()` | `ebay_to_buy` | 5406 | координатор (чтение) |
| Артикулы смарт-деталей (`part_articles`) | `smart` | 5402 | координатор (чтение) |
| Факты eBay | `ebay_data` | 5415 | воркеры (через `ebaylib.Store`), координатор (FDW) |
| Валидатор каталога | `ebay_validation_catalog` | 5421 | координатор (FDW), воркеры (FDW: `done_at`) |
| fx-микросервис (валюты → USD) | — | 8092 (HTTP) | библиотека сама (`FX_API_URL`) |

**FDW.** В `parser_ebay` создаётся `postgres_fdw`: схема `ebay_fdw`
(`catalog_fetches`, `items`, `contexts`, `search_profiles` из `ebay_data`) и
`validation_fdw` (`validated_items`, `reparse_tasks` из базы валидатора) —
координатор вычисляет потребности одним SQL, воркер закрывает reparse.
Чужие данные не копируем — только читаем на месте. `purchase_feed` /
`effective_season_months` — функции, через FDW не зовутся: к `ebay_to_buy`
и `smart` координатор ходит обычными соединениями.

**Распределение ролей с библиотекой `ebaylib` (v2):**

| Делает библиотека | Делает парсер |
|---|---|
| цикл `run_worker` («задача → парсинг → запись → task_done») | поставка задач (`next_task`) и подтверждение (`task_done`) |
| `EbaySession`: warmup, пагинация SRP (до 5 страниц/запрос), PDP+iframe, ZIP-флоу | `get_page`: браузер, контексты, Xvfb (позже — прокси) |
| замена страницы при Access Denied / смерти транспорта (без лимита) | упаковка воркера в контейнер (масштабирование/рестарт — Docker/платформа, §7) |
| конвертация цен в USD (fx), тайминги задачи (`stats.timing`) | список задач и его актуальность (координатор) |
| запись в `ebay_data` (`Store` → `apply_catalog_fetch` / `apply_item_snapshot`) | runs (вход), CLI |
| `e.task` — задача-виновница на исключении | сохранение HTML из `ParseError` в `parse_errors` |

## 3. Модель: журнал задач, потребности — из фактов

`tasks` — журнал: строки не удаляются, а проходят статусы и копятся как
история (чистка — только ретеншеном). **Что вставлять** координатор выводит
из фактов (`purchase_feed`, `ebay_data`, валидатор); **done ставит только
воркер** — строго после записи результата в `ebay_data` (так устроен
`task_done` библиотеки: обработана = записана). Упавшая задача не теряется:
она вернётся в `pending` по таймауту выдачи. Семантика at-least-once;
повторная запись в `ebay_data` идемпотентна (нулевые диффы), поэтому редкий
дубль обработки безвреден.

```
runs (вход: параметры) ──▶ координатор ──▶ tasks: pending ──▶ воркер берёт
        ▲                       ▲                                  (processing)
        │   purchase_feed × smart                                      │
        │   свежесть: ebay_fdw.catalog_fetches      ebaylib.run_worker:│
        │   approved: validation_fdw                парсинг → Store →  │
        │                                           ebay_data          │
   start-run / stop-run / платформа                 task_done → done ──┘
```

Жизнь строки: `pending` → `processing` (воркер взял) → `done`
(воркер отметил после записи) | обратно `pending` (перевыдача по таймауту,
`attempts`++) | `cancelled` (потребность исчезла, см. §5). Активная задача
(pending/processing) на один fingerprint — одна; done-история не мешает
(частичная уникальность).

### Виды задач

- **catalog** — смарт-деталь: «по артикулам детали нет прогона каталога
  свежее порога в профиле run (zip+condition+цены)». Одна задача = одна
  деталь со ВСЕМИ её артикулами (`articles[]`) — валидатор видит деталь
  целиком, переотдача атомарна.
- **item** (source `validator`) — «item одобрен (`status='approved'`), а
  PDP ещё не было (`pdp_seen_at IS NULL`)». PDP делается один раз;
  повторные — только через reparse. К run не привязан.
- **item** (source `reparse`) — «строка `reparse_tasks` валидатора с
  `done_at IS NULL`». В `task_done` воркер ставит `done_at` — потребность
  гаснет. `taken_at` не используем: дубль безвреден. Если на item уже есть
  активная задача от валидатора — fingerprint совпадёт, и reparse
  прикрепляется к ней (`ON CONFLICT … DO UPDATE SET reparse_task_id =
  COALESCE(...)`).

### Откуда берутся детали: фид и сезонное окно

Список деталей — живой: координатор раз в `feed_refresh_sec` перечитывает
`purchase_feed(p_months, include-флаги, p_only_need)` в `ebay_to_buy` и
артикулы из `smart.part_articles`. Деталь ушла из фида (закупили) →
потребность исчезает; появилась — добавляется. Работает в обоих режимах.
Item-задачи от фида не зависят: одобренный item ушедшей детали всё равно
обрабатывается (PDP разовый и дешёвый; «нужна ли деталь» — зона валидатора
и закупки).

`p_months` — глобальное **сезонное окно** `ebay_to_buy` (`app_settings`:
season-filter / season-months-ahead, та же логика, что в UI закупки):
координатор берёт его свежим вызовом `effective_season_months()` при каждом
пересеве — окно скользит само, настройка правится в `ebay_to_buy` без
перезапуска парсера. Run с `season='ignore'` (CLI `--ignore-season`) сезон
не применяет (полный фид).

### Свежесть каталогов и режимы run

- `mode=once`: порог свежести = `runs.created_at` — каждая деталь парсится
  один раз за запуск; всё спарсили → новых pending нет (run активен до
  `stop-run`: новые детали фида будут обработаны).
- `mode=continuous`: порог = `now() − catalog_refresh_sec` — каталоги
  устаревают по скользящему окну и переотправляются сами (новой строкой
  задачи); парсер крутится сутками.

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

### `tasks` — журнал задач

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
| `reparse_task_id` | bigint | ссылка на `reparse_tasks` валидатора |
| `fingerprint` | text | catalog: `part_id+zip+condition+цены`; item: `item_id+zip` |
| `status` | text | `pending` / `processing` / `done` / `cancelled` |
| `attempts` | smallint | число выдач (диагностика; порога нет) |
| `created_at` | timestamptz | |
| `dispatched_at` | timestamptz | момент выдачи воркеру (= начало работы; основа таймаута перевыдачи) |
| `dispatched_to` | text | кто взял: hostname контейнера воркера |
| `parse_ms` | integer | чистое время парсинга (из `stats.timing`; запись асинхронная — в цену задачи не входит) |
| `finished_at` | timestamptz | момент done/cancelled — для темпа и ретеншена |

Индексы: частичный UNIQUE `(fingerprint) WHERE status IN
('pending','processing')` — одна активная задача на потребность; частичный
`(type, task_id) WHERE status = 'pending'` — забор; `(status, finished_at)`
— темп и ретеншен.

### `parse_errors` — сырьё для починки селекторов

`id PK, task_fingerprint text, kind text, error text, html_gz bytea,
created_at`. Retention `parse_errors_retention_days`.

## 5. Координатор

Один активный на систему: каждый контейнер — кандидат, лидер держит
advisory lock в `parser_ebay`; упал — лок перехватывает другой. Цикл раз в
`coordinator_poll_sec`:

1. **Каталоги.** Для активных run: детали фида (§3, кэш `feed_refresh_sec`)
   × несвежие по `ebay_fdw.catalog_fetches` (профиль резолвится join'ом
   `ebay_fdw.search_profiles` по zip+condition+ценам) → INSERT pending
   `ON CONFLICT (fingerprint) WHERE status IN ('pending','processing')
   DO NOTHING`.
2. **Items.** `validation_fdw.validated_items` (`status='approved'`) LEFT
   JOIN `ebay_fdw.items` WHERE `pdp_seen_at IS NULL` → INSERT pending
   (zip — из `validated_items.context_id` → `ebay_fdw.contexts`).
3. **Reparse.** `validation_fdw.reparse_tasks WHERE done_at IS NULL` →
   INSERT pending (source `reparse`, zip = `zip_default`) с прикреплением
   к активному дублю (§3).
4. **Отмена.** Pending-задачи, чья потребность исчезла: деталь ушла из
   фида / run остановлен; item перестал быть approved (отозван валидатором)
   → `status = 'cancelled'`, `finished_at = now()`. Processing не трогаем:
   воркер дожуёт, лишняя запись безвредна.
5. **Перевыдача.** Processing с `dispatched_at < now() −
   dispatch_timeout_sec` → обратно `pending` (воркер умер или завис; если
   старый всё-таки допишет — запись идемпотентна).
6. **Retention.** Done/cancelled старше `tasks_retention_days` и
   `parse_errors` старше своего ретеншена → DELETE.

## 6. Воркер

Один контейнер = один воркер: свой Xvfb = один браузер cloakbrowser
(`headless=False`) = одна задача за раз, без пауз между задачами (темп
системы регулируется только числом воркеров). Процесс запускает
`ebaylib.run_worker(get_page, next_task, Store(EBAY_DATA_DSN), task_done)`
и реализует три колбека:

- **`get_page`** — закрыть предыдущий context → новый context → новая page.
  Библиотека зовёт лениво и при заменах страниц. Будущая точка прокси:
  новый context = новая аренда из proxy-manager.
- **`next_task`** — получен SIGTERM → вернуть `None` (библиотека допишет
  хвост записи и штатно выйдет); иначе забор, пусто → sleep
  `poll_interval_sec` и снова:

  ```sql
  UPDATE tasks SET status = 'processing', dispatched_at = now(),
         dispatched_to = $me, attempts = attempts + 1
  WHERE task_id = (SELECT task_id FROM tasks
                   WHERE status = 'pending'
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
  `ebay_data`: `UPDATE tasks SET status='done', finished_at=now(),
  parse_ms=$timing` по `task_id`; у reparse — ещё `UPDATE
  validation_fdw.reparse_tasks SET done_at = now()`. `stats.db` — в лог.

### Смерти

Любая критическая ошибка (`ParseError`, `ErrorPageError`, Pardon/iframe
таймауты, сбой fx, сбой записи, `TaskFormatError`) валит `run_worker`.
Обёртка процесса: читает `e.task` (виновница), для `ParseError` пишет HTML в
`parse_errors`, логирует и умирает; его processing-задача вернётся
перевыдачей (§5.5). Битая задача будет реинкарнироваться и убивать воркеров,
громко крася лог, пока не починим селекторы — осознанно жёсткая политика,
dead-letter'а нет.

## 7. Процессы и масштабирование

Управление мощностью — НЕ зона парсера (это server_logic); парсер лишь
масштабируем. Один образ, два entrypoint:

- **worker** — контейнер = один воркер (§6). Сколько их и где запускать —
  снаружи: сейчас руками (`docker compose up --scale worker=N`), потом
  платформа ставит replicas. Смерть воркера (критическая ошибка) →
  `restart: always` Docker'а поднимает контейнер заново; SIGTERM (дренаж) →
  штатный выход через `next_task → None`, время ожидания — стандартный
  `stop_grace_period`.
- **coordinator** — один на систему (§5), отдельный контейнер с
  `restart: always`; где он живёт (контрол-сервер / локально) — решим при
  деплое. Advisory lock — защита от случайного второго экземпляра.

## 8. Запуск и CLI

```
start-run [--zip 19701] [--condition new] [--mode continuous|once]
          [--refresh-sec 86400] [--ignore-season]
          [--no-include-personal ... --no-only-need]   # флаги purchase_feed
stop-run <run_id>
status        # активные run, задачи по типам/статусам, темп done, attempts
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
poll_interval_sec: 1        # сон воркера при пустой очереди
dispatch_timeout_sec: 3600  # перевыдача зависшего processing
tasks_retention_days: 30          # done/cancelled
parse_errors_retention_days: 14
```

## 10. Интеграция с server_logic

- **Вход** — INSERT/UPDATE в `runs` (§4): платформенный `POST /run` и
  кнопка «Стоп» делают то же. Её `mode=endless` = наш `continuous` (период
  устаревания рулит наш координатор, платформа видит живой backlog и даёт
  под него мощность, включая ноль); её `mode=drain` + cron = наш `once`.
- **Адаптер-SQL** по журналу `tasks`:
  - backlog: `count(*) WHERE status='pending'` (по типам);
  - done за интервал: `count(*) WHERE status='done' AND finished_at > $1`
    (по типам);
  - время задачи для NCU: `parse_ms` (только парсинг — запись асинхронная и
    перекрывается со следующей задачей);
  - `run_active` для drain: «есть pending/processing каталоги этого run
    ИЛИ любые активные item-задачи» (item'ы глобальные — дренить можно
    только когда дожёваны и они).
- **Единица мощности — воркер** (контейнер); платформа будет управлять их
  числом, ресурсный ориентир — из замеров §11.
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
