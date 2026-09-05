-- Система иерархических тегов проекта (Этап 2).
-- Одна строка на проект: вся структура хранится целиком в jsonb.
-- Загрузка/замена атомарны; при удалении проекта структура удаляется каскадно.
--
-- structure: json-массив записей [{"tag": "...", "tier": 1, "parent": ""}, ...]
--   tag    — название тега (совпадает с колонкой-тегом в выгрузке Brand Analytics
--            или группирующий тег, которого в выгрузке нет)
--   tier   — уровень (1 = верхний)
--   parent — родительский тег ("" для корней)
--
-- Применение: выполнить в Supabase SQL Editor один раз.

create table if not exists public.platform_tag_hierarchies (
    project_id text primary key references public.platform_projects(project_id) on delete cascade,
    structure jsonb not null default '[]'::jsonb,
    source_filename text default '',
    tags_count integer not null default 0,
    max_tier integer not null default 0,
    uploaded_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

comment on table public.platform_tag_hierarchies is
    'Иерархия тегов проекта (дерево tag/tier/parent), загружается аналитиком из Excel';
