-- Схема парсера (база parser_ebay). Колонки — по SPEC §8.

create table if not exists tasks (
    task_id         bigserial primary key,
    type            text        not null check (type in ('seed', 'catalog', 'item')),
    article         text,
    item_id         bigint,
    zip             text        not null,
    params          jsonb,
    source          text        not null check (source in ('cli', 'seed', 'validator', 'reparse')),
    reparse_task_id bigint,
    run_id          bigint,
    status          text        not null default 'pending'
                                check (status in ('pending', 'processing', 'done', 'failed')),
    attempts        smallint    not null default 0,
    fingerprint     text        not null,
    leased_by       text,
    leased_at       timestamptz,
    created_at      timestamptz not null default now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    last_error      text
);

-- единственность активной задачи; ON CONFLICT обязан повторять предикат (SPEC §8)
create unique index if not exists uq_tasks_active_fp
    on tasks (fingerprint) where status in ('pending', 'processing');
-- выбор задач воркером: items-first, FIFO внутри типа
create index if not exists idx_tasks_pending
    on tasks (type, task_id) where status = 'pending';
-- адаптер-SQL платформы (done за интервал) и очистка retention
create index if not exists idx_tasks_status_finished
    on tasks (status, finished_at);
-- reaper зависших processing
create index if not exists idx_tasks_processing_leased
    on tasks (leased_at) where status = 'processing';

create table if not exists runs (
    run_id         bigserial primary key,
    params         jsonb       not null,
    articles_total integer,
    created_at     timestamptz not null default now()
);

create table if not exists cursors (
    name       text        primary key,
    pos        timestamptz not null,
    updated_at timestamptz not null default now()
);

create table if not exists worker_hosts (
    host            text        primary key,
    desired_workers integer     not null check (desired_workers >= 0),
    updated_at      timestamptz not null default now()
);

create table if not exists parse_errors (
    id         bigserial   primary key,
    task_id    bigint,
    kind       text        not null,
    html_gz    bytea,
    created_at timestamptz not null default now()
);

create index if not exists idx_parse_errors_created on parse_errors (created_at);
