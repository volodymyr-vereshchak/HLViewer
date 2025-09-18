import os

from dotenv import load_dotenv

load_dotenv()

backend_settings = {
    "POSTGRES_USER": os.getenv("POSTGRES_USER"),
    "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD"),
    "DB_HOST": os.getenv("DB_HOST"),
    "DB_PORT": os.getenv("DB_PORT"),
    "POSTGRES_DB": os.getenv("POSTGRES_DB"),
    "HOSTLIB_PATH": os.getenv("HOSTLIB_PATH"),
    "CHUNK_SIZE": 1000,
    "BOT_TOKEN": os.getenv("BOT_TOKEN"),
    "CHAT_ID": os.getenv("CHAT_ID"),
    "SENDER_EMAIL": "volodymyr.vereshchak@gmail.com",
    "EMAIL_PASSWORD": os.getenv("EMAIL_PASSWORD"),
    "EMAIL_RECEIVERS": ["v.vereshchak@zp.naftogaz.com"],
    "LINES_IDS": [6, 9, 10, 26, 25, 24, 23, 21, 11, 13, 20, 22, 17, 15, 16],
    "HIGH_P_LINES_IDS": [6, 11, 13, 17],
}
