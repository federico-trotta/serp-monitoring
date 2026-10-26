"""Change detection, confirmation and Slack notification.

Flow (per successful run):
  1. baseline  = preceding successful run in the same series_key
  2. rule fires for the first time  -> alerts row with state="pending"
  3. next successful observation is compared with the *frozen* baseline:
       condition persists -> "confirmed"; reversed -> "cancelled";
       unknown evidence   -> stays "pending"
  4. confirmed events stay active while the condition persists and become
     "resolved" once a valid observation shows recovery.
Notifications are sent after the DB transaction is committed; `sent_at` is set
only after Slack accepts the message (at-least-once delivery).
"""
import json

import requests

from . import config
from .fetch import now_utc

RULES = ("ranking_drop", "top10_exit", "aio_change", "ranking_url_change", "title_change")
ACTIVE_STATES = ("pending", "confirmed")

RANKING_DROP_MIN = 3        # positions
RANKING_DROP_BASELINE_MAX = 5
TOP10 = 10
NOT_FOUND = "not found within fetched depth"


# Define observations
def load_observation(con, run_id, organic_depth=None):
    """Build an observation dict for a stored run.

    `organic_depth` (number of organic results in the response) is only known
    for the run being processed; it is required to verify a top-10 exit when
    the target is no longer found.
    """
    run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return None
    best = con.execute(
        "SELECT position, url, title FROM organic_results WHERE run_id = ? "
        "ORDER BY position LIMIT 1", (run_id,),
    ).fetchone()
    aio = con.execute(
        "SELECT present FROM serp_features WHERE run_id = ? AND feature = 'ai_overviews'",
        (run_id,),
    ).fetchone()
    return {
        "run_id": run_id,
        "fetched_at": run["fetched_at"],
        "light_request": bool(run["light_request"]),
        "position": best["position"] if best else None,
        "url": best["url"] if best else None,
        "title": best["title"] if best else None,
        "aio": None if aio is None or aio["present"] is None else bool(aio["present"]),
        "top10_verified": organic_depth is not None and organic_depth >= TOP10,
    }


def previous_successful_run(con, series, run_id):
    current = con.execute("SELECT fetched_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    row = con.execute(
        "SELECT id FROM runs WHERE series_key = ? AND status = 'ok' AND id != ? "
        "AND fetched_at <= ? ORDER BY fetched_at DESC LIMIT 1",
        (series, run_id, current["fetched_at"]),
    ).fetchone()
    return row["id"] if row else None


def _fmt_pos(position):
    return NOT_FOUND if position is None else str(position)

# Rules: return (condition, baseline_value, observed_value)
# condition: True = triggered, False = not triggered / reversed,
#            None = not evaluable (unknown or missing evidence)
def evaluate_rule(rule, base, cur):
    if rule == "ranking_drop":
        if base["position"] is None or base["position"] > RANKING_DROP_BASELINE_MAX:
            return None, None, None
        if cur["position"] is None:
            return None, base["position"], NOT_FOUND  # Handled by top10_exit
        triggered = cur["position"] - base["position"] >= RANKING_DROP_MIN
        return triggered, base["position"], cur["position"]

    if rule == "top10_exit":
        if base["position"] is None or base["position"] > TOP10:
            return None, None, None
        if cur["position"] is not None:
            return cur["position"] > TOP10, base["position"], cur["position"]
        if cur["top10_verified"]:
            return True, base["position"], NOT_FOUND
        return None, base["position"], NOT_FOUND

    if rule == "aio_change":
        if base["light_request"] or cur["light_request"]:
            return None, None, None  # Light requests never return AI Overviews
        if base["aio"] is None or cur["aio"] is None:
            return None, base["aio"], cur["aio"]
        return base["aio"] != cur["aio"], base["aio"], cur["aio"]

    if rule == "ranking_url_change":
        if not base["url"] or not cur["url"]:
            return None, base["url"], cur["url"]
        return base["url"] != cur["url"], base["url"], cur["url"]

    if rule == "title_change":
        if not base["url"] or not cur["url"] or base["url"] != cur["url"]:
            return None, base["title"], cur["title"]
        if not base["title"] or not cur["title"]:
            return None, base["title"], cur["title"]
        return base["title"] != cur["title"], base["title"], cur["title"]

    raise ValueError(f"unknown rule: {rule}")


def entity_for(rule, obs):
    """Affected entity used to identify an active event."""
    if rule == "title_change":
        return obs["url"]
    return "series"


def find_active_alert(con, series, rule, entity):
    rows = con.execute(
        "SELECT a.* FROM alerts a JOIN runs r ON r.id = a.run_id "
        "WHERE r.series_key = ? AND a.rule = ? AND a.state IN ('pending', 'confirmed') "
        "ORDER BY a.id DESC",
        (series, rule),
    ).fetchall()
    for row in rows:
        if json.loads(row["detail"]).get("entity") == entity:
            return row
    return None


def _update_alert(con, alert_id, state, detail):
    con.execute(
        "UPDATE alerts SET state = ?, detail = ? WHERE id = ?",
        (state, json.dumps(detail, sort_keys=True, default=str), alert_id),
    )


def evaluate_alerts(con, run_id, series, organic_depth):
    """Evaluate all rules for a successful run. Does NOT commit.

    Returns a dict of counters: new_pending, confirmed, cancelled, resolved.
    """
    stats = {"new_pending": 0, "confirmed": 0, "cancelled": 0, "resolved": 0}
    cur = load_observation(con, run_id, organic_depth)
    prev_id = previous_successful_run(con, series, run_id)

    for rule in RULES:
        entity = entity_for(rule, cur)
        active = find_active_alert(con, series, rule, entity) if entity else None

        if active is not None:
            base = load_observation(con, active["baseline_run_id"])
            if base is None:
                continue
            condition, base_val, cur_val = evaluate_rule(rule, base, cur)
            if condition is None:
                continue  # Unknown evidence: no confirmation, no cancellation
            detail = json.loads(active["detail"])
            if active["state"] == "pending":
                if condition:
                    detail.update({
                        "confirming_value": cur_val,
                        "confirm_run_id": run_id,
                        "confirmed_at": cur["fetched_at"],
                    })
                    _update_alert(con, active["id"], "confirmed", detail)
                    stats["confirmed"] += 1
                else:
                    detail.update({"cancel_run_id": run_id, "cancel_value": cur_val})
                    _update_alert(con, active["id"], "cancelled", detail)
                    stats["cancelled"] += 1
            elif not condition:  # Confirmed and now recovered
                detail.update({"resolve_run_id": run_id, "resolve_value": cur_val,
                               "resolved_at": cur["fetched_at"]})
                _update_alert(con, active["id"], "resolved", detail)
                stats["resolved"] += 1
            continue

        if prev_id is None:
            continue  # First observation of this series: establish history only
        base = load_observation(con, prev_id)
        condition, base_val, cur_val = evaluate_rule(rule, base, cur)
        if not condition:
            continue
        detail = {
            "entity": entity,
            "baseline_value": base_val,
            "observed_value": cur_val,
            "baseline_at": base["fetched_at"],
            "observed_at": cur["fetched_at"],
        }
        con.execute(
            "INSERT INTO alerts (run_id, baseline_run_id, rule, detail, state, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (run_id, prev_id, rule, json.dumps(detail, sort_keys=True, default=str), now_utc()),
        )
        stats["new_pending"] += 1
    return stats


# Slack notifications
def _describe(rule, detail):
    base, conf = detail.get("baseline_value"), detail.get("confirming_value")
    if rule in ("ranking_drop", "top10_exit"):
        return f"position {_fmt_pos(base)} -> {_fmt_pos(conf)}, confirmed by two successful checks"
    if rule == "aio_change":
        state = {True: "present", False: "absent", None: "unknown"}
        return (f"AI Overview {state.get(base, base)} -> {state.get(conf, conf)}, "
                "confirmed by two successful checks")
    if rule == "ranking_url_change":
        return f"ranking URL {base} -> {conf}, confirmed by two successful checks"
    if rule == "title_change":
        return f'title "{base}" -> "{conf}", confirmed by two successful checks'
    return f"{base} -> {conf}"


def format_message(alert, run):
    detail = json.loads(alert["detail"])
    location = run["country_code"] or "us"
    if run["latitude"] is not None and run["longitude"] is not None:
        location += f" ({run['latitude']}, {run['longitude']}"
        location += f", r={run['radius']}m)" if run["radius"] is not None else ")"
    lines = [
        f"*SERP alert #{alert['id']}* - {alert['rule']}",
        f"Keyword: {run['keyword']}",
        f"Target domain: {run['target_domain']}",
        f"Location: {location} | Locale: {run['locale']} | Device: {run['device']}",
        f"Change: {_describe(alert['rule'], detail)}",
        f"Baseline: {detail.get('baseline_value')!r} at {detail.get('baseline_at')} "
        f"(run {alert['baseline_run_id']})",
        f"First observed: {detail.get('observed_value')!r} at {detail.get('observed_at')} "
        f"(run {alert['run_id']})",
        f"Confirmed: {detail.get('confirming_value')!r} at {detail.get('confirmed_at')} "
        f"(run {detail.get('confirm_run_id')})",
    ]
    if detail.get("entity") not in (None, "series"):
        lines.append(f"Entity: {detail['entity']}")
    return "\n".join(lines)


def send_pending_notifications(con, webhook_url=None):
    """Send confirmed alerts whose sent_at is NULL. Returns (sent, failed)."""
    webhook_url = webhook_url or config.SLACK_WEBHOOK_URL
    unsent = con.execute(
        "SELECT a.*, r.keyword, r.target_domain, r.country_code, r.locale, r.device, "
        "r.latitude, r.longitude, r.radius FROM alerts a JOIN runs r ON r.id = a.run_id "
        "WHERE a.state = 'confirmed' AND a.sent_at IS NULL ORDER BY a.id"
    ).fetchall()
    if not unsent:
        return 0, 0
    if not webhook_url:
        print(f"[alerts] SLACK_WEBHOOK_URL not set; {len(unsent)} confirmed alert(s) left unsent.")
        for alert in unsent:
            print(format_message(alert, alert))
            print("-" * 60)
        return 0, len(unsent)

    sent = failed = 0
    for alert in unsent:
        text = format_message(alert, alert)
        try:
            resp = requests.post(webhook_url, json={"text": text}, timeout=15)
            ok = 200 <= resp.status_code < 300
        except requests.RequestException as exc:
            print(f"[alerts] Slack error for alert #{alert['id']}: {exc}")
            ok = False
        if ok:
            con.execute("UPDATE alerts SET sent_at = ? WHERE id = ?", (now_utc(), alert["id"]))
            con.commit()
            sent += 1
        else:
            failed += 1
    return sent, failed
