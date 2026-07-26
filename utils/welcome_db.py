from utils.turso_db import get_client


class WelcomeDatabase:
    # Single source of truth for columns/types - add new columns HERE only,
    # _migrate() adds them to any pre-existing remote table automatically.
    SCHEMA = {
        "guild_id": "INTEGER PRIMARY KEY",
        "welcome_type": "TEXT",
        "welcome_message": "TEXT",
        "channel_id": "INTEGER",
        "embed_data": "TEXT",
        "auto_delete_duration": "INTEGER",
    }

    def __init__(self):
        # get_client() only returns the shared client reference, no network
        # I/O - safe to call from a plain sync __init__.
        self.client = get_client()

    async def init(self):
        cols = ", ".join(f"{name} {ctype}" for name, ctype in self.SCHEMA.items())
        await self.client.execute(f"CREATE TABLE IF NOT EXISTS welcome ({cols})")
        await self._migrate()

    async def _migrate(self):
        result = await self.client.execute("PRAGMA table_info(welcome)")
        existing_columns = {row[1] for row in result.rows}
        missing_columns = [name for name in self.SCHEMA if name not in existing_columns]
        for name in missing_columns:
            col_type = self.SCHEMA[name].replace("PRIMARY KEY", "").strip()
            await self.client.execute(f"ALTER TABLE welcome ADD COLUMN {name} {col_type}")

    @staticmethod
    def _row_to_dict(columns, row):
        return {col: row[i] for i, col in enumerate(columns)}

    async def execute(self, q, p=()):
        return await self.client.execute(q, list(p))

    async def fetchone(self, q, p=()):
        result = await self.client.execute(q, list(p))
        if not result.rows:
            return None
        return self._row_to_dict(result.columns, result.rows[0])

    async def fetchall(self, q, p=()):
        result = await self.client.execute(q, list(p))
        return [self._row_to_dict(result.columns, row) for row in result.rows]


# Module-level, lazy - same reasoning as DropdownRoles/Ticket: get_client()
# needs a running event loop, so it can't be built at plain import time.
_db = None


async def get_welcome_db():
    """
    Returns the shared WelcomeDatabase instance, creating + initializing it
    (CREATE TABLE / migration) on first call. Both Welcomer and greet cogs
    call this from their own cog_load - whichever one loads first does the
    actual init() work, the other just gets the already-ready instance back.
    """
    global _db
    if _db is None:
        _db = WelcomeDatabase()
        await _db.init()
    return _db
    
