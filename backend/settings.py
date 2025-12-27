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
    # GRS lines for reports: virtual lines (1001-1004) + physical lines not in rings
    "LINES_IDS": [6, 11, 16, 17, 18, 19, 20, 21, 1001, 1002, 1003, 1004],
    "HIGH_P_LINES_IDS": [6, 1002],  # Updated: 1002 (Кольцо Быт) instead of individual lines
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
