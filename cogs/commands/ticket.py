import discord
from utils.emoji import CROSS, DELETE_ALT1, HANDSHAKE, LOCK, TICK, UNLOCK, ZBAN, ZMODULE, ZWRENCH
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context
import traceback
from datetime import datetime
import asyncio
import io
import re
from utils.config import *
from utils.turso_db import get_client

# --- Configurable Variables ---
EMBED_COLOR = 0xFF0000
TICKET_CHANNEL_IMAGE_URL = "https://cdn.discordapp.com/attachments/1530611685772103750/1530611805926199547/Gemini_Generated_Image_opnjhropnjhropnj.png?ex=6a6634d3&is=6a64e353&hm=c6450bb9d72b189368fcf67702aec5120088ccb8581ffc0ac39a131a7f37eba6&"

# --- Emoji Variables ---
SUCCESS_EMOJI = TICK
ERROR_EMOJI = CROSS
LOCK_EMOJI = f"{UNLOCK} "
UNLOCK_EMOJI = LOCK
CLAIM_EMOJI = HANDSHAKE
CLOSE_EMOJI = ZBAN
DELETE_EMOJI = DELETE_ALT1
REOPEN_EMOJI = ZWRENCH
TRANSCRIPT_EMOJI = ZMODULE

# --- Constants ---
MAX_CATEGORIES = 15
MAX_PANELS_PER_GUILD = 10
TICKET_LIMIT_PER_USER = 3


# --- Database Class (Turso) ---
class TicketDatabase:
    # Single source of truth for expected columns/types per table.
    # Adding a column later: add it HERE only, _migrate() adds it to
    # any pre-existing remote table automatically.
    #
    # NOTE (multi-panel support): guild_configs used to ALSO store the
    # single panel's channel/message/embed data directly (one row per
    # guild_id PK == only one panel possible). That data now lives in
    # its own ticket_panels table (one row PER PANEL, panel_id PK,
    # guild_id is a plain non-unique column so a guild can have many
    # rows/panels). guild_configs keeps only true guild-wide settings.
    #
    # IMPORTANT for existing Turso databases: the OLD panel_* / embed_*
    # columns physically still exist on the remote guild_configs table
    # (CREATE TABLE IF NOT EXISTS never removes columns). We don't touch
    # or drop them - _migrate_legacy_panels() below reads them once on
    # startup to copy any existing panel into the new ticket_panels
    # table, then leaves them alone. Nothing destructive ever happens.
    GUILD_CONFIGS_SCHEMA = {
        "guild_id": "INTEGER PRIMARY KEY",
        "logging_channel_id": "INTEGER",
        "closed_category_id": "INTEGER",
    }
    TICKET_PANELS_SCHEMA = {
        "panel_id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "guild_id": "INTEGER NOT NULL",
        "panel_name": "TEXT",
        "panel_channel_id": "INTEGER",
        "panel_message_id": "INTEGER",
        "panel_type": "TEXT",
        "embed_title": "TEXT",
        "embed_description": "TEXT",
        "embed_color": "INTEGER",
        "embed_image_url": "TEXT",
        "embed_thumbnail_url": "TEXT",
    }
    TICKET_CATEGORIES_SCHEMA = {
        "category_id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "guild_id": "INTEGER",
        "panel_id": "INTEGER",  # NEW: which panel this category/button belongs to
        "name": "TEXT NOT NULL",
        "emoji": "TEXT",
        "notified_roles": "TEXT",
        "button_style": "INTEGER",
        "discord_category_id": "INTEGER",
    }
    OPEN_TICKETS_SCHEMA = {
        "channel_id": "INTEGER PRIMARY KEY",
        "ticket_number": "INTEGER",
        "guild_id": "INTEGER",
        "creator_id": "INTEGER NOT NULL",
        "category_db_id": "INTEGER",
        "created_at": "TEXT NOT NULL",
        "closed_by_id": "INTEGER",
        "closed_at": "TEXT",
        "is_locked": "BOOLEAN DEFAULT 0",
        "is_claimed": "BOOLEAN DEFAULT 0",
        "claimed_by_id": "INTEGER",
        "action_message_id": "INTEGER",
    }

    def __init__(self):
        # get_client() only returns the shared client reference - safe to
        # call from a plain sync __init__ (same reasoning as DropdownRoles).
        self.client = get_client()

    async def init(self):
        guild_cols = ", ".join(f"{n} {t}" for n, t in self.GUILD_CONFIGS_SCHEMA.items())
        await self.client.execute(f"CREATE TABLE IF NOT EXISTS guild_configs ({guild_cols})")

        panel_cols = ", ".join(f"{n} {t}" for n, t in self.TICKET_PANELS_SCHEMA.items())
        await self.client.execute(f"CREATE TABLE IF NOT EXISTS ticket_panels ({panel_cols})")

        cat_cols = ", ".join(f"{n} {t}" for n, t in self.TICKET_CATEGORIES_SCHEMA.items())
        await self.client.execute(f"CREATE TABLE IF NOT EXISTS ticket_categories ({cat_cols})")

        open_cols = ", ".join(f"{n} {t}" for n, t in self.OPEN_TICKETS_SCHEMA.items())
        await self.client.execute(f"CREATE TABLE IF NOT EXISTS open_tickets ({open_cols})")

        # Composite PK table - handled separately from the generic schema
        # dicts above since _migrate()/PRAGMA logic assumes a simple table.
        await self.client.execute(
            "CREATE TABLE IF NOT EXISTS user_ticket_counts ("
            "guild_id INTEGER, user_id INTEGER, ticket_count INTEGER DEFAULT 0, "
            "PRIMARY KEY (guild_id, user_id))"
        )

        await self._migrate("guild_configs", self.GUILD_CONFIGS_SCHEMA)
        await self._migrate("ticket_panels", self.TICKET_PANELS_SCHEMA)
        await self._migrate("ticket_categories", self.TICKET_CATEGORIES_SCHEMA)
        await self._migrate("open_tickets", self.OPEN_TICKETS_SCHEMA)

        # One-time, idempotent, additive migration for guilds that set up
        # a panel under the OLD single-panel schema before this update.
        await self._migrate_legacy_panels()

    async def _migrate(self, table_name, schema):
        result = await self.client.execute(f"PRAGMA table_info({table_name})")
        existing_columns = {row[1] for row in result.rows}
        missing_columns = [name for name in schema if name not in existing_columns]
        for name in missing_columns:
            col_type = schema[name].replace("PRIMARY KEY", "").replace("AUTOINCREMENT", "").replace("NOT NULL", "").strip()
            await self.client.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {col_type}")

    async def _migrate_legacy_panels(self):
        """
        Older versions of this cog stored one panel directly on
        guild_configs (guild_id was the PK, so only one panel per guild
        was possible). Those legacy columns still physically exist on
        the remote Turso table for any guild that ran /ticket setup
        before this update. This copies that single panel + its
        categories into the new multi-panel tables, once, safely.

        Safe to run on every startup: it only acts on guilds that still
        have unmigrated categories (panel_id IS NULL), so once a guild
        is migrated this becomes a no-op for it forever after.
        """
        result = await self.client.execute("PRAGMA table_info(guild_configs)")
        legacy_columns = {row[1] for row in result.rows}
        if "panel_channel_id" not in legacy_columns:
            return  # fresh install, nothing legacy to migrate

        legacy_configs = await self.fetchall(
            "SELECT guild_id, panel_channel_id, panel_message_id, panel_type, "
            "embed_title, embed_description, embed_color, embed_image_url, embed_thumbnail_url "
            "FROM guild_configs WHERE panel_channel_id IS NOT NULL"
        )
        for cfg in legacy_configs:
            unmigrated = await self.fetchone(
                "SELECT 1 FROM ticket_categories WHERE guild_id=? AND panel_id IS NULL",
                (cfg["guild_id"],)
            )
            if not unmigrated:
                continue  # already migrated (or this guild never had categories)

            await self.execute(
                "INSERT INTO ticket_panels (guild_id, panel_name, panel_channel_id, panel_message_id, "
                "panel_type, embed_title, embed_description, embed_color, embed_image_url, embed_thumbnail_url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cfg["guild_id"], cfg["embed_title"] or "Support", cfg["panel_channel_id"], cfg["panel_message_id"],
                 cfg["panel_type"], cfg["embed_title"], cfg["embed_description"], cfg["embed_color"],
                 cfg["embed_image_url"], cfg["embed_thumbnail_url"])
            )
            new_panel = await self.fetchone(
                "SELECT panel_id FROM ticket_panels WHERE guild_id=? AND panel_channel_id=? "
                "ORDER BY panel_id DESC LIMIT 1",
                (cfg["guild_id"], cfg["panel_channel_id"])
            )
            if new_panel:
                await self.execute(
                    "UPDATE ticket_categories SET panel_id=? WHERE guild_id=? AND panel_id IS NULL",
                    (new_panel["panel_id"], cfg["guild_id"])
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


# --- Utility Functions ---
async def get_or_create_log_channel(db, guild):
    config = await db.fetchone("SELECT logging_channel_id FROM guild_configs WHERE guild_id = ?", (guild.id,))
    if config and config["logging_channel_id"] and (ch := guild.get_channel(config["logging_channel_id"])):
        return ch
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    try:
        ch = await guild.create_text_channel(f"{BRAND_NAME}-ticket-logs", overwrites=overwrites)
        await db.execute(
            "INSERT INTO guild_configs (guild_id, logging_channel_id) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET logging_channel_id=excluded.logging_channel_id",
            (guild.id, ch.id)
        )
        return ch
    except Exception:
        return None


async def log_ticket_action(db, guild, user, action, details):
    if log_channel := await get_or_create_log_channel(db, guild):
        embed = discord.Embed(title=f"Ticket Action: {action}", color=EMBED_COLOR, timestamp=datetime.now())
        embed.add_field(name="Action By", value=user.mention).add_field(name="Details", value=details, inline=False)
        try:
            await log_channel.send(embed=embed)
        except Exception:
            pass


async def get_or_create_closed_category(db, guild):
    config = await db.fetchone("SELECT closed_category_id FROM guild_configs WHERE guild_id = ?", (guild.id,))
    if config and config["closed_category_id"] and (cat := guild.get_channel(config["closed_category_id"])):
        return cat
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    try:
        cat = await guild.create_category("Closed Tickets", overwrites=overwrites)
        await db.execute(
            "INSERT INTO guild_configs (guild_id, closed_category_id) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET closed_category_id=excluded.closed_category_id",
            (guild.id, cat.id)
        )
        return cat
    except Exception:
        return None


def resolve_existing_category(guild, raw_input):
    """Try to resolve a user-typed string (ID or name) to an existing discord.CategoryChannel."""
    raw_input = raw_input.strip()
    found = None
    if raw_input.isdigit():
        found = guild.get_channel(int(raw_input))
    if not found:
        cleaned = raw_input.lstrip('#')
        found = discord.utils.find(
            lambda c: c.name.lower() == raw_input.lower() or c.name.lower() == cleaned.lower(),
            guild.categories
        )
    if found and isinstance(found, discord.CategoryChannel):
        return found
    return None


# --- Setup Views ---
class EmbedEditorView(discord.ui.View):
    def __init__(self, cog, ctx, panel_channel, panel_type, panel_name):
        super().__init__(timeout=600)
        self.cog, self.ctx, self.panel_channel, self.panel_type, self.panel_name = cog, ctx, panel_channel, panel_type, panel_name
        self.message = None
        self.embed_data = {"title": "Support Tickets", "description": "Click a button or select an option below to create a ticket.", "color": EMBED_COLOR}

    def _create_preview_embed(self):
        embed = discord.Embed.from_dict(self.embed_data)
        if img_url := self.embed_data.get("image", {}).get("url"): embed.set_image(url=img_url)
        if thumb_url := self.embed_data.get("thumbnail", {}).get("url"): embed.set_thumbnail(url=thumb_url)
        return embed

    async def start(self, interaction):
        await interaction.response.send_message("Use the buttons to customize the panel embed.", embed=self._create_preview_embed(), view=self, ephemeral=True)
        self.message = await interaction.original_response()

    async def _prompt(self, inter, prompt):
        await inter.response.send_message(prompt, ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for("message", check=lambda m: m.author.id == self.ctx.author.id and m.channel.id == self.ctx.channel.id, timeout=120)
            try: await msg.delete()
            except Exception: pass
            return msg.content
        except Exception:
            return None

    @discord.ui.button(label="Title", style=discord.ButtonStyle.green, row=0)
    async def edit_title(self, inter, button):
        if title := await self._prompt(inter, "Enter new title:"):
            self.embed_data["title"] = title
            await self.message.edit(embed=self._create_preview_embed())

    @discord.ui.button(label="Description", style=discord.ButtonStyle.green, row=0)
    async def edit_desc(self, inter, button):
        if desc := await self._prompt(inter, "Enter new description:"):
            self.embed_data["description"] = desc
            await self.message.edit(embed=self._create_preview_embed())

    @discord.ui.button(label="Color", style=discord.ButtonStyle.green, row=0)
    async def edit_color(self, inter, button):
        raw = await self._prompt(inter, "Enter a hex color (e.g. `#FF0000` or `FF0000`):")
        if not raw: return
        hex_clean = raw.strip().lstrip('#')
        if not re.fullmatch(r'[0-9a-fA-F]{6}', hex_clean):
            return await self.ctx.channel.send("That's not a valid hex color, try again with the Color button.", delete_after=8)
        self.embed_data["color"] = int(hex_clean, 16)
        await self.message.edit(embed=self._create_preview_embed())

    @discord.ui.button(label="Image URL", style=discord.ButtonStyle.blurple, row=1)
    async def edit_image(self, inter, button):
        if url := await self._prompt(inter, "Enter image URL (`none` to remove):"):
            self.embed_data["image"] = {"url": url} if url.lower() != 'none' else {}
            await self.message.edit(embed=self._create_preview_embed())

    @discord.ui.button(label="Thumbnail URL", style=discord.ButtonStyle.blurple, row=1)
    async def edit_thumb(self, inter, button):
        if url := await self._prompt(inter, "Enter thumbnail URL (`none` to remove):"):
            self.embed_data["thumbnail"] = {"url": url} if url.lower() != 'none' else {}
            await self.message.edit(embed=self._create_preview_embed())

    @discord.ui.button(label="Submit & Continue", style=discord.ButtonStyle.primary, row=2)
    async def submit(self, inter, button):
        await inter.response.defer()
        for item in self.children: item.disabled = True
        try: await self.message.edit(view=self)
        except Exception: pass

        # NOTE (multi-panel support): this ALWAYS inserts a brand new row
        # in ticket_panels - it never overwrites an existing panel, so a
        # guild can now have many independent panels at once.
        await self.cog.db.execute(
            "INSERT INTO ticket_panels (guild_id, panel_name, panel_channel_id, panel_type, embed_title, "
            "embed_description, embed_color, embed_image_url, embed_thumbnail_url) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.ctx.guild.id, self.panel_name, self.panel_channel.id, self.panel_type, self.embed_data["title"],
             self.embed_data["description"], self.embed_data["color"],
             self.embed_data.get("image", {}).get("url"), self.embed_data.get("thumbnail", {}).get("url"))
        )
        new_panel = await self.cog.db.fetchone(
            "SELECT panel_id FROM ticket_panels WHERE guild_id=? AND panel_channel_id=? ORDER BY panel_id DESC LIMIT 1",
            (self.ctx.guild.id, self.panel_channel.id)
        )
        await CategoryConfigView(self.cog, self.ctx, new_panel['panel_id']).start(inter)
        self.stop()


class CategoryConfigView(discord.ui.View):
    def __init__(self, cog, ctx, panel_id):
        super().__init__(timeout=900)
        self.cog, self.ctx, self.panel_id, self.message, self.categories = cog, ctx, panel_id, None, []
        # NOTE: previously this called a sync _setup_buttons() that ran a DB
        # query (panel_type) whose result was never actually used anywhere.
        # That query is what caused __init__ to try doing network I/O
        # synchronously - removed entirely, no functional change.
        self.add_item(discord.ui.Button(label="Add Category", style=discord.ButtonStyle.success, custom_id="add_cat"))
        self.remove_select = discord.ui.Select(placeholder="Select a category to remove...", custom_id="remove_cat")
        self.add_item(self.remove_select)
        self.add_item(discord.ui.Button(label="Finish Setup", style=discord.ButtonStyle.primary, custom_id="finish_setup", row=2))

    async def start(self, interaction):
        self._update_remove_select()
        await interaction.followup.send(embed=self._update_embed(), view=self, ephemeral=True)
        self.message = await interaction.original_response()

    def _update_embed(self):
        embed = discord.Embed(title="Category Configuration", description="Add or remove ticket categories for your panel.", color=EMBED_COLOR)
        lines = []
        for c in self.categories:
            line = f"{c['emoji'] or ''} {c['name']}"
            if c.get('existing_category_id'):
                line += " *(shared/existing Discord category)*"
            lines.append(line)
        embed.add_field(name="Current Categories", value="\n".join(lines) or "None yet. Click 'Add Category' to begin.")
        return embed

    def _update_remove_select(self):
        self.remove_select.options = [discord.SelectOption(label=c['name'], value=str(i), emoji=c.get('emoji')) for i, c in enumerate(self.categories)] or [discord.SelectOption(label="No categories to remove", value="placeholder")]

    async def _prompt(self, inter: discord.Interaction, prompt_text: str, followup: bool = False):
        send_method = inter.followup.send if followup else inter.response.send_message
        await send_method(prompt_text, ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for("message", check=lambda m: m.author.id == self.ctx.author.id and m.channel.id == inter.channel.id, timeout=120.0)
            try: await msg.delete()
            except discord.HTTPException: pass
            return msg.content
        except asyncio.TimeoutError:
            return None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id: return False
        custom_id = interaction.data["custom_id"]
        if custom_id == "add_cat": await self._add_category_flow(interaction)
        elif custom_id == "remove_cat": await self._remove_category(interaction, interaction.data["values"][0])
        elif custom_id == "finish_setup": await self._finish_setup(interaction)
        return True

    async def _add_category_flow(self, inter: discord.Interaction):
        await inter.response.defer()
        if len(self.categories) >= MAX_CATEGORIES:
            return await inter.followup.send(f"Max {MAX_CATEGORIES} categories reached.", ephemeral=True)

        cat_name = await self._prompt(inter, "Please type the name for the new category (e.g., General Support).", followup=True)
        if not cat_name: return await inter.followup.send("Timed out.", ephemeral=True)

        emoji = await self._prompt(inter, 'Please provide an emoji for the category, or type `skip`.', followup=True)
        if not emoji: return await inter.followup.send("Timed out.", ephemeral=True)
        if emoji.lower() == 'skip': emoji = None

        role_input = await self._prompt(inter, 'Please mention one or more staff roles to ping, separated by spaces (e.g., `@Ticket Support @Moderator`), or type `none`.', followup=True)
        if not role_input: return await inter.followup.send("Timed out.", ephemeral=True)

        role_ids = []
        if role_input.lower() != 'none':
            for role_id_str in re.findall(r'<@&(\d+)>', role_input):
                role_ids.append(int(role_id_str))

        existing_cat_input = await self._prompt(
            inter,
            "If you want this ticket type's channels to go inside an **existing** Discord category, "
            "type that category's name or ID now. Otherwise type `new` and I'll create a fresh category for it.",
            followup=True
        )
        if not existing_cat_input: return await inter.followup.send("Timed out.", ephemeral=True)

        existing_category_id = None
        if existing_cat_input.lower() != 'new':
            found_cat = resolve_existing_category(self.ctx.guild, existing_cat_input)
            if found_cat:
                existing_category_id = found_cat.id
                await inter.followup.send(f"{SUCCESS_EMOJI} Got it, tickets for `{cat_name}` will be created inside **{found_cat.name}**.", ephemeral=True)
            else:
                await inter.followup.send(f"{ERROR_EMOJI} Couldn't find a category with that name/ID, I'll create a new one instead.", ephemeral=True)

        self.categories.append({
            "name": cat_name,
            "emoji": emoji,
            "notified_roles": ",".join(map(str, role_ids)) if role_ids else None,
            "button_style": discord.ButtonStyle.secondary.value,
            "existing_category_id": existing_category_id
        })
        self._update_remove_select()
        await self.message.edit(embed=self._update_embed(), view=self)
        await inter.followup.send(f"Category '{cat_name}' added successfully.", ephemeral=True)

    async def _remove_category(self, inter, value):
        if value == "placeholder": return await inter.response.defer()
        try:
            idx = int(value)
            if 0 <= idx < len(self.categories):
                self.categories.pop(idx)
        except ValueError:
            pass
        self._update_remove_select()
        await self.message.edit(embed=self._update_embed(), view=self)
        await inter.response.defer()

    async def _finish_setup(self, inter):
        if not self.categories: return await inter.response.send_message("Add at least one category.", ephemeral=True)
        await inter.response.defer()
        db, guild_id, panel_id = self.cog.db, self.ctx.guild.id, self.panel_id
        await db.execute("DELETE FROM ticket_categories WHERE panel_id = ?", (panel_id,))
        for cat in self.categories:
            existing_id = cat.get("existing_category_id")
            cat_ch = self.ctx.guild.get_channel(existing_id) if existing_id else None
            if not cat_ch:
                try:
                    cat_ch = await self.ctx.guild.create_category(f"{cat['name']} Tickets", overwrites={self.ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False)})
                except Exception:
                    return await inter.followup.send(f"Can't create category for `{cat['name']}`.", ephemeral=True)
            await db.execute(
                'INSERT INTO ticket_categories (guild_id, panel_id, name, emoji, notified_roles, button_style, discord_category_id) VALUES (?,?,?,?,?,?,?)',
                (guild_id, panel_id, cat['name'], cat['emoji'], cat['notified_roles'], cat['button_style'], cat_ch.id)
            )
        config = await db.fetchone("SELECT * FROM ticket_panels WHERE panel_id=?", (panel_id,))
        panel_ch = self.ctx.guild.get_channel(config['panel_channel_id'])
        panel_embed = discord.Embed(title=config['embed_title'], description=config['embed_description'], color=config['embed_color'])
        if img_url := config['embed_image_url']: panel_embed.set_image(url=img_url)
        if thumb_url := config['embed_thumbnail_url']: panel_embed.set_thumbnail(url=thumb_url)
        final_view = await self.cog.create_panel_view(panel_id)
        msg = await panel_ch.send(embed=panel_embed, view=final_view)
        await db.execute("UPDATE ticket_panels SET panel_message_id = ? WHERE panel_id = ?", (msg.id, panel_id))
        await self.message.edit(content=f"{SUCCESS_EMOJI} Setup complete! Panel **{config['panel_name']}** sent to {panel_ch.mention}.", view=None, embed=None)
        self.stop()


# module-level, lazy - same reasoning as DropdownRoles: get_client() needs a
# running event loop, so it can't be constructed at plain import time.
db = None


class TicketCog(commands.Cog, name="Ticket System"):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        global db
        try:
            if db is None:
                db = TicketDatabase()
            await db.init()
        except Exception:
            print("=" * 60)
            print("[Ticket] FAILED inside db.init() (Turso table setup):")
            traceback.print_exc()
            print("=" * 60)
            raise
        self.db = db

        try:
            await self.load_persistent_views()
        except Exception:
            print("=" * 60)
            print("[Ticket] FAILED inside cog_load() while re-registering persistent views:")
            traceback.print_exc()
            print("=" * 60)
            raise

    async def load_persistent_views(self):
        # 1) Re-register EVERY panel for EVERY guild (a guild can now have
        #    several independent panels, each with its own message).
        for panel in await self.db.fetchall("SELECT panel_id, panel_message_id FROM ticket_panels WHERE panel_message_id IS NOT NULL"):
            if view := await self.create_panel_view(panel['panel_id']):
                self.bot.add_view(view, message_id=panel['panel_message_id'])

        # 2) Re-register the action buttons inside every ticket (open or closed)
        #    so Lock/Unlock/Claim/Close and Reopen/Transcript/Delete keep
        #    working after a restart instead of timing out.
        for t in await self.db.fetchall(
            "SELECT channel_id, category_db_id, closed_at, action_message_id "
            "FROM open_tickets WHERE action_message_id IS NOT NULL"
        ):
            if t['closed_at']:
                view = ClosedTicketActionsView(self, t['channel_id'], t['category_db_id'])
            else:
                view = TicketActionsView(self, t['channel_id'], t['category_db_id'])
            self.bot.add_view(view, message_id=t['action_message_id'])

    async def create_panel_view(self, panel_id):
        config = await self.db.fetchone("SELECT panel_type FROM ticket_panels WHERE panel_id=?", (panel_id,))
        categories = await self.db.fetchall("SELECT * FROM ticket_categories WHERE panel_id=?", (panel_id,))
        if not config or not categories: return None
        view_class = TicketPanelSelect if config['panel_type'] == 'dropdown' else TicketPanelButtons
        view = view_class(self)
        if config['panel_type'] == 'dropdown':
            view.children[0].options = [discord.SelectOption(label=c['name'], value=str(c['category_id']), emoji=c['emoji']) for c in categories]
        else:
            for c in categories:
                view.add_item(discord.ui.Button(label=c['name'], style=discord.ButtonStyle(c['button_style']), emoji=c['emoji'], custom_id=f"create_ticket_{c['category_id']}"))
        return view

    async def refresh_panel(self, panel_id):
        """Re-renders one specific live panel message (embed + buttons/select) after a category or color change."""
        config = await self.db.fetchone("SELECT * FROM ticket_panels WHERE panel_id=?", (panel_id,))
        if not config or not config['panel_channel_id'] or not config['panel_message_id']:
            return None
        guild = self.bot.get_guild(config['guild_id'])
        if not guild: return None
        channel = guild.get_channel(config['panel_channel_id'])
        if not channel: return None
        try:
            msg = await channel.fetch_message(config['panel_message_id'])
        except (discord.NotFound, discord.Forbidden):
            return None
        embed = discord.Embed(title=config['embed_title'], description=config['embed_description'], color=config['embed_color'])
        if config['embed_image_url']: embed.set_image(url=config['embed_image_url'])
        if config['embed_thumbnail_url']: embed.set_thumbnail(url=config['embed_thumbnail_url'])
        view = await self.create_panel_view(panel_id)
        await msg.edit(embed=embed, view=view)
        return msg

    async def resolve_panel(self, guild_id, panel_name):
        """Look up a panel by its friendly name within one guild."""
        return await self.db.fetchone(
            "SELECT * FROM ticket_panels WHERE guild_id=? AND panel_name=?", (guild_id, panel_name)
        )

    # NOTE: cog_unload that used to call self.db.close() was removed - the
    # Turso client is shared across every cog (ReactionRoles, DropdownRoles,
    # Ticket...). Closing it here on unload/reload would break all of them.

    @commands.Cog.listener()
    async def on_interaction(self, inter):
        if inter.type == discord.InteractionType.component and (cid := inter.data.get("custom_id", "")).startswith("create_ticket_"):
            await self.create_ticket_flow(inter, int(cid.split("_")[-1]))

    async def create_ticket_flow(self, inter, cat_id):
        await inter.response.defer(ephemeral=True)
        guild, user = inter.guild, inter.user
        count = await self.db.fetchone("SELECT ticket_count FROM user_ticket_counts WHERE guild_id=? AND user_id=?", (guild.id, user.id))
        if count and count['ticket_count'] >= TICKET_LIMIT_PER_USER:
            return await inter.followup.send(f"You have reached the max of {TICKET_LIMIT_PER_USER} open tickets.", ephemeral=True)

        cat_info = await self.db.fetchone("SELECT * FROM ticket_categories WHERE category_id=?", (cat_id,))
        disc_cat = guild.get_channel(cat_info['discord_category_id']) if cat_info else None
        if not cat_info or not disc_cat:
            return await inter.followup.send("This ticket category has been deleted or is misconfigured.", ephemeral=True)

        max_row = await self.db.fetchone("SELECT MAX(ticket_number) as n FROM open_tickets WHERE guild_id=?", (guild.id,))
        t_num = (max_row['n'] or 0) + 1

        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), user: discord.PermissionOverwrite(view_channel=True), guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True)}

        pings = [user.mention]
        if cat_info['notified_roles']:
            for role_id in cat_info['notified_roles'].split(','):
                if role := guild.get_role(int(role_id)):
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True)
                    pings.append(role.mention)

        try:
            ch = await disc_cat.create_text_channel(name=f"ticket-{t_num:04d}-{user.name.lower()}", overwrites=overwrites)
        except Exception:
            return await inter.followup.send("I lack permissions to create a channel.", ephemeral=True)

        await self.db.execute(
            'INSERT INTO open_tickets (channel_id, ticket_number, guild_id, creator_id, category_db_id, created_at, closed_by_id, closed_at, is_locked, is_claimed, claimed_by_id, action_message_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (ch.id, t_num, guild.id, user.id, cat_id, datetime.now().isoformat(), None, None, False, False, None, None)
        )
        await self.db.execute(
            'INSERT INTO user_ticket_counts VALUES (?,?,1) ON CONFLICT(guild_id,user_id) DO UPDATE SET ticket_count=ticket_count+1',
            (guild.id, user.id)
        )
        await log_ticket_action(self.db, guild, user, "Ticket Created", f"Ticket {ch.mention} by {user.mention} (Category: {cat_info['name']}).")

        ticket_embed = discord.Embed(title=f"Welcome to your Ticket ( #{t_num:04d} )", description="Thank you for reaching out for support. Our staff team has been notified and will be with you as soon as possible.\n\nPlease describe your issue in detail while you wait.", color=EMBED_COLOR)
        ticket_embed.set_image(url=TICKET_CHANNEL_IMAGE_URL)
        action_msg = await ch.send(content=" ".join(pings), embed=ticket_embed, view=TicketActionsView(self, ch.id, cat_id))
        await self.db.execute("UPDATE open_tickets SET action_message_id=? WHERE channel_id=?", (action_msg.id, ch.id))
        await inter.followup.send(f"Your ticket has been successfully created: {ch.mention}", ephemeral=True)

    @commands.hybrid_group(name="ticket", description="Main command group for the ticket system.")
    @commands.guild_only()
    async def ticket(self, ctx):
        if ctx.invoked_subcommand is None: await ctx.send_help(ctx.command)

    @ticket.command(name="setup", description="Create a NEW ticket panel (you can have several panels per server).")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        style="The style of the ticket creation panel.",
        channel="The channel where this panel will be sent.",
        panel_name="A short internal name for this panel (e.g. 'Support', 'Careers') - used to pick it later."
    )
    @app_commands.choices(style=[app_commands.Choice(name="Dropdown Menu", value="dropdown"), app_commands.Choice(name="Buttons", value="button")])
    async def setup(self, ctx, style: app_commands.Choice[str], channel: discord.TextChannel, panel_name: str):
        existing = await self.resolve_panel(ctx.guild.id, panel_name)
        if existing:
            return await ctx.send(f"{ERROR_EMOJI} A panel named `{panel_name}` already exists. Choose a different name.", ephemeral=True)

        panel_count = (await self.db.fetchone("SELECT COUNT(*) as n FROM ticket_panels WHERE guild_id=?", (ctx.guild.id,)))['n']
        if panel_count >= MAX_PANELS_PER_GUILD:
            return await ctx.send(f"{ERROR_EMOJI} Max of {MAX_PANELS_PER_GUILD} panels per server reached.", ephemeral=True)

        await EmbedEditorView(self, ctx, channel, style.value, panel_name).start(ctx.interaction)

    @ticket.command(name="panels", description="List all ticket panels configured on this server.")
    @commands.has_permissions(manage_guild=True)
    async def panels(self, ctx):
        rows = await self.db.fetchall("SELECT panel_name, panel_channel_id FROM ticket_panels WHERE guild_id=?", (ctx.guild.id,))
        if not rows:
            return await ctx.send("No panels have been set up yet. Use `/ticket setup` to create one.", ephemeral=True)
        lines = []
        for r in rows:
            ch = ctx.guild.get_channel(r['panel_channel_id'])
            lines.append(f"**{r['panel_name']}** — {ch.mention if ch else '`channel deleted`'}")
        embed = discord.Embed(title="Ticket Panels", description="\n".join(lines), color=EMBED_COLOR)
        await ctx.send(embed=embed, ephemeral=True)

    @ticket.command(name="delete", description="Delete an entire ticket panel (and its categories).")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(panel="Which panel to delete.")
    async def delete_panel(self, ctx, panel: str):
        await ctx.defer(ephemeral=True)
        guild = ctx.guild
        panel_row = await self.resolve_panel(guild.id, panel)
        if not panel_row:
            return await ctx.send(f"{ERROR_EMOJI} No panel named `{panel}` found.", ephemeral=True)
        panel_id = panel_row['panel_id']

        # امسح الديسكورد كاتيجوريز الخاصة بالبانل ده، بس لو مش مستخدمة في بانل تاني
        cats = await self.db.fetchall("SELECT * FROM ticket_categories WHERE panel_id=?", (panel_id,))
        for cat in cats:
            still_used = await self.db.fetchone(
                "SELECT 1 FROM ticket_categories WHERE guild_id=? AND discord_category_id=? AND panel_id!=?",
                (guild.id, cat['discord_category_id'], panel_id)
            )
            if not still_used and (disc_cat := guild.get_channel(cat['discord_category_id'])):
                try: await disc_cat.delete()
                except Exception: pass

        await self.db.execute("DELETE FROM ticket_categories WHERE panel_id=?", (panel_id,))

        # امسح رسالة البانل نفسها لو موجودة
        if panel_row['panel_channel_id'] and panel_row['panel_message_id']:
            channel = guild.get_channel(panel_row['panel_channel_id'])
            if channel:
                try:
                    msg = await channel.fetch_message(panel_row['panel_message_id'])
                    await msg.delete()
                except Exception:
                    pass

        await self.db.execute("DELETE FROM ticket_panels WHERE panel_id=?", (panel_id,))

        await ctx.send(f"{SUCCESS_EMOJI} Panel `{panel}` and its categories have been deleted.", ephemeral=True)

    @delete_panel.autocomplete('panel')
    async def delete_panel_autocomplete(self, interaction: discord.Interaction, current: str):
        panels = await self.db.fetchall("SELECT panel_name FROM ticket_panels WHERE guild_id=?", (interaction.guild.id,))
        return [app_commands.Choice(name=p['panel_name'], value=p['panel_name']) for p in panels if current.lower() in p['panel_name'].lower()][:25]

    @ticket.group(name="category", description="Add or remove buttons/options from one of your ticket panels.")
    @commands.has_permissions(manage_guild=True)
    async def category(self, ctx):
        if ctx.invoked_subcommand is None: await ctx.send_help(ctx.command)

    async def panel_autocomplete(self, interaction: discord.Interaction, current: str):
        panels = await self.db.fetchall("SELECT panel_name FROM ticket_panels WHERE guild_id=?", (interaction.guild.id,))
        return [app_commands.Choice(name=p['panel_name'], value=p['panel_name']) for p in panels if current.lower() in p['panel_name'].lower()][:25]

    @category.command(name="add", description="Add a new category/button to one of your ticket panels.")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(
        panel="Which panel to add this category to.",
        name="Name shown on the button/option (e.g. Support, Sales).",
        emoji="Emoji to show on the button/option (optional).",
        roles="Mention the staff role(s) to ping/give access, separated by spaces (optional).",
        style="Button color (only used for button-style panels).",
        category="Optional: an EXISTING Discord category to reuse for this ticket type, instead of creating a new one."
    )
    @app_commands.choices(style=[
        app_commands.Choice(name="Grey", value=discord.ButtonStyle.secondary.value),
        app_commands.Choice(name="Blurple", value=discord.ButtonStyle.primary.value),
        app_commands.Choice(name="Green", value=discord.ButtonStyle.success.value),
        app_commands.Choice(name="Red", value=discord.ButtonStyle.danger.value),
    ])
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def category_add(self, ctx, panel: str, name: str, emoji: str = None, roles: str = None, style: app_commands.Choice[int] = None, category: discord.CategoryChannel = None):
        await ctx.defer(ephemeral=True)
        guild = ctx.guild
        panel_row = await self.resolve_panel(guild.id, panel)
        if not panel_row or not panel_row['panel_message_id']:
            return await ctx.send(f"{ERROR_EMOJI} Couldn't find a fully set-up panel named `{panel}`. Run `/ticket setup` first or check `/ticket panels`.", ephemeral=True)
        panel_id = panel_row['panel_id']

        current_count = (await self.db.fetchone("SELECT COUNT(*) as n FROM ticket_categories WHERE panel_id=?", (panel_id,)))['n']
        if current_count >= MAX_CATEGORIES:
            return await ctx.send(f"{ERROR_EMOJI} Max of {MAX_CATEGORIES} categories reached for this panel.", ephemeral=True)

        if await self.db.fetchone("SELECT 1 FROM ticket_categories WHERE panel_id=? AND name=?", (panel_id, name)):
            return await ctx.send(f"{ERROR_EMOJI} A category named `{name}` already exists on panel `{panel}`.", ephemeral=True)

        role_ids = re.findall(r'<@&(\d+)>', roles) if roles else []

        if category is not None:
            cat_ch = category
        else:
            try:
                cat_ch = await guild.create_category(f"{name} Tickets", overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)})
            except discord.Forbidden:
                return await ctx.send(f"{ERROR_EMOJI} I don't have permission to create categories.", ephemeral=True)

        button_style = style.value if style else discord.ButtonStyle.secondary.value
        await self.db.execute(
            "INSERT INTO ticket_categories (guild_id, panel_id, name, emoji, notified_roles, button_style, discord_category_id) VALUES (?,?,?,?,?,?,?)",
            (guild.id, panel_id, name, emoji, ",".join(role_ids) if role_ids else None, button_style, cat_ch.id)
        )

        if await self.refresh_panel(panel_id) is None:
            return await ctx.send(f"{SUCCESS_EMOJI} Category `{name}` created on `{panel}`, but I couldn't find the panel message to update it live.", ephemeral=True)
        await ctx.send(f"{SUCCESS_EMOJI} Category `{name}` added to panel `{panel}` (using Discord category **{cat_ch.name}**) — the panel has been updated.", ephemeral=True)

    @category.command(name="remove", description="Remove a category/button from one of your ticket panels.")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(panel="Which panel to remove the category from.", name="Name of the category to remove.")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def category_remove(self, ctx, panel: str, name: str):
        await ctx.defer(ephemeral=True)
        guild = ctx.guild
        panel_row = await self.resolve_panel(guild.id, panel)
        if not panel_row:
            return await ctx.send(f"{ERROR_EMOJI} No panel named `{panel}` found.", ephemeral=True)
        panel_id = panel_row['panel_id']

        cat = await self.db.fetchone("SELECT * FROM ticket_categories WHERE panel_id=? AND name=?", (panel_id, name))
        if not cat:
            return await ctx.send(f"{ERROR_EMOJI} No category named `{name}` found on panel `{panel}`.", ephemeral=True)

        await self.db.execute("DELETE FROM ticket_categories WHERE category_id=?", (cat['category_id'],))

        still_used = await self.db.fetchone(
            "SELECT 1 FROM ticket_categories WHERE guild_id=? AND discord_category_id=?",
            (guild.id, cat['discord_category_id'])
        )
        if not still_used and (disc_cat := guild.get_channel(cat['discord_category_id'])):
            try: await disc_cat.delete()
            except Exception: pass

        await self.refresh_panel(panel_id)
        await ctx.send(f"{SUCCESS_EMOJI} Category `{name}` removed from panel `{panel}` — the panel has been updated.", ephemeral=True)

    @category_remove.autocomplete('name')
    async def category_remove_autocomplete(self, interaction: discord.Interaction, current: str):
        panel_name = getattr(interaction.namespace, 'panel', None)
        if not panel_name:
            return []
        panel_row = await self.resolve_panel(interaction.guild.id, panel_name)
        if not panel_row:
            return []
        cats = await self.db.fetchall("SELECT name FROM ticket_categories WHERE panel_id=?", (panel_row['panel_id'],))
        return [app_commands.Choice(name=c['name'], value=c['name']) for c in cats if current.lower() in c['name'].lower()][:25]

    @ticket.command(name="color", description="Change one ticket panel's embed color.")
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(panel="Which panel to re-color.", hex_color="Hex color code, e.g. #FF0000")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def color(self, ctx, panel: str, hex_color: str):
        await ctx.defer(ephemeral=True)
        hex_clean = hex_color.strip().lstrip('#')
        if not re.fullmatch(r'[0-9a-fA-F]{6}', hex_clean):
            return await ctx.send(f"{ERROR_EMOJI} Invalid hex color. Use a format like `FF0000` or `#FF0000`.", ephemeral=True)

        panel_row = await self.resolve_panel(ctx.guild.id, panel)
        if not panel_row:
            return await ctx.send(f"{ERROR_EMOJI} No panel named `{panel}` found.", ephemeral=True)

        color_int = int(hex_clean, 16)
        await self.db.execute("UPDATE ticket_panels SET embed_color=? WHERE panel_id=?", (color_int, panel_row['panel_id']))

        if await self.refresh_panel(panel_row['panel_id']) is None:
            return await ctx.send(f"{SUCCESS_EMOJI} Color saved for `{panel}`, but I couldn't find a live panel message to update.", ephemeral=True)
        await ctx.send(f"{SUCCESS_EMOJI} Panel `{panel}` embed color updated to `#{hex_clean.upper()}`.", ephemeral=True)

    # --- Text/slash command versions of the action buttons ---
    async def _dispatch_action(self, ctx: Context, action: str):
        ephemeral = True if ctx.interaction else False
        t = await self.db.fetchone("SELECT * FROM open_tickets WHERE channel_id=?", (ctx.channel.id,))
        if not t:
            return await ctx.send(f"{ERROR_EMOJI} This isn't a ticket channel.", ephemeral=ephemeral)
        if t['closed_at']:
            return await ctx.send(f"{ERROR_EMOJI} This ticket is already closed.", ephemeral=ephemeral)

        # Same permission check as TicketActionsView.interaction_check.
        cat_info = await self.db.fetchone("SELECT notified_roles FROM ticket_categories WHERE category_id=?", (t['category_db_id'],))
        if not cat_info or not cat_info['notified_roles']:
            return await ctx.send(f"{ERROR_EMOJI} This ticket is misconfigured; no staff roles are assigned.", ephemeral=ephemeral)
        allowed_role_ids = {int(r) for r in cat_info['notified_roles'].split(',')}
        user_role_ids = {r.id for r in ctx.author.roles}
        if not user_role_ids.intersection(allowed_role_ids):
            return await ctx.send(f"{ERROR_EMOJI} You do not have the required role to perform this action.", ephemeral=ephemeral)

        guild, channel, user = ctx.guild, ctx.channel, ctx.author

        if action == "lock":
            if t['is_locked']:
                return await ctx.send("This ticket is already locked.", ephemeral=ephemeral)
            creator = guild.get_member(t['creator_id'])
            if creator: await channel.set_permissions(creator, send_messages=False)
            await self.db.execute("UPDATE open_tickets SET is_locked=1 WHERE channel_id=?", (channel.id,))
            await ctx.send(f"{LOCK_EMOJI} Ticket locked by {user.mention}.")
            await log_ticket_action(self.db, guild, user, "Locked", f"{channel.mention}")

        elif action == "unlock":
            if not t['is_locked']:
                return await ctx.send("This ticket is already unlocked.", ephemeral=ephemeral)
            creator = guild.get_member(t['creator_id'])
            if creator: await channel.set_permissions(creator, send_messages=True)
            await self.db.execute("UPDATE open_tickets SET is_locked=0 WHERE channel_id=?", (channel.id,))
            await ctx.send(f"{UNLOCK_EMOJI} Ticket unlocked by {user.mention}.")
            await log_ticket_action(self.db, guild, user, "Unlocked", f"{channel.mention}")

        elif action == "claim":
            if t['is_claimed']:
                return await ctx.send(f"This ticket is already claimed by <@{t['claimed_by_id']}>.", ephemeral=ephemeral)
            await self.db.execute("UPDATE open_tickets SET is_claimed=1, claimed_by_id=? WHERE channel_id=?", (user.id, channel.id))
            await ctx.send(f"{CLAIM_EMOJI} Ticket claimed by {user.mention}. They will now handle this request.")
            await log_ticket_action(self.db, guild, user, "Claimed", f"{channel.mention}")

        elif action == "close":
            if creator := guild.get_member(t['creator_id']):
                await self.db.execute("UPDATE user_ticket_counts SET ticket_count=MAX(0,ticket_count-1) WHERE guild_id=? AND user_id=?", (guild.id, creator.id))
                await channel.set_permissions(creator, send_messages=False, view_channel=False)

            category_info = await self.db.fetchone("SELECT name FROM ticket_categories WHERE category_id=?", (t['category_db_id'],))
            category_name = category_info['name'] if category_info else "Unknown"

            closed_category = await get_or_create_closed_category(self.db, guild)
            if closed_category: await channel.edit(category=closed_category)

            await self.db.execute("UPDATE open_tickets SET closed_by_id=?, closed_at=? WHERE channel_id=?", (user.id, datetime.now().isoformat(), channel.id))
            await log_ticket_action(self.db, guild, user, "Closed", f"Ticket {channel.mention} (Category: {category_name})")

            closed_embed = discord.Embed(
                title="Ticket Closed",
                description=f"This ticket has been officially closed and archived by {user.mention}.\nThe user has been removed from the channel.\n\nStaff can use the buttons below to reopen, create a transcript, or permanently delete the channel.",
                color=EMBED_COLOR,
                timestamp=datetime.now()
            )
            closed_embed.add_field(name="Ticket Creator", value=f"<@{t['creator_id']}>", inline=True)
            closed_embed.add_field(name="Closed By", value=user.mention, inline=True)
            closed_embed.add_field(name="Original Category", value=category_name, inline=True)

            closed_msg = await channel.send(embed=closed_embed, view=ClosedTicketActionsView(self, channel.id, t['category_db_id']))
            await self.db.execute("UPDATE open_tickets SET action_message_id=? WHERE channel_id=?", (closed_msg.id, channel.id))

            # Try to disable the buttons on the original action message too
            # (the button version does this via i.message.edit(view=None)).
            if t.get('action_message_id'):
                try:
                    old_msg = await channel.fetch_message(t['action_message_id'])
                    await old_msg.edit(view=None)
                except Exception:
                    pass

            if ctx.interaction:
                pass  # main response already sent above via ctx.send
            else:
                await ctx.send("Ticket successfully closed and archived.")

    @ticket.command(name="close", description="Close the current ticket channel.")
    @commands.has_permissions(manage_channels=True)
    async def close(self, ctx): await self._dispatch_action(ctx, "close")

    @ticket.command(name="lock", description="Lock the ticket, preventing the user from sending messages.")
    @commands.has_permissions(manage_channels=True)
    async def lock(self, ctx): await self._dispatch_action(ctx, "lock")

    @ticket.command(name="unlock", description="Unlock the ticket, allowing the user to send messages again.")
    @commands.has_permissions(manage_channels=True)
    async def unlock(self, ctx): await self._dispatch_action(ctx, "unlock")

    @ticket.command(name="claim", description="Claim the ticket to notify others that you are handling it.")
    @commands.has_permissions(manage_channels=True)
    async def claim(self, ctx): await self._dispatch_action(ctx, "claim")

    @ticket.command(name="transcript", description="Generate a transcript of a closed ticket.")
    @commands.has_permissions(manage_channels=True)
    async def transcript(self, ctx):
        if not ctx.interaction:
            return await ctx.send("Please use the slash command version of this command.")
        t = await self.db.fetchone("SELECT category_db_id FROM open_tickets WHERE channel_id=?", (ctx.channel.id,))
        if not t:
            return await ctx.send(f"{ERROR_EMOJI} This isn't a ticket channel.", ephemeral=True)
        await ClosedTicketActionsView(self, ctx.channel.id, t['category_db_id'])._generate_transcript(ctx.interaction, False)


class TicketPanelSelect(discord.ui.View):
    def __init__(self, cog): super().__init__(timeout=None); self.cog = cog

    @discord.ui.select(placeholder="Select a category to open a ticket...", custom_id="ticket_panel_select")
    async def select_ticket(self, inter, select): await self.cog.create_ticket_flow(inter, int(select.values[0]))


class TicketPanelButtons(discord.ui.View):
    def __init__(self, cog): super().__init__(timeout=None); self.cog = cog


class TicketActionsView(discord.ui.View):
    def __init__(self, cog, ch_id, cat_id):
        super().__init__(timeout=None)
        self.cog, self.ch_id, self.cat_id = cog, ch_id, cat_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cat_info = await self.cog.db.fetchone("SELECT notified_roles FROM ticket_categories WHERE category_id=?", (self.cat_id,))
        if not cat_info or not cat_info['notified_roles']:
            await interaction.response.send_message("This ticket is misconfigured; no staff roles are assigned.", ephemeral=True)
            return False

        allowed_role_ids = {int(r_id) for r_id in cat_info['notified_roles'].split(',')}
        user_role_ids = {role.id for role in interaction.user.roles}

        if not user_role_ids.intersection(allowed_role_ids):
            await interaction.response.send_message("You do not have the required role to perform this action.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Lock", emoji=LOCK_EMOJI, custom_id="t_lock", style=discord.ButtonStyle.secondary)
    async def b_lock(self, i, b):
        t = await self.cog.db.fetchone("SELECT * FROM open_tickets WHERE channel_id=?", (self.ch_id,))
        if t['is_locked']: return await i.response.send_message("This ticket is already locked.", ephemeral=True)
        creator = i.guild.get_member(t['creator_id'])
        if creator: await i.channel.set_permissions(creator, send_messages=False)
        await self.cog.db.execute("UPDATE open_tickets SET is_locked=1 WHERE channel_id=?", (self.ch_id,))
        await i.response.send_message(f"{LOCK_EMOJI} Ticket locked by {i.user.mention}.")
        await log_ticket_action(self.cog.db, i.guild, i.user, "Locked", f"{i.channel.mention}")

    @discord.ui.button(label="Unlock", emoji=UNLOCK_EMOJI, custom_id="t_unlock", style=discord.ButtonStyle.secondary)
    async def b_unlock(self, i, b):
        t = await self.cog.db.fetchone("SELECT * FROM open_tickets WHERE channel_id=?", (self.ch_id,))
        if not t['is_locked']: return await i.response.send_message("This ticket is already unlocked.", ephemeral=True)
        creator = i.guild.get_member(t['creator_id'])
        if creator: await i.channel.set_permissions(creator, send_messages=True)
        await self.cog.db.execute("UPDATE open_tickets SET is_locked=0 WHERE channel_id=?", (self.ch_id,))
        await i.response.send_message(f"{UNLOCK_EMOJI} Ticket unlocked by {i.user.mention}.")
        await log_ticket_action(self.cog.db, i.guild, i.user, "Unlocked", f"{i.channel.mention}")

    @discord.ui.button(label="Claim", emoji=CLAIM_EMOJI, custom_id="t_claim", style=discord.ButtonStyle.primary)
    async def b_claim(self, i, b):
        t = await self.cog.db.fetchone("SELECT * FROM open_tickets WHERE channel_id=?", (self.ch_id,))
        if t['is_claimed']: return await i.response.send_message(f"This ticket is already claimed by <@{t['claimed_by_id']}>.", ephemeral=True)
        await self.cog.db.execute("UPDATE open_tickets SET is_claimed=1, claimed_by_id=? WHERE channel_id=?", (i.user.id, self.ch_id))
        await i.response.send_message(f"{CLAIM_EMOJI} Ticket claimed by {i.user.mention}. They will now handle this request.")
        await log_ticket_action(self.cog.db, i.guild, i.user, "Claimed", f"{i.channel.mention}")

    @discord.ui.button(label="Close", emoji=CLOSE_EMOJI, style=discord.ButtonStyle.danger, custom_id="t_close")
    async def b_close(self, i, b):
        await i.response.defer(ephemeral=True)
        t = await self.cog.db.fetchone("SELECT * FROM open_tickets WHERE channel_id=?", (self.ch_id,))
        creator = i.guild.get_member(t['creator_id'])
        if creator:
            await self.cog.db.execute("UPDATE user_ticket_counts SET ticket_count=MAX(0,ticket_count-1) WHERE guild_id=? AND user_id=?", (i.guild.id, creator.id))
            await i.channel.set_permissions(creator, send_messages=False, view_channel=False)

        category_info = await self.cog.db.fetchone("SELECT name FROM ticket_categories WHERE category_id=?", (self.cat_id,))
        category_name = category_info['name'] if category_info else "Unknown"

        closed_category = await get_or_create_closed_category(self.cog.db, i.guild)
        if closed_category: await i.channel.edit(category=closed_category)

        await self.cog.db.execute("UPDATE open_tickets SET closed_by_id=?, closed_at=? WHERE channel_id=?", (i.user.id, datetime.now().isoformat(), self.ch_id))
        await log_ticket_action(self.cog.db, i.guild, i.user, "Closed", f"Ticket {i.channel.mention} (Category: {category_name})")

        closed_embed = discord.Embed(
            title="Ticket Closed",
            description=f"This ticket has been officially closed and archived by {i.user.mention}.\nThe user has been removed from the channel.\n\nStaff can use the buttons below to reopen, create a transcript, or permanently delete the channel.",
            color=EMBED_COLOR,
            timestamp=datetime.now()
        )
        closed_embed.add_field(name="Ticket Creator", value=f"<@{t['creator_id']}>", inline=True)
        closed_embed.add_field(name="Closed By", value=i.user.mention, inline=True)
        closed_embed.add_field(name="Original Category", value=category_name, inline=True)

        closed_msg = await i.channel.send(embed=closed_embed, view=ClosedTicketActionsView(self.cog, self.ch_id, self.cat_id))
        await self.cog.db.execute("UPDATE open_tickets SET action_message_id=? WHERE channel_id=?", (closed_msg.id, self.ch_id))
        await i.message.edit(view=None)
        await i.followup.send("Ticket successfully closed and archived.", ephemeral=True)
        self.stop()


class ClosedTicketActionsView(discord.ui.View):
    def __init__(self, cog, ch_id, cat_id):
        super().__init__(timeout=None)
        self.cog, self.ch_id, self.cat_id = cog, ch_id, cat_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cat_info = await self.cog.db.fetchone("SELECT notified_roles FROM ticket_categories WHERE category_id=?", (self.cat_id,))
        if not cat_info or not cat_info['notified_roles']: return False
        allowed_role_ids = {int(r_id) for r_id in cat_info['notified_roles'].split(',')}
        user_role_ids = {role.id for role in interaction.user.roles}
        if not user_role_ids.intersection(allowed_role_ids):
            await interaction.response.send_message("You do not have the required role for this action.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Reopen", emoji=REOPEN_EMOJI, style=discord.ButtonStyle.success, custom_id="t_reopen")
    async def b_reopen(self, i: discord.Interaction, button: discord.ui.Button):
        await i.response.defer(ephemeral=True)
        t = await self.cog.db.fetchone("SELECT * FROM open_tickets WHERE channel_id=?", (self.ch_id,))
        cat_info = await self.cog.db.fetchone("SELECT discord_category_id FROM ticket_categories WHERE category_id=?", (self.cat_id,))

        original_category = i.guild.get_channel(cat_info['discord_category_id']) if cat_info else None
        if original_category: await i.channel.edit(category=original_category)

        creator = i.guild.get_member(t['creator_id'])
        if creator:
            await i.channel.set_permissions(creator, view_channel=True, send_messages=True)
            await self.cog.db.execute("INSERT INTO user_ticket_counts VALUES (?,?,1) ON CONFLICT(guild_id,user_id) DO UPDATE SET ticket_count=ticket_count+1", (i.guild.id, creator.id))

        await self.cog.db.execute("UPDATE open_tickets SET closed_by_id=NULL, closed_at=NULL WHERE channel_id=?", (self.ch_id,))

        reopen_embed = discord.Embed(title="Ticket Reopened", description=f"This ticket has been reopened by {i.user.mention}.", color=EMBED_COLOR)
        reopen_msg = await i.channel.send(embed=reopen_embed, view=TicketActionsView(self.cog, self.ch_id, self.cat_id))
        await self.cog.db.execute("UPDATE open_tickets SET action_message_id=? WHERE channel_id=?", (reopen_msg.id, self.ch_id))
        await i.message.edit(view=None)
        await log_ticket_action(self.cog.db, i.guild, i.user, "Reopened", f"{i.channel.mention}")
        self.stop()

    @discord.ui.button(label="Transcript", emoji=TRANSCRIPT_EMOJI, style=discord.ButtonStyle.primary, custom_id="t_transcript")
    async def b_transcript(self, i, b): await self._generate_transcript(i, False)

    @discord.ui.button(label="Delete", emoji=DELETE_EMOJI, style=discord.ButtonStyle.danger, custom_id="t_delete")
    async def b_delete(self, i, b): await self._generate_transcript(i, True)

    async def _generate_transcript(self, i, delete_after):
        await i.response.defer(ephemeral=True, thinking=True)
        ch = i.guild.get_channel(self.ch_id)
        if not ch: return await i.followup.send("Channel not found.", ephemeral=True)

        messages = [m async for m in ch.history(limit=None, oldest_first=True)]
        content = f"Transcript for ticket #{ch.name} in {i.guild.name}\n\n"
        for m in messages:
            content += f"[{m.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {m.author.display_name}: {m.clean_content}\n"
            for attachment in m.attachments: content += f"  [Attachment: {attachment.url}]\n"
        file = discord.File(io.BytesIO(content.encode()), filename=f"transcript-{ch.name}.txt")

        try:
            await i.user.send(f"Transcript for ticket {ch.mention} in {i.guild.name}:", file=file)
            await i.followup.send("Transcript sent to your DMs.", ephemeral=True)
        except Exception:
            await i.followup.send("Could not DM you the transcript. Do you have DMs disabled?", file=file, ephemeral=True)

        if delete_after:
            await i.followup.send("This ticket channel will be permanently deleted in 10 seconds...", ephemeral=True)
            await log_ticket_action(self.cog.db, i.guild, i.user, "Deletion Scheduled", f"{ch.mention}")
            await asyncio.sleep(10)
            await ch.delete()
            await self.cog.db.execute("DELETE FROM open_tickets WHERE channel_id=?", (self.ch_id,))


async def setup(bot):
    try:
        await bot.add_cog(TicketCog(bot))
    except Exception:
        print("=" * 60)
        print("[Ticket] FAILED inside setup(bot) / bot.add_cog():")
        traceback.print_exc()
        print("=" * 60)
        raise
        
