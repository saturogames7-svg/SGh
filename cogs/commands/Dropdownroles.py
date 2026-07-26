import discord
import emoji as emoji_lib
import sqlite3
import traceback
from discord.ext import commands
from discord.ext.commands import Context
from core.Cog import Cog
from utils.emoji import TICK, CROSS


DB_PATH = "dropdownroles.db"


class DropdownRolesDB:
    # --- Schema definition: single source of truth for expected columns/types. ---
    # Whenever a new column needs to be added in a future update, add it HERE
    # (and nowhere else) - the migration in _migrate() will take care of adding
    # it to any database file that was created before that update, the same
    # way it's handled in the welcomer cog.
    MENUS_SCHEMA = {
        "message_id": "INTEGER PRIMARY KEY",
        "guild_id": "INTEGER",
        "channel_id": "INTEGER",
        "custom_id": "TEXT",
        "placeholder": "TEXT",
        "max_values": "INTEGER DEFAULT 1",
    }
    OPTIONS_SCHEMA = {
        "message_id": "INTEGER",
        "role_id": "INTEGER",
        "label": "TEXT",
        "description": "TEXT",
        "emoji": "TEXT",
    }

    def __init__(self):
        with sqlite3.connect(DB_PATH) as conn:
            menus_cols = ", ".join(f"{name} {ctype}" for name, ctype in self.MENUS_SCHEMA.items())
            conn.execute(f"CREATE TABLE IF NOT EXISTS dropdown_menus ({menus_cols})")

            options_cols = ", ".join(f"{name} {ctype}" for name, ctype in self.OPTIONS_SCHEMA.items())
            conn.execute(f"CREATE TABLE IF NOT EXISTS dropdown_options ({options_cols})")

            self._migrate(conn, "dropdown_menus", self.MENUS_SCHEMA)
            self._migrate(conn, "dropdown_options", self.OPTIONS_SCHEMA)

    def _migrate(self, conn, table_name, schema):
        """
        Ensures an existing (pre-update) table has every column defined in its
        schema dict. CREATE TABLE IF NOT EXISTS does nothing if the table
        already exists, so if a new column is added to a schema in a future
        update, rows saved by an older version of the bot would be missing
        it, and any query touching that column would break after an update.
        This checks the real columns via PRAGMA and ALTERs in whatever is
        missing, so old data keeps working after a restart/update.
        """
        existing_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
        missing_columns = [name for name in schema if name not in existing_columns]
        for name in missing_columns:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but that's
            # fine here since the PK column is always part of the original
            # CREATE TABLE and will already exist.
            col_type = schema[name].replace("PRIMARY KEY", "").strip()
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {col_type}")

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


# --- DIAGNOSTIC WRAP ---
# This cog wasn't showing up as "Loaded cog: DropdownRoles" in the logs at
# all, which means the extension is failing before it even gets registered -
# most likely right here, since this line runs immediately at import time
# (before Discord, before the Cog, before anything). If the bot's loader
# swallows exceptions during `load_extension` without printing them, we'd
# never see why. This print/traceback runs regardless of what the loader
# does afterward, so it WILL show up in the console the next time this
# fails - remove it once the real cause is confirmed and fixed.
try:
    db = DropdownRolesDB()
except Exception:
    print("=" * 60)
    print("[DropdownRoles] FAILED to initialize DropdownRolesDB() at import time:")
    traceback.print_exc()
    print("=" * 60)
    raise


def validate_emoji(emoji: str, bot: commands.Bot = None):
    """
    Parses a raw emoji string typed by a user into a normalized, Discord-API-safe
    representation. Returns a (normalized_str_or_None, error_message_or_None) tuple.

    This is the fix for the 'Invalid Form Body: emoji.name' errors: raw, badly
    formatted emoji strings (bare IDs, bare names, malformed custom emoji syntax)
    were being stored as-is and passed straight to discord.SelectOption, which
    silently builds a broken PartialEmoji instead of raising early. Normalizing
    here means only emoji Discord will actually accept ever get saved.

    If `bot` is passed, this also checks that a custom emoji is actually
    accessible to the bot right now (id shows up in bot.emojis). Discord's
    component API rejects the ENTIRE select menu's Form Body if any one
    option's emoji is a custom emoji the bot can't currently see (deleted,
    or living in a guild the bot isn't a member of) — format alone being
    correct isn't enough.
    """
    if not emoji:
        return None, None

    try:
        partial = discord.PartialEmoji.from_str(emoji)
    except Exception:
        return None, "مش قادر أفهم الإيموجي ده، جرب تاني."

    if partial.id is not None:
        # Custom emoji: <:name:id> or <a:name:id> — syntax is valid, but that's
        # not enough. Confirm the bot can actually see/use this emoji right now.
        if bot is not None:
            accessible_ids = {e.id for e in bot.emojis}
            if partial.id not in accessible_ids:
                return None, (
                    "الإيموجي ده مش متاح للبوت. إما اتمسح، أو البوت مش موجود في "
                    "السيرفر بتاع الإيموجي ده. لازم البوت يكون عضو في نفس السيرفر "
                    "بتاع الإيموجي عشان يقدر يستخدمه في المنيو."
                )
        return str(partial), None

    # No id present. discord.py's PartialEmoji.is_unicode_emoji() only checks
    # "id is None" — it does NOT verify the string is a real emoji character.
    # Plain text like "star" or a stray colon-wrapped word would pass that
    # check and only get rejected later by Discord's API, which is exactly
    # the bug that caused the 'Invalid Form Body' error even after our first
    # fix. We use the `emoji` package here to actually confirm it's a genuine
    # unicode emoji character/sequence.
    if not emoji_lib.is_emoji(emoji):
        return None, (
            "The emoji format is incorrect. Use a standard Unicode emoji (like 🏆) or copy the custom "
            "emoji in its full format from Discord (type \\ before the emoji in any message "
            "to get the correct format like <:name:id>)."
        )

    return str(partial), None


def build_select(message_id: int, custom_id: str, placeholder: str, max_values: int):
    options_data = db.get_options(message_id)

    select_options = []
    for role_id, label, description, emoji in options_data:
        kwargs = dict(
            label=label,
            value=str(role_id),
            description=description if description else None,
        )

        # Defensive re-check at build time too: even though addoption now
        # validates before saving, this guards against any bad data that was
        # stored previously. A single broken emoji here used to invalidate
        # the whole Form Body (all options), so we now just drop that one
        # option's emoji instead of failing the entire select menu.
        if emoji:
            try:
                partial = discord.PartialEmoji.from_str(emoji)
                if partial.id is not None or emoji_lib.is_emoji(emoji):
                    kwargs["emoji"] = emoji
            except Exception:
                pass

        select_options.append(discord.SelectOption(**kwargs))

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
        # Re-register persistent views for existing menus so they survive restarts.
        # Wrapped defensively + logged: if this file's custom_id format ever
        # changes, or a stored row is malformed, we want the error printed
        # loudly here rather than silently killing the whole cog load.
        try:
            for message_id, guild_id, channel_id, custom_id, placeholder, max_values in db.get_all_menus():
                view = RoleDropdownView(message_id, custom_id, placeholder, max_values)
                self.bot.add_view(view, message_id=message_id)
        except Exception:
            print("=" * 60)
            print("[DropdownRoles] FAILED inside cog_load() while re-registering persistent views:")
            traceback.print_exc()
            print("=" * 60)
            raise

    @commands.hybrid_command(name="checkoptions", help="Diagnose which stored option(s) have a broken emoji.", usage="checkoptions <message_id>")
    @commands.has_permissions(manage_roles=True)
    async def checkoptions(self, ctx: Context, message_id: int):
        menu = db.get_menu(message_id)
        if not menu:
            await ctx.send(f"{CROSS} No dropdown menu found with that message ID.", ephemeral=True if ctx.interaction else False)
            return

        options = db.get_options(message_id)
        if not options:
            await ctx.send("No options stored for this menu yet.", ephemeral=True if ctx.interaction else False)
            return

        # Every custom emoji ID the bot can actually use across ALL guilds it's
        # currently a member of (this is what Discord checks server-side when
        # you use emoji= in a component — not just "does this ID look valid").
        accessible_ids = {str(e.id) for e in self.bot.emojis}

        lines = []
        for role_id, label, description, emoji_str in options:
            role = ctx.guild.get_role(role_id)
            role_name = role.name if role else f"(deleted role {role_id})"

            if not emoji_str:
                lines.append(f"⚪ **{label}** ({role_name}) — no emoji set")
                continue

            try:
                partial = discord.PartialEmoji.from_str(emoji_str)
            except Exception:
                lines.append(f"🔴 **{label}** ({role_name}) — `{emoji_str}` cannot be parsed at all")
                continue

            if partial.id is None:
                # unicode emoji, presumably fine — format already validated at addoption time
                lines.append(f"🟢 **{label}** ({role_name}) — unicode emoji {emoji_str}")
                continue

            if str(partial.id) in accessible_ids:
                lines.append(f"🟢 **{label}** ({role_name}) — {emoji_str} (accessible)")
            else:
                lines.append(
                    f"🔴 **{label}** ({role_name}) — {emoji_str} — **NOT accessible to the bot** "
                    f"(deleted, or the bot isn't in the emoji's home server)"
                )

        await ctx.send(
            f"**Emoji check for menu `{message_id}`:**\n" + "\n".join(lines),
            ephemeral=True if ctx.interaction else False
        )

    @commands.hybrid_command(name="createdropdown", help="Attach a dropdown role menu to an existing message.", usage="createdropdown <channel> <message_id>")
    @commands.has_permissions(manage_roles=True)
    async def createdropdown(self, ctx: Context, channel: discord.TextChannel, message_id: int):
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            await ctx.send(f"{CROSS} Message not found in {channel.mention}. Make sure the message ID is correct.", ephemeral=True if ctx.interaction else False)
            return
        except discord.Forbidden:
            await ctx.send(f"{CROSS} I don't have permission to read messages in {channel.mention}.", ephemeral=True if ctx.interaction else False)
            return

        if message.author.id != self.bot.user.id:
            await ctx.send(f"{CROSS} That message wasn't sent by me, so I can't attach a menu to it. Send/build the embed using my embed command first, then run this on it.", ephemeral=True if ctx.interaction else False)
            return

        custom_id = f"dropdown_role_menu:{channel.id}:{message.id}"

        placeholder_view = discord.ui.View(timeout=None)
        placeholder_select = discord.ui.Select(
            custom_id=custom_id,
            placeholder="Choose your roles",
            min_values=0,
            max_values=1,
            options=[discord.SelectOption(label="No roles configured yet", value="none")],
        )
        placeholder_view.add_item(placeholder_select)

        try:
            await message.edit(view=placeholder_view)
        except discord.Forbidden:
            await ctx.send(f"{CROSS} I don't have permission to edit that message (it must be sent by me).", ephemeral=True if ctx.interaction else False)
            return

        db.create_menu(message.id, ctx.guild.id, channel.id, custom_id, "Choose your roles", max_values=1)

        real_view = RoleDropdownView(message.id, custom_id, "Choose your roles", max_values=1)
        self.bot.add_view(real_view, message_id=message.id)

        await ctx.send(
            f"{TICK} Dropdown menu attached to that message in {channel.mention}.\nMessage ID: `{message.id}`\nUse `addoption {message.id} <role> <label>` to add roles.",
            ephemeral=True if ctx.interaction else False
        )

    @commands.hybrid_command(
        name="addoption",
        help="Add a role option to a dropdown menu.",
        usage="addoption <message_id> <role> <label with spaces> [emoji]"
    )
    @commands.has_permissions(manage_roles=True)
    async def addoption(self, ctx: Context, message_id: int, role: discord.Role, *, label_and_emoji: str):
        # ---- Split "label" from an optional trailing "emoji" ----
        # Previously label and emoji were separate parameters, which meant
        # discord.py split on every space and broke multi-word labels
        # ("RoK Services" became label="RoK", emoji="Services"). Now we take
        # the whole rest of the message as one block, and only peel off the
        # LAST word as the emoji if it actually looks like a real emoji
        # (custom <:name:id>/<a:name:id> or a genuine unicode emoji char).
        # Everything else — spaces and all — stays as the label.
        text = label_and_emoji.strip()
        label = text
        emoji = None

        if " " in text:
            possible_label, possible_emoji = text.rsplit(" ", 1)
            possible_emoji = possible_emoji.strip()
            looks_like_emoji = False
            try:
                partial = discord.PartialEmoji.from_str(possible_emoji)
                looks_like_emoji = partial.id is not None or emoji_lib.is_emoji(possible_emoji)
            except Exception:
                looks_like_emoji = False

            if looks_like_emoji:
                label = possible_label.strip()
                emoji = possible_emoji

        if not label:
            await ctx.send(f"{CROSS} You need to provide a label for the role.", ephemeral=True if ctx.interaction else False)
            return

        menu = db.get_menu(message_id)
        if not menu:
            await ctx.send(f"{CROSS} No dropdown menu found with that message ID.", ephemeral=True if ctx.interaction else False)
            return

        # ---- Validate & normalize the emoji BEFORE saving it ----
        # This is the actual fix: previously the raw user-typed string was stored
        # as-is and only failed later, at build time, when Discord rejected the
        # entire Form Body for the whole select menu (all 25 options at once).
        clean_emoji, error = validate_emoji(emoji, bot=self.bot)
        if error:
            await ctx.send(f"{CROSS} {error}", ephemeral=True if ctx.interaction else False)
            return

        _, guild_id, channel_id, custom_id, placeholder, max_values = menu

        existing = db.get_options(message_id)
        if len(existing) >= 25:
            await ctx.send(f"{CROSS} A dropdown menu can only have up to 25 options.", ephemeral=True if ctx.interaction else False)
            return

        db.add_option(message_id, role.id, label, None, clean_emoji)

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
        except discord.HTTPException as e:
            # Extra safety net: if Discord still rejects the form body for any
            # reason (e.g. a bad emoji left over from before this fix), roll
            # back the just-added option instead of leaving the menu broken.
            db.remove_option(message_id, role.id)
            db.create_menu(message_id, guild_id, channel_id, custom_id, placeholder, len(existing))
            await ctx.send(
                f"{CROSS} Discord rejected the update ({e}). The option wasn't saved — check the emoji and try again.",
                ephemeral=True if ctx.interaction else False
            )
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
    try:
        await bot.add_cog(DropdownRoles(bot))
    except Exception:
        print("=" * 60)
        print("[DropdownRoles] FAILED inside setup(bot) / bot.add_cog():")
        traceback.print_exc()
        print("=" * 60)
        raise
