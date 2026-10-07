"""
Data Cloud Server-to-Server (S2S) telemetry ingestion.

Streams robot telemetry to POST /server/events/{appSourceId}, which lands in the
real-time layer (sub-second) rather than the ~3 minute micro-batch of the standard
Ingestion API in datacloud.py. Exposes the same add_record()/flush() interface so
control_loop.py can use either.

Sends happen on a background thread. The control loop ticks at 20 Hz (50 ms), so a
blocking HTTP call made from the loop would stall it.
"""

import queue
import threading
import time
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import requests

from robot import Telemetry
from s2s_auth import get_s2s_auth


logger = logging.getLogger(__name__)

# Must exactly match the event's developerName in schemas/s2s_schema.json. Deliberately not
# "RobotTelemetry" -- that name is already taken by the existing batch Ingestion API
# connector/object (DATACLOUD_SOURCE_NAME/DATACLOUD_OBJECT_NAME), and developer names
# must be unique across the whole Data Cloud ingestion namespace, not just this connector.
EVENT_TYPE = "RobotTelemetryRT"
# Must match the event category set when the schema was uploaded (schemas/s2s_schema.json).
EVENT_CATEGORY = "Other"

# What happened to a batch (see S2SIngestor._post).
SENT, DROP, RETRY = "sent", "drop", "retry"


def _iso_millis(ts: float) -> str:
    """Format a unix timestamp as yyyy-MM-dd'T'HH:mm:ss.SSS'Z' (the only format S2S accepts)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_event(telemetry: Telemetry) -> dict:
    """
    Convert a Telemetry sample into an S2S event.

    Field names are camelCase because S2S developer names are alphanumeric only.
    The schema has no Boolean type, so collision/isMalfunctioning are sent as 0/1.
    """
    return {
        "eventId": str(uuid.uuid4()),
        "eventType": EVENT_TYPE,
        "category": EVENT_CATEGORY,
        "dateTime": _iso_millis(telemetry.ts),

        # Identity + state
        "robotId": telemetry.robot_id,
        "heatNumber": telemetry.heat_number,
        "collision": 1 if telemetry.collision else 0,
        "activeHazard": telemetry.active_hazard or "",
        "isMalfunctioning": 1 if telemetry.is_malfunctioning else 0,

        # Headline motion
        "speed": telemetry.speed,
        "heading": telemetry.heading,
        "battery": telemetry.battery,

        # IMU
        "accelX": telemetry.accel_x,
        "accelY": telemetry.accel_y,
        "accelZ": telemetry.accel_z,
        "gyroX": telemetry.gyro_x,
        "gyroY": telemetry.gyro_y,
        "gyroZ": telemetry.gyro_z,
        # NOTE: attitudePitch/Roll/Yaw temporarily omitted — see docs/deferred-telemetry-fields.md

        # Locator
        "locatorX": telemetry.locator_x,
        "locatorY": telemetry.locator_y,
        "velocityX": telemetry.velocity_x,
        "velocityY": telemetry.velocity_y,

        # Vision / environment
        "colourR": telemetry.colour_r,
        "colourG": telemetry.colour_g,
        "colourB": telemetry.colour_b,
        "colourName": telemetry.colour_name,
        "ambientLight": telemetry.ambient_light,

        # Power / health
        # NOTE: voltage temporarily omitted — see docs/deferred-telemetry-fields.md
        "motorCurrentLeft": telemetry.motor_current_left,
        "motorCurrentRight": telemetry.motor_current_right,
        "temperatureC": telemetry.temperature_c,
    }


class S2SIngestor:
    """
    Queues telemetry events and sends them to Data Cloud from a worker thread.

    - add_record() never blocks: it enqueues and returns.
    - The worker sends when it has max_batch events or flush_interval_s has passed.
    - On failure the batch is kept and retried; the buffer is capped so a long outage
      drops the oldest events rather than growing without limit.
    - Refreshes the Data Cloud token once on a 401.
    """

    def __init__(
        self,
        app_source_id: str,
        flush_interval_s: float = 0.25,
        max_batch: int = 50,
        max_buffer: int = 2000,
    ):
        """
        Args:
            app_source_id: Source ID shown on the S2S connection in Data Cloud Setup
            flush_interval_s: longest an event waits before being sent
            max_batch: events per request
            max_buffer: most events held while sends are failing (oldest dropped beyond this)
        """
        self.app_source_id = app_source_id
        self.flush_interval_s = flush_interval_s
        self.max_batch = max_batch
        self.max_buffer = max_buffer

        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._pending: List[dict] = []  # worker-thread only
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.enqueued_count = 0
        self.sent_count = 0
        self.dropped_count = 0

    def start(self) -> None:
        """Start the worker thread. Safe to call more than once."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="s2s-ingestor", daemon=True)
        self._thread.start()

    def add_record(self, telemetry: Telemetry) -> None:
        """Queue a telemetry sample for sending. Non-blocking."""
        self.start()
        self.enqueued_count += 1
        self._queue.put(build_event(telemetry))

    def flush(self, timeout_s: float = 5.0) -> None:
        """Block until every queued event is sent or dropped, or timeout_s passes."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.sent_count + self.dropped_count >= self.enqueued_count:
                return
            time.sleep(0.05)
        logger.warning("S2S flush timed out with events still unsent")

    def close(self, timeout_s: float = 5.0) -> None:
        """Flush what's left, then stop the worker. Call at shutdown."""
        self.flush(timeout_s)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout_s)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._drain_queue()
            if self._pending:
                self._send_pending()
            # Wake early when new events arrive; otherwise wait one flush interval.
            try:
                self._pending.append(self._queue.get(timeout=self.flush_interval_s))
            except queue.Empty:
                pass

    def _drain_queue(self) -> None:
        while True:
            try:
                self._pending.append(self._queue.get_nowait())
            except queue.Empty:
                break
        overflow = len(self._pending) - self.max_buffer
        if overflow > 0:
            del self._pending[:overflow]
            self.dropped_count += overflow
            logger.error(f"S2S buffer full; dropped {overflow} oldest events")

    def _send_pending(self) -> None:
        while self._pending:
            batch = self._pending[: self.max_batch]
            outcome = self._post(batch)
            if outcome == RETRY:
                return  # keep the batch; retry on the next loop
            del self._pending[: len(batch)]
            if outcome == SENT:
                self.sent_count += len(batch)
            else:
                self.dropped_count += len(batch)

    def _post(self, events: List[dict]) -> str:
        """POST one batch. Returns SENT, DROP (never retry) or RETRY."""
        auth = get_s2s_auth()
        try:
            token, host = auth.get_datacloud_token()
            url = f"https://{host}/server/events/{self.app_source_id}"
            payload = {"events": events}

            resp = self._post_once(url, payload, token)
            if resp.status_code == 401:
                token, host = auth.get_datacloud_token(force_refresh=True)
                url = f"https://{host}/server/events/{self.app_source_id}"
                resp = self._post_once(url, payload, token)

            if resp.ok:  # documented success is 204 No Content
                logger.debug(f"S2S sent {len(events)} events ({resp.status_code})")
                return SENT

            logger.error(f"S2S send failed: {resp.status_code} {resp.text[:500]}")
            if resp.status_code == 400:
                # A malformed batch will never succeed; retrying forever would block
                # every event behind it. Drop it and carry on.
                return DROP
            return RETRY

        except requests.RequestException as e:
            logger.error(f"S2S send error: {e}")
            return RETRY

    @staticmethod
    def _post_once(url: str, payload: dict, token: str) -> requests.Response:
        return requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=10,
        )


# Module-level singleton, matching datacloud.py.
_ingestor: S2SIngestor = None


def init_s2s(app_source_id: str, **kwargs) -> None:
    """Initialise the S2SIngestor. Call once at startup."""
    global _ingestor
    _ingestor = S2SIngestor(app_source_id, **kwargs)


def get_s2s_ingestor() -> S2SIngestor:
    """Get the global S2S ingestor instance."""
    if _ingestor is None:
        raise RuntimeError("S2SIngestor not initialised. Call init_s2s() first.")
    return _ingestor
