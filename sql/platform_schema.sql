-- Multi-project digest platform schema.
-- Uses platform_* tables so the current taxi-chat dashboard dashboard_* tables remain untouched.

create table if not exists public.platform_projects (
    project_id text primary key,
    project_name text not null,
    description text default '',
    status text not null default 'active',
    viewer_code_hash text default '',
    editor_code_hash text default '',
    settings jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.platform_periods (
    project_id text not null references public.platform_projects(project_id) on delete cascade,
    period_id text primary key,
    period_name text not null,
    date_from date,
    date_to date,
    source_filename text default '',
    status text not null default 'active',
    manifest jsonb not null default '{}'::jsonb,
    uploaded_at timestamptz not null default now(),
    unique(project_id, period_id)
);

create table if not exists public.platform_table_rows (
    project_id text not null references public.platform_projects(project_id) on delete cascade,
    period_id text not null,
    table_name text not null,
    row_id text not null,
    payload jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now(),
    primary key (project_id, period_id, table_name, row_id),
    foreign key (project_id, period_id) references public.platform_periods(project_id, period_id) on delete cascade
);

create table if not exists public.platform_manual_rows (
    project_id text not null references public.platform_projects(project_id) on delete cascade,
    table_name text not null,
    row_key text not null,
    payload jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now(),
    primary key (project_id, row_key)
);

create table if not exists public.platform_project_members (
    id bigserial primary key,
    project_id text not null references public.platform_projects(project_id) on delete cascade,
    user_email text not null,
    role text not null check (role in ('owner', 'editor', 'viewer')),
    status text not null default 'active',
    created_at timestamptz not null default now(),
    unique(project_id, user_email)
);

create index if not exists idx_platform_periods_project_status on public.platform_periods(project_id, status);
create index if not exists idx_platform_table_rows_lookup on public.platform_table_rows(project_id, period_id, table_name);
create index if not exists idx_platform_manual_rows_lookup on public.platform_manual_rows(project_id, table_name);
create index if not exists idx_platform_members_email on public.platform_project_members(user_email);


-- Performance indexes for large multi-period dashboards.
create index if not exists idx_platform_periods_project_status_uploaded
    on public.platform_periods(project_id, status, uploaded_at desc);

create index if not exists idx_platform_table_rows_project_table_period
    on public.platform_table_rows(project_id, table_name, period_id);

create index if not exists idx_platform_table_rows_project_period_table
    on public.platform_table_rows(project_id, period_id, table_name);

create index if not exists idx_platform_manual_rows_project_table_updated
    on public.platform_manual_rows(project_id, table_name, updated_at desc);
