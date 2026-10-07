"""
One-shot S2S smoke test: authenticate, send a single event, print exactly what Salesforce says.

Run this first once the External Client App and the Server-to-Server connection exist:

    python s2s_smoke_test.py

It reads the S2S_* values from .env. Each stage prints its own PASS/FAIL so you can see
whether a problem is the key/cert, the app's user policy, the token exchange, or the
Source ID. Tokens are never printed.
"""

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from s2s_auth import S2SAuth


def stage(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def main() -> int:
    load_dotenv()

    cfg = {
        "SF_MY_DOMAIN": os.getenv("SF_MY_DOMAIN", "").strip(),
        "S2S_CLIENT_ID": os.getenv("S2S_CLIENT_ID", "").strip(),
        "S2S_USERNAME": os.getenv("S2S_USERNAME", "").strip(),
        "S2S_PRIVATE_KEY_PATH": os.getenv("S2S_PRIVATE_KEY_PATH", "").strip(),
        "S2S_APP_SOURCE_ID": os.getenv("S2S_APP_SOURCE_ID", "").strip(),
    }
    missing = [k for k, v in cfg.items() if not v or v.startswith("your_")]
    stage("Config present in .env", not missing, f"missing: {', '.join(missing)}" if missing else "")
    if missing:
        return 1

    key_path = Path(cfg["S2S_PRIVATE_KEY_PATH"]).expanduser()
    stage("Private key file found", key_path.is_file(), str(key_path))
    if not key_path.is_file():
        return 1

    auth = S2SAuth(cfg["SF_MY_DOMAIN"], cfg["S2S_CLIENT_ID"], cfg["S2S_USERNAME"], str(key_path))

    try:
        auth.get_salesforce_token(force_refresh=True)
        stage("JWT bearer -> Salesforce token", True)
    except requests.RequestException as e:
        stage("JWT bearer -> Salesforce token", False, "see the error body logged above")
        print(f"       {e}")
        return 1

    try:
        token, host = auth.get_datacloud_token()
        stage("Salesforce token -> Data Cloud token", True, f"tenant host {host}")
    except requests.RequestException as e:
        stage("Salesforce token -> Data Cloud token", False, str(e))
        return 1

    event = {
        "eventId": str(uuid.uuid4()),
        "eventType": "RobotTelemetryRT",
        "category": "Other",
        "dateTime": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "robotId": "SMOKE",
        "speed": 0.0,
        "heading": 0.0,
        "battery": 100.0,
        "collision": 0,
        "activeHazard": "",
        "heatNumber": 0,
    }
    url = f"https://{host}/server/events/{cfg['S2S_APP_SOURCE_ID']}"
    resp = requests.post(
        url,
        json={"events": [event]},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    # 204 No Content is the documented success response.
    stage("POST /server/events/{sourceId}", resp.status_code == 204, f"HTTP {resp.status_code}")
    if resp.status_code != 204:
        print(f"       body: {resp.text[:800]}")
        if resp.status_code in (401, 403):
            print("       Hint: the endpoint may expect the Salesforce token rather than the Data Cloud token.")
        if resp.status_code == 400:
            print("       Hint: the event doesn't match the uploaded schema (field names / types / category).")
        return 1

    print(f"\nSent event {event['eventId']} for robot SMOKE. Run watch_graph.py (or query the DLO) to find it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
