"""CLI (SPEC §4, §8): start-run и set-workers.

    PYTHONPATH=src python -m parser_ebay.cli start-run [--zip 19701] [--product-types ...]
    PYTHONPATH=src python -m parser_ebay.cli set-workers --host HOST N
"""
import argparse
import asyncio
import hashlib
import json
import logging

import asyncpg

from .config import dsn, load_config
from .db import apply_migrations
from .seed import expand_seed

log = logging.getLogger('parser.cli')


async def start_run(args) -> None:
    cfg = load_config(args.config)
    parser_conn = await asyncpg.connect(dsn('PARSER_DSN'))
    tb = await asyncpg.connect(dsn('EBAY_TO_BUY_DSN'))
    sm = await asyncpg.connect(dsn('SMART_DSN'))
    try:
        await apply_migrations(parser_conn)

        # фильтр категорий валидируется по справочнику smart — опечатка падает громко
        if args.product_types:
            known = {r['name'] for r in await sm.fetch('select name from product_types')}
            unknown = set(args.product_types) - known
            if unknown:
                raise SystemExit(
                    f'неизвестные категории: {sorted(unknown)}; есть: {sorted(known)}')

        params = {
            'zip': args.zip or cfg.zip_default,
            'product_types': args.product_types or None,
            'include_personal': not args.no_include_personal,
            'include_in_transit': not args.no_include_in_transit,
            'include_ebay_pending': not args.no_include_ebay_pending,
            'include_kit_breakdown': not args.no_include_kit_breakdown,
            'include_virtual_kit': not args.no_include_virtual_kit,
            'include_defect': not args.no_include_defect,
            'only_need': not args.no_only_need,
        }
        async with parser_conn.transaction():
            run_id = await parser_conn.fetchval(
                'insert into runs (params) values ($1::jsonb) returning run_id',
                json.dumps(params))
            # fingerprint считается в Python: переиспользование $N в разных типовых
            # контекстах (bigint-колонка + конкатенация) asyncpg не выводит
            fp = hashlib.md5(f'seed:{run_id}:{params["zip"]}'.encode()).hexdigest()
            task = await parser_conn.fetchrow('''
                insert into tasks (type, zip, params, source, run_id, fingerprint)
                values ('seed', $1, $2::jsonb, 'cli', $3, $4)
                returning task_id, zip, params, run_id''',
                params['zip'], json.dumps(params), run_id, fp)
        log.info('run %d создан, seed-задача %d', run_id, task['task_id'])

        # этап 2: разворачиваем сразу (потом это заберёт координатор, PLAN этап 4)
        await parser_conn.execute(
            "update tasks set status = 'processing', leased_by = 'cli', leased_at = now(), "
            'attempts = 1, started_at = now() where task_id = $1', task['task_id'])
        inserted = await expand_seed(parser_conn, tb, sm, task)
        print(f'run {run_id}: вставлено {inserted} catalog-задач')
    finally:
        await parser_conn.close()
        await tb.close()
        await sm.close()


async def set_workers(args) -> None:
    conn = await asyncpg.connect(dsn('PARSER_DSN'))
    try:
        await apply_migrations(conn)
        await conn.execute('''
            insert into worker_hosts (host, desired_workers) values ($1, $2)
            on conflict (host) do update
                set desired_workers = excluded.desired_workers, updated_at = now()''',
            args.host, args.n)
        print(f'{args.host}: desired_workers = {args.n}')
    finally:
        await conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog='parser_ebay')
    ap.add_argument('--config', default='config.yaml')
    sub = ap.add_subparsers(dest='cmd', required=True)

    sr = sub.add_parser('start-run', help='создать run и развернуть в catalog-задачи (SPEC §4)')
    sr.add_argument('--zip', default=None, help='по умолчанию zip_default из config.yaml')
    sr.add_argument('--product-types', nargs='*', default=None,
                    help='фильтр категорий smart; пусто = все')
    for flag in ('include-personal', 'include-in-transit', 'include-ebay-pending',
                 'include-kit-breakdown', 'include-virtual-kit', 'include-defect',
                 'only-need'):
        sr.add_argument(f'--no-{flag}', action='store_true',
                        help=f'purchase_feed: p_{flag.replace("-", "_")} = false')
    sr.set_defaults(fn=start_run)

    sw = sub.add_parser('set-workers', help='целевое число воркеров на хосте (SPEC §8)')
    sw.add_argument('--host', required=True)
    sw.add_argument('n', type=int)
    sw.set_defaults(fn=set_workers)

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    asyncio.run(args.fn(args))


if __name__ == '__main__':
    main()
