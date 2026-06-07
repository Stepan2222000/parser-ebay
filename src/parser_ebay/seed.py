"""Разворачивание seed-задачи (SPEC §3, §4): purchase_feed → part_articles → catalog-задачи."""
import json
import logging

from . import queue

log = logging.getLogger('parser.seed')

_FEED = '''
    select smart_part_id from purchase_feed(
        p_product_types         := $1::text[],
        p_include_personal      := $2,
        p_include_in_transit    := $3,
        p_include_ebay_pending  := $4,
        p_include_kit_breakdown := $5,
        p_include_virtual_kit   := $6,
        p_include_defect        := $7,
        p_only_need             := $8)'''

_ARTICLES = 'select article from part_articles where part_id = any($1::text[])'


async def expand_seed(parser_conn, tb_conn, smart_conn, task) -> int:
    """Разворачивает одну seed-задачу в catalog-задачи; возвращает число вставленных.

    Только собственные артикулы смарт-детали; компоненты китов не разворачиваются
    (SPEC §2). Идемпотентно: повторный запуск активных дублей не плодит.
    """
    p = json.loads(task['params'])
    feed = await tb_conn.fetch(
        _FEED, p['product_types'],
        p['include_personal'], p['include_in_transit'], p['include_ebay_pending'],
        p['include_kit_breakdown'], p['include_virtual_kit'], p['include_defect'],
        p['only_need'])
    part_ids = [r['smart_part_id'] for r in feed]
    articles = [r['article'] for r in await smart_conn.fetch(_ARTICLES, part_ids)]
    log.info('seed %s: %d позиций фида, %d артикулов',
             task['task_id'], len(part_ids), len(articles))
    if not articles:
        log.warning('seed %s: фид пуст, catalog-задач не будет', task['task_id'])

    async with parser_conn.transaction():
        inserted = 0
        if articles:
            inserted = await queue.insert_catalog_tasks(
                parser_conn, articles, task['zip'], task['run_id'])
        await parser_conn.execute(
            'update runs set articles_total = $2 where run_id = $1',
            task['run_id'], len(articles))
        await queue.finish(parser_conn, task['task_id'])
    log.info('seed %s: вставлено %d catalog-задач (дубли активных пропущены)',
             task['task_id'], inserted)
    return inserted
