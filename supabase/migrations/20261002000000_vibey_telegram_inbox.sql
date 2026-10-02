-- Telegram webhook inbox for @Vibey_Robot.
-- Telegram -> edge function vibey-telegram-webhook -> this table -> the Mac drains it.
-- RLS on with NO policies: only the service role (edge function, Mac) can touch these.

create table if not exists public.vibey_telegram_inbox (
  update_id          bigint primary key,
  chat_id            bigint,
  payload            jsonb not null,
  received_at        timestamptz not null default now(),
  offline_replied_at timestamptz,
  processed_at       timestamptz
);

create index if not exists vibey_telegram_inbox_unprocessed
  on public.vibey_telegram_inbox (update_id) where processed_at is null;

create index if not exists vibey_telegram_inbox_chat_offline
  on public.vibey_telegram_inbox (chat_id, offline_replied_at desc)
  where offline_replied_at is not null;

create table if not exists public.vibey_heartbeat (
  id        text primary key,
  last_seen timestamptz not null default now()
);

insert into public.vibey_heartbeat (id, last_seen) values ('bot', now())
  on conflict (id) do nothing;

alter table public.vibey_telegram_inbox enable row level security;
alter table public.vibey_heartbeat enable row level security;

revoke all on public.vibey_telegram_inbox from anon, authenticated;
revoke all on public.vibey_heartbeat from anon, authenticated;
