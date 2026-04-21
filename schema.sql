-- Run this entire file in Supabase SQL Editor (Dashboard > SQL Editor > New Query)
-- Creates all tables needed for the multi-user ADAPT-AI assistant.

-- === USERS ===
create table if not exists users (
    id uuid primary key default gen_random_uuid(),
    google_sub text unique not null,
    email text not null,
    name text,
    picture_url text,
    refresh_token text not null,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create index if not exists users_google_sub_idx on users (google_sub);
create index if not exists users_email_idx on users (email);

-- === REMINDERS (fallback when Calendar is unavailable) ===
create table if not exists reminders (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users(id) on delete cascade,
    title text not null,
    when_iso timestamptz,
    description text,
    duration_minutes int default 60,
    source text default 'fallback',
    created_at timestamptz default now()
);

create index if not exists reminders_user_id_idx on reminders (user_id);

-- === BANDIT STATE (per-user UCB1 / future LinUCB) ===
create table if not exists bandit_state (
    user_id uuid not null references users(id) on delete cascade,
    skill text not null,
    variant text not null,
    count int default 0,
    total_reward float default 0,
    updated_at timestamptz default now(),
    primary key (user_id, skill, variant)
);

-- === TRACES (every assistant response gets one, enables feedback loop) ===
create table if not exists traces (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users(id) on delete cascade,
    skill text not null,
    strategy text not null,
    request jsonb,
    response_text text,
    created_at timestamptz default now()
);

create index if not exists traces_user_id_idx on traces (user_id);
create index if not exists traces_created_at_idx on traces (created_at desc);

-- === FEEDBACK (thumbs up / down from UI) ===
create table if not exists feedback (
    id uuid primary key default gen_random_uuid(),
    trace_id uuid not null references traces(id) on delete cascade,
    user_id uuid not null references users(id) on delete cascade,
    reward float not null,
    label text,
    comment text,
    created_at timestamptz default now()
);

create index if not exists feedback_user_id_idx on feedback (user_id);

-- === USER MEMORY (Phase 4: adaptive personalization) ===
create table if not exists user_memory (
    user_id uuid primary key references users(id) on delete cascade,
    preferences jsonb default '{}'::jsonb,
    contacts jsonb default '{}'::jsonb,
    updated_at timestamptz default now()
);

-- Done. All tables ready for the multi-user app.
