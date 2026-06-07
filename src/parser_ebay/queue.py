"""Очередь задач (SPEC §6, §8): вставка по fingerprint, забор с приоритетом, статусы."""

# Вставка идемпотентна: дубль среди активных (pending/processing) тихо пропускается;
# ON CONFLICT обязан повторять предикат частичного индекса (SPEC §8).
_INSERT_CATALOG = '''
    with ins as (
        insert into tasks (type, article, zip, source, run_id, fingerprint)
        select 'catalog', a, $2, 'seed', $3, md5('catalog:' || a || ':' || $2)
        from unnest($1::text[]) as a
        on conflict (fingerprint) where status in ('pending', 'processing') do nothing
        returning 1)
    select count(*) from ins'''

_INSERT_ITEMS = '''
    with ins as (
        insert into tasks (type, item_id, zip, source, fingerprint)
        select 'item', i, $2, $3, md5('item:' || i || ':' || $2)
        from unnest($1::bigint[]) as i
        on conflict (fingerprint) where status in ('pending', 'processing') do nothing
        returning 1)
    select count(*) from ins'''

_PICK_ONE = '''
    update tasks set status = 'processing', leased_by = $2, leased_at = now(),
           attempts = attempts + 1, started_at = coalesce(started_at, now())
    where task_id = (
        select task_id from tasks
        where status = 'pending' and type = any($1::text[])
        order by case type when 'item' then 0 else 1 end, task_id
        limit 1 for update skip locked)
    returning task_id, type, article, item_id, zip, params, source,
              reparse_task_id, run_id, attempts'''

_PICK_CATALOG_BATCH = '''
    update tasks set status = 'processing', leased_by = $1, leased_at = now(),
           attempts = attempts + 1, started_at = coalesce(started_at, now())
    where task_id in (
        select task_id from tasks
        where status = 'pending' and type = 'catalog'
        order by task_id limit $2 for update skip locked)
    returning task_id, article, zip, run_id, attempts'''


async def insert_catalog_tasks(conn, articles: list[str], zip_: str, run_id: int) -> int:
    return await conn.fetchval(_INSERT_CATALOG, articles, zip_, run_id)


async def insert_item_tasks(conn, item_ids: list[int], zip_: str, source: str) -> int:
    return await conn.fetchval(_INSERT_ITEMS, item_ids, zip_, source)


async def item_debt(conn) -> int:
    """Невыполненные item-задачи — долг, тормозящий каталоги (SPEC §6)."""
    return await conn.fetchval(
        "select count(*) from tasks where type = 'item' and status = 'pending'")


async def pick_one(conn, worker: str, allowed_types: list[str]):
    """Одна задача: items-first, FIFO внутри типа. None — очередь пуста."""
    return await conn.fetchrow(_PICK_ONE, allowed_types, worker)


async def pick_catalog_batch(conn, worker: str, limit: int) -> list:
    """Пачка catalog-задач на одну браузер-сессию (fetch_catalogs, SPEC §3)."""
    return await conn.fetch(_PICK_CATALOG_BATCH, worker, limit)


async def finish(conn, task_id: int) -> None:
    await conn.execute(
        "update tasks set status = 'done', finished_at = now() where task_id = $1", task_id)


async def fail(conn, task_id: int, error: str) -> None:
    """Невосстановимая ошибка — failed сразу, без ретраев (SPEC §7)."""
    await conn.execute(
        "update tasks set status = 'failed', finished_at = now(), last_error = $2 "
        'where task_id = $1', task_id, _trunc(error))


async def release_transient(conn, task_id: int, error: str, max_attempts: int) -> None:
    """Транзиентная ошибка: назад в pending; attempts исчерпаны — failed (SPEC §7)."""
    await conn.execute('''
        update tasks set
            status = case when attempts >= $2 then 'failed' else 'pending' end,
            finished_at = case when attempts >= $2 then now() end,
            leased_by = null, leased_at = null, last_error = $3
        where task_id = $1''', task_id, max_attempts, _trunc(error))


def _trunc(error: str, limit: int = 2000) -> str:
    return error if len(error) <= limit else error[:limit] + '…'
