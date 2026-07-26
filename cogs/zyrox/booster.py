import discord 
from utils.emoji import BOOST
from discord .ext import commands 
class __boost(commands .Cog ):
    def __init__ (self ,bot ):
        self .bot =bot 
    """Boost commands"""
    def help_custom (self ):
              emoji =BOOST
              label ="Boost Commands"
              description ="Show you the commands of boost"
              return emoji ,label ,description 
    @commands .group ()
    async def __Boost__ (self ,ctx :commands .Context ):
        """`boost thumbnail` , `boost image` , `boost autodel` , `boost title` , `boost description` , `boost color` , `boost footer` , `boost footericon` , `boost author` , `boost authoricon` , `boost timestamp` , `boost message` , `boost embed` , `boost ping` , `boost channel add` , `boost channel remove` , `boost preview` , `boost test` , `boost config` , `boost reset` , `boostrole add` , `boostrole remove` , `boostrole config` , `boostrole reset`"""
        pass
