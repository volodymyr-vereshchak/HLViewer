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
    "BOT_TOKEN": os.getenv("BOT_TOKEN"),
    "CHAT_ID": os.getenv("CHAT_ID"),
    "SENDER_EMAIL": "volodymyr.vereshchak@gmail.com",
    "EMAIL_PASSWORD": os.getenv("EMAIL_PASSWORD"),
    "EMAIL_RECEIVERS": ["v.vereshchak@zp.naftogaz.com"],
    # DEPRECATED: replaced by include_in_report / is_high_pressure fields in
    # gas_volume_line and virtual_line tables (migration 6f7a8b9c0d1e).
    # Used only as fallback in hostlib_updater when DB has no flagged lines yet.
    # Remove after verifying that DB flags are set for all branches.
    "LINES_IDS": [6, 11, 16, 17, 18, 19, 20, 21, 1001, 1002, 1003, 1004],
    "HIGH_P_LINES_IDS": [6, 1002],
    # DPD API Configuration — dev/fallback credentials.
    # In production each branch loads its own credentials from
    # grmu_branch_dpd_credential (via DPDClient.for_branch()).
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
