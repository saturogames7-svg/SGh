# utils/turso_db.py
#
# Shared Turso (libSQL) client used by every cog that was migrated off local
# SQLite files. One client is created and reused for the whole bot process,
# instead of every cog opening its own connection - this matches how the
# libsql_client library is meant to be used (it keeps a persistent HTTP
# session under the hood).
#
# Requires two environment variables to be set on Railway:
#   TURSO_DATABASE_URL   e.g. libsql://your-db-name.turso.io
#   TURSO_AUTH_TOKEN     the auth token generated in the Turso dashboard

import os
import libsql_client

_client = None


def get_client():
    """
    Returns the shared async libsql_client.Client, creating it on first call.
    Every cog should call this instead of making its own client.
    """
    global _client
    if _client is None:
        url = os.getenv("TURSO_DATABASE_URL")
        token = os.getenv("TURSO_AUTH_TOKEN")
        if not url or not token:
            raise RuntimeError(
                "TURSO_DATABASE_URL or TURSO_AUTH_TOKEN is not set. "
                "Add both in Railway -> Variables before starting the bot."
            )
        _client = libsql_client.create_client(url=url, auth_token=token)
    return _client


async def close_client():
    """Call this once on bot shutdown if you want a clean close (optional)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
