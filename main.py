"""Entry point: run one full monitoring pass over keywords.csv.

Usage:
    python main.py                 # uses keywords.csv
    python main.py my_keywords.csv # custom keyword file

Exit codes:
    0  completed (individual keyword failures are recorded in `runs`)
    1  every request failed
    2  configuration error (missing API key, invalid CSV, ...)
"""
import sys

from serp_monitor import config
from serp_monitor.alerts import evaluate_alerts, send_pending_notifications
from serp_monitor.db import connect_db, save_run
from serp_monitor.fetch import fetch_serp, get_client, load_keywords
from serp_monitor.parse import organic_depth


def run(keywords_path=None):
    # Configuration errors stop execution with a nonzero exit status.
    try:
        get_client()
        rows = load_keywords(keywords_path)
    except SystemExit as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"[config] Could not load keywords: {exc}", file=sys.stderr)
        return 2

    con = connect_db()
    summary = {"ok": 0, "failed": 0, "new_pending": 0, "confirmed": 0,
               "cancelled": 0, "resolved": 0}
    try:
        for row in rows:
            keyword = row.get("keyword", "").strip()
            if not keyword or not (row.get("target_domain") or "").strip():
                print(f"[skip] row without keyword/target_domain: {row}")
                continue

            payload, error, cost, request_id = fetch_serp(row)
            try:
                # Run observations and alert transitions are committed together.
                run_id, series, status = save_run(
                    con, row, payload, error, cost, request_id, commit=False)
                if status == "ok":
                    stats = evaluate_alerts(con, run_id, series, organic_depth(payload))
                    for name, value in stats.items():
                        summary[name] += value
                con.commit()
            except Exception as exc:
                con.rollback()
                print(f"[error] {keyword!r}: could not persist run: {exc}", file=sys.stderr)
                summary["failed"] += 1
                continue

            if status == "ok":
                summary["ok"] += 1
                print(f"[ok] {keyword!r} run={run_id} credits={cost} request_id={request_id}")
            else:
                summary["failed"] += 1
                print(f"[fail] {keyword!r} run={run_id} error={error} request_id={request_id}")

        # Notifications are sent outside the write transaction.
        sent, notify_failed = send_pending_notifications(con)
        pending_total = con.execute(
            "SELECT COUNT(*) FROM alerts WHERE state = 'pending'").fetchone()[0]
        confirmed_total = con.execute(
            "SELECT COUNT(*) FROM alerts WHERE state = 'confirmed'").fetchone()[0]
    finally:
        con.close()

    print(
        "[summary] requests ok={ok} failed={failed} | alerts new_pending={new_pending} "
        "confirmed={confirmed} cancelled={cancelled} resolved={resolved} | "
        .format(**summary)
        + f"active pending={pending_total} active confirmed={confirmed_total} | "
        f"notifications sent={sent} failed={notify_failed} | "
        f"light_request={config.LIGHT_REQUEST} pages={config.PAGES}"
    )
    if summary["ok"] == 0 and summary["failed"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
