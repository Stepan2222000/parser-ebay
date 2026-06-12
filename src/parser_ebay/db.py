"""Соединение с parser_ebay и прогон миграций (SPEC §4). Миграции
применяются только руками при разработке, учёт — в schema_migrations;
координатор и воркеры схему не трогают."""
import json
import logging
import os

import asyncpg

log = logging.getLogger('parser.db')


async def connect(dsn: str) -> asyncpg.Connection:
    """Соединение с parser_ebay: jsonb (runs.params) ходит как dict."""
    conn = await asyncpg.connect(dsn)
    await conn.set_type_codec('jsonb', encoder=json.dumps, decoder=json.loads,
                              schema='pg_catalog')
    return conn


async def apply_migrations(conn, base: str = 'migrations') -> int:
    await conn.execute(
        'create table if not exists schema_migrations('
        'name text primary key, applied_at timestamptz not null default now())')
    applied = {r['name'] for r in await conn.fetch('select name from schema_migrations')}
    n = 0
    for fname in sorted(os.listdir(base)):
        if not fname.endswith('.sql') or fname in applied:
            continue
        async with conn.transaction():
            await conn.execute(open(os.path.join(base, fname)).read())
            await conn.execute('insert into schema_migrations(name) values($1)', fname)
        log.info('миграция применена: %s', fname)
        n += 1
    return n
