"""
Data Cloud telemetry ingestion.

Streams robot sensor data to Data 360 Ingestion API for the live dashboard.
Handles token exchange (Salesforce → Data Cloud) and batching.
"""

import time
import uuid
import logging
from datetime import datetime, timezone
from typing import List
import requests

from robot import Telemetry
from sf_auth import get_auth


logger = logging.getLogger(__name__)


class DataCloudIngestor:
    """
    Streams telemetry records to Data Cloud Ingestion API.

    Batches records to stay within API limits (250 requests/sec, 200 KB/batch).
    Refreshes the Data Cloud token on 401.
    """

    def __init__(self, source_name: str, object_name: str):
        """
        Args:
            source_name: the Ingestion API connector name, e.g. "RobotTelemetry"
            object_name: the schema object name within that connector, e.g. "RobotTelemetry"
        """
        self.source_name = source_name
        self.object_name = object_name
        self._batch: List[dict] = []
        self._last_flush = time.time()
        self._flush_interval = 1.0  # Flush every 1 second (a few per second max).

    def add_record(self, telemetry: Telemetry) -> None:
        """
        Queue a telemetry record for ingestion. Flushes the batch if it's getting large.

        Args:
            telemetry: Telemetry dataclass from the robot
        """
        record = {
            # Primary key: Data Cloud requires a true unique identifier per record.
            "event_id": str(uuid.uuid4()),
            # Engagement category requires a datetime field (ISO 8601).
            "event_time": datetime.fromtimestamp(telemetry.ts, tz=timezone.utc).isoformat(),
            "robot_id": telemetry.robot_id,
            "speed": telemetry.speed,
            "heading": telemetry.heading,
            "battery": telemetry.battery,
            "collision": telemetry.collision,
            "active_hazard": telemetry.active_hazard or "",
            "heat_number": telemetry.heat_number,
        }
        self._batch.append(record)

        # Flush if batch is getting large or enough time has passed.
        if len(self._batch) >= 50 or time.time() - self._last_flush > self._flush_interval:
            self.flush()

    def flush(self) -> None:
        """Send any queued records to Data Cloud Ingestion API."""
        if not self._batch:
            return

        auth = get_auth()
        dc_token, dc_instance_url = auth.get_datacloud_token()

        # The Data Cloud tenant host is dynamic (can rotate) and comes back from the
        # token exchange as `instance_url`. Never hardcode it; build the URL fresh each flush.
        url = f"https://{dc_instance_url}/api/v1/ingest/sources/{self.source_name}/{self.object_name}"

        payload = {"data": self._batch}

        headers = {
            "Authorization": f"Bearer {dc_token}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)

            # On 401, refresh Data Cloud token and retry once.
            if resp.status_code == 401:
                dc_token, _ = auth.get_datacloud_token(force_refresh=True)
                headers["Authorization"] = f"Bearer {dc_token}"
                resp = requests.post(url, json=payload, headers=headers, timeout=10)

            resp.raise_for_status()
            logger.debug(f"Ingested {len(self._batch)} telemetry records to Data Cloud")
            self._batch = []
            self._last_flush = time.time()

        except requests.RequestException as e:
            logger.error(f"Data Cloud ingestion failed: {e}")
            # Keep the batch for retry on next flush. Don't clear it.


# Module-level singleton.
_ingestor: DataCloudIngestor = None


def init_datacloud(source_name: str, object_name: str) -> None:
    """Initialize the DataCloudIngestor. Call once at startup."""
    global _ingestor
    _ingestor = DataCloudIngestor(source_name, object_name)


def get_ingestor() -> DataCloudIngestor:
    """Get the global ingestor instance."""
    if _ingestor is None:
        raise RuntimeError(
            "DataCloudIngestor not initialized. Call init_datacloud() first."
        )
    return _ingestor
