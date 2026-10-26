"""SQLite schema, raw snapshot storage and run persistence."""
import gzip
import json
import sqlite3
import uuid

from . import config
from .fetch import normalize_host, now_utc
from .parse import feature_states, parse_aio_citations, parse_ranking, series_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, series_key TEXT NOT NULL, keyword TEXT NOT NULL,
    target_domain TEXT NOT NULL, country_code TEXT, locale TEXT, device TEXT,
    latitude REAL, longitude REAL, radius INTEGER, fetched_at TEXT NOT NULL,
    request_config_json TEXT NOT NULL, light_request INTEGER NOT NULL,
    credits TEXT, status TEXT NOT NULL, error TEXT, raw_path TEXT
);
CREATE TABLE IF NOT EXISTS organic_results (
    run_id TEXT NOT NULL REFERENCES runs(id), position INTEGER NOT NULL,
    url TEXT NOT NULL, domain TEXT, title TEXT, description TEXT,
    PRIMARY KEY (run_id, position, url)
);
CREATE TABLE IF NOT EXISTS serp_features (
    run_id TEXT NOT NULL REFERENCES runs(id), feature TEXT NOT NULL,
    present INTEGER, PRIMARY KEY (run_id, feature)
);
CREATE TABLE IF NOT EXISTS aio_citations (
    run_id TEXT NOT NULL REFERENCES runs(id), citation_rank INTEGER,
    url TEXT NOT NULL, domain TEXT, PRIMARY KEY (run_id, url)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    baseline_run_id TEXT REFERENCES runs(id), rule TEXT NOT NULL,
    detail TEXT NOT NULL, state TEXT NOT NULL,
    created_at TEXT NOT NULL, sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_series_time
    ON runs(series_key, fetched_at);
CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(state);
"""


def connect_db(path=None):
    con = sqlite3.connect(path or config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def save_snapshot(run_id, fetched_at, payload):
    """Write the raw payload as gzipped JSON. Never overwrites earlier checks."""
    folder = config.SNAPSHOT_DIR / fetched_at[:10]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run_id}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return str(path)


def save_run(con, row, payload, error, cost, request_id, commit=True):
    """Persist one attempt (successful or failed) and its parsed observations.

    All inserts for the run happen in one transaction. With commit=True (the
    default) the transaction is committed here and rolled back on any error.
    Pass commit=False when the caller wants to commit the run together with
    other writes (e.g. alert transitions) - the caller must then call
    con.commit() / con.rollback() itself.
    """
    fetched_at = now_utc()
    run_id = str(uuid.uuid4())
    key, cfg = series_key(row)
    status = "ok" if payload is not None and error is None else "error"
    try:
        raw_path = save_snapshot(run_id, fetched_at, payload) if payload else None
        con.execute(
            """INSERT INTO runs (id, series_key, keyword, target_domain, country_code,
               locale, device, latitude, longitude, radius, fetched_at,
               request_config_json, light_request, credits, status, error, raw_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, key, row["keyword"], normalize_host(row["target_domain"]),
             row.get("country_code") or "us", row.get("locale") or config.LOCALE_DEFAULT,
             row.get("device") or "desktop",
             float(row["latitude"]) if row.get("latitude") else None,
             float(row["longitude"]) if row.get("longitude") else None,
             int(float(row["radius"])) if row.get("radius") else None, fetched_at,
             json.dumps({**cfg, "request_id": request_id}, sort_keys=True),
             int(config.LIGHT_REQUEST), cost, status, error, raw_path),
        )
        if payload:
            ranking = parse_ranking(payload, row["target_domain"])
            if ranking:
                con.execute(
                    "INSERT INTO organic_results (run_id, position, url, domain, title, description) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, ranking["position"], ranking["url"], ranking["domain"],
                     ranking["title"], ranking["description"]),
                )
            for feature, present in feature_states(payload).items():
                con.execute(
                    "INSERT INTO serp_features (run_id, feature, present) VALUES (?, ?, ?)",
                    (run_id, feature, None if present is None else int(present)),
                )
            citations = parse_aio_citations(payload, row["target_domain"])
            if citations:
                con.executemany(
                    "INSERT OR IGNORE INTO aio_citations (run_id, citation_rank, url, domain) "
                    "VALUES (?, ?, ?, ?)",
                    [(run_id, c["citation_rank"], c["url"], c["domain"]) for c in citations],
                )
        if commit:
            con.commit()
    except Exception:
        if commit:
            con.rollback()
        raise
    return run_id, key, status
