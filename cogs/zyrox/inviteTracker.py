"""
Invite Tracker - Menu/Help cog.

This file ONLY exists so the bot's custom help system can list the
Invite Tracker category (emoji, label, description) and show a quick
usage summary when someone runs the group command.

It does NOT implement any invite-tracking logic. The real, working
commands (>invites, >addinvites, >setinvites, >resetinvites,
>inviteleaderboard, >invlog) live in invite_tracker.py and must be
loaded as a separate extension alongside this one.
"""

import discord
from discord.ext import commands
from utils.emoji import ZPEOPLE


class inviteTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    """Invite Tracker"""

    def help_custom(self):
        emoji = ZPEOPLE
        label = "Invite Tracker"
        description = "Manage and track server invites."
        return emoji, label, description

    @commands.group(
        name="InviteTracker",
        aliases=["invtracker", "itracker"],
        invoke_without_command=True,
    )
    async def __InviteTracker__(self, ctx: commands.Context):
        """
        Invite Tracker Commands
        `>invites [member]`
        `>addinvites <member> <amount>`
        `>setinvites <member> <total> [fake] [left] [rejoin]`
        `>resetinvites <member>`
        `>inviteleaderboard`
        `>invlog`
        """
        embed = discord.Embed(
            title=f"{ZPEOPLE} Invite Tracker",
            description=self.__InviteTracker__.help,
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(inviteTracker(bot))
