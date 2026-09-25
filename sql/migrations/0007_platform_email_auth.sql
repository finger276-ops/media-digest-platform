-- Вход по email: одноразовый код из письма (Supabase Auth) и «запомнить меня».
--
-- Доступ человека к проекту хранится в platform_project_members (0002):
-- адрес в нижнем регистре, роль viewer/editor, статус active. Здесь — только
-- то, что нужно самому входу. Код из письма выдаёт и проверяет Supabase Auth,
-- в базе платформы его нет.
--
-- platform_auth_sessions — вход, запомненный в браузере на 14 дней. В cookie
-- лежит случайный ключ, в таблице — только его sha256: утечка таблицы не
-- даёт войти. «Выйти» ставит revoked_at, и ключ перестаёт работать сразу.
create table if not exists public.platform_auth_sessions (
    token_hash text primary key,
    user_email text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    last_seen_at timestamptz not null default now(),
    revoked_at timestamptz
);

comment on table public.platform_auth_sessions is
    'Запомненные входы по email: sha256 ключа из cookie, адрес, срок и отзыв';

create index if not exists idx_platform_auth_sessions_email
    on public.platform_auth_sessions(user_email);

create index if not exists idx_platform_auth_sessions_expires
    on public.platform_auth_sessions(expires_at);

-- platform_auth_throttle — ограничители входа. Supabase Auth не считает
-- неверные попытки на один код, а все запросы приходят с одного адреса
-- сервера Streamlit, поэтому считать приходится платформе. Ключ — sha256
-- адреса, а не сам адрес: запись заводится для любого введённого адреса,
-- в том числе чужого, и хранить их открытым текстом незачем.
create table if not exists public.platform_auth_throttle (
    email_hash text primary key,
    last_sent_at timestamptz,
    window_started_at timestamptz,
    sends_in_window integer not null default 0,
    failed_attempts integer not null default 0,
    updated_at timestamptz not null default now()
);

comment on table public.platform_auth_throttle is
    'Ограничители входа по email: частота отправки кода и неверные попытки';

-- RLS без политик: доступ только у service_role, как у platform_sessions и
-- очереди автозагрузки. В platform_project_members теперь лежат адреса
-- людей, поэтому таблица закрывается так же — приложение ходит под
-- service_role и этого не замечает.
alter table public.platform_auth_sessions enable row level security;
alter table public.platform_auth_throttle enable row level security;
alter table public.platform_project_members enable row level security;

insert into public.schema_migrations (version, name)
values ('0007', 'platform_email_auth')
on conflict (version) do nothing;
