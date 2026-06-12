"""Конфигурация (SPEC §9): .env — подключения, config.yaml — параметры.
Читается один раз при старте; отсутствие ключа — ошибка, дефолтов нет."""
import os
from dataclasses import dataclass

import yaml

DSN_KEYS = ('PARSER_DSN', 'EBAY_TO_BUY_DSN', 'SMART_DSN',
            'EBAY_DATA_DSN', 'VALIDATOR_DSN')

MODES = ('once', 'continuous')
CONDITIONS = ('all', 'new', 'used')


@dataclass(frozen=True)
class Config:
    zip_default: str
    condition_default: str
    mode_default: str
    catalog_refresh_sec_default: int
    coordinator_poll_sec: int
    feed_refresh_sec: int
    poll_interval_sec: int
    dispatch_timeout_sec: int
    tasks_retention_days: int
    parse_errors_retention_days: int


def load_config(path: str = 'config.yaml') -> Config:
    raw = yaml.safe_load(open(path))
    cfg = Config(
        zip_default=str(raw['zip_default']),
        condition_default=str(raw['condition_default']),
        mode_default=str(raw['mode_default']),
        catalog_refresh_sec_default=int(raw['catalog_refresh_sec_default']),
        coordinator_poll_sec=int(raw['coordinator_poll_sec']),
        feed_refresh_sec=int(raw['feed_refresh_sec']),
        poll_interval_sec=int(raw['poll_interval_sec']),
        dispatch_timeout_sec=int(raw['dispatch_timeout_sec']),
        tasks_retention_days=int(raw['tasks_retention_days']),
        parse_errors_retention_days=int(raw['parse_errors_retention_days']),
    )
    if cfg.mode_default not in MODES:
        raise ValueError(f'mode_default: ожидается {MODES}, получено {cfg.mode_default!r}')
    if cfg.condition_default not in CONDITIONS:
        raise ValueError(f'condition_default: ожидается {CONDITIONS}, '
                         f'получено {cfg.condition_default!r}')
    return cfg


def load_dotenv(path: str = '.env') -> None:
    """Простейший загрузчик .env: строки KEY=VALUE; существующие переменные не перетирает."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip())


def load_dsns(env=os.environ) -> dict:
    missing = [k for k in DSN_KEYS if not env.get(k)]
    if missing:
        raise ValueError(f'нет переменных окружения: {missing} (см. .env.example)')
    return {k: env[k] for k in DSN_KEYS}
