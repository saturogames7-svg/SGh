import discord
import sqlite3
from discord.ext import commands
from discord.ext.commands import Context
from core.Cog import Cog
from utils.emoji import TICK, CROSS


DB_PATH = "dropdownroles.db"


class DropdownRolesDB:
    def __init__(self):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dropdown_menus (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    custom_id TEXT,
                    placeholder TEXT,
                    max_values INTEGER DEFAULT 1
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dropdown_options (
                    message_id INTEGER,
                    role_id INTEGER,
                    label TEXT,
                    description TEXT,
                    emoji TEXT
                )
            """)

    def create_menu(self, message_id, guild_id, channel_id, custom_id, placeholder, max_values=1):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dropdown_menus (message_id, guild_id, channel_id, custom_id, placeholder, max_values) VALUES (?, ?, ?, ?, ?, ?)",
                (message_id, guild_id, channel_id, custom_id, placeholder, max_values)
            )

    def add_option(self, message_id, role_id, label, description, emoji):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO dropdown_options (message_id, role_id, label, description, emoji) VALUES (?, ?, ?, ?, ?)",
                (message_id, role_id, label, description, emoji)
            )

    def remove_option(self, message_id, role_id):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "DELETE FROM dropdown_options WHERE message_id = ? AND role_id = ?",
                (message_id, role_id)
            )

    def get_options(self, message_id):
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "SELECT role_id, label, description, emoji FROM dropdown_options WHERE message_id = ?",
                (message_id,)
            )
            return cur.fetchall()

    def get_menu(self, message_id):
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "SELECT message_id, guild_id, channel_id, custom_id, placeholder, max_values FROM dropdown_menus WHERE message_id = ?",
                (message_id,)
            )
            return cur.fetchone()

    def get_all_menus(self):
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute("SELECT message_id, guild_id, channel_id, custom_id, placeholder, max_values FROM dropdown_menus")
            return cur.fetchall()


db = DropdownRolesDB()


def build_select(message_id: int, custom_id: str, placeholder: str, max_values: int):
    options_data = db.get_options(message_id)

    select_options = []
    for role_id, label, description, emoji in options_data:
        select_options.append(
            discord.SelectOption(
                label=label,
                value=str(role_id),
                description=description if description else None,
                emoji=emoji if emoji else None,
            )
        )

    if not select_options:
        select_options.append(discord.SelectOption(label="No roles configured yet", value="none"))

    select = discord.ui.Select(
        custom_id=custom_id,
        placeholder=placeholder or "Choose your roles",
        min_values=0,
        max_values=min(max_values, len(select_options)) if select_options else 1,
        options=select_options,
    )
    return select


class RoleDropdownView(discord.ui.View):
    def __init__(self, message_id: int, custom_id: str, placeholder: str, max_values: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        select = build_select(message_id, custom_id, placeholder, max_values)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        select = interaction.data.get("values", [])
        if not select or select == ["none"]:
            await interaction.response.send_message(f"{CROSS} No roles are configured for this menu yet.", ephemeral=True)
            return

        guild = interaction.guild
        member = interaction.user
        chosen_role_ids = {int(v) for v in select}

        all_options = db.get_options(self.message_id)
        all_role_ids = {int(role_id) for role_id, *_ in all_options}

        added, removed, failed = [], [], []

        for role_id in all_role_ids:
            role = guild.get_role(role_id)
            if not role:
                continue
            has_role = role in member.roles
            wants_role = role_id in chosen_role_ids

            try:
                if wants_role and not has_role:
                    await member.add_roles(role, reason="Dropdown role menu")
                    added.append(role.mention)
                elif not wants_role and has_role:
                    await member.remove_roles(role, reason="Dropdown role menu")
                    removed.append(role.mention)
            except discord.Forbidden:
                failed.append(role.mention)

        parts = []
        if added:
            parts.append(f"**Added:** {', '.join(added)}")
        if removed:
            parts.append(f"**Removed:** {', '.join(removed)}")
        if failed:
            parts.append(f"**Failed (missing permissions):** {', '.join(failed)}")
        if not parts:
            parts.append("No changes made.")

        await interaction.response.send_message(
            f"{TICK} " + "\n".join(parts),
            ephemeral=True
        )


class DropdownRoles(Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Re-register persistent views for existing menus so they survive restarts
        for message_id, guild_id, channel_id, custom_id, placeholder, max_values in db.get_all_menus():
            view = RoleDropdownView(message_id, custom_id, placeholder, max_values)
            self.bot.add_view(view, message_id=message_id)

    @commands.hybrid_command(name="createdropdown", help="Create a dropdown role menu.", usage="createdropdown <channel> <title> <description>")
    @commands.has_permissions(manage_roles=True)
    async def createdropdown(self, ctx: Context, channel: discord.TextChannel, title: str, *, description: str):
        embed = discord.Embed(title=title, description=description, color=0xFF0000)

        custom_id = f"dropdown_role_menu:{channel.id}:{ctx.message.id if not ctx.interaction else ctx.interaction.id}"

        placeholder_view = discord.ui.View(timeout=None)
        placeholder_select = discord.ui.Select(
            custom_id=custom_id,
            placeholder="Choose your roles",
            min_values=0,
            max_values=1,
            options=[discord.SelectOption(label="No roles configured yet", value="none")],
        )
        placeholder_view.add_item(placeholder_select)

        message = await channel.send(embed=embed, view=placeholder_view)

        db.create_menu(message.id, ctx.guild.id, channel.id, custom_id, "Choose your roles", max_values=1)

        real_view = RoleDropdownView(message.id, custom_id, "Choose your roles", max_values=1)
        self.bot.add_view(real_view, message_id=message.id)

        await ctx.send(
            f"{TICK} Dropdown menu created in {channel.mention}.\nMessage ID: `{message.id}`\nUse `addoption {message.id} <role> <label>` to add roles.",
            ephemeral=True if ctx.interaction else False
        )

    @commands.hybrid_command(name="addoption", help="Add a role option to a dropdown menu.", usage="addoption <message_id> <role> <label> [emoji]")
    @commands.has_permissions(manage_roles=True)
    async def addoption(self, ctx: Context, message_id: int, role: discord.Role, label: str, emoji: str = None):
        menu = db.get_menu(message_id)
        if not menu:
            await ctx.send(f"{CROSS} No dropdown menu found with that message ID.", ephemeral=True if ctx.interaction else False)
            return

        _, guild_id, channel_id, custom_id, placeholder, max_values = menu

        existing = db.get_options(message_id)
        if len(existing) >= 25:
            await ctx.send(f"{CROSS} A dropdown menu can only have up to 25 options.", ephemeral=True if ctx.interaction else False)
            return

        db.add_option(message_id, role.id, label, None, emoji)

        new_max_values = min(len(existing) + 1, 25)
        db.create_menu(message_id, guild_id, channel_id, custom_id, placeholder, new_max_values)

        try:
            channel = self.bot.get_channel(channel_id)
            message = await channel.fetch_message(message_id)
            new_view = RoleDropdownView(message_id, custom_id, placeholder, new_max_values)
            await message.edit(view=new_view)
            self.bot.add_view(new_view, message_id=message_id)
        except discord.NotFound:
            await ctx.send(f"{CROSS} The original message could not be found (it may have been deleted).", ephemeral=True if ctx.interaction else False)
            return

        await ctx.send(f"{TICK} Added **{role.name}** to the dropdown menu.", ephemeral=True if ctx.interaction else False)

    @commands.hybrid_command(name="removeoption", help="Remove a role option from a dropdown menu.", usage="removeoption <message_id> <role>")
    @commands.has_permissions(manage_roles=True)
    async def removeoption(self, ctx: Context, message_id: int, role: discord.Role):
        menu = db.get_menu(message_id)
        if not menu:
            await ctx.send(f"{CROSS} No dropdown menu found with that message ID.", ephemeral=True if ctx.interaction else False)
            return

        _, guild_id, channel_id, custom_id, placeholder, max_values = menu

        db.remove_option(message_id, role.id)
        remaining = db.get_options(message_id)
        new_max_values = max(1, min(len(remaining), 25))
        db.create_menu(message_id, guild_id, channel_id, custom_id, placeholder, new_max_values)

        try:
            channel = self.bot.get_channel(channel_id)
            message = await channel.fetch_message(message_id)
            new_view = RoleDropdownView(message_id, custom_id, placeholder, new_max_values)
            await message.edit(view=new_view)
            self.bot.add_view(new_view, message_id=message_id)
        except discord.NotFound:
            pass

        await ctx.send(f"{TICK} Removed **{role.name}** from the dropdown menu.", ephemeral=True if ctx.interaction else False)


async def setup(bot):
    await bot.add_cog(DropdownRoles(bot))
