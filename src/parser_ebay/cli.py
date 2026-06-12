"""CLI (SPEC §8). Пока только команды разработки: migrate, setup-fdw;
start-run / stop-run / status появятся с координатором (этап 2)."""
import argparse
import asyncio
import logging

import asyncpg

from parser_ebay.config import load_dotenv, load_dsns
from parser_ebay.db import apply_migrations
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


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    p = argparse.ArgumentParser(prog='parser_ebay')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('migrate', help='применить миграции (только разработка, SPEC §4)')
    sub.add_parser('setup-fdw', help='пересоздать FDW-объекты (SPEC §2)')
    args = p.parse_args()
    load_dotenv()
    asyncio.run({'migrate': _migrate, 'setup-fdw': _setup_fdw}[args.cmd]())


if __name__ == '__main__':
    main()
