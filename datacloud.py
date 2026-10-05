"""
Data Cloud telemetry ingestion.

Streams robot sensor data to Data 360 Ingestion API for the live dashboard.
Handles token exchange (Salesforce → Data Cloud) and batching.
"""

import time
import logging
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

    def __init__(self, ingest_url: str):
        """
        Args:
            ingest_url: full Ingestion API endpoint, e.g.
                https://gnrdmpldh1zggzrtm74t8zrzg6.pc-rnd.c360a.salesforce.com/api/v1/ingest/sources/CONNECTOR/OBJECT
        """
        self.ingest_url = ingest_url
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
            "robot_id": telemetry.robot_id,
            "ts": telemetry.ts,
            "speed": telemetry.speed,
            "heading": telemetry.heading,
            "battery": telemetry.battery,
            "collision": telemetry.collision,
            "active_hazard": telemetry.active_hazard,
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

        # The ingest URL is typically separate from the Salesforce instance.
        # e.g., gnrdmpldh1zggzrtm74t8zrzg6.pc-rnd.c360a.salesforce.com
        url = self.ingest_url

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


def init_datacloud(ingest_url: str) -> None:
    """Initialize the DataCloudIngestor. Call once at startup."""
    global _ingestor
    _ingestor = DataCloudIngestor(ingest_url)


def get_ingestor() -> DataCloudIngestor:
    """Get the global ingestor instance."""
    if _ingestor is None:
        raise RuntimeError(
            "DataCloudIngestor not initialized. Call init_datacloud() first."
        )
    return _ingestor
