import discord
import asyncio
import logging
from discord.ext import commands
from core import zyrox, Cog
from utils.config import *
from utils.turso_db import get_client

logger = logging.getLogger(__name__)


class Autorole2(Cog):
    def __init__(self, bot: zyrox):
        self.bot = bot
        self.headers = {"Authorization": f"Bot {self.bot.http.token}"}
        # NOTE: reads from the SAME Turso "autorole" table that
        # cogs/commands/autorole.py (AutoRole cog) now writes to.
        # This used to point at a local db/autorole.db file that the
        # other cog wrote to as well - now both go through the shared
        # Turso client so they stay in sync and survive restarts/redeploys.
        self.client = get_client()

    @staticmethod
    def _parse_ids(raw):
        if not raw:
            return []
        return [int(role_id) for role_id in raw.split(",") if role_id]

    async def get_autorole(self, guild_id: int):
        result = await self.client.execute(
            "SELECT humans, bots FROM autorole WHERE guild_id = ?", [guild_id]
        )
        if not result.rows:
            return {"bots": [], "humans": []}
        row = {col: result.rows[0][i] for i, col in enumerate(result.columns)}
        return {"bots": self._parse_ids(row.get("bots")), "humans": self._parse_ids(row.get("humans"))}

    @commands.Cog.listener()
    async def on_member_join(self, member):
        data = await self.get_autorole(member.guild.id)
        bot_roles = data["bots"]
        human_roles = data["humans"]
        if member.bot:
            roles_to_add = bot_roles
        else:
            roles_to_add = human_roles
        for role_id in roles_to_add:
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason=f"{BRAND_NAME} | Autoroles")
                except discord.Forbidden:
                    print(f"Bot lacks permissions to add role in a guild during Autorole Event .")
                except discord.HTTPException as e:
                    if e.status == 429:
                        retry_after = e.response.headers.get('Retry-After')
                        if retry_after:
                            retry_after = float(retry_after)
                            print(f"(Autorole) Rate limit encountered. Retrying after {retry_after} seconds.")
                            await asyncio.sleep(retry_after)
                            await member.add_roles(role, reason=f"{BRAND_NAME} | Autoroles")
                except discord.errors.RateLimited as e:
                    print(f"Rate limit encountered: {e}. Retrying in {e.retry_after} seconds.")
                    await asyncio.sleep(e.retry_after)
                    await member.add_roles(role, reason=f"{BRAND_NAME} | Autoroles")
                except Exception as e:
                    logger.error(f"Unexpected error in Autorole: {e}")
                    
