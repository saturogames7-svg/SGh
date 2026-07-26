from __future__ import annotations
import traceback
import discord
from utils.emoji import CROSS, ICONS_WARNING, TICK
import logging
from discord.ext import commands
from typing import List, Dict
from discord.ui import LayoutView, TextDisplay, Separator, Container
from utils.Tools import *
from utils.cv2 import CV2, build_container
from utils.config import OWNER_IDS
from utils.turso_db import get_client


logging.basicConfig(
    level=logging.INFO,
    format="\x1b[38;5;197m[\x1b[0m%(asctime)s\x1b[38;5;197m]\x1b[0m -> \x1b[38;5;197m%(message)s\x1b[0m",
    datefmt="%H:%M:%S",
)


class BasicView(discord.ui.View):
    def __init__(self, ctx: commands.Context, timeout=60):
        super().__init__(timeout=timeout)
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id and interaction.user.id not in OWNER_IDS:
            await interaction.response.send_message("Uh oh! That message doesn't belong to you.\nYou must run this command to interact with it.", ephemeral=True)
            return False
        return True


# --- Database Class (Turso) ---
# NOTE: this used to be a local aiosqlite file (db/autorole.db). On Railway
# (and most container hosts) the filesystem is ephemeral - it gets wiped on
# every redeploy/restart, silently resetting all autorole configs. Moving
# this to the shared Turso client (same one ticket.py uses) makes it
# actually persistent across restarts and deploys.
class AutoRoleDatabase:
    SCHEMA = {
        "guild_id": "INTEGER PRIMARY KEY",
        "humans": "TEXT NOT NULL DEFAULT ''",
        "bots": "TEXT NOT NULL DEFAULT ''",
    }

    def __init__(self):
        # get_client() only returns the shared client reference - safe to
        # call from a plain sync __init__ (same reasoning as other cogs).
        self.client = get_client()

    async def init(self):
        cols = ", ".join(f"{n} {t}" for n, t in self.SCHEMA.items())
        await self.client.execute(f"CREATE TABLE IF NOT EXISTS autorole ({cols})")
        await self._migrate()

    async def _migrate(self):
        result = await self.client.execute("PRAGMA table_info(autorole)")
        existing_columns = {row[1] for row in result.rows}
        missing_columns = [name for name in self.SCHEMA if name not in existing_columns]
        for name in missing_columns:
            col_type = self.SCHEMA[name].replace("PRIMARY KEY", "").replace("NOT NULL", "").strip()
            await self.client.execute(f"ALTER TABLE autorole ADD COLUMN {name} {col_type}")

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


def _parse_ids(raw: str) -> List[int]:
    if not raw:
        return []
    return [int(x) for x in raw.split(",") if x]


def _join_ids(ids: List[int]) -> str:
    return ",".join(map(str, ids))


# module-level, lazy - same reasoning as the other cogs sharing the Turso
# client: get_client() needs a running event loop, so it can't be
# constructed at plain import time.
db = None


class AutoRole(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.color = 0xFF0000

    async def cog_load(self):
        global db
        try:
            if db is None:
                db = AutoRoleDatabase()
            await db.init()
        except Exception:
            print("=" * 60)
            print("[AutoRole] FAILED inside cog_load() (Turso table setup):")
            traceback.print_exc()
            print("=" * 60)
            raise
        self.db = db

    async def get_autorole(self, guild_id: int) -> Dict[str, List[int]]:
        row = await self.db.fetchone("SELECT humans, bots FROM autorole WHERE guild_id = ?", (guild_id,))
        if not row:
            return {"bots": [], "humans": []}
        return {"bots": _parse_ids(row["bots"]), "humans": _parse_ids(row["humans"])}

    async def update_autorole(self, guild_id: int, data: Dict[str, List[int]]):
        await self.db.execute(
            "INSERT INTO autorole (guild_id, humans, bots) VALUES (?,?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET humans=excluded.humans, bots=excluded.bots",
            (guild_id, _join_ids(data["humans"]), _join_ids(data["bots"]))
        )

    @commands.group(name="autorole", invoke_without_command=True)
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def _autorole(self, ctx):
        if ctx.subcommand_passed is None:
            await ctx.send_help(ctx.command)
            ctx.command.reset_cooldown(ctx)

    @_autorole.command(name="config", help="Shows the current autorole configuration")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(administrator=True)
    async def _ar_config(self, ctx):

        data = await self.get_autorole(ctx.guild.id)
        if data["humans"] or data["bots"]:
            fetched_humans = [ctx.guild.get_role(role_id) for role_id in data["humans"] if ctx.guild.get_role(role_id)]
            fetched_bots = [ctx.guild.get_role(role_id) for role_id in data["bots"] if ctx.guild.get_role(role_id)]

            hums = "\n".join(role.mention for role in fetched_humans) or "None"
            bos = "\n".join(role.mention for role in fetched_bots) or "None"

            view = CV2(
                f"Autorole Configuration for {ctx.guild.name}",
                f"__Humans__\n{hums}",
                f"__Bots__\n{bos}"
            )
            await ctx.send(view=view)
        else:
            view = CV2("Autorole Configuration", "No autorole configuration found in this Guild.")
            await ctx.reply(view=view)

    @_autorole.group(name="reset", help="Clear autorole config in the Guild")
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(administrator=True)
    async def _autorole_reset(self, ctx):
        if ctx.subcommand_passed is None:
            await ctx.send_help(ctx.command)
            ctx.command.reset_cooldown(ctx)

    @_autorole_reset.command(name="humans", help="Clear autorole configuration for humans")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(administrator=True)
    async def _autorole_humans_reset(self, ctx):
        data = await self.get_autorole(ctx.guild.id)

        if data["humans"]:
            data["humans"] = []
            await self.update_autorole(ctx.guild.id, data)
            view = CV2(f"{TICK} Success", "Cleared all human autoroles in this Guild.")
        else:
            view = CV2(f"{CROSS} Error", "No Autoroles set for humans in this Guild.")

        await ctx.reply(view=view)

    @_autorole_reset.command(name="bots", help="Clear autorole configuration for bots")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(administrator=True)
    async def _autorole_bots_reset(self, ctx):
        data = await self.get_autorole(ctx.guild.id)

        if data["bots"]:
            data["bots"] = []
            await self.update_autorole(ctx.guild.id, data)
            view = CV2(f"{TICK} Success", "Cleared all bot autoroles in this Guild.")
        else:
            view = CV2(f"{CROSS} Error", "No Autoroles set for Bots in this Guild.")

        await ctx.reply(view=view)

    @_autorole_reset.command(name="all", help="Clear all autorole configuration in the Guild")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def _autorole_reset_all(self, ctx):
        data = await self.get_autorole(ctx.guild.id)

        if data["humans"] or data["bots"]:
            await self.update_autorole(ctx.guild.id, {"humans": [], "bots": []})
            view = CV2(f"{TICK} Success", "Cleared all autoroles in this Guild.")
        else:
            view = CV2(f"{CROSS} Error", "No Autoroles set in this Guild.")

        await ctx.reply(view=view)

    @_autorole.group(name="humans", help="Setup autoroles for human")
    @blacklist_check()
    @ignore_check()
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def _autorole_humans(self, ctx):
        if ctx.subcommand_passed is None:
            await ctx.send_help(ctx.command)
            ctx.command.reset_cooldown(ctx)

    @_autorole_humans.command(name="add", help="Add role to list of human Autoroles.")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def _autorole_humans_add(self, ctx, *, role: discord.Role):
        data = await self.get_autorole(ctx.guild.id)
        humans = data["humans"]

        if role.id in humans:
            view = CV2(f"{ICONS_WARNING} Access Denied", f"{role.mention} is already in human autoroles.")
        elif len(humans) >= 10:
            view = CV2(f"{ICONS_WARNING} Access Denied", "You can only add upto 10 human autoroles.")
        else:
            humans.append(role.id)
            data["humans"] = humans
            await self.update_autorole(ctx.guild.id, data)
            view = CV2(f"{TICK} Success", f"{role.mention} has been added to human autoroles.")

        await ctx.reply(view=view)

    @_autorole_humans.command(name="remove", help="Remove a role from human Autoroles.")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def _autorole_humans_remove(self, ctx, *, role: discord.Role):
        data = await self.get_autorole(ctx.guild.id)
        humans = data["humans"]

        if role.id not in humans:
            view = CV2(f"{CROSS} Error", f"{role.mention} is not in human autoroles.")
        else:
            humans.remove(role.id)
            data["humans"] = humans
            await self.update_autorole(ctx.guild.id, data)
            view = CV2(f"{TICK} Success", f"{role.mention} has been removed from human autoroles.")

        await ctx.reply(view=view)

    @_autorole.group(name="bots", help="Setup autoroles for bots")
    @blacklist_check()
    @ignore_check()
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def _autorole_bots(self, ctx):
        if ctx.subcommand_passed is None:
            await ctx.send_help(ctx.command)
            ctx.command.reset_cooldown(ctx)

    @_autorole_bots.command(name="add", help="Add role to bot Autoroles.")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def _autorole_bots_add(self, ctx, *, role: discord.Role):
        data = await self.get_autorole(ctx.guild.id)
        bots = data["bots"]

        if role.id in bots:
            view = CV2(f"{ICONS_WARNING} Access Denied", f"{role.mention} is already in bot autoroles.")
        elif len(bots) >= 10:
            view = CV2(f"{ICONS_WARNING} Access Denied", "You can only add upto 10 bot autoroles")
        else:
            bots.append(role.id)
            data["bots"] = bots
            await self.update_autorole(ctx.guild.id, data)
            view = CV2(f"{TICK} Success", f"{role.mention} has been added to bot autoroles.")

        await ctx.reply(view=view)

    @_autorole_bots.command(name="remove", help="Remove a role from bot Autoroles.")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def _autorole_bots_remove(self, ctx, *, role: discord.Role):
        data = await self.get_autorole(ctx.guild.id)
        bots = data["bots"]

        if role.id not in bots:
            view = CV2(f"{CROSS} Error", f"{role.mention} is not in bot autoroles.")
        else:
            bots.remove(role.id)
            data["bots"] = bots
            await self.update_autorole(ctx.guild.id, data)
            view = CV2(f"{TICK} Success", f"{role.mention} has been removed from bot autoroles.")

        await ctx.reply(view=view)
        
