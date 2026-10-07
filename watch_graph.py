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

GRAPH_NAME = "RobotLive_DG"
API_VERSION = "v67.0"


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
            flag = "  COLLISION" if str(e.get("collision__c")).lower() in ("true", "1") else ""
            print(
                f"{datetime.now():%H:%M:%S}  speed={e.get('speed__c')} heading={e.get('heading__c')} "
                f"battery={e.get('battery__c')}  lag={lag:5.1f}s{flag}"
            )

        if first_poll:
            print(f"{datetime.now():%H:%M:%S}  {len(seen)} existing events in graph; waiting for new ones...")
            first_poll = False
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
