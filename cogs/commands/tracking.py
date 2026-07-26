"""
Professional Invite Tracker system for discord.py 2.x
Feature set: interactive setup panel (Views/Buttons/Selects/Modals),
configurable embed/plain messages with full variable support,
fake/rejoin/left invite detection, leaderboard, manual invite management.

Requires: discord.py>=2.0
Storage: Turso (shared client via utils.turso_db.get_client()).
"""

import re
import datetime
import traceback
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord.ext import commands
from utils.turso_db import get_client


# --------------------------------------------------------------------------
# CONSTANTS
# --------------------------------------------------------------------------

DEFAULT_MESSAGE = "📥 {member} joined, invited by {user} • Total invites: **{invites}**"
DEFAULT_EMBED_TITLE = "👋 Welcome to {server}!"
DEFAULT_EMBED_DESCRIPTION = (
    "{member} just joined the server!\n\n"
    "**Invited by:** {user}\n"
    "**Invites:** {invites} (Real: {real} • Fake: {fake} • Left: {left} • Rejoin: {rejoin})"
)
DEFAULT_EMBED_FOOTER = "{server} • Member #{server_members}"
DEFAULT_EMBED_COLOR = 0x2B2D31
DEFAULT_EMBED_THUMBNAIL = ""
DEFAULT_EMBED_IMAGE = ""
DEFAULT_EMBED_ENABLED = True

FAKE_ACCOUNT_AGE_DAYS = 3

VARIABLE_HELP = (
    "**Supported variables**\n"
    "`{member}` mention • `{member_name}` name • `{member_id}` id\n"
    "`{user}` inviter mention • `{user_name}` inviter name • `{user_id}` inviter id\n"
    "`{invites}` real invites • `{real}` real • `{fake}` fake • `{left}` left • `{rejoin}` rejoin\n"
    "`{server}` server name • `{server_id}` server id • `{server_members}` member count\n"
    "`{account_age}` account age • `{created_at}` account creation date\n"
    "Any variable that cannot be resolved is safely replaced with a placeholder."
)

HEX_PATTERN = re.compile(r"^#?[0-9A-Fa-f]{6}$")


# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------

def humanize_timedelta(delta: datetime.timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    years, rem = divmod(seconds, 31536000)
    months, rem = divmod(rem, 2592000)
    days, rem = divmod(rem, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    parts = []
    if years:
        parts.append(f"{years}y")
    if months:
        parts.append(f"{months}mo")
    if days:
        parts.append(f"{days}d")
    if not parts:
        if hours:
            parts.append(f"{hours}h")
        elif minutes:
            parts.append(f"{minutes}m")
        else:
            parts.append("just now")

    return " ".join(parts[:2]) if parts else "just now"


def parse_hex_color(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None
    if not HEX_PATTERN.match(value):
        return None
    value = value.lstrip("#")
    return int(value, 16)


def parse_yes_no(value: str, default: bool = True) -> bool:
    value = value.strip().lower()
    if value in ("yes", "y", "true", "1", "on", "enable", "enabled"):
        return True
    if value in ("no", "n", "false", "0", "off", "disable", "disabled"):
        return False
    return default


def safe_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return ""


def apply_variables(
    text: str,
    *,
    member: discord.abc.User,
    inviter: Optional[discord.abc.User],
    guild: discord.Guild,
    total: int,
    real: int,
    fake: int,
    left: int,
    rejoin: int,
) -> str:
    if text is None:
        text = ""

    now = discord.utils.utcnow()
    try:
        account_age = humanize_timedelta(now - member.created_at)
    except Exception:
        account_age = "unknown"

    try:
        created_at = discord.utils.format_dt(member.created_at, style="D")
    except Exception:
        created_at = "unknown"

    replacements = {
        "{member}": getattr(member, "mention", f"<@{getattr(member, 'id', 0)}>"),
        "{member_name}": str(getattr(member, "name", "Unknown")),
        "{member_id}": str(getattr(member, "id", "0")),
        "{user}": inviter.mention if inviter else "Unknown",
        "{user_name}": inviter.name if inviter else "Unknown",
        "{user_id}": str(inviter.id) if inviter else "0",
        "{invites}": str(real),
        "{real}": str(real),
        "{fake}": str(fake),
        "{left}": str(left),
        "{rejoin}": str(rejoin),
        "{server}": guild.name if guild else "Unknown",
        "{server_id}": str(guild.id) if guild else "0",
        "{server_members}": str(guild.member_count) if guild else "0",
        "{account_age}": account_age,
        "{created_at}": created_at,
    }

    for key, val in replacements.items():
        text = text.replace(key, val)

    return text


# --------------------------------------------------------------------------
# DATABASE LAYER (Turso)
# --------------------------------------------------------------------------

class InviteDatabase:
    """Thin async wrapper around all invite-tracker tables, backed by Turso."""

    def __init__(self):
        # get_client() only returns the shared client reference, no network
        # I/O - safe to call from a plain sync __init__ (same reasoning as
        # WelcomeDatabase/TicketDatabase/DropdownRolesDB).
        self.client = get_client()

    async def init(self) -> None:
        await self.client.execute(
            """
            CREATE TABLE IF NOT EXISTS invite_config (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                message TEXT DEFAULT '',
                embed_enabled INTEGER DEFAULT 1,
                embed_title TEXT DEFAULT '',
                embed_description TEXT DEFAULT '',
                embed_color INTEGER DEFAULT 0,
                embed_footer TEXT DEFAULT '',
                embed_thumbnail TEXT DEFAULT '',
                embed_image TEXT DEFAULT ''
            )
            """
        )
        await self.client.execute(
            """
            CREATE TABLE IF NOT EXISTS invite_stats (
                guild_id INTEGER,
                user_id INTEGER,
                total INTEGER DEFAULT 0,
                fake INTEGER DEFAULT 0,
                left INTEGER DEFAULT 0,
                rejoin INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        await self.client.execute(
            """
            CREATE TABLE IF NOT EXISTS invite_history (
                guild_id INTEGER,
                member_id INTEGER,
                inviter_id INTEGER,
                joined_at TEXT,
                PRIMARY KEY (guild_id, member_id)
            )
            """
        )

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

    # ---------------------------- CONFIG ---------------------------------

    def default_config(self, guild_id: int) -> Dict[str, Any]:
        return {
            "guild_id": guild_id,
            "channel_id": None,
            "message": DEFAULT_MESSAGE,
            "embed_enabled": DEFAULT_EMBED_ENABLED,
            "embed_title": DEFAULT_EMBED_TITLE,
            "embed_description": DEFAULT_EMBED_DESCRIPTION,
            "embed_color": DEFAULT_EMBED_COLOR,
            "embed_footer": DEFAULT_EMBED_FOOTER,
            "embed_thumbnail": DEFAULT_EMBED_THUMBNAIL,
            "embed_image": DEFAULT_EMBED_IMAGE,
        }

    async def get_config(self, guild_id: int) -> Dict[str, Any]:
        row = await self.fetchone("SELECT * FROM invite_config WHERE guild_id=?", (guild_id,))

        if not row:
            return self.default_config(guild_id)

        return {
            "guild_id": row["guild_id"],
            "channel_id": row["channel_id"],
            "message": row["message"] or DEFAULT_MESSAGE,
            "embed_enabled": bool(row["embed_enabled"]),
            "embed_title": row["embed_title"] or DEFAULT_EMBED_TITLE,
            "embed_description": row["embed_description"] or DEFAULT_EMBED_DESCRIPTION,
            "embed_color": row["embed_color"] or DEFAULT_EMBED_COLOR,
            "embed_footer": row["embed_footer"] or "",
            "embed_thumbnail": row["embed_thumbnail"] or "",
            "embed_image": row["embed_image"] or "",
        }

    async def save_config(self, guild_id: int, config: Dict[str, Any]) -> None:
        await self.execute(
            """
            INSERT INTO invite_config (
                guild_id, channel_id, message, embed_enabled, embed_title,
                embed_description, embed_color, embed_footer, embed_thumbnail, embed_image
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                message=excluded.message,
                embed_enabled=excluded.embed_enabled,
                embed_title=excluded.embed_title,
                embed_description=excluded.embed_description,
                embed_color=excluded.embed_color,
                embed_footer=excluded.embed_footer,
                embed_thumbnail=excluded.embed_thumbnail,
                embed_image=excluded.embed_image
            """,
            (
                guild_id,
                config.get("channel_id"),
                config.get("message", DEFAULT_MESSAGE),
                int(bool(config.get("embed_enabled", True))),
                config.get("embed_title", DEFAULT_EMBED_TITLE),
                config.get("embed_description", DEFAULT_EMBED_DESCRIPTION),
                int(config.get("embed_color", DEFAULT_EMBED_COLOR)),
                config.get("embed_footer", ""),
                config.get("embed_thumbnail", ""),
                config.get("embed_image", ""),
            ),
        )

    async def reset_config(self, guild_id: int) -> Dict[str, Any]:
        defaults = self.default_config(guild_id)
        await self.save_config(guild_id, defaults)
        return defaults

    # ---------------------------- STATS -----------------------------------

    async def ensure_stats_row(self, guild_id: int, user_id: int) -> None:
        await self.execute(
            "INSERT OR IGNORE INTO invite_stats (guild_id, user_id) VALUES (?,?)",
            (guild_id, user_id),
        )

    async def get_stats(self, guild_id: int, user_id: int) -> Dict[str, int]:
        row = await self.fetchone(
            "SELECT total, fake, left, rejoin FROM invite_stats WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )

        if not row:
            return {"total": 0, "fake": 0, "left": 0, "rejoin": 0, "real": 0}

        total, fake, left, rejoin = row["total"], row["fake"], row["left"], row["rejoin"]
        real = max(total - fake - left, 0)
        return {"total": total, "fake": fake, "left": left, "rejoin": rejoin, "real": real}

    async def increment(self, guild_id: int, user_id: int, column: str, amount: int = 1) -> None:
        if column not in ("total", "fake", "left", "rejoin"):
            raise ValueError("Invalid stats column")

        await self.ensure_stats_row(guild_id, user_id)
        await self.execute(
            f"UPDATE invite_stats SET {column} = {column} + ? WHERE guild_id=? AND user_id=?",
            (amount, guild_id, user_id),
        )

    async def set_stats(
        self,
        guild_id: int,
        user_id: int,
        total: Optional[int] = None,
        fake: Optional[int] = None,
        left: Optional[int] = None,
        rejoin: Optional[int] = None,
    ) -> None:
        current = await self.get_stats(guild_id, user_id)
        new_total = current["total"] if total is None else total
        new_fake = current["fake"] if fake is None else fake
        new_left = current["left"] if left is None else left
        new_rejoin = current["rejoin"] if rejoin is None else rejoin

        await self.ensure_stats_row(guild_id, user_id)
        await self.execute(
            """
            UPDATE invite_stats
            SET total=?, fake=?, left=?, rejoin=?
            WHERE guild_id=? AND user_id=?
            """,
            (new_total, new_fake, new_left, new_rejoin, guild_id, user_id),
        )

    async def reset_stats(self, guild_id: int, user_id: int) -> None:
        await self.set_stats(guild_id, user_id, total=0, fake=0, left=0, rejoin=0)

    async def leaderboard(self, guild_id: int, limit: int = 10) -> List[Tuple[int, int, int, int, int]]:
        rows = await self.fetchall(
            """
            SELECT user_id, total, fake, left, rejoin
            FROM invite_stats
            WHERE guild_id=?
            ORDER BY (total - fake - left) DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return [(r["user_id"], r["total"], r["fake"], r["left"], r["rejoin"]) for r in rows]

    # ---------------------------- HISTORY ----------------------------------

    async def get_history(self, guild_id: int, member_id: int) -> Optional[int]:
        row = await self.fetchone(
            "SELECT inviter_id FROM invite_history WHERE guild_id=? AND member_id=?",
            (guild_id, member_id),
        )
        return row["inviter_id"] if row else None

    async def has_history(self, guild_id: int, member_id: int) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM invite_history WHERE guild_id=? AND member_id=?",
            (guild_id, member_id),
        )
        return row is not None

    async def record_history(self, guild_id: int, member_id: int, inviter_id: Optional[int]) -> None:
        await self.execute(
            """
            INSERT INTO invite_history (guild_id, member_id, inviter_id, joined_at)
            VALUES (?,?,?,?)
            ON CONFLICT(guild_id, member_id) DO UPDATE SET
                inviter_id=excluded.inviter_id,
                joined_at=excluded.joined_at
            """,
            (guild_id, member_id, inviter_id, discord.utils.utcnow().isoformat()),
        )


# --------------------------------------------------------------------------
# MODALS
# --------------------------------------------------------------------------

class MessageModal(discord.ui.Modal, title="Edit Join Message"):
    message_input = discord.ui.TextInput(
        label="Custom Message",
        style=discord.TextStyle.paragraph,
        max_length=1500,
        required=True,
    )

    def __init__(self, parent_view: "ConfigView", current_message: str):
        super().__init__()
        self.parent_view = parent_view
        self.message_input.default = current_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.parent_view.draft["message"] = self.message_input.value
        await interaction.response.send_message(
            f"✅ Message updated (not yet saved).\n\n{VARIABLE_HELP}", ephemeral=True
        )
        await self.parent_view.refresh_panel()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(
            "❌ Something went wrong while updating the message.", ephemeral=True
        )


class EmbedModalPrimary(discord.ui.Modal, title="Edit Embed (1/2)"):
    title_input = discord.ui.TextInput(
        label="Embed Title", required=False, max_length=256
    )
    description_input = discord.ui.TextInput(
        label="Embed Description",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=2000,
    )
    footer_input = discord.ui.TextInput(label="Embed Footer", required=False, max_length=256)
    color_input = discord.ui.TextInput(
        label="Color (HEX, e.g. #5865F2)", required=False, max_length=7
    )
    enabled_input = discord.ui.TextInput(
        label="Enable Embed? (yes/no)", required=False, max_length=5
    )

    def __init__(self, parent_view: "ConfigView"):
        super().__init__()
        self.parent_view = parent_view
        draft = parent_view.draft
        self.title_input.default = draft.get("embed_title", "")
        self.description_input.default = draft.get("embed_description", "")
        self.footer_input.default = draft.get("embed_footer", "")
        self.color_input.default = f"#{draft.get('embed_color', DEFAULT_EMBED_COLOR):06X}"
        self.enabled_input.default = "yes" if draft.get("embed_enabled", True) else "no"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = self.parent_view.draft
        draft["embed_title"] = self.title_input.value
        draft["embed_description"] = self.description_input.value
        draft["embed_footer"] = self.footer_input.value

        parsed_color = parse_hex_color(self.color_input.value) if self.color_input.value else None
        if self.color_input.value and parsed_color is None:
            draft_color_note = " ⚠️ Invalid HEX color ignored."
        else:
            draft_color_note = ""
        if parsed_color is not None:
            draft["embed_color"] = parsed_color

        draft["embed_enabled"] = parse_yes_no(
            self.enabled_input.value, default=draft.get("embed_enabled", True)
        )

        await self.parent_view.refresh_panel()
        await interaction.response.send_message(
            f"✅ Embed text updated (not yet saved).{draft_color_note}\n"
            "Click **🖼 Media** below to set Thumbnail / Image URLs.",
            view=EmbedMediaPromptView(self.parent_view),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(
            "❌ Something went wrong while updating the embed.", ephemeral=True
        )


class EmbedModalMedia(discord.ui.Modal, title="Edit Embed (2/2) - Media"):
    thumbnail_input = discord.ui.TextInput(
        label="Thumbnail URL", required=False, max_length=500
    )
    image_input = discord.ui.TextInput(label="Image URL", required=False, max_length=500)

    def __init__(self, parent_view: "ConfigView"):
        super().__init__()
        self.parent_view = parent_view
        draft = parent_view.draft
        self.thumbnail_input.default = draft.get("embed_thumbnail", "")
        self.image_input.default = draft.get("embed_image", "")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = self.parent_view.draft
        thumb = safe_url(self.thumbnail_input.value)
        image = safe_url(self.image_input.value)

        warning = ""
        if self.thumbnail_input.value and not thumb:
            warning += " ⚠️ Thumbnail URL was invalid and ignored."
        if self.image_input.value and not image:
            warning += " ⚠️ Image URL was invalid and ignored."

        draft["embed_thumbnail"] = thumb
        draft["embed_image"] = image

        await self.parent_view.refresh_panel()
        await interaction.response.send_message(
            f"✅ Embed media updated (not yet saved).{warning}", ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(
            "❌ Something went wrong while updating embed media.", ephemeral=True
        )


class EmbedMediaPromptView(discord.ui.View):
    """Small ephemeral bridge view offering the second embed modal."""

    def __init__(self, parent_view: "ConfigView"):
        super().__init__(timeout=120)
        self.parent_view = parent_view

    @discord.ui.button(label="Media", emoji="🖼", style=discord.ButtonStyle.secondary)
    async def open_media_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.parent_view.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the setup command can use this.", ephemeral=True
            )
            return
        await interaction.response.send_modal(EmbedModalMedia(self.parent_view))


# --------------------------------------------------------------------------
# CHANNEL SELECT VIEW
# --------------------------------------------------------------------------

class ChannelSelectView(discord.ui.View):
    def __init__(self, parent_view: "ConfigView"):
        super().__init__(timeout=120)
        self.parent_view = parent_view

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.parent_view.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran the setup command can use this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select the invite log channel",
        channel_types=[discord.ChannelType.text],
        min_values=1,
        max_values=1,
    )
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = select.values[0]
        resolved = channel.resolve() or await channel.fetch()

        permissions = resolved.permissions_for(interaction.guild.me)
        if not permissions.send_messages or not permissions.view_channel:
            await interaction.response.edit_message(
                content=(
                    f"❌ I don't have permission to send messages in {resolved.mention}. "
                    "Choose a different channel."
                ),
                view=self,
            )
            return

        self.parent_view.draft["channel_id"] = resolved.id
        await self.parent_view.refresh_panel()
        await interaction.response.edit_message(
            content=f"✅ Log channel set to {resolved.mention} (not yet saved).", view=None
        )
        self.stop()


# --------------------------------------------------------------------------
# MAIN CONFIG PANEL VIEW
# --------------------------------------------------------------------------

class ConfigView(discord.ui.View):
    def __init__(self, cog: "Tracking", guild: discord.Guild, author_id: int, config: Dict[str, Any]):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.author_id = author_id
        self.draft: Dict[str, Any] = dict(config)
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who ran this command can use these buttons.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def build_embed(self) -> discord.Embed:
        channel = self.guild.get_channel(self.draft.get("channel_id")) if self.draft.get("channel_id") else None

        embed = discord.Embed(
            title="⚙️ Invite Tracker Configuration",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Current Channel",
            value=channel.mention if channel else "*Not set*",
            inline=True,
        )
        embed.add_field(
            name="Embed",
            value="Enabled ✅" if self.draft.get("embed_enabled") else "Disabled ❌",
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(
            name="Current Message",
            value=f"```{self.draft.get('message', '')[:500]}```" if self.draft.get("message") else "*Not set*",
            inline=False,
        )
        embed.add_field(
            name="Embed Title",
            value=self.draft.get("embed_title") or "*Not set*",
            inline=True,
        )
        embed.add_field(
            name="Embed Color",
            value=f"#{self.draft.get('embed_color', DEFAULT_EMBED_COLOR):06X}",
            inline=True,
        )
        embed.set_footer(text="Changes are only permanent after you press Save.")
        return embed

    async def refresh_panel(self) -> None:
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except discord.HTTPException:
                pass

    # ------------------------------- ROW 0 --------------------------------

    @discord.ui.button(label="Select Channel", emoji="📢", style=discord.ButtonStyle.primary, row=0)
    async def select_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ChannelSelectView(self)
        await interaction.response.send_message(
            "Select the text channel for invite logs:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Edit Message", emoji="📝", style=discord.ButtonStyle.primary, row=0)
    async def edit_message_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = MessageModal(self, self.draft.get("message", DEFAULT_MESSAGE))
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Edit Embed", emoji="🎨", style=discord.ButtonStyle.primary, row=0)
    async def edit_embed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EmbedModalPrimary(self)
        await interaction.response.send_modal(modal)

    # ------------------------------- ROW 1 --------------------------------

    @discord.ui.button(label="Preview", emoji="👀", style=discord.ButtonStyle.secondary, row=1)
    async def preview_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)

        fake_member = interaction.user
        fake_inviter_mention = "**Saturo**"
        fake_inviter_name = "Saturo"
        fake_inviter_id = "000000000000000000"
        fake_real, fake_fake, fake_left, fake_rejoin = 25, 2, 1, 3
        fake_total = fake_real + fake_fake + fake_left

        def _apply(text: str) -> str:
            now = discord.utils.utcnow()
            account_age = humanize_timedelta(now - fake_member.created_at)
            created_at = discord.utils.format_dt(fake_member.created_at, style="D")
            replacements = {
                "{member}": fake_member.mention,
                "{member_name}": fake_member.name,
                "{member_id}": str(fake_member.id),
                "{user}": fake_inviter_mention,
                "{user_name}": fake_inviter_name,
                "{user_id}": fake_inviter_id,
                "{invites}": str(fake_real),
                "{real}": str(fake_real),
                "{fake}": str(fake_fake),
                "{left}": str(fake_left),
                "{rejoin}": str(fake_rejoin),
                "{server}": self.guild.name,
                "{server_id}": str(self.guild.id),
                "{server_members}": str(self.guild.member_count),
                "{account_age}": account_age,
                "{created_at}": created_at,
            }
            for key, val in replacements.items():
                text = text.replace(key, val)
            return text

        if self.draft.get("embed_enabled"):
            embed = discord.Embed(
                title=_apply(self.draft.get("embed_title", "")) or None,
                description=_apply(self.draft.get("embed_description", "")) or None,
                color=self.draft.get("embed_color", DEFAULT_EMBED_COLOR),
            )
            footer = _apply(self.draft.get("embed_footer", ""))
            if footer:
                embed.set_footer(text=footer)
            if self.draft.get("embed_thumbnail"):
                embed.set_thumbnail(url=self.draft["embed_thumbnail"])
            if self.draft.get("embed_image"):
                embed.set_image(url=self.draft["embed_image"])

            await interaction.followup.send(
                content="**👀 Preview** (using sample data)", embed=embed, ephemeral=True
            )
        else:
            content = _apply(self.draft.get("message", DEFAULT_MESSAGE))
            await interaction.followup.send(
                content=f"**👀 Preview** (using sample data)\n\n{content}", ephemeral=True
            )

    @discord.ui.button(label="Save", emoji="💾", style=discord.ButtonStyle.success, row=1)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.cog.db.save_config(self.guild.id, self.draft)
        except Exception:
            traceback.print_exc()
            await interaction.response.send_message(
                "❌ Failed to save configuration due to a database error.", ephemeral=True
            )
            return

        await interaction.response.send_message("💾 Configuration saved!", ephemeral=True)
        await self.refresh_panel()

    @discord.ui.button(label="Reset", emoji="🗑", style=discord.ButtonStyle.danger, row=1)
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            defaults = await self.cog.db.reset_config(self.guild.id)
        except Exception:
            traceback.print_exc()
            await interaction.response.send_message(
                "❌ Failed to reset configuration due to a database error.", ephemeral=True
            )
            return

        self.draft = defaults
        await interaction.response.send_message("🗑 Configuration reset to defaults.", ephemeral=True)
        await self.refresh_panel()

    @discord.ui.button(label="Close", emoji="❌", style=discord.ButtonStyle.danger, row=1)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


# --------------------------------------------------------------------------
# COG
# --------------------------------------------------------------------------

class Tracking(commands.Cog, name="Invite Tracker"):
    """Complete invite tracking system with an interactive setup panel.

    This cog contains the actual working logic (database, listeners, commands).
    The help-menu display (help_custom + the `InviteTracker` group) lives in a
    separate cog file (cogs/zyrox/inviteTracker.py) and is loaded independently.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invites_cache: Dict[int, List[discord.Invite]] = {}
        self.vanity_cache: Dict[int, int] = {}

    async def cog_load(self):
        # DB creation/init happens here (not __init__), guaranteed to run
        # inside a live event loop before any command/listener can fire -
        # same reasoning as WelcomeDatabase/TicketDatabase/DropdownRolesDB.
        try:
            self.db = InviteDatabase()
            await self.db.init()
        except Exception:
            print("=" * 60)
            print("[Tracking/InviteTracker] FAILED inside cog_load() (Turso table setup):")
            traceback.print_exc()
            print("=" * 60)
            raise

    # ------------------------------ CACHING --------------------------------

    async def cache_invites(self, guild: discord.Guild) -> None:
        try:
            self.invites_cache[guild.id] = await guild.invites()
        except discord.Forbidden:
            self.invites_cache[guild.id] = []
        except discord.HTTPException:
            self.invites_cache[guild.id] = []

        if "VANITY_URL" in guild.features:
            try:
                vanity = await guild.vanity_invite()
                self.vanity_cache[guild.id] = vanity.uses if vanity else 0
            except (discord.Forbidden, discord.HTTPException):
                self.vanity_cache[guild.id] = 0

    # ------------------------------ LISTENERS -------------------------------

    @commands.Cog.listener()
    async def on_ready(self):
        # NOTE: db.init() used to be called here (and could re-run on every
        # reconnect). It now happens once in cog_load(); this listener only
        # refreshes the invite cache.
        for guild in self.bot.guilds:
            await self.cache_invites(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.cache_invites(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        await self.cache_invites(invite.guild)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        await self.cache_invites(invite.guild)

    async def _resolve_inviter(self, guild: discord.Guild) -> Optional[discord.abc.User]:
        """Diff cached invites against current invites to find who was used."""

        old_invites = self.invites_cache.get(guild.id, [])

        try:
            new_invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            new_invites = None

        inviter: Optional[discord.abc.User] = None

        if new_invites is not None:
            old_map = {inv.code: inv for inv in old_invites}
            new_map = {inv.code: inv for inv in new_invites}

            for code, new_inv in new_map.items():
                old_inv = old_map.get(code)
                if old_inv and new_inv.uses is not None and old_inv.uses is not None:
                    if new_inv.uses > old_inv.uses:
                        inviter = new_inv.inviter
                        break

            if inviter is None:
                # An invite may have been consumed and deleted (max uses reached).
                for code, old_inv in old_map.items():
                    if code not in new_map:
                        inviter = old_inv.inviter
                        break

            self.invites_cache[guild.id] = new_invites

        if inviter is None and "VANITY_URL" in guild.features:
            try:
                vanity = await guild.vanity_invite()
                if vanity and vanity.uses is not None:
                    old_uses = self.vanity_cache.get(guild.id, 0)
                    if vanity.uses > old_uses:
                        inviter = None  # Vanity invites have no attributable inviter.
                    self.vanity_cache[guild.id] = vanity.uses
            except (discord.Forbidden, discord.HTTPException):
                pass

        return inviter

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild

        try:
            inviter = await self._resolve_inviter(guild)
        except Exception:
            traceback.print_exc()
            inviter = None

        is_rejoin = await self.db.has_history(guild.id, member.id)
        account_age = discord.utils.utcnow() - member.created_at
        is_suspected_fake = account_age < datetime.timedelta(days=FAKE_ACCOUNT_AGE_DAYS)

        total = fake = left = rejoin = real = 0

        if inviter is not None and not inviter.bot:
            try:
                if is_rejoin:
                    await self.db.increment(guild.id, inviter.id, "rejoin", 1)
                elif is_suspected_fake:
                    await self.db.increment(guild.id, inviter.id, "total", 1)
                    await self.db.increment(guild.id, inviter.id, "fake", 1)
                else:
                    await self.db.increment(guild.id, inviter.id, "total", 1)
            except Exception:
                traceback.print_exc()

            try:
                await self.db.record_history(guild.id, member.id, inviter.id)
            except Exception:
                traceback.print_exc()

            try:
                stats = await self.db.get_stats(guild.id, inviter.id)
                total, fake, left, rejoin, real = (
                    stats["total"],
                    stats["fake"],
                    stats["left"],
                    stats["rejoin"],
                    stats["real"],
                )
            except Exception:
                traceback.print_exc()
        else:
            try:
                await self.db.record_history(guild.id, member.id, None)
            except Exception:
                traceback.print_exc()

        try:
            config = await self.db.get_config(guild.id)
        except Exception:
            traceback.print_exc()
            return

        if not config.get("channel_id"):
            return

        channel = guild.get_channel(config["channel_id"])
        if channel is None:
            return

        permissions = channel.permissions_for(guild.me)
        if not permissions.send_messages or not permissions.view_channel:
            return

        try:
            if config.get("embed_enabled"):
                embed = discord.Embed(
                    title=apply_variables(
                        config.get("embed_title", ""),
                        member=member, inviter=inviter, guild=guild,
                        total=total, real=real, fake=fake, left=left, rejoin=rejoin,
                    ) or None,
                    description=apply_variables(
                        config.get("embed_description", ""),
                        member=member, inviter=inviter, guild=guild,
                        total=total, real=real, fake=fake, left=left, rejoin=rejoin,
                    ) or None,
                    color=config.get("embed_color", DEFAULT_EMBED_COLOR),
                )
                footer = apply_variables(
                    config.get("embed_footer", ""),
                    member=member, inviter=inviter, guild=guild,
                    total=total, real=real, fake=fake, left=left, rejoin=rejoin,
                )
                if footer:
                    embed.set_footer(text=footer)
                if config.get("embed_thumbnail"):
                    embed.set_thumbnail(url=config["embed_thumbnail"])
                if config.get("embed_image"):
                    embed.set_image(url=config["embed_image"])

                await channel.send(embed=embed)
            else:
                content = apply_variables(
                    config.get("message", DEFAULT_MESSAGE),
                    member=member, inviter=inviter, guild=guild,
                    total=total, real=real, fake=fake, left=left, rejoin=rejoin,
                )
                await channel.send(content=content)
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            traceback.print_exc()

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild

        try:
            inviter_id = await self.db.get_history(guild.id, member.id)
        except Exception:
            traceback.print_exc()
            return

        if inviter_id is None:
            return

        try:
            await self.db.increment(guild.id, inviter_id, "left", 1)
        except Exception:
            traceback.print_exc()

    # ------------------------------- COMMANDS -------------------------------

    @commands.command(
        name="invlog",
        aliases=["invitelogging", "invitesetup"],
        help="Open the interactive invite tracker configuration panel.",
    )
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(send_messages=True, embed_links=True)
    async def invlog(self, ctx: commands.Context):
        config = await self.db.get_config(ctx.guild.id)
        view = ConfigView(self, ctx.guild, ctx.author.id, config)
        message = await ctx.send(embed=view.build_embed(), view=view)
        view.message = message

    @commands.command(name="invites", aliases=["inv"], help="Show invite stats for yourself or another member.")
    async def invites(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        stats = await self.db.get_stats(ctx.guild.id, member.id)

        embed = discord.Embed(
            title=f"📊 Invite Stats — {member.display_name}",
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Real", value=str(stats["real"]), inline=True)
        embed.add_field(name="Fake", value=str(stats["fake"]), inline=True)
        embed.add_field(name="Left", value=str(stats["left"]), inline=True)
        embed.add_field(name="Rejoin", value=str(stats["rejoin"]), inline=True)
        embed.add_field(name="Total Credited", value=str(stats["total"]), inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="addinvites", help="Add bonus invites to a member. Usage: addinvites @member amount")
    @commands.has_permissions(manage_guild=True)
    async def addinvites(self, ctx: commands.Context, member: discord.Member, amount: int):
        if amount == 0:
            await ctx.send("❌ Amount must be a non-zero integer.")
            return

        await self.db.increment(ctx.guild.id, member.id, "total", amount)
        stats = await self.db.get_stats(ctx.guild.id, member.id)

        embed = discord.Embed(
            title="✅ Invites Updated",
            description=f"Added `{amount}` invites to {member.mention}.\nNew real total: `{stats['real']}`",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @commands.command(
        name="setinvites",
        help="Set exact invite stats for a member. Usage: setinvites @member total [fake] [left] [rejoin]",
    )
    @commands.has_permissions(manage_guild=True)
    async def setinvites(
        self,
        ctx: commands.Context,
        member: discord.Member,
        total: int,
        fake: int = None,
        left: int = None,
        rejoin: int = None,
    ):
        await self.db.set_stats(ctx.guild.id, member.id, total=total, fake=fake, left=left, rejoin=rejoin)
        stats = await self.db.get_stats(ctx.guild.id, member.id)

        embed = discord.Embed(
            title="✅ Invites Set",
            description=(
                f"Updated stats for {member.mention}\n\n"
                f"Total: `{stats['total']}` • Fake: `{stats['fake']}` • "
                f"Left: `{stats['left']}` • Rejoin: `{stats['rejoin']}`"
            ),
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="resetinvites", help="Reset a member's invite stats to zero.")
    @commands.has_permissions(manage_guild=True)
    async def resetinvites(self, ctx: commands.Context, member: discord.Member):
        await self.db.reset_stats(ctx.guild.id, member.id)
        embed = discord.Embed(
            title="🗑 Invites Reset",
            description=f"All invite stats for {member.mention} have been reset to `0`.",
            color=discord.Color.red(),
        )
        await ctx.send(embed=embed)

    @commands.command(name="inviteleaderboard", aliases=["invlb", "invitelb"], help="Show the top inviters.")
    async def inviteleaderboard(self, ctx: commands.Context):
        rows = await self.db.leaderboard(ctx.guild.id, limit=10)

        if not rows:
            await ctx.send("📉 No invite data recorded yet.")
            return

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for index, (user_id, total, fake, left, rejoin) in enumerate(rows):
            real = max(total - fake - left, 0)
            prefix = medals[index] if index < 3 else f"`#{index + 1}`"
            member = ctx.guild.get_member(user_id)
            name = member.mention if member else f"<@{user_id}>"
            lines.append(f"{prefix} {name} — **{real}** invites (Fake: {fake} • Left: {left} • Rejoin: {rejoin})")

        embed = discord.Embed(
            title=f"🏆 Invite Leaderboard — {ctx.guild.name}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed)

    # ------------------------------- ERRORS ---------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need the **Manage Server** permission to use this command.")
            return

        if isinstance(error, commands.BotMissingPermissions):
            missing = ", ".join(error.missing_permissions)
            await ctx.send(f"❌ I'm missing the following permissions: `{missing}`.")
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Missing required argument: `{error.param.name}`.")
            return

        if isinstance(error, commands.BadArgument):
            await ctx.send("❌ Invalid argument provided. Please check your input and try again.")
            return

        if isinstance(error, discord.Forbidden):
            await ctx.send("❌ I don't have permission to perform that action.")
            return

        traceback.print_exc()
        await ctx.send("❌ An unexpected error occurred while running that command.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Tracking(bot))
    
