import discord
import re
import time
import random
from utils.emoji import CROSS, TICK, MONEY, HANDSHAKE
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context
import traceback
from datetime import datetime
from utils.config import *
from utils.turso_db import get_client

# --- Configurable Variables ---
EMBED_COLOR = 0x3498DB
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

# --- Income command balancing: kept deliberately modest with long
# cooldowns so members can't stack up coins too easily. ---
WORK_COOLDOWN = 60 * 60          # 1 hour
WORK_MIN, WORK_MAX = 20, 60

CRIME_COOLDOWN = 2 * 60 * 60     # 2 hours
CRIME_SUCCESS_CHANCE = 0.5
CRIME_SUCCESS_MIN, CRIME_SUCCESS_MAX = 50, 150
CRIME_FAIL_MIN, CRIME_FAIL_MAX = 30, 100

DAILY_COOLDOWN = 24 * 60 * 60    # 24 hours
DAILY_MIN, DAILY_MAX = 100, 250

# --- Store display: the store message uses Discord's newer "Components
# V2" layout (discord.ui.LayoutView / Container / Section) so each item
# renders as its own row: emoji+name on the left, a button showing just
# the price on the right that instantly buys it. Requires discord.py >=
# 2.6 (that's when discord.ui.Section landed).
#
# V2 messages cap out at 40 total components INCLUDING nested ones. Each
# item row costs 3 components (Section + its TextDisplay + its Button
# accessory), plus ~3 more for the container/header/separator, so we cap
# the buttoned rows well under that limit. A guild can still stock up to
# MAX_SHOP_ITEMS_PER_GUILD items - anything beyond the buttoned rows is
# still purchasable with /economy store buy <name>.
MAX_STORE_BUTTON_ITEMS = 10

# --- Trivia: no risk of losing coins, just a modest reward for a correct
# answer - the 15-minute per-member cooldown (same pattern as work/crime/
# daily, tracked via wallets.last_trivia) is what stops someone from
# farming it back-to-back all day. ---
TRIVIA_COOLDOWN = 15 * 60        # 15 minutes
TRIVIA_MIN, TRIVIA_MAX = 10, 25
TRIVIA_TIMEOUT_SECONDS = 30

TRIVIA_QUESTIONS = [
    # (question, [options...], correct_option_index)
    ("What is the largest planet in our solar system?", ["Earth", "Jupiter", "Saturn", "Neptune"], 1),
    ("Which country is home to the kangaroo?", ["South Africa", "Brazil", "Australia", "India"], 2),
    ("What is the chemical symbol for gold?", ["Ag", "Au", "Gd", "Go"], 1),
    ("How many continents are there on Earth?", ["5", "6", "7", "8"], 2),
    ("Who wrote the play 'Romeo and Juliet'?", ["Charles Dickens", "William Shakespeare", "Mark Twain", "Jane Austen"], 1),
    ("What is the capital city of Japan?", ["Seoul", "Beijing", "Tokyo", "Bangkok"], 2),
    ("Which ocean is the largest in the world?", ["Atlantic", "Indian", "Arctic", "Pacific"], 3),
    ("How many legs does a spider have?", ["6", "8", "10", "12"], 1),
    ("What is the tallest mountain in the world?", ["K2", "Mount Kilimanjaro", "Mount Everest", "Denali"], 2),
    ("Which planet is known as the Red Planet?", ["Venus", "Mars", "Mercury", "Jupiter"], 1),
    ("What is the smallest prime number?", ["0", "1", "2", "3"], 2),
    ("Which language has the most native speakers worldwide?", ["English", "Hindi", "Spanish", "Mandarin Chinese"], 3),
    ("What is the freezing point of water in Celsius?", ["0°C", "32°C", "-1°C", "100°C"], 0),
    ("Which country gifted the Statue of Liberty to the USA?", ["United Kingdom", "France", "Spain", "Italy"], 1),
    ("How many players are on a standard soccer team on the field?", ["9", "10", "11", "12"], 2),
    ("What gas do plants absorb from the atmosphere for photosynthesis?", ["Oxygen", "Nitrogen", "Carbon dioxide", "Hydrogen"], 2),
    ("Which organ in the human body pumps blood?", ["Lungs", "Liver", "Heart", "Kidney"], 2),
    ("What is the currency of Japan?", ["Won", "Yuan", "Yen", "Ringgit"], 2),
    ("In which year did the Titanic sink?", ["1905", "1912", "1918", "1923"], 1),
    ("Which desert is the largest in the world?", ["Sahara", "Gobi", "Antarctic", "Arabian"], 2),
]

WORK_MESSAGES = [
    "You delivered packages all day and earned",
    "You worked a shift at the coffee shop and made",
    "You helped fix a neighbor's fence and got paid",
    "You walked some dogs around the block and earned",
    "You tutored a kid in math and were paid",
]

CRIME_SUCCESS_MESSAGES = [
    "You pickpocketed a stranger and got away with",
    "You snuck into a warehouse and sold some goods for",
    "You ran a small scam and made off with",
]

CRIME_FAIL_MESSAGES = [
    "You got caught trying to shoplift and paid a fine of",
    "The cops caught you red-handed, costing you",
    "Your scheme fell apart and you had to pay",
]


def fmt_amount(amount: int) -> str:
    """Format an integer amount with the coin emoji and thousands separators."""
    return f"{COIN_EMOJI} {amount:,}"


def split_item_emoji(name: str) -> tuple[str, str]:
    """Splits a leading emoji (custom or unicode) off an item name.
    e.g. '💥 player addicted role' -> ('💥', 'player addicted role')
    Falls back to (COIN_EMOJI, name) if there's no leading emoji."""
    match = re.match(r"^(\S+)\s+(.+)$", name)
    if match and not match.group(1).isalnum():
        return match.group(1), match.group(2)
    return COIN_EMOJI, name


def fmt_cooldown(seconds: int) -> str:
    """Formats a remaining-cooldown duration as e.g. '1h 12m 4s'."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


# --- Trivia buttons/view ---
class TriviaButton(discord.ui.Button):
    def __init__(self, label: str, index: int):
        # 2 buttons per row so 4 options render as a neat 2x2 grid.
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=index // 2)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view: TriviaView = self.view
        await view.answer(interaction, self.index)


class TriviaView(discord.ui.View):
    """A single trivia question with up to 4 answer buttons. Only the
    member who triggered /economy trivia can answer it."""

    def __init__(self, cog: "EconomyCog", guild_id: int, player: discord.Member,
                 question: str, options: list[str], correct_index: int):
        super().__init__(timeout=TRIVIA_TIMEOUT_SECONDS)
        self.cog = cog
        self.guild_id = guild_id
        self.player = player
        self.question = question
        self.options = options
        self.correct_index = correct_index
        self.answered = False
        self.message: discord.Message | None = None

        for i, option in enumerate(options):
            self.add_item(TriviaButton(option, i))

    def _result_embed(self, *, correct: bool | None, reward: int = 0, timed_out: bool = False) -> discord.Embed:
        if timed_out:
            color = 0x95A5A6
        else:
            color = 0x2ECC71 if correct else 0xE74C3C

        embed = discord.Embed(title="🧠 Trivia", description=self.question, color=color)
        embed.set_author(name=self.player.display_name, icon_url=self.player.display_avatar.url)

        if timed_out:
            embed.add_field(
                name="Result",
                value=f"⌛ Time's up! The correct answer was **{self.options[self.correct_index]}**.",
                inline=False,
            )
        elif correct:
            embed.add_field(name="Result", value=f"✅ Correct! You earned {fmt_amount(reward)}.", inline=False)
        else:
            embed.add_field(
                name="Result",
                value=f"❌ Wrong! The correct answer was **{self.options[self.correct_index]}**.",
                inline=False,
            )
        return embed

    def _lock_buttons(self, chosen_index: int | None):
        for child in self.children:
            child.disabled = True
            if child.index == self.correct_index:
                child.style = discord.ButtonStyle.success
            elif chosen_index is not None and child.index == chosen_index:
                child.style = discord.ButtonStyle.danger

    async def answer(self, interaction: discord.Interaction, index: int):
        if interaction.user.id != self.player.id:
            return await interaction.response.send_message(f"{ERROR_EMOJI} This isn't your question.", ephemeral=True)
        if self.answered:
            return await interaction.response.defer()

        self.answered = True
        correct = index == self.correct_index
        self._lock_buttons(index)

        reward = 0
        if correct:
            reward = random.randint(TRIVIA_MIN, TRIVIA_MAX)
            await self.cog.add_balance(self.guild_id, self.player.id, reward)

        self.stop()
        await interaction.response.edit_message(embed=self._result_embed(correct=correct, reward=reward), view=self)

    async def on_timeout(self):
        if self.answered:
            return
        self._lock_buttons(None)
        if self.message:
            try:
                await self.message.edit(embed=self._result_embed(correct=None, timed_out=True), view=self)
            except discord.HTTPException:
                pass


# --- Store buttons: instant-buy directly from the /economy store list message.
class StoreBuyButton(discord.ui.Button):
    def __init__(self, item: dict):
        # Only the price is shown on the button itself - the item's
        # name/emoji is shown as the row text next to it (see
        # StoreItemSection below).
        super().__init__(
            label=f"{item['price']:,}",
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


class StoreItemSection(discord.ui.Section):
    """One row of the store: the item's emoji+name as text on the left,
    and its price as a button accessory on the right that instantly buys
    it when clicked."""
    def __init__(self, item: dict):
        emoji, label_name = split_item_emoji(item["name"])
        super().__init__(
            discord.ui.TextDisplay(f"{emoji}  **{label_name}**"),
            accessory=StoreBuyButton(item),
        )


class StoreView(discord.ui.LayoutView):
    """Components V2 layout for the store message: one row per item
    (name left, price button right) instead of a plain button grid."""
    def __init__(self, items: list[dict]):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_color=STORE_EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(
            "🛒 **Store**\nClick a button to instantly buy an item, or use `/economy store buy <name>`."
        ))
        container.add_item(discord.ui.Separator())
        for item in items[:MAX_STORE_BUTTON_ITEMS]:
            container.add_item(StoreItemSection(item))
        self.add_item(container)


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
    # last_work/last_crime/last_daily/last_trivia store unix timestamps
    # (seconds) of the last time each income command was used, for
    # cooldown checks.
    WALLETS_SCHEMA = {
        "guild_id": "INTEGER",
        "user_id": "INTEGER",
        "balance": "INTEGER DEFAULT 0",
        "last_work": "INTEGER DEFAULT 0",
        "last_crime": "INTEGER DEFAULT 0",
        "last_daily": "INTEGER DEFAULT 0",
        "last_trivia": "INTEGER DEFAULT 0",
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

    async def get_cooldown_remaining(self, guild_id: int, user_id: int, column: str, cooldown_seconds: int) -> int:
        """Returns seconds remaining before `column` (last_work/last_crime/last_daily/last_trivia)
        is off cooldown for this member. 0 or negative means ready to use."""
        row = await self.db.fetchone(
            f"SELECT {column} FROM wallets WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        last_used = row[column] if row and row[column] else 0
        elapsed = time.time() - last_used
        return int(cooldown_seconds - elapsed)

    async def set_cooldown(self, guild_id: int, user_id: int, column: str):
        await self.db.execute(
            f"INSERT INTO wallets (guild_id, user_id, {column}) VALUES (?,?,?) "
            f"ON CONFLICT(guild_id,user_id) DO UPDATE SET {column}=excluded.{column}",
            (guild_id, user_id, int(time.time()))
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

    # --- Income commands ---
    @economy.command(name="work", description="Work a shift for some guaranteed coins.")
    async def work(self, ctx: Context):
        remaining = await self.get_cooldown_remaining(ctx.guild.id, ctx.author.id, "last_work", WORK_COOLDOWN)
        if remaining > 0:
            return await ctx.send(f"{ERROR_EMOJI} You're tired! Try working again in **{fmt_cooldown(remaining)}**.", ephemeral=True)

        amount = random.randint(WORK_MIN, WORK_MAX)
        message = random.choice(WORK_MESSAGES)

        await self.add_balance(ctx.guild.id, ctx.author.id, amount)
        await self.set_cooldown(ctx.guild.id, ctx.author.id, "last_work")

        await ctx.send(f"💼 {message} {fmt_amount(amount)}!")

    @economy.command(name="crime", description="Attempt a crime for a bigger payout, but risk a fine if caught.")
    async def crime(self, ctx: Context):
        remaining = await self.get_cooldown_remaining(ctx.guild.id, ctx.author.id, "last_crime", CRIME_COOLDOWN)
        if remaining > 0:
            return await ctx.send(f"{ERROR_EMOJI} Lay low for a while! Try again in **{fmt_cooldown(remaining)}**.", ephemeral=True)

        await self.set_cooldown(ctx.guild.id, ctx.author.id, "last_crime")

        if random.random() < CRIME_SUCCESS_CHANCE:
            amount = random.randint(CRIME_SUCCESS_MIN, CRIME_SUCCESS_MAX)
            message = random.choice(CRIME_SUCCESS_MESSAGES)
            await self.add_balance(ctx.guild.id, ctx.author.id, amount)
            await ctx.send(f"🕵️ {message} {fmt_amount(amount)}!")
        else:
            balance = await self.get_balance(ctx.guild.id, ctx.author.id)
            fine = min(random.randint(CRIME_FAIL_MIN, CRIME_FAIL_MAX), balance)
            message = random.choice(CRIME_FAIL_MESSAGES)
            await self.add_balance(ctx.guild.id, ctx.author.id, -fine)
            await ctx.send(f"🚨 {message} {fmt_amount(fine)}.")

    @economy.command(name="daily", description="Claim your daily coin reward.")
    async def daily(self, ctx: Context):
        remaining = await self.get_cooldown_remaining(ctx.guild.id, ctx.author.id, "last_daily", DAILY_COOLDOWN)
        if remaining > 0:
            return await ctx.send(f"{ERROR_EMOJI} You've already claimed today's reward. Come back in **{fmt_cooldown(remaining)}**.", ephemeral=True)

        amount = random.randint(DAILY_MIN, DAILY_MAX)

        await self.add_balance(ctx.guild.id, ctx.author.id, amount)
        await self.set_cooldown(ctx.guild.id, ctx.author.id, "last_daily")

        await ctx.send(f"🎁 You claimed your daily reward of {fmt_amount(amount)}!")

    # --- Games ---
    @economy.command(name="trivia", description="Answer a trivia question correctly for some coins.")
    async def trivia(self, ctx: Context):
        remaining = await self.get_cooldown_remaining(ctx.guild.id, ctx.author.id, "last_trivia", TRIVIA_COOLDOWN)
        if remaining > 0:
            return await ctx.send(f"{ERROR_EMOJI} You've already played trivia recently. Try again in **{fmt_cooldown(remaining)}**.", ephemeral=True)

        # Cooldown is set immediately (before the answer), same pattern as
        # /economy crime - so re-rolling the question doesn't reset the timer.
        await self.set_cooldown(ctx.guild.id, ctx.author.id, "last_trivia")

        question, options, correct_index = random.choice(TRIVIA_QUESTIONS)
        shuffled = list(enumerate(options))
        random.shuffle(shuffled)
        shuffled_options = [option for _, option in shuffled]
        shuffled_correct_index = next(i for i, (orig_i, _) in enumerate(shuffled) if orig_i == correct_index)

        embed = discord.Embed(title="🧠 Trivia", description=question, color=EMBED_COLOR)
        embed.set_footer(text="You have 30 seconds to answer.")

        view = TriviaView(self, ctx.guild.id, ctx.author, question, shuffled_options, shuffled_correct_index)
        view.message = await ctx.send(embed=embed, view=view)

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

        view = StoreView(rows)

        # Always send as a fresh, standalone message - never as a reply to
        # the invoking message. (mention_author only matters for prefix
        # invocation; slash-command responses are never shown as replies
        # in the first place, and passing reply-only kwargs to an
        # interaction response would error, hence the guard.)
        send_kwargs = {"view": view}
        if ctx.interaction is None:
            send_kwargs["mention_author"] = False
        await ctx.send(**send_kwargs)

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
        
