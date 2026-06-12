"""CLI (SPEC §8): start-run / stop-run / status + команды разработки
migrate / setup-fdw. Дефолты start-run — из config.yaml."""
import argparse
import asyncio
import logging

import asyncpg

from parser_ebay.config import CONDITIONS, MODES, load_config, load_dotenv, load_dsns
from parser_ebay.db import apply_migrations, connect
from parser_ebay.fdw import SERVERS, setup_fdw


async def _migrate() -> None:
    conn = await asyncpg.connect(load_dsns()['PARSER_DSN'])
    try:
        n = await apply_migrations(conn)
        print(f'применено миграций: {n}')
    finally:
        await conn.close()


async def _setup_fdw() -> None:
    dsns = load_dsns()
    conn = await asyncpg.connect(dsns['PARSER_DSN'])
    try:
        await setup_fdw(conn, dsns)
        for _, _, schema, tables, _ in SERVERS:
            for t in tables:
                print(f'{schema}.{t}: {await conn.fetchval(f"select count(*) from {schema}.{t}")} строк')
    finally:
        await conn.close()


async def _start_run(args) -> None:
    params = {
        'zip': args.zip, 'condition': args.condition,
        'min_price': None, 'max_price': None,           # задел (SPEC §12)
        'season': 'ignore' if args.ignore_season else 'auto',
        'mode': args.mode, 'catalog_refresh_sec': args.refresh_sec,
        'include_personal': args.include_personal,
        'include_in_transit': args.include_in_transit,
        'include_ebay_pending': args.include_ebay_pending,
        'include_kit_breakdown': args.include_kit_breakdown,
        'include_virtual_kit': args.include_virtual_kit,
        'include_defect': args.include_defect,
        'only_need': args.only_need,
    }
    conn = await connect(load_dsns()['PARSER_DSN'])
    try:
        run_id = await conn.fetchval(
            'insert into runs(params) values($1) returning run_id', params)
        print(f'run_id={run_id}  '
              f"{params['mode']}/{params['zip']}/{params['condition']}/"
              f"season={params['season']}")
    finally:
        await conn.close()


async def _stop_run(args) -> None:
    conn = await connect(load_dsns()['PARSER_DSN'])
    try:
        tag = await conn.execute(
            'update runs set is_active = false where run_id = $1 and is_active',
            args.run_id)
        print(f'run {args.run_id}: ' +
              ('остановлен' if tag.endswith('1') else 'не найден или уже остановлен'))
    finally:
        await conn.close()


async def _status() -> None:
    conn = await connect(load_dsns()['PARSER_DSN'])
    try:
        runs = await conn.fetch(
            'select run_id, params, created_at from runs where is_active order by 1')
        print(f'активные runs: {len(runs)}')
        for r in runs:
            p = r['params']
            print(f"  run {r['run_id']}: {p['mode']}/{p['zip']}/{p['condition']}/"
                  f"season={p['season']}  с {r['created_at']:%Y-%m-%d %H:%M}")

        rows = await conn.fetch("""
            select type, status, count(*) n, min(created_at) oldest
            from tasks group by 1, 2 order by 1, 2""")
        print('задачи:')
        for r in rows:
            age = (f"  (старейшая {r['oldest']:%m-%d %H:%M})"
                   if r['status'] == 'pending' else '')
            print(f"  {r['type']:8} {r['status']:10} {r['n']}{age}")

        rate = await conn.fetch("""
            select type,
                   count(*) filter (where finished_at > now() - interval '1 hour') done_1h,
                   count(*) filter (where finished_at > now() - interval '24 hours') done_24h,
                   round(avg(parse_ms) filter
                         (where finished_at > now() - interval '24 hours')) parse_ms
            from tasks where status = 'done' group by 1 order by 1""")
        for r in rate:
            print(f"  {r['type']:8} done: {r['done_1h']}/час, {r['done_24h']}/сутки, "
                  f"avg parse {r['parse_ms']} мс")

        retried = await conn.fetchrow("""
            select count(*) n, max(attempts) mx from tasks
            where status in ('pending', 'processing') and attempts > 1""")
        if retried['n']:
            print(f"  перевыданных активных: {retried['n']} (max attempts {retried['mx']}) "
                  f"— кто-то умирает, смотри логи воркеров")
    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    load_dotenv()
    cfg = load_config()

    p = argparse.ArgumentParser(prog='parser_ebay')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('migrate', help='применить миграции (только разработка, SPEC §4)')
    sub.add_parser('setup-fdw', help='пересоздать FDW-объекты (SPEC §2)')

    sr = sub.add_parser('start-run', help='новый run (печатает run_id)')
    sr.add_argument('--zip', default=cfg.zip_default)
    sr.add_argument('--condition', default=cfg.condition_default, choices=CONDITIONS)
    sr.add_argument('--mode', default=cfg.mode_default, choices=MODES)
    sr.add_argument('--refresh-sec', type=int, default=cfg.catalog_refresh_sec_default,
                    help='continuous: окно свежести каталогов')
    sr.add_argument('--ignore-season', action='store_true',
                    help='полный фид без сезонного окна')
    for flag in ('personal', 'in-transit', 'ebay-pending', 'kit-breakdown',
                 'virtual-kit', 'defect'):
        sr.add_argument(f'--no-include-{flag}', action='store_false',
                        dest=f"include_{flag.replace('-', '_')}")
    sr.add_argument('--no-only-need', action='store_false', dest='only_need')

    st = sub.add_parser('stop-run', help='остановить run (каталоги отменятся)')
    st.add_argument('run_id', type=int)

    sub.add_parser('status', help='runs, задачи по статусам, темп, attempts')

    args = p.parse_args()
    cmd = {'migrate': lambda: _migrate(),
           'setup-fdw': lambda: _setup_fdw(),
           'start-run': lambda: _start_run(args),
           'stop-run': lambda: _stop_run(args),
           'status': lambda: _status()}[args.cmd]
    asyncio.run(cmd())


if __name__ == '__main__':
    main()
