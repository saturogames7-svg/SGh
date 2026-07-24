import discord
from discord.ext import commands
import aiosqlite
from utils.cv2 import CV2
from utils.emoji import ARROWRED
from utils.config import BotName


INVITE_DB = "db/invite.db"
EMOJI_INVITE = ARROWRED


class Tracking(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.invites = {}


    async def ensure_tables(self, guild_id):

        async with aiosqlite.connect(INVITE_DB) as db:

            await db.execute(f"""
            CREATE TABLE IF NOT EXISTS invites_{guild_id}(
                user_id INTEGER PRIMARY KEY,
                total INTEGER DEFAULT 0,
                fake INTEGER DEFAULT 0,
                left INTEGER DEFAULT 0,
                rejoin INTEGER DEFAULT 0
            )
            """)


            await db.execute("""
            CREATE TABLE IF NOT EXISTS logging(
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                message TEXT
            )
            """)


            await db.execute("""
            CREATE TABLE IF NOT EXISTS invite_history(
                guild_id INTEGER,
                member_id INTEGER PRIMARY KEY,
                inviter_id INTEGER
            )
            """)


            await db.commit()



    async def cache_invites(self, guild):

        try:
            self.invites[guild.id] = await guild.invites()

        except:
            self.invites[guild.id] = []



    @commands.Cog.listener()
    async def on_ready(self):

        for guild in self.bot.guilds:

            await self.ensure_tables(guild.id)
            await self.cache_invites(guild)



    @commands.Cog.listener()
    async def on_invite_create(self, invite):

        await self.cache_invites(invite.guild)



    @commands.Cog.listener()
    async def on_invite_delete(self, invite):

        await self.cache_invites(invite.guild)



    @commands.Cog.listener()
    async def on_member_join(self, member):

        guild = member.guild

        await self.ensure_tables(guild.id)


        old_invites = self.invites.get(
            guild.id,
            []
        )


        try:
            new_invites = await guild.invites()

        except:
            return



        inviter = None


        for new in new_invites:

            for old in old_invites:

                if (
                    new.code == old.code
                    and new.uses > old.uses
                ):
                    inviter = new.inviter
                    break

            if inviter:
                break



        self.invites[guild.id] = new_invites



        if inviter:

            async with aiosqlite.connect(INVITE_DB) as db:


                cursor = await db.execute(
                f"""
                SELECT member_id
                FROM invite_history
                WHERE member_id=?
                """,
                (member.id,)
                )


                existed = await cursor.fetchone()



                if existed:

                    await db.execute(
                    f"""
                    INSERT OR IGNORE INTO invites_{guild.id}
                    (user_id)
                    VALUES(?)
                    """,
                    (inviter.id,)
                    )


                    await db.execute(
                    f"""
                    UPDATE invites_{guild.id}
                    SET rejoin = rejoin + 1
                    WHERE user_id=?
                    """,
                    (inviter.id,)
                    )


                else:


                    await db.execute(
                    f"""
                    INSERT OR IGNORE INTO invites_{guild.id}
                    (user_id)
                    VALUES(?)
                    """,
                    (inviter.id,)
                    )


                    await db.execute(
                    f"""
                    UPDATE invites_{guild.id}
                    SET total = total + 1
                    WHERE user_id=?
                    """,
                    (inviter.id,)
                    )



                await db.execute(
                """
                INSERT OR REPLACE INTO invite_history
                VALUES(?,?,?)
                """,
                (
                    guild.id,
                    member.id,
                    inviter.id
                )
                )


                await db.commit()



        async with aiosqlite.connect(INVITE_DB) as db:

            cursor = await db.execute(
            """
            SELECT channel_id,message
            FROM logging
            WHERE guild_id=?
            """,
            (guild.id,)
            )


            config = await cursor.fetchone()



        if not config:
            return



        channel = guild.get_channel(
            config[0]
        )


        if not channel:
            return



        total = 0


        if inviter:

            total = await self.get_total_invites(
                guild.id,
                inviter.id
            )



        text = config[1]


        text = text.replace(
            "{member}",
            member.mention
        )


        text = text.replace(
            "{user}",
            inviter.mention if inviter else "Unknown"
        )


        text = text.replace(
            "{invites}",
            str(total)
        )


        text = text.replace(
            "{server}",
            guild.name
        )


        await channel.send(
            view=CV2(
                "📥 Member Joined",
                text
            )
        )



    @commands.Cog.listener()
    async def on_member_remove(self, member):

        guild = member.guild

        await self.ensure_tables(guild.id)


        async with aiosqlite.connect(INVITE_DB) as db:


            cursor = await db.execute(
            """
            SELECT inviter_id
            FROM invite_history
            WHERE member_id=?
            """,
            (member.id,)
            )


            row = await cursor.fetchone()



            if row:

                await db.execute(
                f"""
                UPDATE invites_{guild.id}
                SET left = left + 1
                WHERE user_id=?
                """,
                (row[0],)
                )


                await db.commit()



    async def get_total_invites(
        self,
        guild_id,
        user_id
    ):

        async with aiosqlite.connect(INVITE_DB) as db:

            cursor = await db.execute(
            f"""
            SELECT total
            FROM invites_{guild_id}
            WHERE user_id=?
            """,
            (user_id,)
            )


            row = await cursor.fetchone()

            return row[0] if row else 0



    @commands.command(
        aliases=["invlog"]
    )
    @commands.has_permissions(
        administrator=True
    )
    async def invitelogging(
        self,
        ctx,
        channel: discord.TextChannel,
        *,
        message="📥 {member} joined invited by {user} | Total: {invites}"
    ):


        await self.ensure_tables(
            ctx.guild.id
        )


        async with aiosqlite.connect(INVITE_DB) as db:

            await db.execute(
            """
            INSERT OR REPLACE INTO logging
            VALUES(?,?,?)
            """,
            (
                ctx.guild.id,
                channel.id,
                message
            )
            )


            await db.commit()



        await ctx.send(
            view=CV2(
                "✅ Invite Logger",
                f"Logs: {channel.mention}\n\n{message}"
            )
        )



    @commands.command(
        aliases=["inv"]
    )
    async def invites(
        self,
        ctx,
        member:discord.Member=None
    ):

        member = member or ctx.author


        await self.ensure_tables(
            ctx.guild.id
        )


        async with aiosqlite.connect(INVITE_DB) as db:

            cursor = await db.execute(
            f"""
            SELECT total,fake,left,rejoin
            FROM invites_{ctx.guild.id}
            WHERE user_id=?
            """,
            (member.id,)
            )

            row = await cursor.fetchone()



        if row:

            total,fake,left,rejoin=row

        else:

            total=fake=left=rejoin=0



        real = total-fake-left-rejoin



        await ctx.send(
            view=CV2(
                f"Invite Stats - {member.name}",
                f"""
{EMOJI_INVITE} Total: `{total}`

Real: `{real}`
Fake: `{fake}`
Left: `{left}`
Rejoin: `{rejoin}`
"""
            )
        )



async def setup(bot):

    await bot.add_cog(
        Tracking(bot)
    )
