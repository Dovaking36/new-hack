import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# GigaChat credentials
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS", "MDE5Y2Q2OTYtMTk2ZC03YzVjLTgxZTQtOTk5NjhlNWRjYWFlOjFjZWU1YjI4LWRiYWUtNGIxMS05NGMyLTBlYmQ4NWEyMTVhYw==")

# Telegram bot token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8562857508:AAFW3w8W2u44fYte2LZCoorZ9pfOgieYKkc")

# Paths
BASE_DIR = Path(__file__).parent
HISTORY_DIR = BASE_DIR / "chat_history"
TXT_EXPORT_DIR = BASE_DIR / "txt_exports"

# Analysis settings
ANALYSIS_INTERVAL = 1500  # seconds

# Create directories if not exist
HISTORY_DIR.mkdir(exist_ok=True)
TXT_EXPORT_DIR.mkdir(exist_ok=True)
