"""Идемпотентная настройка postgres_fdw в parser_ebay (SPEC §2).

Хосты — имена контейнеров в сети db_default (FDW-соединения исходят из
контейнера parser_ebay); учётки и имена баз — из EBAY_DATA_DSN/VALIDATOR_DSN.
Повторный прогон безопасен: DROP SERVER CASCADE + пересоздание, потому что
IMPORT FOREIGN SCHEMA не идемпотентен (падает на существующих таблицах).
"""
from urllib.parse import urlsplit

# (server, контейнер-хост, локальная схема, таблицы, ключ DSN)
SERVERS = (
    ('ebay_fdw_srv', 'ebay_data', 'ebay_fdw',
     ('catalog_fetches', 'items', 'contexts', 'search_profiles'), 'EBAY_DATA_DSN'),
    ('validation_fdw_srv', 'ebay_validation_catalog', 'validation_fdw',
     ('validated_items', 'reparse_tasks'), 'VALIDATOR_DSN'),
)


def _q(s: str) -> str:
    return s.replace("'", "''")


async def setup_fdw(conn, dsns: dict) -> None:
    await conn.execute('create extension if not exists postgres_fdw')
    for server, host, schema, tables, dsn_key in SERVERS:
        u = urlsplit(dsns[dsn_key])
        await conn.execute(f"""
            drop server if exists {server} cascade;
            create server {server} foreign data wrapper postgres_fdw
                options (host '{host}', port '5432', dbname '{_q(u.path.lstrip('/'))}',
                         fetch_size '1000', connect_timeout '5');
            create user mapping for current_user server {server}
                options (user '{_q(u.username)}', password '{_q(u.password)}');
            create schema if not exists {schema};
            import foreign schema public limit to ({', '.join(tables)})
                from server {server} into {schema};
        """)
