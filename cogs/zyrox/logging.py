import discord 
from utils.emoji import CAST
from discord .ext import commands 
class _logging (commands .Cog ):
    def __init__ (self ,bot ):
        self .bot =bot 
    """Logging commands"""
    def help_custom (self ):
		      emoji =CAST
		      label ="Logging Commands"
		      description ="Shows you the commands of logging"
		      return emoji ,label ,description 
    @commands .group ()
    async def __Logging__ (self ,ctx :commands .Context ):
        """`log setup` , `log setall` , `log status` , `log config` , `log toggle` , `log ignore` , `log search` , `log test` , `log export` , `log reset`"""
		
