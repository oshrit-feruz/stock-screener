# Supabase migrations

Schema for the Postgres tables this system stores state in. Files are numbered
and applied in order:

```sh
psql "$SUPABASE_DB_URL" -f migrations/001_bot_active_overrides.sql
```

or by pasting the file into the Supabase SQL editor. Each one is written to be
safe to re-run (`create table if not exists`, and `enable row level security`
is idempotent), so re-applying the whole directory is not destructive.

`public.bot_positions` — the position book, the other table this system uses —
predates this directory and was created by hand; it is documented in
[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) rather than defined here. It is
not reproduced as a migration because writing one now would invite someone to
"apply all the migrations" against a project that already holds live positions.

## The rule every table here follows

Access is **service-role only**: RLS is enabled with **no policies**, so the
anon key — which is public and ships in the browser — can neither read nor
write, while the service role bypasses RLS. Enabling RLS without adding a
policy is the intent, not an unfinished step.

`SUPABASE_SERVICE_KEY` is server-side only. Never in `product/web/`, never in a
URL.

## Without these tables

Two different things can be missing, and they fail differently.

**Missing credentials.** When `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are not
both set, every store uses JSON files under `data/` instead. That is what lets
the test suite and local development run with no credentials and no network.
On Render it is ephemeral — the container filesystem is discarded on restart —
so a deploy without credentials keeps working but silently forgets its state.
That is the failure `product/storage/positions.py` was written to prevent; see
its module docstring.

**Missing table.** When the credentials are set but a migration here has not
been applied, Supabase is still the backend — there is no fallback to files —
and its operations fail. Writes surface as a 503 from the API. Reads are
handled per store: the override store logs the failure and serves no overrides
for one cache window, so `/api/screener` keeps answering with policy values.
The fix is to apply the migration, not to unset the credentials.
