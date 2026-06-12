-- Схема parser_ebay (SPEC §4): runs — вход системы, tasks — журнал задач,
-- parse_errors — сырьё для починки селекторов.

create table runs (
    run_id     bigserial primary key,
    params     jsonb       not null,
    is_active  boolean     not null default true,
    created_at timestamptz not null default now()
);

create table tasks (
    task_id         bigserial primary key,
    type            text        not null check (type in ('catalog', 'item')),
    part_id         text,
    articles        text[],
    item_id         bigint,
    zip             text        not null,
    condition       text,
    min_price       numeric,
    max_price       numeric,
    run_id          bigint      references runs,
    source          text        not null check (source in ('feed', 'validator', 'reparse')),
    reparse_task_id bigint,
    fingerprint     text        not null,
    status          text        not null default 'pending'
                    check (status in ('pending', 'processing', 'done', 'cancelled')),
    attempts        smallint    not null default 0,
    created_at      timestamptz not null default now(),
    dispatched_at   timestamptz,
    dispatched_to   text,
    parse_ms        integer,
    finished_at     timestamptz
);

-- одна активная задача на потребность; done/cancelled-история не мешает
create unique index tasks_active_fp on tasks (fingerprint)
    where status in ('pending', 'processing');
-- забор воркером: item раньше каталога, FIFO внутри типа
create index tasks_pending_pick on tasks (type, task_id) where status = 'pending';
-- темп done и ретеншен
create index tasks_status_finished on tasks (status, finished_at);

create table parse_errors (
    id               bigserial primary key,
    task_fingerprint text,
    kind             text,
    error            text,
    html_gz          bytea,
    created_at       timestamptz not null default now()
);
