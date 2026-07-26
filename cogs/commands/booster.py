from __future__ import annotations 
import discord 
from utils.emoji import CROSS, NITRO_BOOST, TICK, TIMER
import asyncio 
import logging 
import aiosqlite 
import json 
import random
from discord .ext import commands 
from utils .Tools import *
from discord .ext .commands import Context 
from discord import app_commands 
import time 
import datetime 
import re 
from typing import *
from time import strftime 
from core import Cog ,zyrox ,Context 
from discord.ui import LayoutView, TextDisplay, Separator, Container
from utils.cv2 import CV2, build_container

class CV2(LayoutView):
    def __init__(self, title, *sections):
        super().__init__(timeout=None)
        items = [TextDisplay(f"**{title}**")]
        for s in sections:
            if s:
                items.append(Separator(visible=True))
                items.append(TextDisplay(str(s)))
        self.add_item(build_container(*items))

logging .basicConfig (
level =logging .INFO ,
format ="\x1b[38;5;197m[\x1b[0m%(asctime)s\x1b[38;5;197m]\x1b[0m -> \x1b[38;5;197m%(message)s\x1b[0m",
datefmt ="%H:%M:%S",
)

class SetupCancelled (Exception ):
    """Raised internally when the user cancels the `boost setup` wizard"""
    pass 

class SetupTimeout (Exception ):
    """Raised internally when the user doesn't respond to a `boost setup` prompt in time"""
    pass 

class Booster (Cog ):

    COLOR_MAP = {
        "red": 0xFF0000,
        "green": 0x00FF00,
        "blue": 0x0000FF,
        "orange": 0xFFA500,
        "purple": 0x800080,
        "gold": 0xFFD700,
    }

    def __init__ (self ,bot : Zyrox ):
        self .bot =bot 
        self .color =0xFF0000 
        self .db_path ="db/boost.db"
        self .bot .loop .create_task (self .setup_database ())


        self .url_pattern =re .compile (
        r'^(?:http|ftp)s?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$',re .IGNORECASE 
        )


 
    async def setup_database (self ):
        """Initialize boost database tables"""
        async with aiosqlite .connect (self .db_path )as db :
            await db .execute ("""
                CREATE TABLE IF NOT EXISTS boost_config (
                    guild_id INTEGER PRIMARY KEY,
                    config TEXT NOT NULL
                )
            """)
            await db .commit ()

    def _default_config (self )->dict :
        """Return a fresh copy of the default boost configuration"""
        return {
            "boost":{
                "channel":[],
                "message":"{user.mention} just boosted {server.name}! 🎉",
                "embed":True ,
                "ping":False ,
                "image":"",
                "thumbnail":"",
                "autodel":0,
                "title":"",
                "description":"",
                "color":0xFF0000,
                "footer":"",
                "footericon":"",
                "author":"",
                "authoricon":"",
                "timestamp":True
            },
            "boost_roles":{
                "roles":[]
            }
        }

    def _merge_config (self ,default :dict ,existing :dict )->dict :
        """Recursively merge default config keys into an existing config without losing any existing data"""
        merged =dict (existing )
        for key ,value in default .items ():
            if key not in merged :
                merged [key ]=value 
            elif isinstance (value ,dict )and isinstance (merged .get (key ),dict ):
                merged [key ]=self ._merge_config (value ,merged [key ])
        return merged 

    async def get_boost_config (self ,guild_id :int )->dict :
        """Get boost configuration for a guild, upgrading it with any missing fields"""
        default_config =self ._default_config ()

        async with aiosqlite .connect (self .db_path )as db :
            async with db .execute ("SELECT config FROM boost_config WHERE guild_id = ?",(guild_id ,))as cursor :
                row =await cursor .fetchone ()

                if row :
                    existing =json .loads (row [0 ])
                    merged =self ._merge_config (default_config ,existing )
                    if merged !=existing :
                        await self .update_boost_config (guild_id ,merged )
                    return merged 

                await self .update_boost_config (guild_id ,default_config )
                return default_config 

    async def update_boost_config (self ,guild_id :int ,config :dict ):
        """Update boost configuration for a guild"""
        async with aiosqlite .connect (self .db_path )as db :
            await db .execute (
            "INSERT OR REPLACE INTO boost_config (guild_id, config) VALUES (?, ?)",
            (guild_id ,json .dumps (config ))
            )
            await db .commit ()

    def is_authorized (self ,ctx )->bool :
        """Check if user is authorized to use admin commands"""
        return (
        ctx .author ==ctx .guild .owner 
        or ctx .author .guild_permissions .administrator 
        or ctx .author .top_role .position >=ctx .guild .me .top_role .position 
        )

    def _is_clear_value (self ,value :str )->bool :
        """Check whether the given value should clear/reset a text field"""
        return value .strip ().lower ()in ("none","reset","clear")

    def parse_color (self ,value :str )->Optional [int ]:
        """Parse a color string (hex, name, random, reset) into an integer color value"""
        if value is None :
            return None 
        value =value .strip ().lower ()
        if not value :
            return None 
        if value =="reset":
            return 0xFF0000 
        if value =="random":
            return random .randint (0 ,0xFFFFFF )
        if value in self .COLOR_MAP :
            return self .COLOR_MAP [value ]

        hex_str =value 
        if hex_str .startswith ("#"):
            hex_str =hex_str [1 :]
        elif hex_str .startswith ("0x"):
            hex_str =hex_str [2 :]

        if re .fullmatch (r"[0-9a-f]{6}",hex_str ):
            return int (hex_str ,16 )

        if re .fullmatch (r"[0-9a-f]{3}",hex_str ):
            expanded ="".join (ch *2 for ch in hex_str )
            return int (expanded ,16 )

        return None 

    def format_boost_message (self ,message :str ,user :discord .Member ,guild :discord .Guild )->str :
        """Format boost message with new variable style"""
        replacements ={

        "{server.name}":guild .name ,
        "{server.id}":str (guild .id ),
        "{server.owner}":str (guild .owner ),
        "{server.icon}":guild .icon .url if guild .icon else "",
        "{server.boost_count}":str (guild .premium_subscription_count ),
        "{server.boost_level}":f"Level {guild.premium_tier}",
        "{server.member_count}":str (guild .member_count ),


        "{user.name}":user .display_name ,
        "{user.mention}":user .mention ,
        "{user.tag}":str (user ),
        "{user.id}":str (user .id ),
        "{user.avatar}":user .display_avatar .url ,
        "{user.created_at}":f"<t:{int(user.created_at.timestamp())}:F>",
        "{user.joined_at}":f"<t:{int(user.joined_at.timestamp())}:F>"if user .joined_at else "Unknown",
        "{user.top_role}":user .top_role .name if user .top_role else "None",
        "{user.is_booster}":str (bool (user .premium_since )),
        "{user.is_mobile}":str (user .is_on_mobile ()),
        "{user.boosted_at}":f"<t:{int(user.premium_since.timestamp())}:F>"if user .premium_since else "Unknown"
        }


        for old ,new in replacements .items ():
            message =message .replace (old ,new )

        return message 

    async def build_boost_embed (self ,data :dict ,user :discord .Member ,guild :discord .Guild )->discord .Embed :
        """Build the boost embed based on the guild's full embed configuration"""
        boost =data ["boost"]
        color =boost .get ("color",self .color )

        embed =discord .Embed (color =color )

        if boost .get ("timestamp",True ):
            embed .timestamp =discord .utils .utcnow ()

        title =boost .get ("title","").strip ()
        description =boost .get ("description","").strip ()

        if title :
            embed .title =self .format_boost_message (title ,user ,guild )[:256 ]

        if description :
            embed .description =self .format_boost_message (description ,user ,guild )[:4096 ]
        else :
            embed .description =self .format_boost_message (boost ["message"],user ,guild )[:4096 ]

        author_name =boost .get ("author","").strip ()
        author_icon =boost .get ("authoricon","").strip ()

        if author_name :
            icon_url =self .format_boost_message (author_icon ,user ,guild )if author_icon else None 
            embed .set_author (name =self .format_boost_message (author_name ,user ,guild )[:256 ],icon_url =icon_url )
        else :
            embed .set_author (name =user .display_name ,icon_url =user .display_avatar .url )

        footer_text =boost .get ("footer","").strip ()
        footer_icon =boost .get ("footericon","").strip ()

        if footer_text :
            icon_url =self .format_boost_message (footer_icon ,user ,guild )if footer_icon else None 
            embed .set_footer (text =self .format_boost_message (footer_text ,user ,guild )[:2048 ],icon_url =icon_url )
        elif guild .icon :
            embed .set_footer (text =guild .name ,icon_url =guild .icon .url )

        if boost .get ("image"):
            embed .set_image (url =boost ["image"])

        if boost .get ("thumbnail"):
            embed .set_thumbnail (url =boost ["thumbnail"])

        return embed 

    async def _auto_delete (self ,message :discord .Message ,delay :int ):
        """Delete a sent boost message after the configured delay"""
        try :
            await asyncio .sleep (delay )
            await message .delete ()
        except (discord .NotFound ,discord .Forbidden ,discord .HTTPException ):
            pass 

    async def send_boost_message (self ,guild :discord .Guild ,user :discord .Member ,data :Optional [dict ]=None )->Tuple [bool ,str ]:
        """Reusable function that sends the configured boost message/embed to every configured channel.
        Used by both the automatic boost listener and the `boost test` command so the logic never gets duplicated."""
        if data is None :
            data =await self .get_boost_config (guild .id )

        channel_ids =data ["boost"]["channel"]

        if not channel_ids :
            return False ,f"{CROSS} No boost channel configured."

        autodel =data ["boost"].get ("autodel",0 )
        sent =0 
        errors =[]

        for channel_id in channel_ids :
            channel =self .bot .get_channel (int (channel_id ))
            if not channel :
                errors .append (f"Channel `{channel_id}` no longer exists.")
                continue 

            try :
                if data ["boost"]["embed"]:
                    embed =await self .build_boost_embed (data ,user ,guild )
                    ping_content =user .mention if data ["boost"]["ping"]else None 
                    msg =await channel .send (content =ping_content ,embed =embed )
                else :
                    formatted =self .format_boost_message (data ["boost"]["message"],user ,guild )
                    ping_content =f"{user.mention} "if data ["boost"]["ping"]else ""
                    msg =await channel .send (f"{ping_content}{formatted}")

                sent +=1 

                if autodel and autodel >0 :
                    asyncio .create_task (self ._auto_delete (msg ,autodel ))

            except discord .Forbidden :
                errors .append (f"Missing permission to send messages in {channel.mention}.")
            except Exception as e :
                errors .append (f"Error in {channel.mention}: `{str(e)}`")

        if sent ==0 :
            if errors :
                return False ,f"{CROSS} "+" ".join (errors )
            return False ,f"{CROSS} Failed to send the boost message."

        message =f"{TICK} Boost message sent to {sent} channel(s)."
        if errors :
            message +=" "+" ".join (errors )

        return True ,message 

    async def send_permission_error (self ,ctx ):
        """Send permission error embed"""
        await ctx.send(view=CV2("Permission Error", "```diff\n- You must have Administrator permission.\n- Your top role should be above my top role.\n```"))

    # ------------------------------------------------------------------
    # Setup wizard helpers
    # ------------------------------------------------------------------

    async def _wait_for_reply (self ,ctx )->str :
        """Wait for a single reply from the command invoker in the same channel"""
        def check (m ):
            return m .author ==ctx .author and m .channel ==ctx .channel 

        try :
            message =await self .bot .wait_for ('message',check =check ,timeout =60.0 )
        except asyncio .TimeoutError :
            return "__TIMEOUT__"

        content =message .content .strip ()
        if content .lower ()=="cancel":
            return "__CANCEL__"
        if content .lower ()=="skip":
            return "__SKIP__"
        return content 

    async def _prompt_field (self ,ctx ,step_no :int ,total :int ,title :str ,question :str ,validator :Optional [Callable [[str ],Any ]]=None ,allow_clear :bool =True )->Any :
        """Ask a single setup wizard question, re-prompting on invalid input.
        Returns "__SKIP__" if the user skips, the parsed value, or "" if the field was cleared.
        Raises SetupCancelled / SetupTimeout so the caller can unwind in one place."""
        while True :
            prompt_text =f"**Step {step_no}/{total} — {title}**\n{question}\n*Type `skip` to keep the current value, or `cancel` to abort setup.*"
            await ctx .send (view =CV2 ("Boost Setup",prompt_text ))

            answer =await self ._wait_for_reply (ctx )

            if answer =="__CANCEL__":
                raise SetupCancelled ()
            if answer =="__TIMEOUT__":
                raise SetupTimeout ()
            if answer =="__SKIP__":
                return "__SKIP__"
            if allow_clear and self ._is_clear_value (answer ):
                return ""

            if validator is not None :
                result =validator (answer )
                if result is None :
                    await ctx .send (view =CV2 ("Error",f"{CROSS} That value isn't valid. Please try again, `skip`, or `cancel`."))
                    continue 
                return result 

            return answer 

    def _validate_channels (self ,answer :str ,guild :discord .Guild )->Optional [List [str ]]:
        """Parse up to 3 channel mentions/IDs out of a setup wizard reply"""
        channel_ids :List [str ]=[]
        for word in answer .split ():
            match =re .search (r"\d{15,20}",word )
            if match :
                channel =guild .get_channel (int (match .group ()))
                if channel and str (channel .id )not in channel_ids :
                    channel_ids .append (str (channel .id ))
        if not channel_ids :
            return None 
        return channel_ids [:3 ]

    def _validate_url (self ,answer :str )->Optional [str ]:
        return answer if self .url_pattern .match (answer )else None 

    def _validate_bool (self ,answer :str )->Optional [bool ]:
        value =answer .strip ().lower ()
        if value in ("yes","y","true","on","enable","enabled"):
            return True 
        if value in ("no","n","false","off","disable","disabled"):
            return False 
        return None 

    def _validate_autodel (self ,answer :str )->Optional [int ]:
        try :
            value =int (answer .strip ())
        except ValueError :
            return None 
        if value <0 :
            return None 
        return value 

    def _validate_max_length (self ,answer :str ,max_len :int )->Optional [str ]:
        return answer if len (answer )<=max_len else None 

    # ------------------------------------------------------------------
    # Automatic boost detection
    # ------------------------------------------------------------------

    @Cog .listener ()
    async def on_member_update (self ,before :discord .Member ,after :discord .Member ):
        """Detect new boosts, removed boosts, and re-boosts and act on them automatically"""
        if before .premium_since !=after .premium_since :
            logging .info (f"[Boost] premium_since changed for {after} ({after.id}) in {after.guild.name}: {before.premium_since} -> {after.premium_since}")

        if before .premium_since ==after .premium_since :
            return 

        try :
            data =await self .get_boost_config (after .guild .id )
        except Exception :
            logging .exception ("Failed to load boost config for guild %s",after .guild .id )
            return 

        if before .premium_since is None and after .premium_since is not None :
            # Brand new boost
            await self ._handle_boost_start (after ,data )
        elif before .premium_since is not None and after .premium_since is None :
            # Boost removed
            await self ._handle_boost_end (after ,data )
        elif before .premium_since is not None and after .premium_since is not None and before .premium_since !=after .premium_since :
            # Re-boost (premium_since timestamp changed while still boosting)
            await self ._handle_boost_start (after ,data )

    async def _handle_boost_start (self ,member :discord .Member ,data :dict ):
        """Handle a new boost or re-boost: send the configured message and grant boost roles"""
        try :
            await self .send_boost_message (member .guild ,member ,data )
        except Exception :
            logging .exception ("Failed to send boost message for %s in %s",member .id ,member .guild .id )

        role_ids =data .get ("boost_roles",{}).get ("roles",[])
        for role_id in role_ids :
            role =member .guild .get_role (int (role_id ))
            if role and role not in member .roles :
                try :
                    await member .add_roles (role ,reason ="Server boost started")
                except (discord .Forbidden ,discord .HTTPException ):
                    pass 

    async def _handle_boost_end (self ,member :discord .Member ,data :dict ):
        """Handle a removed boost: remove any assigned boost roles"""
        role_ids =data .get ("boost_roles",{}).get ("roles",[])
        for role_id in role_ids :
            role =member .guild .get_role (int (role_id ))
            if role and role in member .roles :
                try :
                    await member .remove_roles (role ,reason ="Server boost ended")
                except (discord .Forbidden ,discord .HTTPException ):
                    pass 

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands .group (name ="boost",aliases =['bst'],invoke_without_command =True ,help ="Boost message configuration commands")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,5 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost (self ,ctx ):
        if ctx .subcommand_passed is None :
            await ctx .send_help (ctx .command )
            ctx .command .reset_cooldown (ctx )

    @_boost .command (name ="setup",help ="Interactive step-by-step setup wizard for the full boost message system")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,10 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_setup (self ,ctx ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        data =await self .get_boost_config (ctx .guild .id )
        boost =data ["boost"]
        total =15 

        await ctx .send (view =CV2 (
        f"{NITRO_BOOST} Boost Setup Wizard",
        "I'll walk you through configuring the entire boost message system, one step at a time.\n"
        "At any step, type `skip` to keep the current value, or `cancel` to stop without saving anything.\n"
        "You have 60 seconds to answer each question."
        ))

        try :
            channels =await self ._prompt_field (
            ctx ,1 ,total ,"Boost Channel(s)",
            "Mention up to 3 channels where boost messages should be sent (space separated).",
            validator =lambda a :self ._validate_channels (a ,ctx .guild ),allow_clear =False 
            )
            if channels !="__SKIP__":
                boost ["channel"]=channels 

            use_embed =await self ._prompt_field (
            ctx ,2 ,total ,"Embed Mode",
            "Should boost messages be sent as an embed? Reply `yes` or `no`.",
            validator =self ._validate_bool ,allow_clear =False 
            )
            if use_embed !="__SKIP__":
                boost ["embed"]=use_embed 

            message =await self ._prompt_field (
            ctx ,3 ,total ,"Message Content",
            "Send the boost message text. Variables like {user.mention} and {server.name} are supported.\n"
            "This is used as the embed description if no custom description is set below, or as the plain message if embeds are off."
            )
            if message !="__SKIP__":
                boost ["message"]=message 

            title =await self ._prompt_field (
            ctx ,4 ,total ,"Embed Title",
            "Send a title for the embed (max 256 characters), or `none` to leave it blank.",
            validator =lambda a :self ._validate_max_length (a ,256 )
            )
            if title !="__SKIP__":
                boost ["title"]=title 

            description =await self ._prompt_field (
            ctx ,5 ,total ,"Embed Description",
            "Send a custom description for the embed (max 4096 characters), or `none` to fall back to the message content.",
            validator =lambda a :self ._validate_max_length (a ,4096 )
            )
            if description !="__SKIP__":
                boost ["description"]=description 

            color =await self ._prompt_field (
            ctx ,6 ,total ,"Embed Color",
            "Send a color: a hex code (`#5865F2` or `0x5865F2`), a name (`red`, `green`, `blue`, `orange`, `purple`, `gold`), `random`, or `reset`.",
            validator =self .parse_color ,allow_clear =False 
            )
            if color !="__SKIP__":
                boost ["color"]=color 

            thumbnail =await self ._prompt_field (
            ctx ,7 ,total ,"Thumbnail",
            "Send an image URL to use as the thumbnail, or `none` to remove it.",
            validator =self ._validate_url 
            )
            if thumbnail !="__SKIP__":
                boost ["thumbnail"]=thumbnail 

            image =await self ._prompt_field (
            ctx ,8 ,total ,"Image",
            "Send an image URL to use as the embed image, or `none` to remove it.",
            validator =self ._validate_url 
            )
            if image !="__SKIP__":
                boost ["image"]=image 

            footer =await self ._prompt_field (
            ctx ,9 ,total ,"Footer Text",
            "Send footer text for the embed (max 2048 characters), or `none` to remove it.",
            validator =lambda a :self ._validate_max_length (a ,2048 )
            )
            if footer !="__SKIP__":
                boost ["footer"]=footer 

            footericon =await self ._prompt_field (
            ctx ,10 ,total ,"Footer Icon",
            "Send an image URL for the footer icon, or `none` to remove it.",
            validator =self ._validate_url 
            )
            if footericon !="__SKIP__":
                boost ["footericon"]=footericon 

            author =await self ._prompt_field (
            ctx ,11 ,total ,"Author Name",
            "Send a name to show in the embed author field (max 256 characters), or `none` to remove it.",
            validator =lambda a :self ._validate_max_length (a ,256 )
            )
            if author !="__SKIP__":
                boost ["author"]=author 

            authoricon =await self ._prompt_field (
            ctx ,12 ,total ,"Author Icon",
            "Send an image URL for the author icon, or `none` to remove it.",
            validator =self ._validate_url 
            )
            if authoricon !="__SKIP__":
                boost ["authoricon"]=authoricon 

            ping =await self ._prompt_field (
            ctx ,13 ,total ,"Ping Booster",
            "Should the booster be pinged when the message is sent? Reply `yes` or `no`.",
            validator =self ._validate_bool ,allow_clear =False 
            )
            if ping !="__SKIP__":
                boost ["ping"]=ping 

            timestamp =await self ._prompt_field (
            ctx ,14 ,total ,"Timestamp",
            "Should the embed show a timestamp? Reply `yes` or `no`.",
            validator =self ._validate_bool ,allow_clear =False 
            )
            if timestamp !="__SKIP__":
                boost ["timestamp"]=timestamp 

            autodel =await self ._prompt_field (
            ctx ,15 ,total ,"Auto-delete",
            "Send how many seconds boost messages should stay before being auto-deleted, or `0` to disable.",
            validator =self ._validate_autodel ,allow_clear =False 
            )
            if autodel !="__SKIP__":
                boost ["autodel"]=autodel 

        except SetupCancelled :
            await ctx .send (view =CV2 ("Cancelled",f"{CROSS} Setup cancelled. No changes were saved."))
            return 
        except SetupTimeout :
            await ctx .send (view =CV2 ("Timeout",f"{TIMER} Setup timed out. No changes were saved."))
            return 

        await self .update_boost_config (ctx .guild .id ,data )

        await ctx .send (view =CV2 (f"{TICK} Boost Setup Complete","Your boost message system has been fully configured. Use `boost preview` to see it, or `boost config` to review every setting."))

    @_boost .command (name ="thumbnail",help ="Set boost message thumbnail")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_thumbnail (self ,ctx ,thumbnail_url :str ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        if self ._is_clear_value (thumbnail_url ):
            data =await self .get_boost_config (ctx .guild .id )
            data ["boost"]["thumbnail"]=""
            await self .update_boost_config (ctx .guild .id ,data )
            await ctx.send(view=CV2("Success", f"{TICK} Successfully cleared the boost thumbnail."))
            return 

        if not self .url_pattern .match (thumbnail_url ):
            await ctx.send(view=CV2("Error", f"{CROSS} Please provide a valid URL."))

            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["thumbnail"]=thumbnail_url 
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully updated the boost thumbnail URL."))

    @_boost .command (name ="image",help ="Set boost message image")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_image (self ,ctx ,*,image_url :str ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        if self ._is_clear_value (image_url ):
            data =await self .get_boost_config (ctx .guild .id )
            data ["boost"]["image"]=""
            await self .update_boost_config (ctx .guild .id ,data )
            await ctx.send(view=CV2("Success", f"{TICK} Successfully cleared the boost image."))
            return 

        if not self .url_pattern .match (image_url ):
            await ctx.send(view=CV2("Error", f"{CROSS} Please provide a valid URL."))

            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["image"]=image_url 
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully updated the boost image URL."))

    @_boost .command (name ="autodel",help ="Set auto-delete timer for boost messages (0 to disable)")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_autodel (self ,ctx ,seconds :int ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        if seconds <0 :
            await ctx.send(view=CV2("Error", f"{CROSS} Auto-delete timer must be 0 or greater."))

            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["autodel"]=seconds 
        await self .update_boost_config (ctx .guild .id ,data )

        description =f"{TICK} Successfully set auto-delete timer to {seconds} seconds."
        if seconds ==0 :
            description =f"{TICK} Auto-delete has been disabled."

        await ctx.send(view=CV2("Success", description))

    @_boost .command (name ="title",help ="Set boost embed title")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_title (self ,ctx ,*,title :str ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        if self ._is_clear_value (title ):
            data =await self .get_boost_config (ctx .guild .id )
            data ["boost"]["title"]=""
            await self .update_boost_config (ctx .guild .id ,data )
            await ctx.send(view=CV2("Success", f"{TICK} Successfully cleared the boost embed title."))
            return 

        if len (title )>256 :
            await ctx.send(view=CV2("Error", f"{CROSS} Title must be 256 characters or fewer."))
            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["title"]=title 
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully updated the boost embed title."))

    @_boost .command (name ="description",aliases =["desc"],help ="Set boost embed description")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_description (self ,ctx ,*,description :str ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        if self ._is_clear_value (description ):
            data =await self .get_boost_config (ctx .guild .id )
            data ["boost"]["description"]=""
            await self .update_boost_config (ctx .guild .id ,data )
            await ctx.send(view=CV2("Success", f"{TICK} Successfully cleared the boost embed description."))
            return 

        if len (description )>4096 :
            await ctx.send(view=CV2("Error", f"{CROSS} Description must be 4096 characters or fewer."))
            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["description"]=description 
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully updated the boost embed description."))

    @_boost .command (name ="color",aliases =["colour"],help ="Set boost embed color")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_color (self ,ctx ,*,color :str ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        parsed =self .parse_color (color )
        if parsed is None :
            await ctx.send(view=CV2("Error", f"{CROSS} Invalid color. Use a hex code (`#5865F2` or `0x5865F2`), a name (`red`, `green`, `blue`, `orange`, `purple`, `gold`), `random`, or `reset`."))
            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["color"]=parsed 
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully updated the boost embed color to `#{parsed:06X}`."))

    @_boost .command (name ="footer",help ="Set boost embed footer text")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_footer (self ,ctx ,*,footer :str ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        if self ._is_clear_value (footer ):
            data =await self .get_boost_config (ctx .guild .id )
            data ["boost"]["footer"]=""
            await self .update_boost_config (ctx .guild .id ,data )
            await ctx.send(view=CV2("Success", f"{TICK} Successfully cleared the boost embed footer."))
            return 

        if len (footer )>2048 :
            await ctx.send(view=CV2("Error", f"{CROSS} Footer must be 2048 characters or fewer."))
            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["footer"]=footer 
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully updated the boost embed footer."))

    @_boost .command (name ="footericon",help ="Set boost embed footer icon URL")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_footericon (self ,ctx ,footericon_url :str ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        if self ._is_clear_value (footericon_url ):
            data =await self .get_boost_config (ctx .guild .id )
            data ["boost"]["footericon"]=""
            await self .update_boost_config (ctx .guild .id ,data )
            await ctx.send(view=CV2("Success", f"{TICK} Successfully cleared the boost embed footer icon."))
            return 

        if not self .url_pattern .match (footericon_url ):
            await ctx.send(view=CV2("Error", f"{CROSS} Please provide a valid URL."))
            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["footericon"]=footericon_url 
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully updated the boost embed footer icon."))

    @_boost .command (name ="author",help ="Set boost embed author name")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_author (self ,ctx ,*,author :str ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        if self ._is_clear_value (author ):
            data =await self .get_boost_config (ctx .guild .id )
            data ["boost"]["author"]=""
            await self .update_boost_config (ctx .guild .id ,data )
            await ctx.send(view=CV2("Success", f"{TICK} Successfully cleared the boost embed author."))
            return 

        if len (author )>256 :
            await ctx.send(view=CV2("Error", f"{CROSS} Author name must be 256 characters or fewer."))
            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["author"]=author 
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully updated the boost embed author."))

    @_boost .command (name ="authoricon",help ="Set boost embed author icon URL")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_authoricon (self ,ctx ,authoricon_url :str ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        if self ._is_clear_value (authoricon_url ):
            data =await self .get_boost_config (ctx .guild .id )
            data ["boost"]["authoricon"]=""
            await self .update_boost_config (ctx .guild .id ,data )
            await ctx.send(view=CV2("Success", f"{TICK} Successfully cleared the boost embed author icon."))
            return 

        if not self .url_pattern .match (authoricon_url ):
            await ctx.send(view=CV2("Error", f"{CROSS} Please provide a valid URL."))
            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["authoricon"]=authoricon_url 
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully updated the boost embed author icon."))

    @_boost .command (name ="timestamp",help ="Toggle the timestamp on the boost embed")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_timestamp (self ,ctx ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["timestamp"]=not data ["boost"].get ("timestamp",True )
        await self .update_boost_config (ctx .guild .id ,data )

        status ="enabled"if data ["boost"]["timestamp"]else "disabled"
        await ctx.send(view=CV2("Success", f"{TICK} Embed timestamp has been **{status}**."))

    @_boost .command (name ="message",help ="Set boost message content")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_message (self ,ctx ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 
        variables_text = (
            "Send your boost message in this channel now.\n\n**Available Variables:**\n"
            "```\n"
            "{server.name}         - Server name\n"
            "{server.id}           - Server ID\n"
            "{server.owner}        - Server owner\n"
            "{server.icon}         - Server icon URL\n"
            "{server.boost_count}  - Current boost count\n"
            "{server.boost_level}  - Boost level (e.g., Level 2)\n"
            "{server.member_count} - Total member count\n\n"
            "{user.name}        - Booster's display name\n"
            "{user.mention}     - Mention the booster\n"
            "{user.tag}         - Booster's full tag\n"
            "{user.id}          - Booster's ID\n"
            "{user.avatar}      - Booster's avatar URL\n"
            "{user.created_at}  - When the account was created\n"
            "{user.joined_at}   - When user joined the server\n"
            "{user.top_role}    - Booster's top role name\n"
            "{user.is_booster}  - Whether they're a booster\n"
            "{user.is_mobile}   - Whether on mobile\n"
            "{user.boosted_at}  - Boost timestamp\n"
            "```\n"
            "*These variables also work in `title`, `description`, `footer`, and `author`.*\n"
            "*You have 60 seconds to respond*"
        )
        await ctx.send(view=CV2(f"{TICK} Boost Message Setup", variables_text))

        def check (m ):
            return m .author ==ctx .author and m .channel ==ctx .channel 

        try :
            message =await self .bot .wait_for ('message',check =check ,timeout =60.0 )
        except asyncio .TimeoutError :
            await ctx.send(view=CV2("Timeout", f"{TIMER} Timeout! Please try again."))

            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["message"]=message .content 
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully updated the boost message."))

    @_boost .command (name ="embed",help ="Toggle embed formatting for boost messages")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_embed (self ,ctx ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["embed"]=not data ["boost"]["embed"]
        await self .update_boost_config (ctx .guild .id ,data )

        status ="enabled"if data ["boost"]["embed"]else "disabled"
        await ctx.send(view=CV2("Success", f"{TICK} Embed formatting has been **{status}**."))

    @_boost .command (name ="ping",help ="Toggle pinging the booster")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,2 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_ping (self ,ctx ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        data =await self .get_boost_config (ctx .guild .id )
        data ["boost"]["ping"]=not data ["boost"]["ping"]
        await self .update_boost_config (ctx .guild .id ,data )

        status ="enabled"if data ["boost"]["ping"]else "disabled"
        await ctx.send(view=CV2("Success", f"{TICK} Booster pinging has been **{status}**."))

    @_boost .group (name ="channel",help ="Manage boost notification channels")
    @blacklist_check ()
    @ignore_check ()
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_channel (self ,ctx ):
        if ctx .subcommand_passed is None :
            await ctx .send_help (ctx .command )
            ctx .command .reset_cooldown (ctx )

    @_boost_channel .command (name ="add",help ="Add a boost notification channel")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,3 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_channel_add (self ,ctx ,channel :discord .TextChannel ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        data =await self .get_boost_config (ctx .guild .id )
        channels =data ["boost"]["channel"]

        if len (channels )>=3 :
            await ctx.send(view=CV2("Error", f"{CROSS} Maximum boost channel limit reached (3 channels)."))
            return 

        if str (channel .id )in channels :
            await ctx.send(view=CV2("Error", f"{CROSS} This channel is already in the boost channels list."))
            return 

        channels .append (str (channel .id ))
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully added {channel.mention} to boost channels list."))

    @_boost_channel .command (name ="remove",help ="Remove a boost notification channel")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,3 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_channel_remove (self ,ctx ,channel :discord .TextChannel ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        data =await self .get_boost_config (ctx .guild .id )
        channels =data ["boost"]["channel"]

        if not channels :
            await ctx.send(view=CV2("Error", f"{CROSS} No boost channels are currently set up."))
            return 

        if str (channel .id )not in channels :
            await ctx.send(view=CV2("Error", f"{CROSS} This channel is not in the boost channels list."))
            return 

        channels .remove (str (channel .id ))
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully removed {channel.mention} from boost channels list."))

    @_boost .command (name ="preview",help ="Preview the boost embed/message without posting it to the boost channel")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,3 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_preview (self ,ctx ):
        data =await self .get_boost_config (ctx .guild .id )

        try :
            if data ["boost"]["embed"]:
                embed =await self .build_boost_embed (data ,ctx .author ,ctx .guild )
                ping_content =ctx .author .mention if data ["boost"]["ping"]else None 
                await ctx .send (content =f"**Preview** — this is exactly what will be sent when someone boosts:\n{ping_content or ''}",embed =embed )
            else :
                formatted =self .format_boost_message (data ["boost"]["message"],ctx .author ,ctx .guild )
                ping_content =f"{ctx.author.mention} "if data ["boost"]["ping"]else ""
                await ctx .send (f"**Preview** — this is exactly what will be sent when someone boosts:\n{ping_content}{formatted}")
        except Exception as e :
            await ctx.send(view=CV2("Error", f"{CROSS} An error occurred while generating the preview: `{str(e)}`"))

    @_boost .command (name ="test",help ="Test how the boost message will look by sending it to the boost channel(s)")
    @blacklist_check ()
    @ignore_check ()
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boost_test (self ,ctx ):
        data =await self .get_boost_config (ctx .guild .id )

        if not data ["boost"]["channel"]:
            await ctx.send(view=CV2("Error", f"{CROSS} Please set up a boost channel first using `boost channel add #channel`."))
            return 

        success ,message =await self .send_boost_message (ctx .guild ,ctx .author ,data )

        if success :
            await ctx.send(view=CV2("Success", message))
        else :
            await ctx.send(view=CV2("Error", message))


    @_boost .command (name ="config",help ="View current boost configuration")
    @blacklist_check ()
    @ignore_check ()
    @commands .has_permissions (administrator =True )
    async def _boost_config (self ,ctx ):
        data =await self .get_boost_config (ctx .guild .id )
        boost =data ["boost"]
        channels =boost ["channel"]

        channel_mentions =[]
        for channel_id in channels :
            channel =self .bot .get_channel (int (channel_id ))
            if channel :
                channel_mentions .append (channel .mention )

        def field (value :str )->str :
            return value if value else "Not configured"

        channels_str = "\n".join(channel_mentions) if channel_mentions else "Not configured"
        embed_status = f"{TICK} Enabled" if boost["embed"] else f"{CROSS} Disabled"
        ping_status = f"{TICK} Enabled" if boost["ping"] else f"{CROSS} Disabled"
        timestamp_status = f"{TICK} Enabled" if boost.get("timestamp", True) else f"{CROSS} Disabled"
        autodel_status = f"{boost['autodel']}s" if boost["autodel"] else "Disabled"
        color_value = boost.get("color", self.color)
        color_str = f"#{color_value:06X}"

        config_text = (
            f"**Channels**\n{channels_str}\n\n"
            f"**Message**\n```{boost['message']}```\n"
            f"**Title:** {field(boost.get('title', ''))}\n"
            f"**Description:** {field(boost.get('description', ''))}\n"
            f"**Color:** {color_str}\n"
            f"**Author:** {field(boost.get('author', ''))}\n"
            f"**Author Icon:** {field(boost.get('authoricon', ''))}\n"
            f"**Footer:** {field(boost.get('footer', ''))}\n"
            f"**Footer Icon:** {field(boost.get('footericon', ''))}\n"
            f"**Thumbnail:** {field(boost.get('thumbnail', ''))}\n"
            f"**Image:** {field(boost.get('image', ''))}\n"
            f"**Timestamp:** {timestamp_status}\n"
            f"**Embed:** {embed_status}\n"
            f"**Ping:** {ping_status}\n"
            f"**Auto-delete:** {autodel_status}"
        )

        await ctx.send(view=CV2(f"{NITRO_BOOST} Boost Configuration for {ctx.guild.name}", config_text))

    @_boost .command (name ="reset",help ="Reset boost configuration")
    @commands .cooldown (1 ,5 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @blacklist_check ()
    @ignore_check ()
    @commands .has_permissions (administrator =True )
    async def _boost_reset (self ,ctx ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        data =await self .get_boost_config (ctx .guild .id )

        if not data ["boost"]["channel"]:
            await ctx.send(view=CV2("Error", f"{CROSS} No boost configuration found to reset."))
            return 

        defaults =self ._default_config ()
        data ["boost"]=defaults ["boost"]

        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully reset all boost configuration."))

    @commands .group (name ="boostrole",invoke_without_command =True ,help ="Manage boost roles")
    @commands .cooldown (1 ,5 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @blacklist_check ()
    @ignore_check ()
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boostrole (self ,ctx ):
        if ctx .subcommand_passed is None :
            await ctx .send_help (ctx .command )
            ctx .command .reset_cooldown (ctx )

    @_boostrole .command (name ="config",help ="View boost role configuration")
    @commands .cooldown (1 ,5 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @blacklist_check ()
    @ignore_check ()
    @commands .has_permissions (administrator =True )
    async def _boostrole_config (self ,ctx ):
        data =await self .get_boost_config (ctx .guild .id )
        role_ids =data ["boost_roles"]["roles"]

        if not role_ids :
            await ctx.send(view=CV2(f"Boost Roles - {ctx.guild.name}", "No boost roles configured."))
            return 

        roles =[]
        for role_id in role_ids :
            role =ctx .guild .get_role (int (role_id ))
            if role :
                roles .append (role .mention )

        roles_str = "\n".join(roles) if roles else "No valid roles found"
        await ctx.send(view=CV2(f"Boost Roles - {ctx.guild.name}", roles_str))

    @_boostrole .command (name ="add",help ="Add a boost role")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,3 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boostrole_add (self ,ctx ,role :discord .Role ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        data =await self .get_boost_config (ctx .guild .id )
        roles =data ["boost_roles"]["roles"]

        if len (roles )>=10 :
            await ctx.send(view=CV2("Error", f"{CROSS} Maximum boost role limit reached (10 roles)."))
            return 

        if str (role .id )in roles :
            await ctx.send(view=CV2("Error", f"{CROSS} {role.mention} is already a boost role."))
            return 

        roles .append (str (role .id ))
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} {role.mention} has been added as a boost role."))

    @_boostrole .command (name ="remove",help ="Remove a boost role")
    @blacklist_check ()
    @ignore_check ()
    @commands .cooldown (1 ,3 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @commands .has_permissions (administrator =True )
    async def _boostrole_remove (self ,ctx ,role :discord .Role ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        data =await self .get_boost_config (ctx .guild .id )
        roles =data ["boost_roles"]["roles"]

        if not roles :
            await ctx.send(view=CV2("Error", f"{CROSS} No boost roles are currently configured."))
            return 

        if str (role .id )not in roles :
            await ctx.send(view=CV2("Error", f"{CROSS} {role.mention} is not a boost role."))
            return 

        roles .remove (str (role .id ))
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} {role.mention} has been removed from boost roles."))

    @_boostrole .command (name ="reset",help ="Reset boost role configuration")
    @commands .cooldown (1 ,3 ,commands .BucketType .user )
    @commands .max_concurrency (1 ,per =commands .BucketType .default ,wait =False )
    @commands .guild_only ()
    @blacklist_check ()
    @ignore_check ()
    @commands .has_permissions (administrator =True )
    async def _boostrole_reset (self ,ctx ):
        if not self .is_authorized (ctx ):
            await self .send_permission_error (ctx )
            return 

        data =await self .get_boost_config (ctx .guild .id )

        if not data ["boost_roles"]["roles"]:
            await ctx.send(view=CV2("Error", f"{CROSS} No boost roles are currently configured."))
            return 

        data ["boost_roles"]["roles"]=[]
        await self .update_boost_config (ctx .guild .id ,data )

        await ctx.send(view=CV2("Success", f"{TICK} Successfully cleared all boost roles."))

async def setup (bot ):
    await bot .add_cog (Booster(bot ))
