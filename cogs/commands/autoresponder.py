import discord
from utils.emoji import CROSS, TICK
from discord.ext import commands
from discord.ui import LayoutView, TextDisplay, Separator, Container
from utils.turso_db import execute as turso_execute
from utils.Tools import *
from utils.cv2 import CV2, build_container


class AutoResponder(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.initialize_db())

    async def initialize_db(self):
        await turso_execute('''
            CREATE TABLE IF NOT EXISTS autoresponses (
                guild_id TEXT,
                name TEXT,
                message TEXT,
                PRIMARY KEY (guild_id, name)
            )
        ''')

    @commands.group(name="autoresponder", invoke_without_command=True, aliases=['ar'], help="Manage autoresponders in the server.")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def _ar(self, ctx):
        if ctx.subcommand_passed is None:
            await ctx.send_help(ctx.command)
            ctx.command.reset_cooldown(ctx)

    @_ar.command(name="create", help="Create a new autoresponder.")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.has_permissions(administrator=True)
    async def _create(self, ctx, name, *, message):
        name_lower = name.lower()

        rs = await turso_execute(
            "SELECT COUNT(*) FROM autoresponses WHERE guild_id = ?",
            [str(ctx.guild.id)],
        )
        count = rs.rows[0][0]
        if count >= 20:
            view = CV2(f"{CROSS} Error!", f"You can't add more than 20 autoresponses in {ctx.guild.name}")
            return await ctx.reply(view=view)

        rs = await turso_execute(
            "SELECT 1 FROM autoresponses WHERE guild_id = ? AND LOWER(name) = ?",
            [str(ctx.guild.id), name_lower],
        )
        if rs.rows:
            view = CV2(f"{CROSS} Error!", f"The autoresponse with the name `{name}` already exists in {ctx.guild.name}")
            return await ctx.reply(view=view)

        await turso_execute(
            "INSERT INTO autoresponses (guild_id, name, message) VALUES (?, ?, ?)",
            [str(ctx.guild.id), name_lower, message],
        )
        view = CV2(f"{TICK} Success", f"Created autoresponder `{name}` in {ctx.guild.name}")
        await ctx.reply(view=view)

    @_ar.command(name="delete", help="Delete an existing autoresponder.")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.has_permissions(administrator=True)
    async def _delete(self, ctx, name):
        name_lower = name.lower()

        rs = await turso_execute(
            "SELECT 1 FROM autoresponses WHERE guild_id = ? AND LOWER(name) = ?",
            [str(ctx.guild.id), name_lower],
        )
        if not rs.rows:
            view = CV2(f"{CROSS} Error!", f"No autoresponder found with the name `{name}` in {ctx.guild.name}")
            return await ctx.reply(view=view)

        await turso_execute(
            "DELETE FROM autoresponses WHERE guild_id = ? AND LOWER(name) = ?",
            [str(ctx.guild.id), name_lower],
        )
        view = CV2(f"{TICK} Success", f"Deleted autoresponder `{name}` in {ctx.guild.name}")
        await ctx.reply(view=view)

    @_ar.command(name="edit", help="Edit an existing autoresponder.")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.has_permissions(administrator=True)
    async def _edit(self, ctx, name, *, message):
        name_lower = name.lower()

        rs = await turso_execute(
            "SELECT 1 FROM autoresponses WHERE guild_id = ? AND LOWER(name) = ?",
            [str(ctx.guild.id), name_lower],
        )
        if not rs.rows:
            view = CV2(f"{CROSS} Error!", f"No autoresponder found with the name `{name}` in {ctx.guild.name}")
            return await ctx.reply(view=view)

        await turso_execute(
            "UPDATE autoresponses SET message = ? WHERE guild_id = ? AND LOWER(name) = ?",
            [message, str(ctx.guild.id), name_lower],
        )
        view = CV2(f"{TICK} Success", f"Edited autoresponder `{name}` in {ctx.guild.name}")
        await ctx.reply(view=view)

    @_ar.command(name="config", help="List all autoresponders in the server.")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.has_permissions(administrator=True)
    async def _config(self, ctx):
        rs = await turso_execute(
            "SELECT name FROM autoresponses WHERE guild_id = ?",
            [str(ctx.guild.id)],
        )
        autoresponses = rs.rows

        if not autoresponses:
            view = CV2("No Autoresponders", f"There are no autoresponders in {ctx.guild.name}")
            return await ctx.reply(view=view)

        ar_list = "\n".join([f"**[{i}]** {row[0]}" for i, row in enumerate(autoresponses, start=1)])
        view = CV2(f"Autoresponders in {ctx.guild.name}", ar_list)
        await ctx.send(view=view)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        rs = await turso_execute(
            "SELECT message FROM autoresponses WHERE guild_id = ? AND LOWER(name) = ?",
            [str(message.guild.id), message.content.lower()],
        )
        row = rs.rows[0] if rs.rows else None

        if row:
            await message.channel.send(row[0])

async def setup(bot):
    await bot.add_cog(AutoResponder(bot))
    
