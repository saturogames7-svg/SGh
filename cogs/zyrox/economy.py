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
        """`/economy balance`, `/economy leaderboard`, `/economy pay`, `/economy give-money`, `/economy remove-money`, `/economy inventory`, `/economy trivia`, `/economy store list`, `/economy store add`, `/economy store remove`, `/economy store buy`"""
        
