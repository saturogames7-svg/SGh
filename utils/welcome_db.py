import json
from utils.turso_db import get_client

# --- Schema definition: single source of truth for expected columns/types. ---
# Whenever a new column needs to be added in a future update, add it HERE
# (and nowhere else) - the migration in initialize() will take care of
# adding it to the Turso database if it was created before that update.
SCHEMA = {
    "guild_id": "INTEGER PRIMARY KEY",
    "welcome_type": "TEXT",
    "welcome_message": "TEXT",
    "channel_id": "INTEGER",
    "embed_data": "TEXT",
    "auto_delete_duration": "INTEGER",
}


class WelcomeDB:
    def __init__(self):
        # get_client() only reuses/creates the shared Turso client - no
        # network I/O happens here, so this is safe to call at import time.
        self.client = get_client()

    async def initialize(self):
        """
        Creates the table if missing and migrates in any columns that were
        added to SCHEMA after older rows/tables were created. Safe to call
        from more than one cog's cog_load() - CREATE TABLE IF NOT EXISTS and
        the migration below are both idempotent.
        """
        cols_sql = ", ".join(f"{name} {ctype}" for name, ctype in SCHEMA.items())
        await self.client.execute(f"CREATE TABLE IF NOT EXISTS welcome ({cols_sql})")
        await self._migrate()

    async def _migrate(self):
        rs = await self.client.execute("PRAGMA table_info(welcome)")
        existing_columns = {row[1] for row in rs.rows}
        missing_columns = [name for name in SCHEMA if name not in existing_columns]
        for name in missing_columns:
            col_type = SCHEMA[name].replace("PRIMARY KEY", "").strip()
            await self.client.execute(f"ALTER TABLE welcome ADD COLUMN {name} {col_type}")

    async def exists(self, guild_id):
        rs = await self.client.execute("SELECT 1 FROM welcome WHERE guild_id = ?", [guild_id])
        return bool(rs.rows)

    async def get_row(self, guild_id):
        """Returns the full row (guild_id, welcome_type, welcome_message, channel_id, embed_data, auto_delete_duration) or None."""
        rs = await self.client.execute("SELECT * FROM welcome WHERE guild_id = ?", [guild_id])
        return tuple(rs.rows[0]) if rs.rows else None

    async def get_columns(self, columns, guild_id):
        """columns: list of column names, e.g. ['welcome_type', 'channel_id']. Returns a tuple in that order, or None."""
        col_str = ", ".join(columns)
        rs = await self.client.execute(f"SELECT {col_str} FROM welcome WHERE guild_id = ?", [guild_id])
        return tuple(rs.rows[0]) if rs.rows else None

    async def save_welcome_data(self, guild_id, welcome_type, message, embed_data=None):
        # UPDATE-if-exists / INSERT-if-not, instead of INSERT OR REPLACE, so
        # columns not passed here (channel_id, auto_delete_duration) aren't
        # wiped out if a row already exists.
        already_exists = await self.exists(guild_id)
        embed_json = json.dumps(embed_data) if embed_data else None
        if already_exists:
            await self.client.execute(
                "UPDATE welcome SET welcome_type = ?, welcome_message = ?, embed_data = ? WHERE guild_id = ?",
                [welcome_type, message, embed_json, guild_id]
            )
        else:
            await self.client.execute(
                "INSERT INTO welcome (guild_id, welcome_type, welcome_message, embed_data) VALUES (?, ?, ?, ?)",
                [guild_id, welcome_type, message, embed_json]
            )

    async def delete_guild(self, guild_id):
        await self.client.execute("DELETE FROM welcome WHERE guild_id = ?", [guild_id])

    async def update_channel(self, guild_id, channel_id):
        await self.client.execute("UPDATE welcome SET channel_id = ? WHERE guild_id = ?", [channel_id, guild_id])

    async def update_auto_delete(self, guild_id, duration):
        await self.client.execute("UPDATE welcome SET auto_delete_duration = ? WHERE guild_id = ?", [duration, guild_id])

    async def update_welcome_message(self, guild_id, message):
        await self.client.execute("UPDATE welcome SET welcome_message = ? WHERE guild_id = ?", [message, guild_id])

    async def update_embed_data(self, guild_id, embed_data_dict):
        await self.client.execute(
            "UPDATE welcome SET embed_data = ? WHERE guild_id = ?",
            [json.dumps(embed_data_dict), guild_id]
        )


# Shared singleton - both greet2.py and welcome.py import this same instance.
welcome_db = WelcomeDB()
