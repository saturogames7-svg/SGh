import discord
from discord.ext import commands
from utils.emoji import ZPEOPLE


class inviteTracker(commands.Cog):

    def __init__(self, bot):
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
        invoke_without_command=True
    )
    async def __InviteTracker__(self, ctx: commands.Context):
        """
        Invite Tracker Commands

        `>invites [member]`
        `>addinvites <member> <amount>`
        `>setinvites <member> <amount>`
        `>resetinvites <member>`
        `>inviteleaderboard`
        `>invitelogging <channel> [message]`
        """
        pass
