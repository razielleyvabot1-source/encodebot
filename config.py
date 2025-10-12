from logging import WARNING
import os


API_ID = os.environ.get("API_ID","23810582")
API_HASH = os.environ.get( "API_HASH","079750c767b2d4154acfff724a1a6b6e")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8049618344:AAHufMuSrA7kgvOIX1PpuXP6FAWqm793si4") 
ADMINS_IDS = [int(x) for x in os.environ.get("ADMINS", "5644237743,6586671402").split(",") if x]
USERS = [int(x) for x in os.environ.get("USERS", "").split(",") if x]
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://elizaleyvaperez4338_db_user:4338@razielbd.w0hm6yh.mongodb.net")
DATABASE_NAME = os.environ.get("DATABASE_NAME", "encodebot1")
BOT_IS_PUBLIC = os.environ.get("BOT_IS_PUBLIC", "false").lower() == "true"
