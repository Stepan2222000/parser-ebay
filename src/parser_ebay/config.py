"""Загрузка и валидация конфига (SPEC §11). Читается один раз при старте."""
import os
from dataclasses import dataclass

import yaml

DSN_KEYS = ('PARSER_DSN', 'EBAY_TO_BUY_DSN', 'SMART_DSN', 'EBAY_DATA_DSN', 'VALIDATOR_DSN')


@dataclass(frozen=True)
class ProxyConfig:
    enabled: bool
    server_url: str
    scope: str


@dataclass(frozen=True)
class Config:
    zip_default: str
    poll_interval_sec: float
    coordinator_poll_sec: float
    reparse_poll_sec: float
    cursor_overlap_sec: int
    catalog_batch_size: int
    catalog_throttle_threshold: int
    max_attempts: int
    lease_timeout_sec: int
    tasks_retention_days: int
    parse_errors_retention_days: int
    desired_workers_default: int
    shutdown_grace_sec: int
    proxy: ProxyConfig


_POSITIVE = (
    'poll_interval_sec', 'coordinator_poll_sec', 'reparse_poll_sec',
    'cursor_overlap_sec', 'catalog_batch_size', 'catalog_throttle_threshold',
    'max_attempts', 'lease_timeout_sec', 'tasks_retention_days',
    'parse_errors_retention_days', 'shutdown_grace_sec',
)


def load_config(path: str = 'config.yaml') -> Config:
    raw = yaml.safe_load(open(path))
    if not isinstance(raw, dict):
        raise ValueError(f'{path}: ожидался YAML-словарь')

    missing = [k for k in (*_POSITIVE, 'zip_default', 'desired_workers_default', 'proxy')
               if k not in raw]
    if missing:
        raise ValueError(f'{path}: отсутствуют ключи {missing}')

    for key in _POSITIVE:
        if not isinstance(raw[key], (int, float)) or raw[key] <= 0:
            raise ValueError(f'{path}: {key} должен быть положительным числом, получено {raw[key]!r}')
    if not isinstance(raw['desired_workers_default'], int) or raw['desired_workers_default'] < 0:
        raise ValueError(f'{path}: desired_workers_default должен быть целым >= 0')

    zip_default = str(raw['zip_default'])
    if not zip_default.strip():
        raise ValueError(f'{path}: zip_default пуст')

    p = raw['proxy'] or {}
    proxy = ProxyConfig(
        enabled=bool(p.get('enabled', False)),
        server_url=str(p.get('server_url', '')),
        scope=str(p.get('scope', 'ebay')),
    )
    if proxy.enabled and not proxy.server_url:
        raise ValueError(f'{path}: proxy.enabled требует proxy.server_url')

    return Config(
        zip_default=zip_default,
        poll_interval_sec=float(raw['poll_interval_sec']),
        coordinator_poll_sec=float(raw['coordinator_poll_sec']),
        reparse_poll_sec=float(raw['reparse_poll_sec']),
        cursor_overlap_sec=int(raw['cursor_overlap_sec']),
        catalog_batch_size=int(raw['catalog_batch_size']),
        catalog_throttle_threshold=int(raw['catalog_throttle_threshold']),
        max_attempts=int(raw['max_attempts']),
        lease_timeout_sec=int(raw['lease_timeout_sec']),
        tasks_retention_days=int(raw['tasks_retention_days']),
        parse_errors_retention_days=int(raw['parse_errors_retention_days']),
        desired_workers_default=int(raw['desired_workers_default']),
        shutdown_grace_sec=int(raw['shutdown_grace_sec']),
        proxy=proxy,
    )


def dsn(name: str) -> str:
    """DSN из окружения (.env через docker compose / source). Отсутствие — громкая ошибка."""
    if name not in DSN_KEYS:
        raise ValueError(f'неизвестный DSN: {name}; допустимые: {DSN_KEYS}')
    val = os.environ.get(name, '').strip()
    if not val:
        raise RuntimeError(f'{name} не задан (см. .env.example)')
    return val
