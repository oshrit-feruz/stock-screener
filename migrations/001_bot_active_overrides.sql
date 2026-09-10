-- Manual `active` overrides for screener signals.
--
-- Backs product/storage/active_overrides.py. `active` is normally DERIVED --
-- satellite_policy.is_active() computes it from the signal and the market
-- regime -- and this table does not change that. It stores only the human
-- decisions that sit BESIDE the computed value; /api/screener keeps publishing
-- what the policy said (`active_policy`) alongside where the served value came
-- from (`active_source`).
--
-- Apply with: psql "$SUPABASE_DB_URL" -f migrations/001_bot_active_overrides.sql
-- or paste into the Supabase SQL editor. Safe to re-run.

create table if not exists public.bot_active_overrides (
    -- One row per ticker: an override is a current decision, not a history, so
    -- re-flagging a name replaces it. The primary key is also what lets
    -- PostgREST's `resolution=merge-duplicates` upsert resolve.
    ticker text primary key,

    -- NOT NULL on purpose: a null here would be a third state that the reader
    -- cannot tell from "no override stored", and the absence of an override is
    -- already expressed by the absence of a row.
    active boolean not null,

    -- The app always sends this; the default is only a backstop for a row
    -- inserted by hand. It is published with the override so a stale one is
    -- visible as stale rather than passing for a fresh decision.
    set_at timestamptz not null default now(),

    -- The same symbol shape product/storage/positions.py enforces in Python
    -- (_TICKER_RE). Duplicated here deliberately: the application check stops a
    -- bad value from steering a PostgREST filter, and this one stops anything
    -- that reaches the table by another route from being stored at all.
    constraint bot_active_overrides_ticker_is_a_symbol
        check (ticker ~ '^[A-Z0-9][A-Z0-9.-]{0,14}$')
);

comment on table public.bot_active_overrides is
    'Manual overrides of a screener signal''s `active` flag. The policy-derived '
    'value is never stored here; see product/storage/active_overrides.py.';

-- Service-role only, exactly as bot_positions is: RLS on with NO policies, so
-- the anon key -- which is public and ships in the browser -- can neither read
-- nor write this table, while the service role bypasses RLS. Enabling RLS
-- without adding a policy is the point, not an oversight.
alter table public.bot_active_overrides enable row level security;
