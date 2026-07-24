import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite


DATABASE = "db/invites.db"


class InviteTracker(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.invites = {}

    async def cog_load(self):
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS invite_config(
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                message TEXT
            )
            """)

            await db.execute("""
            CREATE TABLE IF NOT EXISTS invite_users(
                guild_id INTEGER,
                user_id INTEGER,
                invites INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id,user_id)
            )
            """)

            await db.commit()


    async def cache_invites(self,guild):

        invites = await guild.invites()

        self.invites[guild.id] = {
            invite.code: invite.uses
            for invite in invites
        }


    @commands.Cog.listener()
    async def on_ready(self):

        for guild in self.bot.guilds:
            try:
                await self.cache_invites(guild)
            except:
                pass


    @commands.Cog.listener()
    async def on_member_join(self,member):

        guild = member.guild

        before = self.invites.get(guild.id,{})

        try:
            invites = await guild.invites()
        except:
            return


        used_invite = None


        for invite in invites:

            old = before.get(invite.code,0)

            if invite.uses > old:
                used_invite = invite
                break


        await self.cache_invites(guild)


        if not used_invite:
            return


        inviter = used_invite.inviter


        async with aiosqlite.connect(DATABASE) as db:

            await db.execute("""
            INSERT INTO invite_users
            VALUES(?,?,1)
            ON CONFLICT(guild_id,user_id)
            DO UPDATE SET invites = invites + 1
            """,
            (
                guild.id,
                inviter.id
            ))

            await db.commit()



            cursor = await db.execute(
                """
                SELECT invites 
                FROM invite_users
                WHERE guild_id=? AND user_id=?
                """,
                (
                    guild.id,
                    inviter.id
                )
            )

            total = await cursor.fetchone()


        async with aiosqlite.connect(DATABASE) as db:

            cursor = await db.execute(
            """
            SELECT channel_id,message
            FROM invite_config
            WHERE guild_id=?
            """,
            (guild.id,)
            )

            config = await cursor.fetchone()


        if not config:
            return


        channel = guild.get_channel(config[0])


        if not channel:
            return



        embed = discord.Embed(
            title="🎉 New Invite!",
            color=0x00FF00
        )

        embed.add_field(
            name="Member Joined",
            value=member.mention,
            inline=False
        )

        embed.add_field(
            name="Invited By",
            value=inviter.mention,
            inline=False
        )

        embed.add_field(
            name="Total Invites",
            value=f"**{total[0]}**",
            inline=False
        )


        await channel.send(
            content=config[1].replace(
                "{user}",
                inviter.mention
            ).replace(
                "{member}",
                member.mention
            ),
            embed=embed
        )



    @app_commands.command(
        name="invite_setup",
        description="Setup invite tracker"
    )
    @app_commands.checks.has_permissions(
        administrator=True
    )
    async def invite_setup(
        self,
        interaction:discord.Interaction,
        channel:discord.TextChannel,
        message:str
    ):


        async with aiosqlite.connect(DATABASE) as db:

            await db.execute(
            """
            INSERT INTO invite_config
            VALUES(?,?,?)
            ON CONFLICT(guild_id)
            DO UPDATE SET
            channel_id=?,
            message=?
            """,
            (
                interaction.guild.id,
                channel.id,
                message,
                channel.id,
                message
            ))

            await db.commit()



        await interaction.response.send_message(
            "✅ Invite Tracker has been configured.",
            ephemeral=True
        )



async def setup(bot):

    await bot.add_cog(
        InviteTracker(bot)
    )
