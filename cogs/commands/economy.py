import discord
from utils.emoji import CROSS, TICK, MONEY, HANDSHAKE
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context
import traceback
from datetime import datetime
from utils.config import *
from utils.turso_db import get_client

# --- Configurable Variables ---
EMBED_COLOR = 0xFF0000
STORE_EMBED_COLOR = 0x3498DB
COIN_EMOJI = "🪙"
CURRENCY_NAME = "Coins"
STARTING_BALANCE = 0

# --- Emoji Variables ---
SUCCESS_EMOJI = TICK
ERROR_EMOJI = CROSS
MONEY_EMOJI = MONEY
PAY_EMOJI = HANDSHAKE

# --- Constants ---
MAX_SHOP_ITEMS_PER_GUILD = 50
LEADERBOARD_PAGE_SIZE = 10


def fmt_amount(amount: int) -> str:
    """Format an integer amount with the coin emoji and thousands separators."""
    return f"{COIN_EMOJI} {amount:,}"


# --- Store buttons: instant-buy directly from the /economy store list embed.
# Discord hard-caps components at 25 per message (5 rows x 5 buttons), so if
# the store has more than 25 items only the first 25 (cheapest, since
# store_list orders by price ASC) get a button - the rest are still
# purchasable with /economy store buy <name>. ---
class StoreBuyButton(discord.ui.Button):
    def __init__(self, item: dict):
        super().__init__(
            label=f"{item['name']} — {item['price']:,}",
            emoji=COIN_EMOJI,
            style=discord.ButtonStyle.green,
        )
        self.item_id = item["item_id"]

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Economy")
        if not cog:
            return await interaction.response.send_message(
                f"{ERROR_EMOJI} Store is unavailable right now.", ephemeral=True
            )

        # re-fetch fresh from db - the item may have been removed/changed
        # since this button was rendered
        item = await cog.db.fetchone("SELECT * FROM shop_items WHERE item_id=?", (self.item_id,))
        if not item:
            return await interaction.response.send_message(
                f"{ERROR_EMOJI} This item no longer exists.", ephemeral=True
            )

        balance = await cog.get_balance(interaction.guild.id, interaction.user.id)
        if balance < item["price"]:
            return await interaction.response.send_message(
                f"{ERROR_EMOJI} You need {fmt_amount(item['price'])} but only have {fmt_amount(balance)}.",
                ephemeral=True,
            )

        role_to_give = interaction.guild.get_role(item["role_id"]) if item["role_id"] else None
        if item["role_id"] and not role_to_give:
            return await interaction.response.send_message(
                f"{ERROR_EMOJI} This item's role no longer exists, ask an admin to fix `/economy store remove` + `/economy store add` it.",
                ephemeral=True,
            )

        await cog.add_balance(interaction.guild.id, interaction.user.id, -item["price"])
        await cog.db.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, item_name, purchased_at) VALUES (?,?,?,?,?)",
            (interaction.guild.id, interaction.user.id, item["item_id"], item["name"], datetime.now().isoformat()),
        )

        if role_to_give:
            try:
                await interaction.user.add_roles(role_to_give, reason=f"Purchased '{item['name']}' from the store")
            except Exception:
                return await interaction.response.send_message(
                    f"{SUCCESS_EMOJI} Bought **{item['name']}**, but I couldn't assign {role_to_give.mention} (check my role position/permissions).",
                    ephemeral=True,
                )

        await interaction.response.send_message(
            f"{SUCCESS_EMOJI} You bought **{item['name']}** for {fmt_amount(item['price'])}!"
            + (f" You received {role_to_give.mention}." if role_to_give else ""),
            ephemeral=True,
        )


class StoreView(discord.ui.View):
    def __init__(self, items: list[dict]):
        super().__init__(timeout=None)
        for item in items[:25]:
            self.add_item(StoreBuyButton(item))


# --- Database Class (Turso) ---
class EconomyDatabase:
    # Single source of truth for expected columns/types per table.
    # Adding a column later: add it HERE only, _migrate() adds it to
    # any pre-existing remote table automatically. Same pattern as the
    # Ticket system's TicketDatabase.
    SHOP_ITEMS_SCHEMA = {
        "item_id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "guild_id": "INTEGER NOT NULL",
        "name": "TEXT NOT NULL",
        "price": "INTEGER NOT NULL",
        "role_id": "INTEGER",
    }
    INVENTORY_SCHEMA = {
        "entry_id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "guild_id": "INTEGER NOT NULL",
        "user_id": "INTEGER NOT NULL",
        "item_id": "INTEGER",
        "item_name": "TEXT",
        "purchased_at": "TEXT",
    }
    # wallets uses a composite PK (guild_id, user_id) so it's handled like
    # user_ticket_counts in ticket.py - a raw CREATE TABLE, with _migrate()
    # still able to add future columns since it only inspects columns.
    WALLETS_SCHEMA = {
        "guild_id": "INTEGER",
        "user_id": "INTEGER",
        "balance": "INTEGER DEFAULT 0",
    }

    def __init__(self):
        # get_client() only returns the shared client reference - safe to
        # call from a plain sync __init__ (same reasoning as every other
        # cog using Turso in this bot).
        self.client = get_client()

    async def init(self):
        await self.client.execute(
            "CREATE TABLE IF NOT EXISTS wallets ("
            "guild_id INTEGER, user_id INTEGER, balance INTEGER DEFAULT 0, "
            "PRIMARY KEY (guild_id, user_id))"
        )

        shop_cols = ", ".join(f"{n} {t}" for n, t in self.SHOP_ITEMS_SCHEMA.items())
        await self.client.execute(f"CREATE TABLE IF NOT EXISTS shop_items ({shop_cols})")

        inv_cols = ", ".join(f"{n} {t}" for n, t in self.INVENTORY_SCHEMA.items())
        await self.client.execute(f"CREATE TABLE IF NOT EXISTS inventory ({inv_cols})")

        await self._migrate("wallets", self.WALLETS_SCHEMA)
        await self._migrate("shop_items", self.SHOP_ITEMS_SCHEMA)
        await self._migrate("inventory", self.INVENTORY_SCHEMA)

    async def _migrate(self, table_name, schema):
        result = await self.client.execute(f"PRAGMA table_info({table_name})")
        existing_columns = {row[1] for row in result.rows}
        missing_columns = [name for name in schema if name not in existing_columns]
        for name in missing_columns:
            col_type = schema[name].replace("PRIMARY KEY", "").replace("AUTOINCREMENT", "").replace("NOT NULL", "").strip()
            await self.client.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {col_type}")

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


# module-level, lazy - same reasoning as every other Turso-backed cog:
# get_client() needs a running event loop, so it can't be constructed at
# plain import time.
db = None


class EconomyCog(commands.Cog, name="Economy"):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        global db
        try:
            if db is None:
                db = EconomyDatabase()
            await db.init()
        except Exception:
            print("=" * 60)
            print("[Economy] FAILED inside db.init() (Turso table setup):")
            traceback.print_exc()
            print("=" * 60)
            raise
        self.db = db

    # --- Internal helpers ---
    async def get_balance(self, guild_id: int, user_id: int) -> int:
        row = await self.db.fetchone(
            "SELECT balance FROM wallets WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        return row["balance"] if row else STARTING_BALANCE

    async def get_rank(self, guild_id: int, user_id: int, balance: int) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) as n FROM wallets WHERE guild_id=? AND balance > ?", (guild_id, balance)
        )
        return (row["n"] if row else 0) + 1

    async def add_balance(self, guild_id: int, user_id: int, amount: int):
        """Adds (or subtracts, if amount is negative) coins for a member. Creates the wallet row if missing.
        Public helper: other cogs can call bot.get_cog("Economy").add_balance(...) to award coins
        (e.g. for winning a game) without touching this file."""
        await self.db.execute(
            "INSERT INTO wallets (guild_id, user_id, balance) VALUES (?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=balance+excluded.balance",
            (guild_id, user_id, amount)
        )

    async def set_balance(self, guild_id: int, user_id: int, amount: int):
        await self.db.execute(
            "INSERT INTO wallets (guild_id, user_id, balance) VALUES (?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance",
            (guild_id, user_id, amount)
        )

    async def resolve_item(self, guild_id: int, name: str):
        """Case-insensitive lookup of a shop item by name within one guild."""
        return await self.db.fetchone(
            "SELECT * FROM shop_items WHERE guild_id=? AND LOWER(name)=LOWER(?)", (guild_id, name)
        )

    # --- Root group: everything lives under /economy so the whole feature
    # only costs ONE global slash-command slot (Discord caps a bot at 100
    # top-level application commands; a group with subcommands still only
    # counts as 1, no matter how many subcommands it has). ---
    @commands.hybrid_group(name="economy", aliases=["eco"], description="Coins, the store, and everything money-related.")
    @commands.guild_only()
    async def economy(self, ctx: Context):
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    # --- Wallet / Balance ---
    @economy.command(name="balance", aliases=["bal", "wallet"], description="Check your or another member's coin balance.")
    @app_commands.describe(member="The member whose balance you want to check (defaults to you).")
    async def balance(self, ctx: Context, member: discord.Member = None):
        member = member or ctx.author
        bal = await self.get_balance(ctx.guild.id, member.id)
        rank = await self.get_rank(ctx.guild.id, member.id, bal)

        embed = discord.Embed(title=f"{MONEY_EMOJI} {member.display_name}'s Wallet", color=EMBED_COLOR)
        embed.add_field(name="Balance", value=fmt_amount(bal), inline=True)
        embed.add_field(name="Leaderboard Rank", value=f"#{rank}", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)
        await ctx.send(embed=embed)

    @economy.command(name="leaderboard", aliases=["lb"], description="Show the server's richest members.")
    @app_commands.describe(page="Which page of the leaderboard to view (defaults to 1).")
    async def leaderboard(self, ctx: Context, page: int = 1):
        page = max(1, page)
        offset = (page - 1) * LEADERBOARD_PAGE_SIZE
        rows = await self.db.fetchall(
            "SELECT user_id, balance FROM wallets WHERE guild_id=? AND balance > 0 "
            "ORDER BY balance DESC LIMIT ? OFFSET ?",
            (ctx.guild.id, LEADERBOARD_PAGE_SIZE, offset)
        )
        if not rows:
            return await ctx.send(f"{ERROR_EMOJI} No one has any {CURRENCY_NAME} yet.", ephemeral=True)

        lines = []
        for i, r in enumerate(rows, start=offset + 1):
            member = ctx.guild.get_member(r["user_id"])
            name = member.mention if member else f"<@{r['user_id']}>"
            lines.append(f"**{i}.** {name} — {fmt_amount(r['balance'])}")

        embed = discord.Embed(title=f"{MONEY_EMOJI} {ctx.guild.name} Leaderboard", description="\n".join(lines), color=EMBED_COLOR)
        embed.set_footer(text=f"Page {page}")
        await ctx.send(embed=embed)

    @economy.command(name="pay", description="Send some of your coins to another member.")
    @app_commands.describe(member="The member you want to pay.", amount="How many coins to send.")
    async def pay(self, ctx: Context, member: discord.Member, amount: int):
        if amount <= 0:
            return await ctx.send(f"{ERROR_EMOJI} Amount must be greater than 0.", ephemeral=True)
        if member.id == ctx.author.id:
            return await ctx.send(f"{ERROR_EMOJI} You can't pay yourself.", ephemeral=True)
        if member.bot:
            return await ctx.send(f"{ERROR_EMOJI} You can't pay a bot.", ephemeral=True)

        sender_balance = await self.get_balance(ctx.guild.id, ctx.author.id)
        if sender_balance < amount:
            return await ctx.send(f"{ERROR_EMOJI} You only have {fmt_amount(sender_balance)}.", ephemeral=True)

        await self.add_balance(ctx.guild.id, ctx.author.id, -amount)
        await self.add_balance(ctx.guild.id, member.id, amount)
        await ctx.send(f"{PAY_EMOJI} {ctx.author.mention} paid {fmt_amount(amount)} to {member.mention}.")

    # --- Admin: manual balance adjustments ---
    @economy.command(name="give-money", description="[Admin] Add coins to a member's balance.")
    @app_commands.describe(member="The member to give coins to.", amount="How many coins to add.")
    @commands.has_permissions(manage_guild=True)
    async def give_money(self, ctx: Context, member: discord.Member, amount: int):
        if amount <= 0:
            return await ctx.send(f"{ERROR_EMOJI} Amount must be greater than 0.", ephemeral=True)
        await self.add_balance(ctx.guild.id, member.id, amount)
        new_balance = await self.get_balance(ctx.guild.id, member.id)
        await ctx.send(f"{SUCCESS_EMOJI} Gave {fmt_amount(amount)} to {member.mention}. New balance: {fmt_amount(new_balance)}.")

    @economy.command(name="remove-money", description="[Admin] Remove coins from a member's balance.")
    @app_commands.describe(member="The member to remove coins from.", amount="How many coins to remove.")
    @commands.has_permissions(manage_guild=True)
    async def remove_money(self, ctx: Context, member: discord.Member, amount: int):
        if amount <= 0:
            return await ctx.send(f"{ERROR_EMOJI} Amount must be greater than 0.", ephemeral=True)
        current = await self.get_balance(ctx.guild.id, member.id)
        new_balance = max(0, current - amount)
        await self.set_balance(ctx.guild.id, member.id, new_balance)
        await ctx.send(f"{SUCCESS_EMOJI} Removed {fmt_amount(amount)} from {member.mention}. New balance: {fmt_amount(new_balance)}.")

    # --- Inventory ---
    @economy.command(name="inventory", aliases=["inv"], description="See what a member has bought from the store.")
    @app_commands.describe(member="The member whose inventory you want to check (defaults to you).")
    async def inventory(self, ctx: Context, member: discord.Member = None):
        member = member or ctx.author
        rows = await self.db.fetchall(
            "SELECT item_name, purchased_at FROM inventory WHERE guild_id=? AND user_id=? ORDER BY purchased_at DESC",
            (ctx.guild.id, member.id)
        )
        if not rows:
            return await ctx.send(f"{ERROR_EMOJI} {member.mention} hasn't bought anything from the store yet.", ephemeral=True)

        lines = [f"• **{r['item_name']}**" for r in rows]
        embed = discord.Embed(title=f"🎒 {member.display_name}'s Inventory", description="\n".join(lines), color=EMBED_COLOR)
        await ctx.send(embed=embed)

    # --- Store (nested subcommand-group: /economy store <list|add|remove|buy>) ---
    @economy.group(name="store", description="Browse or manage this server's coin store.")
    async def store(self, ctx: Context):
        if ctx.invoked_subcommand is None:
            await self.store_list(ctx)

    @store.command(name="list", description="Show every item currently in the store.")
    async def store_list(self, ctx: Context):
        rows = await self.db.fetchall(
            "SELECT * FROM shop_items WHERE guild_id=? ORDER BY price ASC", (ctx.guild.id,)
        )
        if not rows:
            return await ctx.send(f"{ERROR_EMOJI} The store is empty. An admin can add items with `/economy store add`.", ephemeral=True)

        embed = discord.Embed(
            title="🛒 Store",
            description="Click a button below to instantly buy an item, or use `/economy store buy <name>`.",
            color=STORE_EMBED_COLOR,
        )
        for r in rows:
            embed.add_field(name=r['name'], value=fmt_amount(r['price']), inline=True)

        view = StoreView(rows)
        await ctx.send(embed=embed, view=view)

    @store.command(name="add", description="[Admin] Add a new item to the store.")
    @app_commands.describe(
        name="Name of the item.",
        price="Price in coins.",
        role="Optional: a Discord role to automatically give when this item is bought."
    )
    @commands.has_permissions(manage_guild=True)
    async def store_add(self, ctx: Context, name: str, price: int, role: discord.Role = None):
        if price <= 0:
            return await ctx.send(f"{ERROR_EMOJI} Price must be greater than 0.", ephemeral=True)

        current_count = (await self.db.fetchone("SELECT COUNT(*) as n FROM shop_items WHERE guild_id=?", (ctx.guild.id,)))['n']
        if current_count >= MAX_SHOP_ITEMS_PER_GUILD:
            return await ctx.send(f"{ERROR_EMOJI} Max of {MAX_SHOP_ITEMS_PER_GUILD} store items reached.", ephemeral=True)

        if await self.resolve_item(ctx.guild.id, name):
            return await ctx.send(f"{ERROR_EMOJI} An item named `{name}` already exists in the store.", ephemeral=True)

        await self.db.execute(
            "INSERT INTO shop_items (guild_id, name, price, role_id) VALUES (?,?,?,?)",
            (ctx.guild.id, name, price, role.id if role else None)
        )
        await ctx.send(f"{SUCCESS_EMOJI} Added **{name}** to the store for {fmt_amount(price)}" + (f" (grants {role.mention})" if role else "") + ".")

    @store.command(name="remove", description="[Admin] Remove an item from the store.")
    @app_commands.describe(name="Name of the item to remove.")
    @commands.has_permissions(manage_guild=True)
    async def store_remove(self, ctx: Context, name: str):
        item = await self.resolve_item(ctx.guild.id, name)
        if not item:
            return await ctx.send(f"{ERROR_EMOJI} No item named `{name}` found in the store.", ephemeral=True)

        await self.db.execute("DELETE FROM shop_items WHERE item_id=?", (item['item_id'],))
        await ctx.send(f"{SUCCESS_EMOJI} Removed **{item['name']}** from the store.")

    @store.command(name="buy", description="Buy an item from the store.")
    @app_commands.describe(name="Name of the item to buy.")
    async def store_buy(self, ctx: Context, name: str):
        item = await self.resolve_item(ctx.guild.id, name)
        if not item:
            return await ctx.send(f"{ERROR_EMOJI} No item named `{name}` found in the store.", ephemeral=True)

        balance = await self.get_balance(ctx.guild.id, ctx.author.id)
        if balance < item['price']:
            return await ctx.send(f"{ERROR_EMOJI} You need {fmt_amount(item['price'])} but only have {fmt_amount(balance)}.", ephemeral=True)

        role_to_give = ctx.guild.get_role(item['role_id']) if item['role_id'] else None
        if item['role_id'] and not role_to_give:
            return await ctx.send(f"{ERROR_EMOJI} This item's role no longer exists, ask an admin to fix `/economy store remove` + `/economy store add` it.", ephemeral=True)

        await self.add_balance(ctx.guild.id, ctx.author.id, -item['price'])
        await self.db.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, item_name, purchased_at) VALUES (?,?,?,?,?)",
            (ctx.guild.id, ctx.author.id, item['item_id'], item['name'], datetime.now().isoformat())
        )

        if role_to_give:
            try:
                await ctx.author.add_roles(role_to_give, reason=f"Purchased '{item['name']}' from the store")
            except Exception:
                return await ctx.send(f"{SUCCESS_EMOJI} Bought **{item['name']}**, but I couldn't assign {role_to_give.mention} (check my role position/permissions).")

        await ctx.send(f"{SUCCESS_EMOJI} {ctx.author.mention} bought **{item['name']}** for {fmt_amount(item['price'])}!" + (f" You received {role_to_give.mention}." if role_to_give else ""))

    @store_remove.autocomplete('name')
    @store_buy.autocomplete('name')
    async def store_item_autocomplete(self, interaction: discord.Interaction, current: str):
        items = await self.db.fetchall("SELECT name FROM shop_items WHERE guild_id=?", (interaction.guild.id,))
        return [app_commands.Choice(name=i['name'], value=i['name']) for i in items if current.lower() in i['name'].lower()][:25]


async def setup(bot):
    try:
        await bot.add_cog(EconomyCog(bot))
    except Exception:
        print("=" * 60)
        print("[Economy] FAILED inside setup(bot) / bot.add_cog():")
        traceback.print_exc()
        print("=" * 60)
        raise
        
