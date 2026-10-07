"""
Subscribe to Robot_Command__e platform events and act on them locally.

This is the robot end of the "fake fix" loop: an operator runs /fix EDI in Slack,
Salesforce publishes a Robot_Command__e, and this subscriber — running on the Pi
(or on a Mac during development) — receives it over the Streaming API (CometD) and
clears the robot's malfunction state so it carries on.

Design:
- dispatch_command() and should_handle() are pure functions, unit-tested without any
  network or org. They are the whole behaviour that matters for the demo.
- CommandSubscriber runs an asyncio CometD client on a daemon thread so it never
  blocks the 20 Hz control loop. The streaming client is injectable, so tests drive
  the dispatch path with a fake and never touch Salesforce.

Auth reuses the existing client-credentials SalesforceAuth (sf_auth.py): we hand the
CometD client a bearer token + instance URL rather than a second OAuth flow.
"""

import logging
import threading
from typing import Callable, Optional

from robot import Robot

logger = logging.getLogger(__name__)

STREAMING_CHANNEL = "/event/Robot_Command__e"

# Command verb -> what we do to the robot. Keep in step with RobotCommandPublisher
# (Apex) VALID_COMMANDS.
VALID_COMMANDS = {"RESUME", "HALT", "RESET_HAZARD"}


def should_handle(payload: dict, my_robot_id: str) -> bool:
    """
    True if this event is addressed to the robot we're running.

    The platform event payload nests fields under 'payload'; field API names carry
    the __c suffix. Matching is case-insensitive on robot id.
    """
    fields = payload.get("payload", payload)
    event_robot = str(fields.get("Robot_ID__c", "")).strip().upper()
    return bool(event_robot) and event_robot == my_robot_id.strip().upper()


def dispatch_command(robot: Robot, command: str) -> bool:
    """
    Apply a command to the robot. Returns True if something was done.

    RESUME / RESET_HAZARD both clear the malfunction and let the robot drive on;
    HALT stops it. Unknown verbs are ignored (logged), never raised — a bad event
    must not kill the subscriber thread.
    """
    verb = (command or "").strip().upper()
    if verb in ("RESUME", "RESET_HAZARD"):
        robot.clear_hazard()
        logger.info(f"Command {verb}: cleared hazard, robot resuming")
        return True
    if verb == "HALT":
        robot.stop()
        logger.info("Command HALT: robot stopped")
        return True
    logger.warning(f"Ignoring unknown command: {command!r}")
    return False


def handle_event(payload: dict, robot: Robot, my_robot_id: str) -> bool:
    """Filter then dispatch. Returns True if the command was applied to this robot."""
    if not should_handle(payload, my_robot_id):
        return False
    fields = payload.get("payload", payload)
    return dispatch_command(robot, str(fields.get("Command__c", "")))


class CommandSubscriber:
    """
    Runs a CometD subscription on a background daemon thread.

    The asyncio streaming client is created by `client_factory`, which defaults to a
    real aiosfstream client but is injectable for tests. The subscriber reconnects
    on drop — a venue Wi-Fi blip must not need a Pi reboot.
    """

    def __init__(
        self,
        robot: Robot,
        robot_id: str,
        token_provider: Callable[[], tuple],
        client_factory: Optional[Callable] = None,
    ):
        """
        Args:
            robot: the shared Robot instance the control loop also drives
            robot_id: EDI / LON — only events for this robot are acted on
            token_provider: returns (access_token, instance_url) for the Streaming API
            client_factory: builds the CometD client; defaults to aiosfstream
        """
        self.robot = robot
        self.robot_id = robot_id
        self.token_provider = token_provider
        self.client_factory = client_factory or self._default_client_factory
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="command-subscriber", daemon=True)
        self._thread.start()
        logger.info(f"Command subscriber started for {self.robot_id}")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._listen()
            except Exception as e:
                # Any failure (auth expiry, network drop) falls through to a reconnect
                # after a short backoff. Never let the thread die.
                logger.error(f"Command subscriber error, reconnecting in 5s: {e}")
                self._stop.wait(5.0)

    def _listen(self) -> None:
        """One full Bayeux session: handshake, subscribe, then long-poll /connect."""
        client = self.client_factory()
        client.handshake()
        client.subscribe(STREAMING_CHANNEL)
        logger.info(f"Subscribed to {STREAMING_CHANNEL}")
        try:
            while not self._stop.is_set():
                for message in client.connect():
                    data = message.get("data", {}) if isinstance(message, dict) else {}
                    try:
                        handle_event(data, self.robot, self.robot_id)
                    except Exception as e:
                        logger.error(f"Failed handling command event: {e}")
        finally:
            client.disconnect()

    def _default_client_factory(self):
        """
        Build a dependency-free Bayeux/CometD long-poll client over `requests`.

        We implement the Salesforce Streaming API directly rather than use
        aiosfstream/aiocometd — those are unmaintained and pass loop= to asyncio
        primitives, which was removed in Python 3.10+. A plain long-poll loop is
        also simpler and has no async-in-a-thread complexity.
        """
        return _BayeuxClient(self.token_provider, api_version=API_VERSION)


# Default Streaming API version (Bayeux endpoint path).
API_VERSION = "64.0"


class _BayeuxClient:
    """
    Minimal CometD/Bayeux client for the Salesforce Streaming API.

    Implements just the four meta channels we need: /meta/handshake,
    /meta/subscribe, /meta/connect (long-poll), /meta/disconnect. Uses the
    long-polling transport, which is all Salesforce supports.
    """

    def __init__(self, token_provider, api_version: str = API_VERSION):
        import requests

        self._requests = requests
        self.token_provider = token_provider
        self.api_version = api_version
        self.session = requests.Session()
        self.client_id = None
        access_token, instance_url = token_provider()
        self._access_token = access_token
        self._endpoint = f"{instance_url.rstrip('/')}/cometd/{api_version}"

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _post(self, payload, timeout):
        resp = self.session.post(self._endpoint, json=payload, headers=self._headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def handshake(self):
        messages = self._post([{
            "channel": "/meta/handshake",
            "version": "1.0",
            "supportedConnectionTypes": ["long-polling"],
            "minimumVersion": "1.0",
        }], timeout=30)
        msg = messages[0]
        if not msg.get("successful"):
            raise RuntimeError(f"Bayeux handshake failed: {msg}")
        self.client_id = msg["clientId"]

    def subscribe(self, channel):
        messages = self._post([{
            "channel": "/meta/subscribe",
            "clientId": self.client_id,
            "subscription": channel,
        }], timeout=30)
        msg = messages[0]
        if not msg.get("successful"):
            raise RuntimeError(f"Bayeux subscribe failed: {msg}")

    def connect(self):
        """
        One long-poll. Returns the event messages received (channel not starting
        with /meta/). Blocks up to the server's advised timeout (~110s) or until
        an event arrives. The /meta/connect advice reconnect handling is implicit:
        we just call connect() again in a loop.
        """
        messages = self._post([{
            "channel": "/meta/connect",
            "clientId": self.client_id,
            "connectionType": "long-polling",
        }], timeout=130)
        events = []
        for msg in messages:
            channel = msg.get("channel", "")
            if channel.startswith("/meta/"):
                if channel == "/meta/connect" and not msg.get("successful", True):
                    raise RuntimeError(f"Bayeux connect unsuccessful: {msg}")
                continue
            events.append(msg)
        return events

    def disconnect(self):
        if not self.client_id:
            return
        try:
            self._post([{"channel": "/meta/disconnect", "clientId": self.client_id}], timeout=10)
        except Exception:
            pass  # best-effort on shutdown
