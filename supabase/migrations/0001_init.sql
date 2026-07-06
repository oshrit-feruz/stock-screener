-- Phase 1 — Supabase auth schema (schema + RLS only; NOT wired into the API yet).
--
-- Introduces the per-user data model for the B2C product. Nothing reads these
-- tables at runtime yet: the existing data/positions/*.json and
-- data/portfolio/portfolio.json remain the live source until Phase 2 flips the
-- API over. This migration is additive and parallel — it does not change any
-- current behaviour.
--
-- Design decisions (from the Phase 0 investigation):
--   Q1 daily cron  → stays global for the signal feed; iterates users' positions
--                    for exit reminders/beta in Phase 2 (no schema impact here).
--   Q2 beta scope  → GLOBAL "official beta" record. The current single-tenant
--                    positions are preserved as official-beta rows:
--                    user_id IS NULL + is_official_beta = true. Regular users get
--                    their own rows (user_id = auth.uid()); those never feed the
--                    official beta dashboard.
--   Q3 alerts      → /api/alerts stays global; /api/portfolio/alerts becomes
--                    per-user in Phase 2 (no schema impact here).

create extension if not exists pgcrypto with schema extensions;   -- gen_random_uuid()

-- ── profiles ────────────────────────────────────────────────────────────────
-- One row per authenticated user. Mirrors the onboarding preferences that live
-- in localStorage today (user_mode / time_horizon / tax_mode).
create table if not exists public.profiles (
  id           uuid primary key references auth.users(id) on delete cascade,
  user_mode    text check (user_mode in ('fresh', 'existing', 'both')),
  time_horizon text,
  tax_mode     text,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- ── positions ───────────────────────────────────────────────────────────────
-- Per-user tracked positions (open + closed via `status`). The pre-auth,
-- single-tenant history is migrated in as OFFICIAL BETA rows
-- (user_id IS NULL + is_official_beta = true) so the global beta dashboard keeps
-- its track record. Columns mirror the JSON schema from data/positions/*.json.
create table if not exists public.positions (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid references auth.users(id) on delete cascade,
  is_official_beta boolean not null default false,
  ticker           text not null,
  entry_date       date not null,
  entry_price      double precision not null,
  signal_composite double precision,
  signal_drawdown  double precision,
  status           text not null default 'open' check (status in ('open', 'closed')),
  exit_date        date,
  exit_price       double precision,
  realized_return  double precision,
  days_held        integer,
  reminder_sent    boolean not null default false,
  created_at       timestamptz not null default now(),
  -- Every row is either a real user's row or an official-beta row (never neither).
  constraint positions_owner_chk check (user_id is not null or is_official_beta)
);
create index if not exists positions_user_id_idx  on public.positions (user_id);
create index if not exists positions_official_idx on public.positions (is_official_beta) where is_official_beta;

-- ── portfolio_holdings ──────────────────────────────────────────────────────
-- Per-user watch/holdings with price-alert thresholds. Mirrors portfolio.json.
create table if not exists public.portfolio_holdings (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references auth.users(id) on delete cascade,
  ticker         text not null,
  entry_price    double precision,
  alert_up_pct   double precision not null default 20,
  alert_down_pct double precision not null default 10,
  created_at     timestamptz not null default now(),
  unique (user_id, ticker)
);
create index if not exists portfolio_holdings_user_id_idx on public.portfolio_holdings (user_id);

-- ── Row-Level Security ──────────────────────────────────────────────────────
-- A user can see/manage only their own rows (user_id = auth.uid()). Official-beta
-- position rows (user_id IS NULL) match no user, so they are invisible via these
-- policies — the global beta dashboard reads them through the service role / a
-- dedicated view added in Phase 2, never through user RLS.
alter table public.profiles           enable row level security;
alter table public.positions          enable row level security;
alter table public.portfolio_holdings enable row level security;

-- profiles
create policy profiles_select_own on public.profiles
  for select using (auth.uid() = id);
create policy profiles_insert_own on public.profiles
  for insert with check (auth.uid() = id);
create policy profiles_update_own on public.profiles
  for update using (auth.uid() = id) with check (auth.uid() = id);

-- positions
create policy positions_select_own on public.positions
  for select using (auth.uid() = user_id);
create policy positions_insert_own on public.positions
  for insert with check (auth.uid() = user_id);
create policy positions_update_own on public.positions
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy positions_delete_own on public.positions
  for delete using (auth.uid() = user_id);

-- portfolio_holdings
create policy ph_select_own on public.portfolio_holdings
  for select using (auth.uid() = user_id);
create policy ph_insert_own on public.portfolio_holdings
  for insert with check (auth.uid() = user_id);
create policy ph_update_own on public.portfolio_holdings
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy ph_delete_own on public.portfolio_holdings
  for delete using (auth.uid() = user_id);
