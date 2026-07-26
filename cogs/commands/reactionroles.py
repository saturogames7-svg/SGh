import discord
from utils.emoji import CROSS, TICK
from discord.ext import commands
from discord.ext.commands import Context
from discord import app_commands
from utils.turso_db import get_client


class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = get_client()

    async def cog_load(self):
        # Table creation moved here from __init__ because it now needs to be
        # awaited (the old sqlite3 version ran this synchronously in __init__,
        # which isn't possible anymore with an async client).
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS reaction_roles (
                guild_id INTEGER,
                message_id INTEGER,
                emoji TEXT,
                role_id INTEGER
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS rr_settings (
                guild_id INTEGER PRIMARY KEY,
                dm_enabled INTEGER DEFAULT 1
            )
        """)

    async def add_reaction_role(self, guild_id, message_id, emoji, role_id):
        await self.db.execute(
            "INSERT INTO reaction_roles (guild_id, message_id, emoji, role_id) VALUES (?, ?, ?, ?)",
            [guild_id, message_id, emoji, role_id]
        )

    async def get_role_by_emoji(self, guild_id, message_id, emoji):
        rs = await self.db.execute(
            "SELECT role_id FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?",
            [guild_id, message_id, emoji]
        )
        return rs.rows[0][0] if rs.rows else None

    async def get_dm_setting(self, guild_id):
        rs = await self.db.execute(
            "SELECT dm_enabled FROM rr_settings WHERE guild_id = ?",
            [guild_id]
        )
        return rs.rows[0][0] == 1 if rs.rows else True

    async def set_dm_setting(self, guild_id, value):
        await self.db.execute(
            "REPLACE INTO rr_settings (guild_id, dm_enabled) VALUES (?, ?)",
            [guild_id, value]
        )

    @commands.hybrid_command(name="createrr", help="Create a reaction role.", usage="createrr <channel> <message_id> <emoji> <role>")
    @commands.has_permissions(manage_roles=True)
    async def createrr(self, ctx: Context, channel: discord.TextChannel, message_id: int, emoji: str, role: discord.Role):
        try:
            message = await channel.fetch_message(message_id)
            await message.add_reaction(emoji)
            await self.add_reaction_role(ctx.guild.id, message.id, emoji, role.id)
            await ctx.send(f"{TICK} Reaction role added: React with {emoji} to get {role.name}", ephemeral=True if ctx.interaction else False)
        except discord.NotFound:
            await ctx.send(f"{CROSS} Message not found.", ephemeral=True if ctx.interaction else False)
        except discord.HTTPException as e:
            await ctx.send(f"{CROSS}  Error: {str(e)}", ephemeral=True if ctx.interaction else False)

    @commands.hybrid_command(name="dmrr", help="Enable or disable DM messages for reaction roles.", usage="dmrr <enable|disable>")
    @commands.has_permissions(manage_guild=True)
    async def dmrr(self, ctx: Context, mode: str):
        if mode.lower() not in ["enable", "disable"]:
            await ctx.send(f"{CROSS} Use `enable` or `disable`.", ephemeral=True if ctx.interaction else False)
            return

        value = 1 if mode.lower() == "enable" else 0
        await self.set_dm_setting(ctx.guild.id, value)
        await ctx.send(f"{TICK} DM messages for reaction roles {'enabled' if value else 'disabled'}.", ephemeral=True if ctx.interaction else False)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.guild_id is None or payload.member.bot:
            return

        role_id = await self.get_role_by_emoji(payload.guild_id, payload.message_id, str(payload.emoji))
        if role_id:
            guild = self.bot.get_guild(payload.guild_id)
            role = guild.get_role(role_id)
            member = payload.member

            if role and member:
                await member.add_roles(role, reason="Reaction role added")

                # Remove reaction
                channel = guild.get_channel(payload.channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(payload.message_id)

                    except discord.NotFound:
                        pass

                # DM if enabled
                if await self.get_dm_setting(payload.guild_id):
                    try:
                        await member.send(f"{TICK} You received the **{role.name}** role from {guild.name}.")
                    except discord.Forbidden:
                        pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if payload.guild_id is None:
            return

        role_id = await self.get_role_by_emoji(payload.guild_id, payload.message_id, str(payload.emoji))
        if role_id:
            guild = self.bot.get_guild(payload.guild_id)
            member = guild.get_member(payload.user_id)
            role = guild.get_role(role_id)
            if role and member:
                await member.remove_roles(role, reason="Reaction role removed")

# Setup
async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
    
