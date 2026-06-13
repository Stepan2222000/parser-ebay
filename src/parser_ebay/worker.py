"""Воркер (SPEC §6): один контейнер = один воркер = один браузер cloakbrowser
(headless=False под Xvfb) = одна задача за раз, без пауз между задачами (темп
системы регулируется только числом воркеров, SPEC §7).

Процесс запускает ``ebaylib.run_worker`` с тремя колбэками и реализует жёсткую
политику смертей: любая критическая ошибка валит воркер, причина пишется в
``parse_errors``, Docker ``restart: always`` поднимает заново. Брошенную задачу
возвращает координатор перевыдачей (SPEC §5.5).
"""
import asyncio
import gzip
import json
import logging
import os
import signal
import socket
import traceback
from urllib.parse import urlsplit

import asyncpg
from cloakbrowser import launch_async
from ebaylib import ParseError, Store, run_worker

from parser_ebay.config import load_config, load_dotenv
from parser_ebay.db import connect

log = logging.getLogger('parser.worker')

ME = f"{socket.gethostname()}:{os.getpid()}"

# Забор: item раньше каталога (абсолютный приоритет — «сразу после каталога его
# item'ы»), FIFO внутри типа, FOR UPDATE SKIP LOCKED разводит воркеров (SPEC §6).
PICK = """
update tasks set status = 'processing', dispatched_at = now(),
       dispatched_to = $1, attempts = attempts + 1
where task_id = (select task_id from tasks
                 where status = 'pending'
                 order by (type = 'item') desc, task_id
                 limit 1 for update skip locked)
returning task_id, type, part_id, articles, item_id, zip, condition,
          min_price, max_price, reparse_task_id, fingerprint
"""

DONE = "update tasks set status = 'done', finished_at = now(), parse_ms = $2 where task_id = $1"
# reparse: правим источник на месте через FDW (SPEC §2 — без дублирования к себе)
REPARSE_DONE = ("update validation_fdw.reparse_tasks set done_at = now() "
                "where task_id = $1 and done_at is null")
SAVE_ERROR = ("insert into parse_errors(task_fingerprint, kind, error, html_gz) "
              "values($1, $2, $3, $4)")


def _proxy_cfg():
    """Прокси для нового контекста. Сейчас — статичный из ``EBAY_PROXY_URL`` (свой
    на воркера); позже эту функцию заменит выдача из proxy-manager (SPEC §12).
    Нет переменной → direct."""
    url = os.environ.get('EBAY_PROXY_URL')
    if not url:
        return None
    u = urlsplit(url)
    cfg = {'server': f'{u.scheme}://{u.hostname}:{u.port}'}
    if u.username:
        cfg['username'] = u.username
        cfg['password'] = u.password or ''
    return cfg


def _to_task(row) -> dict:
    """Строка журнала → задача формата ebaylib (всё вне ``params`` — метаданные
    оркестратора, библиотека вернёт их в ``task_done`` как есть, SPEC §6)."""
    if row['type'] == 'catalog':
        params = {'articles': list(row['articles']), 'zip': row['zip'],
                  'condition': row['condition'],
                  'min_price': float(row['min_price']) if row['min_price'] is not None else None,
                  'max_price': float(row['max_price']) if row['max_price'] is not None else None}
    else:
        params = {'item_id': str(row['item_id']), 'zip': row['zip']}
    return {'type': row['type'], 'params': params, 'task_id': row['task_id'],
            'reparse_task_id': row['reparse_task_id'], 'fingerprint': row['fingerprint']}


async def amain() -> None:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    load_dotenv()
    cfg = load_config()
    parser_dsn = os.environ['PARSER_DSN']
    ebay_data_dsn = os.environ['EBAY_DATA_DSN']

    pc = await connect(parser_dsn)    # забор задач (next_task)
    pcd = await connect(parser_dsn)   # подтверждения (task_done) — отдельное
                                      # соединение: библиотека пишет фоновым
                                      # писателем КОНКУРЕНТНО с забором, asyncpg
                                      # не допускает параллельных операций на одном

    # Кэш/фильтрация (SPEC §12) — по наличию EBAY_FILTERING_DSN; best-effort.
    cache = None
    fdsn = os.environ.get('EBAY_FILTERING_DSN')
    if fdsn:
        from ebay_filtering import Backend, CacheClient
        cache = await CacheClient(Backend(fdsn)).connect()

    browser = await launch_async(headless=False)
    state = {'ctx': None}
    proxy = _proxy_cfg()
    log.info('воркер %s стартовал, proxy=%s, cache=%s', ME,
             proxy['server'] if proxy else 'нет', bool(cache))

    stopping = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)

    async def get_page():
        if state['ctx'] is not None:
            await state['ctx'].close()      # старую страницу утилизирует закрытие контекста
        state['ctx'] = (await browser.new_context(proxy=proxy) if proxy
                        else await browser.new_context())
        page = await state['ctx'].new_page()
        if cache is not None:
            try:
                await cache.attach(page)    # best-effort: мозг лёг → страница без кэша
            except Exception as e:
                log.warning('cache.attach не удался (%s) — страница без кэша', e)
        return page

    async def next_task():
        # SIGTERM → None: библиотека допишет хвост записи и штатно выйдет (дренаж).
        while not stopping.is_set():
            row = await pc.fetchrow(PICK, ME)
            if row is not None:
                t = _to_task(row)
                log.info('взял %s#%s %s', t['type'], t['task_id'], t['fingerprint'])
                return t
            await asyncio.sleep(cfg.poll_interval_sec)
        log.info('SIGTERM — дренаж: новых задач не беру')
        return None

    async def task_done(task, stats):
        timing = stats['timing']
        stages = timing['stages']
        # parse_ms = чистый парсинг без асинхронной записи (SPEC §4/§10): из total
        # убираем write-сторону (queue + write — она перекрывается со следующей задачей)
        parse_ms = round(timing['total_ms'] - stages.get('write', 0) - stages.get('queue', 0))
        # оба апдейта — одна транзакция (SPEC §2): FDW-апдейт валидатора участвует
        # в локальной транзакции, либо оба, либо ни одного — без рассинхрона
        async with pcd.transaction():
            if task['reparse_task_id'] is not None:
                await pcd.execute(REPARSE_DONE, task['reparse_task_id'])
            await pcd.execute(DONE, task['task_id'], parse_ms)
        log.info('done %s#%s parse_ms=%s total_ms=%s stages=%s db=%s',
                 task['type'], task['task_id'], parse_ms, round(timing['total_ms']),
                 {k: round(v) for k, v in stages.items()},
                 json.dumps(stats['db'], ensure_ascii=False, default=str)[:200])

    try:
        await run_worker(get_page, next_task, Store(ebay_data_dsn), task_done=task_done)
        log.info('штатное завершение (дренаж дописан)')
    except Exception as e:
        # Любая критическая ошибка валит воркер (SPEC §6). Виновница — e.task;
        # в parse_errors — сырьё: полный трейсбек + HTML, когда есть (ParseError).
        # Запись защищена: не вышло записать → воркер всё равно падает с исходной
        # причиной (Docker поднимет, причина видна в логе).
        task = getattr(e, 'task', None)
        fp = task.get('fingerprint') if isinstance(task, dict) else None
        try:
            html_gz = (gzip.compress(e.html.encode())
                       if isinstance(e, ParseError) and getattr(e, 'html', None) else None)
            err = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
            await pc.execute(SAVE_ERROR, fp, type(e).__name__, err, html_gz)
        except Exception as rec:
            log.error('не удалось записать parse_errors (%r) — исходная причина ниже', rec)
        log.error('смерть воркера, виновница=%s: %r', fp, e)
        raise
    finally:
        await browser.close()
        if cache is not None:
            cache.close()
        await pc.close()
        await pcd.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == '__main__':
    main()
