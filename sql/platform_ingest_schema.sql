-- Автозагрузка выгрузок (n8n → Supabase → воркер платформы).
-- Выполнить один раз в Supabase SQL Editor. Скрипт идемпотентный.
--
-- Схема состоит из двух таблиц:
--   platform_ingest_sources — маппинг внешнего источника (отчет Brand Analytics,
--       адрес отправителя, папка) на проект платформы и параметры обработки;
--   platform_ingest_queue   — очередь файлов, которые положил n8n и которые
--       должен обработать воркер (scripts/ingest_worker.py).

create table if not exists public.platform_ingest_sources (
    source_key text primary key,
    project_id text not null references public.platform_projects(project_id) on delete cascade,
    title text default '',
    source_system text not null default 'auto',
    -- параметры алгоритма и поведения импорта:
    -- {"similarity_threshold": 0.3, "event_gap_hours": 3, "event_window_hours": 16,
    --  "period_name_template": "", "replace": true}
    params jsonb not null default '{}'::jsonb,
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

comment on table public.platform_ingest_sources is
    'Маппинг внешнего источника автозагрузки (отчет BA, отправитель, папка) на проект платформы';

create table if not exists public.platform_ingest_queue (
    task_id text primary key,
    -- project_id может быть пустым: тогда воркер определит проект по source_key
    project_id text references public.platform_projects(project_id) on delete cascade,
    source_key text default '',
    storage_path text not null,
    original_filename text not null default 'upload.xlsx',
    file_sha256 text not null default '',
    file_size bigint not null default 0,
    source_system text not null default 'auto',
    period_name text default '',
    date_from date,
    date_to date,
    params jsonb not null default '{}'::jsonb,
    -- произвольный контекст от n8n: тема письма, отправитель, id выполнения и т.д.
    context jsonb not null default '{}'::jsonb,
    status text not null default 'pending'
        check (status in ('pending', 'processing', 'done', 'error', 'skipped')),
    attempts integer not null default 0,
    max_attempts integer not null default 3,
    worker_id text default '',
    error_message text default '',
    result jsonb not null default '{}'::jsonb,
    period_id text default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz
);

comment on table public.platform_ingest_queue is
    'Очередь автозагрузки: n8n кладет файл в Storage и задачу сюда, воркер обрабатывает';

-- Очередь воркера: самые старые pending первыми.
create index if not exists idx_platform_ingest_queue_status_created
    on public.platform_ingest_queue(status, created_at);

create index if not exists idx_platform_ingest_queue_project_created
    on public.platform_ingest_queue(project_id, created_at desc);

create index if not exists idx_platform_ingest_queue_source
    on public.platform_ingest_queue(source_key, created_at desc);

-- Защита от повторной загрузки одного и того же файла в один проект.
-- n8n отправляет insert с `Prefer: resolution=ignore-duplicates`, поэтому
-- повторное письмо с тем же вложением просто не создаст новую задачу.
create unique index if not exists uq_platform_ingest_queue_project_hash
    on public.platform_ingest_queue(project_id, file_sha256)
    where file_sha256 <> '';

-- Ту же защиту нужно иметь и для задач без project_id (проект резолвится по source_key).
create unique index if not exists uq_platform_ingest_queue_source_hash
    on public.platform_ingest_queue(source_key, file_sha256)
    where project_id is null and file_sha256 <> '';

-- RLS: доступ к очереди только у service_role (n8n и воркер ходят с service-ключом).
-- Анонимный ключ не должен видеть ни задачи, ни маппинг источников.
alter table public.platform_ingest_queue enable row level security;
alter table public.platform_ingest_sources enable row level security;

-- Автообновление updated_at.
create or replace function public.platform_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_platform_ingest_queue_touch on public.platform_ingest_queue;
create trigger trg_platform_ingest_queue_touch
    before update on public.platform_ingest_queue
    for each row execute function public.platform_touch_updated_at();

drop trigger if exists trg_platform_ingest_sources_touch on public.platform_ingest_sources;
create trigger trg_platform_ingest_sources_touch
    before update on public.platform_ingest_sources
    for each row execute function public.platform_touch_updated_at();
