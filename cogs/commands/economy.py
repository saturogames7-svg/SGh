import discord
from discord.ext import commands
class _economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    """Economy"""
    def help_custom(self):
        emoji = "🪙"
        label = "Economy"
        description = "Show you Commands of Economy"
        return emoji, label, description
    @commands.group()
    async def __Economy__(self, ctx: commands.Context):
        """`/balance`, `/leaderboard`, `/pay`, `/give-money`, `/remove-money`, `/store list`, `/store add`, `/store remove`, `/store buy`, `/inventory`"""
      
