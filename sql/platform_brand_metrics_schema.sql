-- Категорийные бенчмарки для метрик SOV и ReachScore.
-- Выполнить один раз в Supabase SQL Editor. Скрипт идемпотентный.
--
-- Одна строка на период проекта. Вся разбивка по брендам лежит в jsonb:
--   brands: [{"brand": "...", "messages": 0, "audience": 0, "reach": 0,
--             "engagement": 0, "is_own": false}, ...]
--
-- Данные считаются при загрузке выгрузки по всей категории и хранятся уже
-- агрегированными: сами сообщения конкурентов платформе не нужны, поэтому
-- таблица остаётся маленькой (несколько строк на период).

create table if not exists public.platform_category_benchmarks (
    project_id text not null references public.platform_projects(project_id) on delete cascade,
    period_id text not null,
    own_brand text default '',
    brands jsonb not null default '[]'::jsonb,
    brand_mode text not null default 'columns',   -- columns | values
    brand_source text default '',                 -- колонка или список колонок
    source_filename text default '',
    messages_total integer not null default 0,
    updated_at timestamptz not null default now(),
    primary key (project_id, period_id)
);

comment on table public.platform_category_benchmarks is
    'Агрегаты по брендам категории для расчёта SOV и ReachScore';

create index if not exists idx_platform_category_benchmarks_project
    on public.platform_category_benchmarks(project_id, updated_at desc);
