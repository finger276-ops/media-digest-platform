-- Живые сессии платформы: кто сейчас онлайн, в каком проекте, с какой ролью.
-- Выполнить один раз в Supabase SQL Editor. Скрипт идемпотентный.
--
-- У платформы нет системы логинов (доступ — общие коды на проект), поэтому
-- сессия анонимна: это случайный ID браузерной вкладки, сгенерированный при
-- заходе, а не человек. Вкладка каждые ~45 секунд обновляет last_seen_at
-- (см. src/session_presence_ui.py). Таблица нужна только для панели
-- «Платформа → Сессии» (видна только владельцу платформы) и не участвует в
-- кешировании (services/cached_store.py) — читается и пишется напрямую,
-- по образцу services/ingest_queue.py.

create table if not exists public.platform_sessions (
    session_id text primary key,
    project_id text references public.platform_projects(project_id) on delete cascade,
    role text not null default 'viewer'
        check (role in ('owner', 'editor', 'viewer')),
    started_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now()
);

comment on table public.platform_sessions is
    'Живые анонимные сессии платформы: последняя активность браузерной вкладки для панели "кто сейчас онлайн"';

-- Выборка «кто онлайн / недавно ушёл» всегда сортирует/фильтрует по last_seen_at.
create index if not exists idx_platform_sessions_last_seen
    on public.platform_sessions(last_seen_at desc);

-- Разбивка «сколько онлайн по проектам».
create index if not exists idx_platform_sessions_project
    on public.platform_sessions(project_id, last_seen_at desc);

-- RLS: доступ только у service_role, как и у остальных platform_* таблиц.
alter table public.platform_sessions enable row level security;
