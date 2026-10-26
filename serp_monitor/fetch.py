"""Keyword loading, domain helpers and SERP fetching via ScrapingBee."""
import csv
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from scrapingbee import ScrapingBeeClient

from . import config

_client = None


def get_client():
    """Create the ScrapingBee client lazily; stop if the API key is missing."""
    global _client
    if _client is None:
        if not config.API_KEY:
            raise SystemExit("Set SCRAPINGBEE_API_KEY in .env or the environment.")
        _client = ScrapingBeeClient(api_key=config.API_KEY)
    return _client


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def load_keywords(path=None):
    path = path or config.KEYWORDS_CSV
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    required = {"keyword", "target_domain", "country_code", "device"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"CSV must contain columns: {sorted(required)}")
    return rows


def normalize_host(value):
    value = (value or "").strip().lower()
    if "://" not in value:
        value = "https://" + value
    host = (urlparse(value).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def owns_domain(result_host, target_domain):
    host, target = normalize_host(result_host), normalize_host(target_domain)
    return bool(host and target and (host == target or host.endswith("." + target)))


def fetch_serp(row, pages=None, light_request=None):
    """Fetch one SERP. Returns (payload, error, cost, request_id).

    The error is returned separately from the payload so a failed request is
    never interpreted as a ranking loss.
    """
    pages = config.PAGES if pages is None else pages
    light_request = config.LIGHT_REQUEST if light_request is None else light_request
    client = get_client()

    params = {
        "country_code": row.get("country_code") or "us",
        "device": row.get("device") or "desktop",
        "page": 1,
        "pages": pages,
        "light_request": str(light_request).lower(),
        "extra_params": f"locale={row.get('locale') or config.LOCALE_DEFAULT}",
    }
    if row.get("latitude") and row.get("longitude"):
        params["latitude"] = float(row["latitude"])
        params["longitude"] = float(row["longitude"])
        if row.get("radius"):
            params["radius"] = int(float(row["radius"]))

    last_error = None
    cost = "unknown"
    request_id = "unknown"
    for attempt in range(3):
        try:
            response = client.google_search(row["keyword"], params=dict(params), timeout=60)
        except requests.RequestException as exc:
            last_error = f"request error: {type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return None, last_error, cost, request_id

        cost = response.headers.get("Spb-cost") or "unknown"
        request_id = response.headers.get("Spb-request-id") or "unknown"
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                return None, "invalid JSON response", cost, request_id
            if not isinstance(payload, dict) or not isinstance(payload.get("organic_results"), list):
                return None, "missing or invalid organic_results", cost, request_id
            return payload, None, cost, request_id

        last_error = f"HTTP {response.status_code}"
        # Auth / invalid-parameter errors must be fixed, not retried.
        if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
            break
        time.sleep(2 ** attempt)
    return None, last_error, cost, request_id
