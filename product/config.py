"""Central config for external-service credentials, read from the environment.

Phase 1 (Supabase setup): this module *exposes* the Supabase settings so later
phases can import them from one place. It is intentionally NOT wired into any
request path yet — no endpoint reads these, and nothing here touches the existing
env vars (NEWS_API_KEY, ALLOWED_ORIGINS, PORT, EDGAR_USER_AGENT), which continue
to be read where they already are.

Supabase env vars (added in Phase 1):
  SUPABASE_URL          project URL,       e.g. https://<ref>.supabase.co   (public)
  SUPABASE_ANON_KEY     anon/public key   — safe for the frontend            (public)
  SUPABASE_JWT_SECRET   JWT signing secret — BACKEND ONLY, never shipped to the client
                        (used in Phase 2 to verify the Authorization: Bearer token)
"""
from __future__ import annotations

import os

SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY: str = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_JWT_SECRET: str = os.environ.get("SUPABASE_JWT_SECRET", "")


def supabase_configured() -> bool:
    """True when all three Supabase settings are present (Phase 2 will gate on this)."""
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_JWT_SECRET)
