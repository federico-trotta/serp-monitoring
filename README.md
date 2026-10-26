# SERP Monitor

A Python Google SERP monitor built on ScrapingBee's Google Search API. Each time it runs, it reads a keyword list, fetches the results page, keeps the raw JSON as a gzipped snapshot, parses the ranking, SERP features and AI Overview citations, and stores everything in SQLite. It alerts you on Slack when a change is still there on a second, confirming run.

## Repository layout

```
serp-monitor/
├── .github/workflows/serp-monitor.yml   # GitHub Actions schedule, with state persistence
├── examples/
│   ├── keywords2.csv                    # city-level example row (Chicago)
│   └── local_check.py                   # quick check of a latitude/longitude request
├── scripts/run_monitor.sh               # cron wrapper (flock lock + logging)
├── serp_monitor/
│   ├── __init__.py
│   ├── config.py                        # .env loading and constants
│   ├── fetch.py                         # keyword CSV, domain helpers, fetch_serp()
│   ├── parse.py                         # ranking, series_key, features, AIO citations
│   ├── db.py                            # SQLite schema, snapshots, save_run()
│   └── alerts.py                        # detection, confirmation and Slack notifications
├── .env.example
├── .gitignore
├── keywords.csv
├── main.py                              # entry point: one full monitoring pass
├── requirements.txt
└── README.md
```

## Installation

```bash
git clone <your-repo-url> serp-monitor
cd serp-monitor
python -m venv venv
source venv/bin/activate        # On Mac/Linux. On Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # Put your real SCRAPINGBEE_API_KEY in .env
```

## Configuration

**`.env`**
- `SCRAPINGBEE_API_KEY` (required): copy it from your ScrapingBee dashboard.
- `SLACK_WEBHOOK_URL` (optional): a Slack incoming webhook. If it isn't set, confirmed alerts are printed to the log and stay unsent, and they are retried on the next run.
- `SERP_PAGES` (default `1`), `SERP_LIGHT_REQUEST` (default `false`), `SERP_LOCALE_DEFAULT` (default `en-US`), `SERP_DB_PATH`, `SERP_SNAPSHOT_DIR`, `SERP_KEYWORDS_CSV`.

`SERP_LIGHT_REQUEST=true` uses light requests: 10 credits per call, no browser rendering and **no AI Overviews**. Regular requests cost 15 credits per call and can return AI Overviews when Google shows one.

**`keywords.csv`**

| Column | Required | Notes |
|---|---|---|
| `keyword` | yes | The search query |
| `target_domain` | yes | Your domain. Subdomains count as yours |
| `country_code` | yes | ISO 3166-1 alpha-2, default `us` |
| `device` | yes | `desktop` or `mobile` |
| `locale` | no | Sent as `extra_params=locale=...` (interface language) |
| `latitude`, `longitude` | no | City-level location. Supply both |
| `radius` | no | In metres. Only used when both coordinates are set |

## Usage

```bash
python main.py                 # process keywords.csv
python main.py other.csv       # use another keyword file lke examples/keywords2.csv
python examples/local_check.py # print the raw response for the first row of examples/keywords2.csv
```

Each run:
1. Writes a row to `runs` for every attempt, including failed ones. Only successful responses get a snapshot in `snapshots/YYYY-MM-DD/<run_id>.json.gz`.
2. Stores the target's best organic result, the state of each SERP feature (`1`/`0`, or `NULL` when unknown) and the AI Overview citation URLs.
3. Checks the alert rules and commits each run's observations and alert transitions in a single transaction.
4. Sends confirmed alerts that haven't been sent yet to Slack, after the commit.
5. Prints a summary of successful and failed requests, pending and confirmed alerts, and notification results.

Exit codes: `0` = finished (failures for individual keywords are recorded in the database), `1` = every request failed, `2` = configuration error (missing API key or invalid CSV).

### Alert rules

| Rule | Trigger |
|---|---|
| `ranking_drop` | The target falls at least 3 positions from a baseline in the top 5 |
| `top10_exit` | The target was in the top 10 and is now below position 10, or is no longer found in a response that has at least 10 organic results |
| `aio_change` | An AI Overview appears or disappears. Only uses regular requests where both states are known |
| `ranking_url_change` | A different target URL becomes the best-ranking match |
| `title_change` | Google returns a different, non-empty title for the same URL |

How alerts move through their states:
- When a rule first triggers, the alert is `pending`.
- The next successful run is compared with the same original baseline. If the condition still holds, the alert becomes `confirmed`. If the change has reversed, it becomes `cancelled`. If the result is unknown or the request failed, it stays `pending`.
- A confirmed alert becomes `resolved` once a later run shows the ranking or feature has recovered.

If the target isn't in the results, the monitor reports it as "not found within fetched depth". It is never recorded as position 0 or 11.

## Scheduling

Scheduling can be made alternatively with Chron or GitHub actions

### cron (single host)

```cron
0 6 * * * /absolute/path/to/serp-monitor/scripts/run_monitor.sh
```

The wrapper script:
- changes into the repo directory
- calls `venv/bin/python` directly
- holds a `flock` lock, so a new run is skipped while the previous one is still going
- appends all output to `logs/monitor.log`

`flock` comes with Linux (util-linux). On macOS, install it with `brew install flock`.

### GitHub Actions

`.github/workflows/serp-monitor.yml` runs every day at 06:00 UTC and can also be started manually.

1. Add the repository secrets `SCRAPINGBEE_API_KEY` and, optionally, `SLACK_WEBHOOK_URL`.
2. For the very first run, start the workflow manually with **allow_empty_state = true**.
3. Every run restores the newest `serp-monitor-state` artifact (`monitor.db` plus `snapshots/`) and saves the updated state at the end, even if some requests failed.
4. If no saved state can be found, the run stops instead of quietly starting with an empty database.
5. Scheduled and manual runs share one concurrency group, and a run that is already writing is never cancelled.
6. Artifacts are kept for 90 days, so the workflow has to run at least once in any 90-day period or the state is lost.

Before you leave it running unattended, run it twice in a row against the same database. Check that the run count goes up, that older snapshots are still there, and that pending alerts survive between runs.

## Cost estimate (30 days, page 1 only)

| Monitoring setup | Calls | Light credits | Regular credits |
|---|---|---|---|
| 200 keywords, one location, desktop | 6,000 | 60,000 | 90,000 |
| 500 keywords, one location, desktop and mobile | 30,000 | 300,000 | 450,000 |
| 50 keywords, five cities, desktop | 7,500 | 75,000 | 112,500 |

Light requests cost 10 credits and regular requests 15. Repeated successful calls are billed as well.