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
    # DPD API Configuration
    "DPD_API_BASE_URL": "https://rest-direct.zp.iot.grmu.com.ua/api/v1/",
    "DPD_AUTH_URL": "https://auth-direct.zp.iot.grmu.com.ua/auth/login",
    "DPD_USERNAME": os.getenv("DPD_USERNAME", "zaporizhDirect"),
    "DPD_PASSWORD": os.getenv("DPD_PASSWORD", "xTqYaRmlYQFY"),
    "DPD_TIMEOUT": 30,
    "ENTERPRISE_MAPPINGS_PATH": os.getenv(
        "ENTERPRISE_MAPPINGS_PATH",
        "backend/data/enterprise_mappings.xlsx"
    ),
}
