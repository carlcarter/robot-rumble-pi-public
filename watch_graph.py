"""
Watch telemetry arrive in the real-time data graph, and measure how far behind it is.

    python watch_graph.py            # robot EDI, poll every 2 seconds
    python watch_graph.py LON 1      # robot LON, poll every 1 second

Run it in one terminal, then run control_loop.py in another. Each new event is printed
with its "lag": the time from the robot's own timestamp to this script seeing it in the
graph. With the batch Ingestion API expect minutes; with S2S expect about a second.

Uses the existing client-credentials app (read-only). Reads .env; never prints tokens.
"""

import html
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from sf_auth import SalesforceAuth

# Real-time Data Graphs can't be edited after creation, so the graph gets recreated
# under a new name when its field set changes. Override without editing code:
#   GRAPH_NAME=RobotLiveExtended_DG python watch_graph.py EDI 1
GRAPH_NAME = os.getenv("GRAPH_NAME", "RobotLiveExtended_DG")
API_VERSION = "v67.0"


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _num(value, fmt: str = "{:.2f}"):
    """Format a value as a number if it looks like one, else return it as-is or '-'."""
    if value in (None, ""):
        return "-"
    try:
        return fmt.format(float(value))
    except (TypeError, ValueError):
        return str(value)


def format_event(e: dict) -> str:
    """
    One-line summary of a telemetry event. Only shows a field if the Data Graph
    actually returned it, so this works regardless of which fields are mapped.

    Field API names match RobotLiveExtended_DG: the originally-mapped fields are
    snake_case (speed__c, heading__c, active_hazard__c) while the fields added in
    phase 2 are camelCase (locatorX__c, motorCurrentLeft__c) and colour uses the
    American spelling (colorR__c). ambientLight and collision are not mapped on
    this graph; isMalfunctioning stands in for the stopped/faulted state.
    """
    is_malf = str(e.get("isMalfunctioning__c")).lower() in ("true", "1")

    parts = [
        f"spd={_num(e.get('speed__c'))}",
        f"hdg={_num(e.get('heading__c'), '{:.0f}')}",
        f"bat={_num(e.get('battery__c'), '{:.0f}')}%",
    ]

    # Locator XY
    if e.get("locatorX__c") is not None or e.get("locatorY__c") is not None:
        parts.append(f"xy=({_num(e.get('locatorX__c'))},{_num(e.get('locatorY__c'))})")

    # Velocity magnitude
    if e.get("velocityX__c") is not None or e.get("velocityY__c") is not None:
        parts.append(f"vel=({_num(e.get('velocityX__c'))},{_num(e.get('velocityY__c'))})")

    # Colour — name plus swatch RGB
    colour_name = e.get("colorName__c")
    if colour_name:
        parts.append(f"colour={colour_name}")
    if e.get("colorR__c") is not None:
        parts.append(
            f"rgb=({_num(e.get('colorR__c'), '{:.0f}')},"
            f"{_num(e.get('colorG__c'), '{:.0f}')},"
            f"{_num(e.get('colorB__c'), '{:.0f}')})"
        )

    # IMU accel magnitude (x/y plane)
    if e.get("accelX__c") is not None:
        parts.append(f"accX={_num(e.get('accelX__c'))}")

    # Motor currents
    if e.get("motorCurrentLeft__c") is not None or e.get("motorCurrentRight__c") is not None:
        parts.append(
            f"motA=({_num(e.get('motorCurrentLeft__c'))}/"
            f"{_num(e.get('motorCurrentRight__c'))})"
        )

    # Temperature
    if e.get("temperatureC__c") is not None:
        parts.append(f"temp={_num(e.get('temperatureC__c'), '{:.1f}')}C")

    # Flags at the end so they stand out
    if e.get("active_hazard__c"):
        parts.append(f"HAZARD={e.get('active_hazard__c')}")
    if is_malf:
        parts.append("MALFUNCTION")

    return "  ".join(parts)


def fetch_events(auth: SalesforceAuth, robot_id: str) -> list:
    """Return the telemetry rows currently in the graph for this robot."""
    url = f"https://{auth.my_domain}.my.salesforce.com/services/data/{API_VERSION}/ssot/data-graphs/data/{GRAPH_NAME}/{robot_id}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {auth.get_salesforce_token()}"}, timeout=15)
    if resp.status_code == 401:
        resp = requests.get(url, headers={"Authorization": f"Bearer {auth.get_salesforce_token(True)}"}, timeout=15)
    resp.raise_for_status()

    # The graph returns one row whose json_blob__c is HTML-escaped JSON.
    rows = resp.json().get("data") or []
    if not rows:
        return []
    blob = json.loads(html.unescape(rows[0]["json_blob__c"]))
    for key, value in blob.items():
        if isinstance(value, list) and key.endswith("__dlm"):
            return value
    return []


def main() -> None:
    load_dotenv()
    robot_id = sys.argv[1] if len(sys.argv) > 1 else os.getenv("ROBOT_ID", "EDI")
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0

    auth = SalesforceAuth(
        os.environ["SF_MY_DOMAIN"].strip(),
        os.environ["SF_CLIENT_ID"].strip(),
        os.environ["SF_CLIENT_SECRET"].strip(),
    )

    # Debug: `python watch_graph.py EDI --keys` dumps the actual field names +
    # values of the newest event, so we can see exactly what the graph serves
    # (field API names vary depending on how the DMO mapping was done).
    if "--keys" in sys.argv:
        events = fetch_events(auth, robot_id)
        if not events:
            print("No events in graph yet.")
            return
        newest = sorted(events, key=lambda x: x.get("event_time__c", ""))[-1]
        print(f"Newest event for {robot_id} has {len(newest)} fields:\n")
        for key in sorted(newest.keys()):
            print(f"  {key} = {newest[key]!r}")
        return

    print(f"Watching {GRAPH_NAME} for robot {robot_id} every {interval:g}s. Ctrl+C to stop.\n")
    seen = set()
    first_poll = True

    while True:
        try:
            events = fetch_events(auth, robot_id)
        except requests.RequestException as e:
            print(f"{datetime.now():%H:%M:%S}  poll failed: {e}")
            time.sleep(interval)
            continue

        new = [e for e in events if e.get("event_id__c") not in seen]
        for e in sorted(new, key=lambda x: x.get("event_time__c", "")):
            seen.add(e.get("event_id__c"))
            if first_poll:
                continue  # don't replay history as if it were live
            lag = (datetime.now(timezone.utc) - parse_time(e["event_time__c"])).total_seconds()
            print(f"{datetime.now():%H:%M:%S}  {format_event(e)}  lag={lag:5.1f}s")

        if first_poll:
            print(f"{datetime.now():%H:%M:%S}  {len(seen)} existing events in graph; waiting for new ones...")
            first_poll = False
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
