"""Координатор (SPEC §5): выводит потребности из фактов и ведёт журнал tasks.

Один активный на систему: каждый процесс — кандидат, лидер держит advisory
lock на своём рабочем соединении: смерть соединения = падение процесса =
лок свободен, перехватит другой кандидат. Ошибки не перехватываются — любая
(включая недоступность внешних баз) валит процесс, рестарт — Docker (SPEC §7).
"""
import asyncio
import logging
import time

import asyncpg

from parser_ebay.config import load_config, load_dotenv, load_dsns
from parser_ebay.db import connect

log = logging.getLogger('parser.coordinator')

LOCK_KEY = 0x70617273  # advisory lock лидера ('pars')

ON_CONFLICT = "on conflict (fingerprint) where status in ('pending','processing') do nothing"


def fp_catalog(part_id, zip_, condition, min_price, max_price) -> str:
    return f"catalog:{part_id}|{zip_}|{condition}|{min_price or ''}|{max_price or ''}"


# Несвежие детали run: деталь несвежая, если хотя бы один её артикул не имеет
# прогона каталога с fetched_at >= порога в профиле run (SPEC §3).
STALE_PARTS_SQL = """
with pa(article, part_id) as (select unnest($1::text[]), unnest($2::text[])),
fresh as (
    select distinct cf.article
    from ebay_fdw.catalog_fetches cf
    join ebay_fdw.search_profiles sp using (profile_id)
    join ebay_fdw.contexts cx using (context_id)
    where cx.zip = $3 and sp.condition = $4
      and sp.min_price is not distinct from $5::numeric
      and sp.max_price is not distinct from $6::numeric
      and cf.fetched_at >= $7
)
select pa.part_id, array_agg(pa.article order by pa.article) as articles
from pa left join fresh f using (article)
group by pa.part_id
having bool_or(f.article is null)
"""

INSERT_CATALOG = f"""
insert into tasks(type, part_id, articles, zip, condition, min_price, max_price,
                  run_id, source, fingerprint)
values ('catalog', $1, $2, $3, $4, $5, $6, $7, 'feed', $8)
{ON_CONFLICT}
"""

INSERT_ITEMS = f"""
insert into tasks(type, item_id, zip, source, fingerprint)
select 'item', vi.item_id, cx.zip, 'validator',
       'item:' || vi.item_id || '|' || cx.zip
from validation_fdw.validated_items vi
join ebay_fdw.contexts cx using (context_id)
left join ebay_fdw.items i using (item_id)
where vi.status = 'approved'
  and i.pdp_seen_at is null and not coalesce(i.is_dead, false)
group by vi.item_id, cx.zip
{ON_CONFLICT}
"""

INSERT_REPARSE = f"""
insert into tasks(type, item_id, zip, source, reparse_task_id, fingerprint)
select 'item', rt.item_id, $1, 'reparse', rt.task_id,
       'reparse:' || rt.task_id || '|' || rt.item_id || '|' || $1
from validation_fdw.reparse_tasks rt
where rt.done_at is null
{ON_CONFLICT}
"""

# Отмена каталогов run: pending-задача жива, только если деталь в текущем фиде,
# у неё остались артикулы и их состав не дрейфанул (SPEC §5.4).
CANCEL_CATALOG = """
with cur(part_id, articles) as (
    select part_id, array_agg(article order by article)
    from unnest($2::text[], $3::text[]) as u(article, part_id)
    group by 1)
update tasks t set status = 'cancelled', finished_at = now()
where t.status = 'pending' and t.type = 'catalog' and t.run_id = $1
  and not exists (select 1 from cur
                  where cur.part_id = t.part_id and cur.articles = t.articles)
returning t.task_id
"""

CANCEL_RUN_STOPPED = """
update tasks t set status = 'cancelled', finished_at = now()
from runs r
where t.run_id = r.run_id and t.status = 'pending' and t.type = 'catalog'
  and not r.is_active
returning t.task_id
"""

# Отмена item-задач — буквально по SPEC §5.4: отозван валидатором или мёртв.
# Появившийся мимо задачи PDP потребность НЕ отменяет (осознанно: случай
# редкий, лишний PDP безвреден — запись идемпотентна).
CANCEL_ITEMS = """
update tasks t set status = 'cancelled', finished_at = now()
where t.status = 'pending' and t.type = 'item' and t.source = 'validator'
  and (not exists (select 1
                   from validation_fdw.validated_items vi
                   join ebay_fdw.contexts cx using (context_id)
                   where vi.item_id = t.item_id and cx.zip = t.zip
                     and vi.status = 'approved')
       or exists (select 1 from ebay_fdw.items i
                  where i.item_id = t.item_id and i.is_dead))
returning t.task_id
"""

CANCEL_REPARSE = """
update tasks t set status = 'cancelled', finished_at = now()
where t.status = 'pending' and t.type = 'item' and t.source = 'reparse'
  and not exists (select 1 from validation_fdw.reparse_tasks rt
                  where rt.task_id = t.reparse_task_id and rt.done_at is null)
returning t.task_id
"""

REDISPATCH = """
update tasks set status = 'pending'
where status = 'processing' and dispatched_at < now() - $1 * interval '1 second'
returning task_id, attempts
"""

RETENTION_TASKS = """
delete from tasks
where status in ('done', 'cancelled') and finished_at < now() - $1 * interval '1 day'
"""

RETENTION_ERRORS = """
delete from parse_errors where created_at < now() - $1 * interval '1 day'
"""


def _feed_key(p: dict) -> tuple:
    return (p['season'], p['include_personal'], p['include_in_transit'],
            p['include_ebay_pending'], p['include_kit_breakdown'],
            p['include_virtual_kit'], p['include_defect'], p['only_need'])


async def feed_for(tb, sm, p: dict, cache: dict, ttl: int) -> dict:
    """Фид + артикулы smart для профиля run; кэш на feed_refresh_sec.

    Сезонное окно берётся свежим вызовом effective_season_months() при каждом
    пересеве (SPEC §3) — окно скользит само, без перезапуска парсера.
    """
    key = _feed_key(p)
    e = cache.get(key)
    if e and time.monotonic() - e['at'] < ttl:
        return e
    months = (await tb.fetchval('select effective_season_months(current_date)')
              if p['season'] == 'auto' else None)
    parts = [r['smart_part_id'] for r in await tb.fetch(
        'select smart_part_id from purchase_feed($1,$2,$3,$4,$5,$6,$7,$8)',
        months, p['include_personal'], p['include_in_transit'],
        p['include_ebay_pending'], p['include_kit_breakdown'],
        p['include_virtual_kit'], p['include_defect'], p['only_need'])]
    pairs = await sm.fetch(
        'select article, part_id from part_articles where part_id = any($1::text[])',
        parts)
    missing = [x for x in parts if x not in {r['part_id'] for r in pairs}]
    if missing:
        log.warning('деталей фида без артикулов в smart: %d (%s%s) — пропущены',
                    len(missing), ', '.join(missing[:5]),
                    '…' if len(missing) > 5 else '')
    e = {'at': time.monotonic(), 'parts': parts,
         'arts': [r['article'] for r in pairs],
         'parts_of': [r['part_id'] for r in pairs]}
    cache[key] = e
    return e


def _n(status_tag: str) -> int:
    """'INSERT 0 183' / 'DELETE 4' -> 183 / 4."""
    return int(status_tag.split()[-1])


async def tick(pc, tb, sm, cfg, cache: dict) -> dict:
    s = dict.fromkeys(('catalog_new', 'items_new', 'reparse_new', 'cancelled',
                       'redispatched', 'purged'), 0)
    runs = await pc.fetch(
        'select run_id, params, created_at from runs where is_active')

    # 1. потребности каталогов — по каждому активному run, вставка в порядке фида
    for run in runs:
        p = run['params']
        feed = await feed_for(tb, sm, p, cache, cfg.feed_refresh_sec)
        threshold = (run['created_at'] if p['mode'] == 'once' else
                     await pc.fetchval("select now() - $1 * interval '1 second'",
                                       p['catalog_refresh_sec']))
        stale = {r['part_id']: r['articles'] for r in await pc.fetch(
            STALE_PARTS_SQL, feed['arts'], feed['parts_of'],
            p['zip'], p['condition'], p['min_price'], p['max_price'], threshold)}
        rows = [(pid, stale[pid], p['zip'], p['condition'], p['min_price'],
                 p['max_price'], run['run_id'],
                 fp_catalog(pid, p['zip'], p['condition'],
                            p['min_price'], p['max_price']))
                for pid in feed['parts'] if pid in stale]
        if rows:
            before = await pc.fetchval(
                "select count(*) from tasks where type = 'catalog' "
                "and status in ('pending', 'processing')")
            await pc.executemany(INSERT_CATALOG, rows)
            after = await pc.fetchval(
                "select count(*) from tasks where type = 'catalog' "
                "and status in ('pending', 'processing')")
            s['catalog_new'] += after - before
        # 4. отмена: деталь ушла из фида / артикулы дрейфанули или исчезли
        s['cancelled'] += len(await pc.fetch(
            CANCEL_CATALOG, run['run_id'], feed['arts'], feed['parts_of']))

    # 2–3. потребности items и reparse (глобальные, от runs не зависят)
    s['items_new'] = _n(await pc.execute(INSERT_ITEMS))
    s['reparse_new'] = _n(await pc.execute(INSERT_REPARSE, cfg.zip_default))

    # 4. остальные отмены
    s['cancelled'] += len(await pc.fetch(CANCEL_RUN_STOPPED))
    s['cancelled'] += len(await pc.fetch(CANCEL_ITEMS))
    s['cancelled'] += len(await pc.fetch(CANCEL_REPARSE))

    # 5. перевыдача брошенного processing
    redispatched = await pc.fetch(REDISPATCH, cfg.dispatch_timeout_sec)
    s['redispatched'] = len(redispatched)
    for r in redispatched:
        log.warning('перевыдача task_id=%s (attempts=%s)', r['task_id'], r['attempts'])

    # 6. retention
    s['purged'] = (_n(await pc.execute(RETENTION_TASKS, cfg.tasks_retention_days))
                   + _n(await pc.execute(RETENTION_ERRORS,
                                         cfg.parse_errors_retention_days)))
    return s


async def amain() -> None:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    load_dotenv()
    cfg = load_config()
    dsns = load_dsns()

    pc = await connect(dsns['PARSER_DSN'])
    tb = await asyncpg.connect(dsns['EBAY_TO_BUY_DSN'])
    sm = await asyncpg.connect(dsns['SMART_DSN'])

    while not await pc.fetchval('select pg_try_advisory_lock($1)', LOCK_KEY):
        log.info('координатор уже работает — кандидат ждёт лок')
        await asyncio.sleep(cfg.coordinator_poll_sec)
    log.info('лидер: лок получен, цикл раз в %s сек', cfg.coordinator_poll_sec)

    cache: dict = {}
    while True:
        s = await tick(pc, tb, sm, cfg, cache)
        if any(s.values()):
            log.info('тик: каталоги +%(catalog_new)s, items +%(items_new)s, '
                     'reparse +%(reparse_new)s, отменено %(cancelled)s, '
                     'перевыдано %(redispatched)s, вычищено %(purged)s', s)
        await asyncio.sleep(cfg.coordinator_poll_sec)


def main() -> None:
    asyncio.run(amain())


if __name__ == '__main__':
    main()
