"""Ручное применение миграций: PYTHONPATH=src python -m parser_ebay.migrate."""
import asyncio
import logging

import asyncpg

from .config import dsn
from .db import apply_migrations


async def main() -> None:
    conn = await asyncpg.connect(dsn('PARSER_DSN'))
    try:
        await apply_migrations(conn)
    finally:
        await conn.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    asyncio.run(main())
