import os

from dotenv import load_dotenv

load_dotenv()

backend_settings = {
    "DB_USERNAME": os.getenv("DB_USERNAME"),
    "DB_PASSWORD": os.getenv("DB_PASSWORD"),
    "DB_HOST": os.getenv("DB_HOST"),
    "DB_PORT": os.getenv("DB_PORT"),
    "DB_NAME": os.getenv("DB_NAME"),
    "HOSTLIB_PATH": os.getenv("HOSTLIBS_PATH"),
    "CHUNK_SIZE": int(os.getenv("CHUNK_SIZE")),
}
