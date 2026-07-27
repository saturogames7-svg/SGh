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
import logging
import libsql_client

logger = logging.getLogger(__name__)

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
        # Force HTTP instead of the websocket (wss://) protocol that
        # libsql:// triggers - the websocket handshake fails on this host
        # (WSServerHandshakeError: 400). Doing the swap here means the
        # Railway variable itself can stay as libsql://... untouched.
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        _client = libsql_client.create_client(url=url, auth_token=token)
    return _client


async def close_client():
    """Call this once on bot shutdown if you want a clean close (optional)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def reset_client():
    """
    Force-drop the cached client so the next get_client() call opens a brand
    new connection. Call this after a transport-level failure (e.g. 'Server
    disconnected') - the shared client's underlying HTTP session can be
    closed by Turso after sitting idle, and libsql_client does not
    automatically reconnect on its own.
    """
    global _client
    if _client is not None:
        try:
            await _client.close()
        except Exception:
            pass
        _client = None


async def execute(sql: str, args=None, retries: int = 1):
    """
    Run a single statement against Turso, automatically reconnecting and
    retrying once if the shared connection was dropped by the server.

    Use this instead of calling get_client().execute(...) directly so every
    cog benefits from the same reconnect logic in one place.
    """
    args = args or []
    last_error = None
    for attempt in range(retries + 1):
        client = get_client()
        try:
            return await client.execute(sql, args)
        except Exception as e:
            last_error = e
            is_last_attempt = attempt >= retries
            if is_last_attempt:
                logger.error(f"Turso execute failed after retries: {e}")
                raise
            logger.warning(f"Turso execute failed ({e}), reconnecting and retrying...")
            await reset_client()
    raise last_error
    
