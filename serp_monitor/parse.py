"""Parsing of rankings, SERP feature states and AI Overview citations."""
import json

from . import config
from .fetch import normalize_host, owns_domain

FEATURE_FIELDS = (
    "ai_overviews", "questions", "local_results", "top_stories",
    "top_ads", "bottom_ads", "knowledge_graph",
)


def parse_ranking(payload, target_domain):
    """Return the target's best-ranking organic result, or None if not found."""
    matches = []
    for result in payload.get("organic_results", []):
        url = result.get("url") or result.get("link") or ""
        host = normalize_host(url)
        if owns_domain(host, target_domain):
            try:
                position = int(result["position"])
            except (KeyError, TypeError, ValueError):
                continue
            matches.append({
                "position": position,
                "url": url,
                "domain": host,
                "title": result.get("title"),
                "description": result.get("description") or result.get("snippet"),
            })
    return min(matches, key=lambda item: item["position"]) if matches else None


def organic_depth(payload):
    """Number of organic results carrying a valid integer position."""
    count = 0
    for result in payload.get("organic_results", []):
        try:
            int(result["position"])
        except (KeyError, TypeError, ValueError):
            continue
        count += 1
    return count


def series_key(row):
    """Stable identity of a comparable series (keyword + all request settings)."""
    cfg = {
        "keyword": row["keyword"].strip(),
        "target_domain": normalize_host(row["target_domain"]),
        "country_code": row.get("country_code") or "us",
        "locale": row.get("locale") or config.LOCALE_DEFAULT,
        "device": row.get("device") or "desktop",
        "latitude": row.get("latitude") or None,
        "longitude": row.get("longitude") or None,
        "radius": row.get("radius") or None,
        "pages": config.PAGES,
        "light_request": config.LIGHT_REQUEST,
    }
    return json.dumps(cfg, sort_keys=True, separators=(",", ":")), cfg


def feature_states(payload):
    """True = observed, False = returned empty, None = unknown (missing/wrong type)."""
    states = {}
    for name in FEATURE_FIELDS:
        if name not in payload:
            states[name] = None
            continue
        value = payload[name]
        if isinstance(value, (list, dict)):
            states[name] = bool(value)
        else:
            states[name] = None
    return states


def collect_urls(value):
    """Collect URL-shaped strings recursively without assuming one schema path."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and key.lower() in {
                "url", "link", "href", "source_url", "source_link"
            }:
                if child.startswith(("https://", "http://")):
                    found.append(child)
            else:
                found.extend(collect_urls(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(collect_urls(child))
    return found


def parse_aio_citations(payload, target_domain):
    aio = payload.get("ai_overviews")
    if not isinstance(aio, (dict, list)) or not aio:
        return None  # No reliable citation series for this response
    urls = collect_urls(aio)
    # Preserve first-seen order while deduplicating normalized URLs.
    unique = []
    seen = set()
    for url in urls:
        key = url.strip().rstrip("/")
        if key not in seen:
            seen.add(key)
            unique.append(url)
    return [
        {"citation_rank": i, "url": url,
         "domain": normalize_host(url),
         "is_target": owns_domain(url, target_domain)}
        for i, url in enumerate(unique, start=1)
    ]
