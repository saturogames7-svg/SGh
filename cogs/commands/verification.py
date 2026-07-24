import discord
from utils.emoji import TICK
from discord.ext import commands
from discord import app_commands
import aiosqlite
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from utils.Tools import *

logger = logging.getLogger('discord')
DATABASE_PATH = 'db/verification.db'

DEFAULT_COLOR = 0xFF0000


def utc_to_ist(dt: datetime) -> datetime:
    ist_offset = timedelta(hours=5, minutes=30)
    return dt.replace(tzinfo=timezone.utc).astimezone(timezone(ist_offset))


STYLE_MAP = {
    "green": discord.ButtonStyle.green,
    "blurple": discord.ButtonStyle.blurple,
    "blue": discord.ButtonStyle.blurple,
    "grey": discord.ButtonStyle.grey,
    "gray": discord.ButtonStyle.grey,
    "red": discord.ButtonStyle.red,
}


def parse_color(raw: Optional[str]) -> int:
    if not raw:
        return DEFAULT_COLOR
    raw = raw.strip().lstrip('#')
    try:
        return int(raw, 16)
    except ValueError:
        return DEFAULT_COLOR


def parse_style(raw: Optional[str]) -> discord.ButtonStyle:
    if not raw:
        return discord.ButtonStyle.green
    return STYLE_MAP.get(str(raw).strip().lower(), discord.ButtonStyle.green)


# ---------------- Database helpers ----------------

async def db_execute(query, params=(), fetch=None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.cursor() as cur:
            await cur.execute(query, params)
            if fetch == "one":
                result = await cur.fetchone()
            elif fetch == "all":
                result = await cur.fetchall()
            else:
                result = cur.lastrowid
            await db.commit()
            return result


async def get_panel_buttons(panel_id: int):
    return await db_execute(
        "SELECT id, label, style, role_id, emoji FROM verification_buttons WHERE panel_id = ? ORDER BY id",
        (panel_id,), fetch="all"
    )


async def log_verification(guild_id: int, user_id: int, role_id: int):
    await db_execute(
        "INSERT INTO verification_logs (guild_id, user_id, role_id, verified_at) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, role_id, utc_to_ist(discord.utils.utcnow()).isoformat())
    )


# ---------------- Dynamic verification buttons/view ----------------

def make_verify_callback(role_id: int):
    async def callback(interaction: discord.Interaction):
        try:
            guild = interaction.guild

            if guild is None:
                await interaction.response.send_message(
                    "This button can only be used inside a server.",
                    ephemeral=True
                )
                return

            role = guild.get_role(role_id)

            if role is None:
                await interaction.response.send_message(
                    "Role not found anymore, contact an admin.",
                    ephemeral=True
                )
                return

            member = interaction.user

            # Already has role
            if role in member.roles:
                await interaction.response.send_message(
                    f"You already have the **{role.name}** role.",
                    ephemeral=True
                )
                return

            # Bot permission check
            bot_member = guild.me

            if bot_member is None:
                bot_member = guild.get_member(interaction.client.user.id)

            if (
                bot_member is None
                or not bot_member.guild_permissions.manage_roles
            ):
                await interaction.response.send_message(
                    "I don't have the Manage Roles permission.",
                    ephemeral=True
                )
                return

            # Role hierarchy check
            if bot_member.top_role.position <= role.position:
                await interaction.response.send_message(
                    "I cannot give this role because it is higher than my role.",
                    ephemeral=True
                )
                return

            # Give role
            await member.add_roles(
                role,
                reason=f"Verification button used by {member}"
            )

            # Save log
            await log_verification(
                guild.id,
                member.id,
                role.id
            )

            # Success message
            await interaction.response.send_message(
                f"Given the **{role.name}** role.",
                ephemeral=True
            )

        except discord.Forbidden:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "I don't have permission to give this role.",
                    ephemeral=True
                )

        except discord.HTTPException as e:
            logger.error(
                f"Discord API error in verification button: {e}"
            )

            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Discord error happened while giving the role.",
                    ephemeral=True
                )

        except Exception as e:
            logger.exception(
                "Error in verification callback"
            )

            error_message = (
                f"Verification failed:\n"
                f"```{type(e).__name__}: {e}```"
            )

            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        error_message,
                        ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        error_message,
                        ephemeral=True
                    )

            except Exception:
                pass

    return callback

def build_panel_view(buttons_rows) -> discord.ui.View:
    """buttons_rows: iterable of (id, label, style, role_id, emoji)"""
    view = discord.ui.View(timeout=None)
    for (btn_id, label, style, role_id, emoji) in buttons_rows:
        btn = discord.ui.Button(
            label=label,
            style=parse_style(style),
            custom_id=f"vf:{btn_id}",
            emoji=emoji if emoji else None
        )
        btn.callback = make_verify_callback(role_id)
        view.add_item(btn)
    return view


# ---------------- Setup wizard modals/views ----------------

class PanelInfoModal(discord.ui.Modal, title="Panel Message Settings"):
    panel_title = discord.ui.TextInput(label="Embed Title", max_length=256, required=True)
    panel_desc = discord.ui.TextInput(
        label="Embed Description", style=discord.TextStyle.paragraph, max_length=2000, required=True
    )
    panel_color = discord.ui.TextInput(label="Embed Color (hex, e.g. FF0000)", max_length=7, required=False)
    panel_image = discord.ui.TextInput(label="Image URL (optional)", required=False)

    def __init__(self, setup_view: "PanelSetupView"):
        super().__init__()
        self.setup_view = setup_view
        self.panel_title.default = setup_view.panel_title
        self.panel_desc.default = setup_view.panel_description
        self.panel_color.default = setup_view.color_hex
        self.panel_image.default = setup_view.image_url or ""

    async def on_submit(self, interaction: discord.Interaction):
        self.setup_view.panel_title = str(self.panel_title.value)
        self.setup_view.panel_description = str(self.panel_desc.value)
        self.setup_view.color_hex = str(self.panel_color.value) if self.panel_color.value else "FF0000"
        self.setup_view.image_url = str(self.panel_image.value) if self.panel_image.value else None
        await interaction.response.edit_message(embed=self.setup_view.build_preview_embed(), view=self.setup_view)


class ButtonInfoModal(discord.ui.Modal, title="Button Settings"):
    label_input = discord.ui.TextInput(label="Button Label", max_length=80, required=True)
    style_input = discord.ui.TextInput(
        label="Style (green/blurple/grey/red)", max_length=20, required=False, default="green"
    )

    def __init__(self, setup_view: "PanelSetupView", role: discord.Role):
        super().__init__()
        self.setup_view = setup_view
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        if len(self.setup_view.buttons) >= 20:
            await interaction.response.send_message("Maximum of 20 buttons reached.", ephemeral=True)
            return

        pending = {
            "label": str(self.label_input.value),
            "style": str(self.style_input.value) if self.style_input.value else "green",
            "role_id": self.role.id,
        }

        # Move to the emoji picker step, editing the same ephemeral message
        # that originally held the role-select menu.
        view = EmojiSelectView(interaction.guild, self.setup_view, pending)
        await interaction.response.edit_message(
            content="Pick an emoji for this button from the server (optional):",
            embed=None,
            view=view
        )


class EmojiSelect(discord.ui.Select):
    def __init__(self, parent_view: "EmojiSelectView", options):
        super().__init__(placeholder="Choose a server emoji...", min_values=1, max_values=1, options=options)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        emoji_str = None
        if value != "none":
            emoji_obj = discord.utils.get(interaction.guild.emojis, id=int(value))
            if emoji_obj:
                emoji_str = str(emoji_obj)
        await self.parent_view.finish(interaction, emoji_str)


class TypeEmojiModal(discord.ui.Modal, title="Enter Emoji"):
    emoji_input = discord.ui.TextInput(label="Emoji (unicode or custom)", required=False, max_length=100)

    def __init__(self, parent_view: "EmojiSelectView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.emoji_input.value).strip() or None
        await self.parent_view.finish(interaction, value)


class TypeManuallyButton(discord.ui.Button):
    def __init__(self, parent_view: "EmojiSelectView"):
        super().__init__(label="Type Manually", style=discord.ButtonStyle.blurple)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TypeEmojiModal(self.parent_view))


class SkipEmojiButton(discord.ui.Button):
    def __init__(self, parent_view: "EmojiSelectView"):
        super().__init__(label="Skip / No Emoji", style=discord.ButtonStyle.grey)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.finish(interaction, None)


class EmojiSelectView(discord.ui.View):
    """Lets the admin pick one of the server's custom emojis for a button,
    with pagination if the server has more than 24 emojis, plus a manual
    text fallback and a skip option."""

    PAGE_SIZE = 24

    def __init__(self, guild: discord.Guild, setup_view: "PanelSetupView", pending: dict, page: int = 0):
        super().__init__(timeout=180)
        self.guild = guild
        self.setup_view = setup_view
        self.pending = pending
        self.page = page
        self.build_items()

    def build_items(self):
        self.clear_items()
        emojis = [e for e in self.guild.emojis if e.is_usable()]
        start = self.page * self.PAGE_SIZE
        chunk = emojis[start:start + self.PAGE_SIZE]

        if chunk:
            options = [
                discord.SelectOption(label=e.name[:100], value=str(e.id), emoji=e)
                for e in chunk
            ]
            self.add_item(EmojiSelect(self, options))

        self.add_item(SkipEmojiButton(self))
        self.add_item(TypeManuallyButton(self))

        if start > 0:
            self.add_item(self._nav_button("◀ Prev", -1))
        if start + self.PAGE_SIZE < len(emojis):
            self.add_item(self._nav_button("Next ▶", 1))

    def _nav_button(self, label: str, delta: int) -> discord.ui.Button:
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.grey)

        async def cb(interaction: discord.Interaction):
            self.page += delta
            self.build_items()
            await interaction.response.edit_message(view=self)

        btn.callback = cb
        return btn

    async def finish(self, interaction: discord.Interaction, emoji_str: Optional[str]):
        if len(self.setup_view.buttons) >= 20:
            await interaction.response.edit_message(
                content="Maximum of 20 buttons reached.", embed=None, view=None
            )
            return

        self.pending["emoji"] = emoji_str
        self.setup_view.buttons.append(self.pending)

        if self.setup_view.message:
            try:
                await self.setup_view.message.edit(
                    embed=self.setup_view.build_preview_embed(), view=self.setup_view
                )
            except (discord.NotFound, discord.HTTPException):
                pass

        role_mention = f"<@&{self.pending['role_id']}>"
        await interaction.response.edit_message(
            content=f"✅ Button **{self.pending['label']}** added for {role_mention}.",
            embed=None,
            view=None
        )
        self.stop()


class RoleSelectForButton(discord.ui.View):
    def __init__(self, setup_view: "PanelSetupView"):
        super().__init__(timeout=120)
        self.setup_view = setup_view

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Select the role for this button...")
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        await interaction.response.send_modal(ButtonInfoModal(self.setup_view, role))
        self.stop()


class PanelSetupView(discord.ui.View):
    def __init__(self, bot, author: discord.Member):
        super().__init__(timeout=600)
        self.bot = bot
        self.author = author
        self.channel: Optional[discord.TextChannel] = None
        self.panel_title = "Server Verification"
        self.panel_description = "Click a button below to verify yourself."
        self.color_hex = "FF0000"
        self.image_url = None
        self.buttons = []  # list of dicts: label, style, role_id, emoji
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This setup isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                for item in self.children:
                    item.disabled = True
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    def build_preview_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.panel_title or "Untitled",
            description=self.panel_description or "",
            color=parse_color(self.color_hex)
        )
        if self.image_url:
            embed.set_image(url=self.image_url)
        if self.buttons:
            listing = "\n".join(f"• **{b['label']}** → <@&{b['role_id']}>" for b in self.buttons)
        else:
            listing = "No buttons added yet."
        embed.add_field(name="Buttons configured", value=listing, inline=False)
        embed.add_field(
            name="Target channel",
            value=self.channel.mention if self.channel else "Not selected",
            inline=False
        )
        embed.set_footer(text="Setup preview — this is not the final message")
        return embed

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Select channel to post the panel...",
        row=0
    )
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.channel = interaction.guild.get_channel(select.values[0].id)
        await interaction.response.edit_message(embed=self.build_preview_embed(), view=self)

    @discord.ui.button(label="Edit Message", style=discord.ButtonStyle.primary, row=1)
    async def edit_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PanelInfoModal(self))

    @discord.ui.button(label="Add Button", style=discord.ButtonStyle.secondary, row=1)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.buttons) >= 20:
            await interaction.response.send_message("Maximum of 20 buttons reached.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select the role this button should give:",
            view=RoleSelectForButton(self),
            ephemeral=True
        )

    @discord.ui.button(label="Remove Last Button", style=discord.ButtonStyle.secondary, row=1)
    async def remove_last(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.buttons:
            self.buttons.pop()
        await interaction.response.edit_message(embed=self.build_preview_embed(), view=self)

    @discord.ui.button(label="Send Panel", style=discord.ButtonStyle.green, row=2)
    async def send_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.channel:
            await interaction.response.send_message("Select a channel first.", ephemeral=True)
            return
        if not self.buttons:
            await interaction.response.send_message("Add at least one button first.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        current_time = utc_to_ist(discord.utils.utcnow())
        final_embed = discord.Embed(
            title=self.panel_title or "Server Verification",
            description=self.panel_description or "",
            color=parse_color(self.color_hex),
            timestamp=current_time
        )
        if self.image_url:
            final_embed.set_image(url=self.image_url)
        final_embed.set_footer(text=f"Verification panel • {current_time.strftime('%I:%M %p IST')}")

        panel_id = await db_execute(
            """INSERT INTO verification_panels
               (guild_id, channel_id, title, description, color, image_url, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                interaction.guild.id, self.channel.id, self.panel_title,
                self.panel_description, self.color_hex, self.image_url,
                current_time.isoformat()
            )
        )

        stored_buttons = []
        for b in self.buttons:
            btn_id = await db_execute(
                "INSERT INTO verification_buttons (panel_id, label, style, role_id, emoji) VALUES (?, ?, ?, ?, ?)",
                (panel_id, b["label"], b["style"], b["role_id"], b["emoji"])
            )
            stored_buttons.append((btn_id, b["label"], b["style"], b["role_id"], b["emoji"]))

        view = build_panel_view(stored_buttons)
        try:
            message = await self.channel.send(embed=final_embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to send messages in that channel.", ephemeral=True
            )
            return

        await db_execute("UPDATE verification_panels SET message_id = ? WHERE id = ?", (message.id, panel_id))
        self.bot.add_view(view, message_id=message.id)

        await interaction.followup.send(
            f"Panel sent in {self.channel.mention} with {len(stored_buttons)} button(s).",
            ephemeral=True
        )
        self.stop()


class Verification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.setup_db_and_views())

    async def setup_db_and_views(self):
        await self.create_tables()
        await self.load_persistent_views()

    async def create_tables(self):
        try:
            async with aiosqlite.connect(DATABASE_PATH) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS verification_panels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        message_id INTEGER,
                        title TEXT,
                        description TEXT,
                        color TEXT,
                        image_url TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS verification_buttons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        panel_id INTEGER NOT NULL,
                        label TEXT NOT NULL,
                        style TEXT DEFAULT 'green',
                        role_id INTEGER NOT NULL,
                        emoji TEXT,
                        FOREIGN KEY (panel_id) REFERENCES verification_panels (id)
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS verification_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        role_id INTEGER NOT NULL,
                        verified_at TEXT NOT NULL
                    )
                """)
                await db.commit()
        except Exception as e:
            logger.error(f"Error creating verification tables: {e}")

    async def load_persistent_views(self):
        """Re-register all saved panels as persistent views on bot startup"""
        try:
            panels = await db_execute(
                "SELECT id, message_id FROM verification_panels WHERE message_id IS NOT NULL",
                fetch="all"
            )
            for panel_id, message_id in panels:
                buttons_rows = await get_panel_buttons(panel_id)
                if not buttons_rows:
                    continue
                view = build_panel_view(buttons_rows)
                self.bot.add_view(view, message_id=message_id)
        except Exception as e:
            logger.error(f"Error loading persistent verification views: {e}")

    @commands.hybrid_group(
        name="verification", invoke_without_command=True,
        description="Manage the custom verification panel system."
    )
    @commands.has_permissions(administrator=True)
    async def verification(self, ctx):
        await ctx.send_help(ctx.command)

    @verification.command(
        name="create",
        description="Create a custom verification panel with your own message, color, image and buttons."
    )
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(administrator=True)
    async def verification_create(self, ctx):
        view = PanelSetupView(self.bot, ctx.author)
        embed = view.build_preview_embed()
        embed.title = "Panel Setup"
        sent = await ctx.send(
            content=(
                "Configure your verification panel: select a channel, edit the message "
                "(title/description/color/image), then add one or more buttons — "
                "each button can give a different role. When you add a button you'll "
                "be able to pick an emoji directly from this server's emoji list."
            ),
            embed=embed,
            view=view
        )
        view.message = sent

    @verification.command(name="list", description="List configured verification panels in this server.")
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(administrator=True)
    async def verification_list(self, ctx):
        panels = await db_execute(
            "SELECT id, channel_id, title FROM verification_panels WHERE guild_id = ?",
            (ctx.guild.id,), fetch="all"
        )
        if not panels:
            await ctx.send("No verification panels configured yet. Use `/verification create`.")
            return
        lines = []
        for panel_id, channel_id, title in panels:
            channel = ctx.guild.get_channel(channel_id)
            lines.append(
                f"**#{panel_id}** — {title or 'Untitled'} — {channel.mention if channel else 'unknown channel'}"
            )
        embed = discord.Embed(title="Verification Panels", description="\n".join(lines), color=DEFAULT_COLOR)
        await ctx.send(embed=embed)

    @verification.command(name="delete", description="Delete a verification panel by its ID (see /verification list).")
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(administrator=True)
    async def verification_delete(self, ctx, panel_id: int):
        row = await db_execute(
            "SELECT channel_id, message_id FROM verification_panels WHERE id = ? AND guild_id = ?",
            (panel_id, ctx.guild.id), fetch="one"
        )
        if not row:
            await ctx.send("Panel not found.")
            return
        channel_id, message_id = row
        channel = ctx.guild.get_channel(channel_id)
        if channel and message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        await db_execute("DELETE FROM verification_buttons WHERE panel_id = ?", (panel_id,))
        await db_execute("DELETE FROM verification_panels WHERE id = ?", (panel_id,))
        await ctx.send(f"Panel #{panel_id} deleted.")

    @verification.command(name="verify", description="Manually give a user a specific role (bypasses buttons).")
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(administrator=True)
    async def verification_verify(self, ctx, user: discord.Member, role: discord.Role):
        if role in user.roles:
            await ctx.send(f"{user.mention} already has {role.mention}.")
            return
        if ctx.guild.me.top_role.position <= role.position or not ctx.guild.me.guild_permissions.manage_roles:
            await ctx.send("I can't manage that role (permissions or role hierarchy).")
            return
        await user.add_roles(role, reason=f"Manual verification by {ctx.author}")
        await log_verification(ctx.guild.id, user.id, role.id)
        embed = discord.Embed(
            title="User Manually Verified",
            description=f"{user.mention} has been given {role.mention} by {ctx.author.mention}.",
            color=DEFAULT_COLOR,
            timestamp=utc_to_ist(discord.utils.utcnow())
        )
        await ctx.send(embed=embed)

    @verification.command(name="logs", description="View recent verification logs.")
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(administrator=True)
    async def verification_logs(self, ctx, limit: int = 10):
        limit = min(limit, 50)
        logs = await db_execute(
            """SELECT user_id, role_id, verified_at FROM verification_logs
               WHERE guild_id = ? ORDER BY verified_at DESC LIMIT ?""",
            (ctx.guild.id, limit), fetch="all"
        )
        if not logs:
            await ctx.send("No verification logs found.")
            return
        lines = []
        for user_id, role_id, verified_at in logs:
            member = ctx.guild.get_member(user_id)
            role = ctx.guild.get_role(role_id)
            lines.append(
                f"**{member.display_name if member else user_id}** → "
                f"{role.mention if role else role_id} — {verified_at}"
            )
        embed = discord.Embed(
            title=f"Recent Verifications ({len(logs)})",
            description="\n".join(lines),
            color=DEFAULT_COLOR
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Verification(bot))
    logger.info("Custom verification cog loaded successfully")
