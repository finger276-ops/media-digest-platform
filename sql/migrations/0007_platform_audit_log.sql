-- Журнал ручных правок: кто, когда и что поменял в данных проекта.
--
-- Правки аналитиков (названия и описания инфоповодов, объединения, скрытые и
-- перенесённые сообщения, саммари) хранятся в platform_manual_rows и
-- перезаписываются: прежнее значение пропадает, а кто его поменял — неизвестно.
-- Журнал дописывает строку на каждую правку и никогда не меняет старые.
--
-- Личностей у кодов доступа нет, поэтому «кто» — роль и анонимный ID
-- браузерной сессии (тот же, что в platform_sessions). Две правки с одним ID
-- сделаны в одной вкладке. Читает журнал владелец платформы: раздел
-- «Платформа → Журнал». Пишется из services/audit_log.py, без кеша.

create table if not exists public.platform_audit_log (
    id bigserial primary key,
    project_id text not null references public.platform_projects(project_id) on delete cascade,
    created_at timestamptz not null default now(),
    actor_role text not null default '',
    actor_session text not null default '',
    action text not null check (action in ('save', 'delete')),
    table_name text not null default '',
    row_key text not null default '',
    summary text not null default '',
    before jsonb,
    after jsonb
);

comment on table public.platform_audit_log is
    'Журнал ручных правок: роль и анонимная сессия автора, время, прежнее и новое значение';

-- Журнал читается по проекту от новых к старым.
create index if not exists idx_platform_audit_log_project_time
    on public.platform_audit_log(project_id, created_at desc);

create index if not exists idx_platform_audit_log_time
    on public.platform_audit_log(created_at desc);

-- RLS: доступ только у service_role, как и у остальных platform_* таблиц.
alter table public.platform_audit_log enable row level security;

insert into public.schema_migrations (version, name)
values ('0007', 'platform_audit_log')
on conflict (version) do nothing;
