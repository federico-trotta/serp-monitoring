"""Quick visual check of city-level (latitude/longitude/radius) results.

Reads the first row of examples/keywords2.csv (or a path given on the command
line) and prints the raw Google Search API response.

    python examples/local_check.py
    python examples/local_check.py path/to/other.csv
"""
import csv
import os
import sys

from dotenv import load_dotenv
from scrapingbee import ScrapingBeeClient

load_dotenv()
API_KEY = os.getenv("SCRAPINGBEE_API_KEY")
if not API_KEY:
    raise SystemExit("Set SCRAPINGBEE_API_KEY in .env or the environment.")
client = ScrapingBeeClient(api_key=API_KEY)

DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keywords2.csv")


def send_request(path=DEFAULT_CSV):
    with open(path, newline="", encoding="utf-8") as fh:
        row = next(csv.DictReader(fh), None)
    if not row or not (row.get("latitude") or "").strip() or not (row.get("longitude") or "").strip():
        raise SystemExit(f"The first row of {path} must contain latitude and longitude.")
    params = {
        "latitude": row["latitude"].strip(),
        "longitude": row["longitude"].strip(),
        "device": (row.get("device") or "desktop").strip(),
    }
    if (row.get("radius") or "").strip():
        params["radius"] = row["radius"].strip()
    response = client.google_search(
        (row.get("keyword") or "coffee shops").strip(),
        params=params,
        timeout=60,
    )
    print("Response HTTP Status Code:", response.status_code)
    print("Response HTTP Response Body:", response.content)


if __name__ == "__main__":
    send_request(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV)
