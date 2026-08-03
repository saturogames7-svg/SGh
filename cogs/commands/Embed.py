import json
import logging
import discord
from utils.emoji import CROSS, TICK
from discord.ext import commands
from discord import ui
import asyncio
from utils.Tools import *
import re

from utils.turso_db import execute

logger = logging.getLogger(__name__)


class TemplateDB:
    """Stores/retrieves embed templates in Turso, keyed by (guild_id, name).

    Goes through utils.turso_db.execute() so this shares the bot's one
    persistent libsql_client connection and its reconnect-on-failure logic,
    instead of opening a connection of its own.
    """

    async def ensure_table(self):
        await execute(
            """
            CREATE TABLE IF NOT EXISTS embed_templates (
                guild_id TEXT NOT NULL,
                name TEXT NOT NULL,
                data TEXT NOT NULL,
                created_by TEXT,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (guild_id, name)
            )
            """
        )

    async def save(self, guild_id, name, embed_data, created_by):
        payload = json.dumps(embed_data)
        await execute(
            """
            INSERT INTO embed_templates (guild_id, name, data, created_by, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(guild_id, name) DO UPDATE SET
                data = excluded.data,
                created_by = excluded.created_by,
                updated_at = excluded.updated_at
            """,
            [str(guild_id), name, payload, str(created_by)],
        )

    async def load(self, guild_id, name):
        rs = await execute(
            "SELECT data FROM embed_templates WHERE guild_id = ? AND name = ?",
            [str(guild_id), name],
        )
        if not rs.rows:
            return None
        return json.loads(rs.rows[0]["data"])

    async def list_names(self, guild_id):
        rs = await execute(
            "SELECT name FROM embed_templates WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
            [str(guild_id)],
        )
        return [row["name"] for row in rs.rows]

    async def delete(self, guild_id, name):
        await execute(
            "DELETE FROM embed_templates WHERE guild_id = ? AND name = ?",
            [str(guild_id), name],
        )


class EmbedBuilder(ui.LayoutView):
    def __init__(self, ctx, target_message: discord.Message = None, db: TemplateDB = None):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.message = None
        self.db = db
        # If set, "Send/Update Embed" edits this existing message instead of
        # asking for a channel and sending a brand new embed.
        self.target_message = target_message
        self.embed_data = {
            "title": "Edit your Embed!",
            "description": "Select Options from the menu below to customize.",
            "color": 0xFF0000,
            "thumbnail": None,
            "image": None,
            "footer_text": None,
            "footer_icon": None,
            "author_text": None,
            "author_icon": None,
            "fields": []
        }

        if target_message and target_message.embeds:
            self._load_from_embed(target_message.embeds[0])

        self.container = ui.Container(accent_color=None)
        self._build_view()
        self.add_item(self.container)

    @staticmethod
    def _proxy_url(proxy):
        """Safely pull a .url off an EmbedProxy that might be empty."""
        return getattr(proxy, "url", None)

    def _load_from_embed(self, embed: discord.Embed):
        """Populate embed_data from an existing discord.Embed, so an
        already-sent embed can be loaded into the builder for editing."""
        d = self.embed_data
        d["title"] = embed.title if embed.title else None
        d["description"] = embed.description if embed.description else None
        d["color"] = embed.color.value if embed.color else 0xFF0000
        d["thumbnail"] = self._proxy_url(embed.thumbnail)
        d["image"] = self._proxy_url(embed.image)
        d["footer_text"] = embed.footer.text if embed.footer else None
        d["footer_icon"] = self._proxy_url(embed.footer) if embed.footer else None
        d["author_text"] = embed.author.name if embed.author else None
        d["author_icon"] = self._proxy_url(embed.author) if embed.author else None
        d["fields"] = [{"name": f.name, "value": f.value} for f in embed.fields]

    def _get_preview(self):
        d = self.embed_data
        lines = []
        if d["title"]:
            lines.append(f"**Title:** {d['title']}")
        if d["description"]:
            lines.append(f"**Description:** {d['description']}")
        if d["color"]:
            lines.append(f"**Color:** `#{d['color']:06X}`")
        if d["thumbnail"]:
            lines.append(f"**Thumbnail:** [Set]({d['thumbnail']})")
        if d["image"]:
            lines.append(f"**Image:** [Set]({d['image']})")
        if d["footer_text"]:
            lines.append(f"**Footer:** {d['footer_text']}")
        if d["footer_icon"]:
            lines.append(f"**Footer Icon:** [Set]({d['footer_icon']})")
        if d["author_text"]:
            lines.append(f"**Author:** {d['author_text']}")
        if d["author_icon"]:
            lines.append(f"**Author Icon:** [Set]({d['author_icon']})")
        if d["fields"]:
            for i, f in enumerate(d["fields"]):
                lines.append(f"**Field {i+1}:** {f['name']} — {f['value']}")
        return "\n".join(lines) if lines else "No properties set yet."

    def _build_view(self):
        self.container.clear_items()

        header = "# Embed Editor" if self.target_message else "# Embed Builder"
        self.container.add_item(ui.TextDisplay(header))
        self.container.add_item(ui.Separator())
        self.container.add_item(ui.TextDisplay(self._get_preview()))
        self.container.add_item(ui.Separator())
        self.container.add_item(ui.TextDisplay("*Select an option to edit. Respond within 30 seconds.*"))

        # Select menu
        select = ui.Select(
            placeholder="Choose an option to edit the Embed",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="Title", description="Edit the title"),
                discord.SelectOption(label="Description", description="Edit the description"),
                discord.SelectOption(label="Add Field", description="Add a field"),
                discord.SelectOption(label="Edit Field", description="Edit an existing field"),
                discord.SelectOption(label="Remove Field", description="Remove an existing field"),
                discord.SelectOption(label="Color", description="Edit the color (hex)"),
                discord.SelectOption(label="Thumbnail", description="Set thumbnail URL"),
                discord.SelectOption(label="Image", description="Set image URL"),
                discord.SelectOption(label="Footer Text", description="Edit footer text"),
                discord.SelectOption(label="Footer Icon", description="Set footer icon URL"),
                discord.SelectOption(label="Author Text", description="Edit author text"),
                discord.SelectOption(label="Author Icon", description="Set author icon URL"),
            ]
        )
        select.callback = self._select_callback
        self.container.add_item(ui.ActionRow(select))

        # Buttons
        send_label = "Update Embed" if self.target_message else "Send Embed"
        send_btn = ui.Button(label=send_label, emoji=TICK, style=discord.ButtonStyle.success)
        send_btn.callback = self._send_callback

        save_tpl_btn = ui.Button(label="Save Template", style=discord.ButtonStyle.primary)
        save_tpl_btn.callback = self._save_template_callback

        cancel_btn = ui.Button(label="Cancel Setup", emoji=CROSS, style=discord.ButtonStyle.danger)
        cancel_btn.callback = self._cancel_callback

        self.container.add_item(ui.ActionRow(send_btn, save_tpl_btn, cancel_btn))

    def _build_embed(self):
        """Build a real discord.Embed from stored data"""
        d = self.embed_data
        embed = discord.Embed(
            title=d["title"],
            description=d["description"],
            color=d["color"]
        )
        if d["thumbnail"]:
            embed.set_thumbnail(url=d["thumbnail"])
        if d["image"]:
            embed.set_image(url=d["image"])
        if d["footer_text"] or d["footer_icon"]:
            embed.set_footer(text=d["footer_text"] or "", icon_url=d["footer_icon"] or discord.Embed.Empty)
        if d["author_text"] or d["author_icon"]:
            embed.set_author(name=d["author_text"] or "", icon_url=d["author_icon"] or discord.Embed.Empty)
        for field in d["fields"]:
            embed.add_field(name=field["name"], value=field["value"], inline=False)
        return embed

    def _fields_list_text(self):
        """Numbered list of current fields, used when asking the user to pick one."""
        return "\n".join(f"`{i+1}.` **{f['name']}** — {f['value']}" for i, f in enumerate(self.embed_data["fields"]))

    async def _select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This builder doesn't belong to you.", ephemeral=True)
            return
        await interaction.response.defer()

        value = interaction.data["values"][0]

        def chk(m):
            return m.channel.id == self.ctx.channel.id and m.author.id == self.ctx.author.id

        # --- Edit Field / Remove Field need the list of existing fields up front,
        # so they're handled separately from the generic single-prompt flow below.
        if value in ("Edit Field", "Remove Field"):
            if not self.embed_data["fields"]:
                await self.ctx.send("There are no fields yet to " + ("edit." if value == "Edit Field" else "remove."))
                return

            action_word = "edit" if value == "Edit Field" else "remove"
            await self.ctx.send(f"Which field number do you want to {action_word}?\n{self._fields_list_text()}")

            try:
                idx_msg = await self.ctx.bot.wait_for("message", timeout=30, check=chk)
                idx = int(idx_msg.content.strip()) - 1
                if not (0 <= idx < len(self.embed_data["fields"])):
                    await self.ctx.send("That's not a valid field number.")
                    return
            except ValueError:
                await self.ctx.send("That's not a valid field number.")
                return
            except asyncio.TimeoutError:
                await self.ctx.send("Timed Out.")
                return

            if value == "Remove Field":
                removed = self.embed_data["fields"].pop(idx)
                await self.ctx.send(f"Removed field **{removed['name']}**.")
            else:
                await self.ctx.send("Enter the new **Field title**:")
                try:
                    name_msg = await self.ctx.bot.wait_for("message", timeout=30, check=chk)
                except asyncio.TimeoutError:
                    await self.ctx.send("Timed Out.")
                    return
                await self.ctx.send("Enter the new **Field value**:")
                try:
                    val_msg = await self.ctx.bot.wait_for("message", timeout=30, check=chk)
                except asyncio.TimeoutError:
                    await self.ctx.send("Timed Out.")
                    return
                self.embed_data["fields"][idx] = {"name": name_msg.content, "value": val_msg.content}

            self._build_view()
            await self.message.edit(view=self)
            return

        prompts = {
            "Title": "Enter the **Title** of the embed:",
            "Description": "Enter the **Description** of the embed:",
            "Color": "Enter the color as a hex value (e.g., `#FF0000`):",
            "Thumbnail": "Enter the **Thumbnail URL**:",
            "Image": "Enter the **Image URL**:",
            "Footer Text": "Enter the **Footer text**:",
            "Footer Icon": "Enter the **Footer icon URL**:",
            "Author Text": "Enter the **Author text**:",
            "Author Icon": "Enter the **Author icon URL**:",
            "Add Field": "Enter the **Field title**:",
        }

        await self.ctx.send(prompts.get(value, "Enter a value:"))

        try:
            msg = await self.ctx.bot.wait_for("message", timeout=30, check=chk)

            if value == "Title":
                self.embed_data["title"] = msg.content
            elif value == "Description":
                self.embed_data["description"] = msg.content
            elif value == "Color":
                try:
                    self.embed_data["color"] = int(msg.content.strip("#"), 16)
                except ValueError:
                    await self.ctx.send("Invalid hex color. Please try again.")
                    return
            elif value == "Thumbnail":
                if not msg.content.startswith("http"):
                    await self.ctx.send("Invalid URL format.")
                    return
                self.embed_data["thumbnail"] = msg.content
            elif value == "Image":
                if not msg.content.startswith("http"):
                    await self.ctx.send("Invalid URL format.")
                    return
                self.embed_data["image"] = msg.content
            elif value == "Footer Text":
                self.embed_data["footer_text"] = msg.content
            elif value == "Footer Icon":
                if not msg.content.startswith("http"):
                    await self.ctx.send("Invalid URL format.")
                    return
                self.embed_data["footer_icon"] = msg.content
            elif value == "Author Text":
                self.embed_data["author_text"] = msg.content
            elif value == "Author Icon":
                if not msg.content.startswith("http"):
                    await self.ctx.send("Invalid URL format.")
                    return
                self.embed_data["author_icon"] = msg.content
            elif value == "Add Field":
                field_name = msg.content
                await self.ctx.send("Enter the **Field value**:")
                val_msg = await self.ctx.bot.wait_for("message", timeout=30, check=chk)
                self.embed_data["fields"].append({"name": field_name, "value": val_msg.content})

            # Rebuild and update
            self._build_view()
            await self.message.edit(view=self)

        except asyncio.TimeoutError:
            await self.ctx.send("Timed Out.")

    async def _send_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This builder doesn't belong to you.", ephemeral=True)
            return
        await interaction.response.defer()

        embed = self._build_embed()

        # --- Edit mode: update the original message in place, no channel prompt needed.
        if self.target_message:
            try:
                await self.target_message.edit(embed=embed)
            except discord.Forbidden:
                await self.ctx.send(f"{CROSS} I don't have permission to edit that message.")
                return
            except discord.NotFound:
                await self.ctx.send(f"{CROSS} That message no longer exists.")
                return
            except discord.HTTPException:
                await self.ctx.send(f"{CROSS} Failed to update the embed.")
                return

            self.container.clear_items()
            self.container.add_item(ui.TextDisplay(f"# {TICK} Embed Updated"))
            self.container.add_item(ui.Separator())
            self.container.add_item(ui.TextDisplay(f"Successfully updated [the embed]({self.target_message.jump_url})."))
            await self.message.edit(view=self)
            return

        # --- Normal mode: ask which channel to send the new embed to.
        await self.ctx.send("Mention the **channel** where you want to send this embed:")

        def chk(m):
            return m.channel.id == self.ctx.channel.id and m.author.id == self.ctx.author.id

        try:
            msg = await self.ctx.bot.wait_for("message", timeout=30, check=chk)
            chnl = msg.channel_mentions[0]
            await chnl.send(embed=embed)

            # Show success
            self.container.clear_items()
            self.container.add_item(ui.TextDisplay(f"# {TICK} Embed Sent"))
            self.container.add_item(ui.Separator())
            self.container.add_item(ui.TextDisplay(f"Successfully sent the embed to {chnl.mention}"))
            await self.message.edit(view=self)

        except asyncio.TimeoutError:
            await self.ctx.send("Timed Out.")
        except (IndexError, AttributeError):
            await self.ctx.send("Please mention a valid channel.")

    async def _save_template_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This builder doesn't belong to you.", ephemeral=True)
            return
        await interaction.response.defer()

        if self.db is None:
            await self.ctx.send(f"{CROSS} Template storage isn't configured.")
            return

        await self.ctx.send("Enter a **name** to save this template as:")

        def chk(m):
            return m.channel.id == self.ctx.channel.id and m.author.id == self.ctx.author.id

        try:
            msg = await self.ctx.bot.wait_for("message", timeout=30, check=chk)
        except asyncio.TimeoutError:
            await self.ctx.send("Timed Out.")
            return

        name = msg.content.strip()
        if not name:
            await self.ctx.send(f"{CROSS} Invalid name.")
            return

        try:
            await self.db.save(self.ctx.guild.id, name, self.embed_data, self.ctx.author.id)
        except Exception:
            await self.ctx.send(f"{CROSS} Failed to save the template — check the database connection.")
            return

        await self.ctx.send(f"{TICK} Saved template **{name}**. Load it later with `loadtemplate {name}`.")

    async def _cancel_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This builder doesn't belong to you.", ephemeral=True)
            return
        self.container.clear_items()
        self.container.add_item(ui.TextDisplay("# Embed Builder"))
        self.container.add_item(ui.Separator())
        self.container.add_item(ui.TextDisplay(f"{CROSS} Embed setup cancelled."))
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        try:
            self.container.clear_items()
            self.container.add_item(ui.TextDisplay("# Embed Builder"))
            self.container.add_item(ui.Separator())
            self.container.add_item(ui.TextDisplay("⏰ Builder timed out. Use the command again."))
            await self.message.edit(view=self)
        except:
            pass


class Embed(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = TemplateDB()

    async def cog_load(self):
        try:
            await self.db.ensure_table()
        except Exception as e:
            logger.error(f"Failed to ensure embed_templates table exists: {e}")

    @commands.hybrid_command(name="embed")
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 7, commands.BucketType.user)
    @commands.has_permissions(manage_messages=True)
    async def _embed(self, ctx):
        view = EmbedBuilder(ctx, db=self.db)
        view.message = await ctx.send(view=view)

    async def _resolve_message(self, ctx, message_ref: str = None):
        """Resolve a target message either from a reply, or from a
        message ID / jump-URL passed as an argument."""
        if message_ref:
            match = re.search(r'(\d+)$', message_ref.strip())
            if not match:
                return None
            msg_id = int(match.group(1))
            try:
                return await ctx.channel.fetch_message(msg_id)
            except (discord.NotFound, discord.HTTPException):
                return None

        if ctx.message.reference:
            try:
                return ctx.message.reference.resolved or await ctx.channel.fetch_message(
                    ctx.message.reference.message_id
                )
            except (discord.NotFound, discord.HTTPException):
                return None

        return None

    @commands.hybrid_command(
        name="editembed",
        aliases=["embededit"],
        description="Edit an embed the bot already sent, either by replying to it or giving its ID/link."
    )
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 7, commands.BucketType.user)
    @commands.has_permissions(manage_messages=True)
    async def _edit_embed(self, ctx, message: str = None):
        target = await self._resolve_message(ctx, message)

        if target is None:
            await ctx.send(f"{CROSS} Reply to the embed's message, or give its message ID / link.")
            return

        if not target.embeds:
            await ctx.send(f"{CROSS} That message doesn't have an embed.")
            return

        if target.author.id != self.bot.user.id:
            await ctx.send(f"{CROSS} I can only edit embeds that I sent myself.")
            return

        view = EmbedBuilder(ctx, target_message=target, db=self.db)
        view.message = await ctx.send(view=view)

    @commands.hybrid_command(
        name="loadtemplate",
        aliases=["templateembed"],
        description="Load a saved embed template into the builder to edit and send."
    )
    @blacklist_check()
    @ignore_check()
    @commands.cooldown(1, 7, commands.BucketType.user)
    @commands.has_permissions(manage_messages=True)
    async def _load_template(self, ctx, name: str):
        try:
            data = await self.db.load(ctx.guild.id, name)
        except Exception:
            await ctx.send(f"{CROSS} Couldn't reach the database.")
            return

        if data is None:
            await ctx.send(f"{CROSS} No template named **{name}** found.")
            return

        view = EmbedBuilder(ctx, db=self.db)
        view.embed_data = data
        view._build_view()
        view.message = await ctx.send(view=view)

    @commands.hybrid_command(name="templates", description="List saved embed templates for this server.")
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(manage_messages=True)
    async def _list_templates(self, ctx):
        try:
            names = await self.db.list_names(ctx.guild.id)
        except Exception:
            await ctx.send(f"{CROSS} Couldn't reach the database.")
            return

        if not names:
            await ctx.send("No saved templates yet.")
            return

        formatted = "\n".join(f"• {n}" for n in names)
        await ctx.send(f"**Saved templates:**\n{formatted}")

    @commands.hybrid_command(name="deletetemplate", description="Delete a saved embed template.")
    @blacklist_check()
    @ignore_check()
    @commands.has_permissions(manage_messages=True)
    async def _delete_template(self, ctx, name: str):
        try:
            existing = await self.db.load(ctx.guild.id, name)
        except Exception:
            await ctx.send(f"{CROSS} Couldn't reach the database.")
            return

        if existing is None:
            await ctx.send(f"{CROSS} No template named **{name}** found.")
            return

        await self.db.delete(ctx.guild.id, name)
        await ctx.send(f"{TICK} Deleted template **{name}**.")


async def setup(bot):
    await bot.add_cog(Embed(bot))
    
