-- Daylist task storage. Run this in the Supabase SQL Editor.
create table if not exists public.tasks (
  id uuid primary key,
  owner_id uuid not null references auth.users (id) on delete cascade,
  title text not null check (char_length(title) between 1 and 160),
  done boolean not null default false,
  created_at timestamptz not null default now(),
  due_at timestamptz,
  reminded boolean not null default false,
  notes text not null default '',
  color text check (color is null or color in ('yellow', 'mint', 'rose', 'peach', 'sky', 'lilac'))
);

-- CREATE TABLE IF NOT EXISTS does not update a table created by an older schema.
-- Add this column explicitly so rerunning the script can upgrade that table too.
-- It remains nullable during migration so existing rows are preserved; new app
-- writes always set owner_id and the row-level security policies isolate users.
alter table public.tasks
  add column if not exists owner_id uuid references auth.users (id) on delete cascade;

-- Upgrade older installations that used user_id as the owner column. Keep its
-- values, copy valid owners into owner_id, then make the legacy column optional
-- because the current app writes owner_id.
do $migration$
declare
  legacy_user_id_type text;
  legacy_user_id_nullable text;
begin
  select data_type, is_nullable
    into legacy_user_id_type, legacy_user_id_nullable
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'tasks'
      and column_name = 'user_id';

  if found then
    if legacy_user_id_type = 'uuid' then
      update public.tasks as task
        set owner_id = task.user_id
        where task.owner_id is null
          and exists (select 1 from auth.users as auth_user where auth_user.id = task.user_id);
    end if;

    if legacy_user_id_nullable = 'NO' then
      alter table public.tasks alter column user_id drop not null;
    end if;
  end if;
end;
$migration$;

-- Remove legacy RLS policies that still require user_id; the owner_id policies
-- below replace them while preserving per-user isolation.
do $migration$
declare
  legacy_policy record;
begin
  for legacy_policy in
    select policyname
      from pg_policies
      where schemaname = 'public'
        and tablename = 'tasks'
        and (
          position('user_id' in lower(coalesce(qual, ''))) > 0
          or position('user_id' in lower(coalesce(with_check, ''))) > 0
        )
  loop
    execute format('drop policy %I on public.tasks', legacy_policy.policyname);
  end loop;
end;
$migration$;

create index if not exists tasks_owner_created_at_idx
  on public.tasks (owner_id, created_at desc);

alter table public.tasks enable row level security;

grant select, insert, update, delete on public.tasks to authenticated;

drop policy if exists "Users can read their own tasks" on public.tasks;
create policy "Users can read their own tasks"
  on public.tasks for select
  to authenticated
  using ((select auth.uid()) = owner_id);

drop policy if exists "Users can create their own tasks" on public.tasks;
create policy "Users can create their own tasks"
  on public.tasks for insert
  to authenticated
  with check ((select auth.uid()) = owner_id);

drop policy if exists "Users can update their own tasks" on public.tasks;
create policy "Users can update their own tasks"
  on public.tasks for update
  to authenticated
  using ((select auth.uid()) = owner_id)
  with check ((select auth.uid()) = owner_id);

drop policy if exists "Users can delete their own tasks" on public.tasks;
create policy "Users can delete their own tasks"
  on public.tasks for delete
  to authenticated
  using ((select auth.uid()) = owner_id);
