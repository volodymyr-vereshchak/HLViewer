import os

from dotenv import load_dotenv

load_dotenv()

frontend_settings = {
    "API_URL": os.getenv("API_URL"),
    "API_PORT": os.getenv("API_PORT"),
}
