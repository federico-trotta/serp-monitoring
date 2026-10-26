"""Configuration and constants for the SERP monitor.

Paths are resolved relative to the current working directory (as in the
original article), so always start the monitor from the repository root.
Every constant can be overridden with an environment variable or `.env`.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


API_KEY = os.getenv("SCRAPINGBEE_API_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

DB_PATH = Path(os.getenv("SERP_DB_PATH", "monitor.db"))
SNAPSHOT_DIR = Path(os.getenv("SERP_SNAPSHOT_DIR", "snapshots"))
KEYWORDS_CSV = Path(os.getenv("SERP_KEYWORDS_CSV", "keywords.csv"))

LOCALE_DEFAULT = os.getenv("SERP_LOCALE_DEFAULT", "en-US")
PAGES = int(os.getenv("SERP_PAGES", "1"))
# False = regular request (15 credits/page, can return AI Overviews).
# True  = light request (10 credits/page, no AI Overviews).
LIGHT_REQUEST = _env_bool("SERP_LIGHT_REQUEST", False)
