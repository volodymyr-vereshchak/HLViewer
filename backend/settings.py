import os

from dotenv import load_dotenv

load_dotenv()

backend_settings = {
    "DEBUG": os.getenv("DEBUG", "false"),
    "POSTGRES_USER": os.getenv("POSTGRES_USER"),
    "POSTGRES_PASSWORD": os.getenv("POSTGRES_PASSWORD"),
    "DB_HOST": os.getenv("DB_HOST"),
    "DB_PORT": os.getenv("DB_PORT"),
    "POSTGRES_DB": os.getenv("POSTGRES_DB"),
    "CHUNK_SIZE": 1000,
    # Commercial-day start hour (07:00 → 07:00). Global project setting; the
    # frontend reads it from GET /config. Override per-deployment in the env file.
    "CONTRACT_HOUR": int(os.getenv("CONTRACT_HOUR", "7")),
    # Max simultaneous connections to the DPD API. get_volumes fans out one
    # request per device; the pooled client caps concurrency at this value and
    # reuses keep-alive connections instead of opening hundreds at once.
    "DPD_MAX_CONCURRENCY": int(os.getenv("DPD_MAX_CONCURRENCY", "10")),
    "BOT_TOKEN": os.getenv("BOT_TOKEN"),
    "CHAT_ID": os.getenv("CHAT_ID"),
    "SENDER_EMAIL": os.getenv("SENDER_EMAIL", ""),
    "EMAIL_PASSWORD": os.getenv("EMAIL_PASSWORD"),
    "EMAIL_RECEIVERS": [x for x in os.getenv("EMAIL_RECEIVERS", "").split(",") if x],
    "ENTERPRISE_MAPPINGS_PATH": os.getenv(
        "ENTERPRISE_MAPPINGS_PATH",
        "backend/data/enterprise_mappings.xlsx"
    ),
}
