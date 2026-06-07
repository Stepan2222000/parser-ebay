"""Прогон миграций (SPEC §8): один раз каждая, учёт в schema_migrations."""
import logging
import os

log = logging.getLogger('parser.db')


async def apply_migrations(conn, base: str = 'migrations') -> None:
    await conn.execute(
        'create table if not exists schema_migrations('
        'name text primary key, applied_at timestamptz not null default now())')
    applied = {r['name'] for r in await conn.fetch('select name from schema_migrations')}
    for fname in sorted(os.listdir(base)):
        if not fname.endswith('.sql') or fname in applied:
            continue
        async with conn.transaction():
            await conn.execute(open(os.path.join(base, fname)).read())
            await conn.execute('insert into schema_migrations(name) values($1)', fname)
        log.info('миграция применена: %s', fname)
