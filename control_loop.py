"""
Main control loop: drives the robot and integrates with Salesforce.

1. Read robot telemetry
2. Post to Data Cloud (throttled)
3. Detect crash/stall (debounced)
4. Raise Service Cloud Case if needed
5. Hot-reload params from params.json
6. Repeat
"""

import time
import json
import logging
from pathlib import Path
from typing import Optional

from robot import Robot, FakeRobot
from sf_auth import init_auth, get_auth
from sf_service import init_cases, get_case_manager
from datacloud import init_datacloud, get_ingestor
from s2s_auth import init_s2s_auth
from s2s_events import init_s2s, get_s2s_ingestor


logger = logging.getLogger(__name__)


def _auth_is_configured(sf_domain, sf_client_id, sf_client_secret, placeholders) -> bool:
    """True only if all three SF credentials are present and not left as placeholders."""
    return (
        bool(sf_domain) and sf_domain not in placeholders
        and bool(sf_client_id) and sf_client_id not in placeholders
        and bool(sf_client_secret) and sf_client_secret not in placeholders
    )


class ControlLoop:
    """Main robot control loop."""

    def __init__(
        self,
        robot: Robot,
        robot_id: str,
        params_file: Path,
        datacloud_enabled: bool = False,
        datacloud_send_interval_ms: int = 100,
        s2s_enabled: bool = False,
        cruise_speed: float = 0.5,
    ):
        """
        Args:
            robot: Robot instance (FakeRobot or SpheroRobot)
            robot_id: e.g. "EDI" or "LON"
            params_file: path to params.json
            datacloud_enabled: True if init_datacloud() was called at startup (skip if no Data Cloud)
            datacloud_send_interval_ms: minimum gap between telemetry sends to Data Cloud,
                in milliseconds (e.g. 100 = up to 10/sec). Independent of tick_interval, so it
                behaves the same regardless of loop speed. Does not by itself control how often
                Data Cloud's own async processor ingests the batch (see datacloud.py).
            s2s_enabled: True if init_s2s() was called at startup. Sends each sample to the
                real-time Server-to-Server endpoint (s2s_events.py), on the same throttle.
        """
        self.robot = robot
        self.robot_id = robot_id
        self.params_file = params_file
        self.datacloud_enabled = datacloud_enabled
        self.datacloud_send_interval_s = datacloud_send_interval_ms / 1000.0
        self.s2s_enabled = s2s_enabled
        self.cruise_speed = cruise_speed

        self._running = False
        self._last_params_mtime = 0.0
        self._collision_cooldown_until = 0.0
        self._last_datacloud_send = 0.0
        self._last_telemetry = None

    def load_params(self) -> dict:
        """Load params.json. Handles missing file gracefully."""
        if not self.params_file.exists():
            logger.warning(f"params_file not found: {self.params_file}")
            return {}
        try:
            with open(self.params_file) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load params.json: {e}")
            return {}

    def run(self, max_iterations: int = None, tick_interval: float = 0.05) -> None:
        """
        Run the control loop.

        Args:
            max_iterations: for testing; if set, stop after this many ticks
            tick_interval: sleep this many seconds between ticks (e.g. 0.05 = 20 Hz)
        """
        self._running = True
        iteration = 0

        logger.info(f"Control loop starting. Robot: {self.robot_id} (max_iterations={max_iterations})")

        try:
            while self._running:
                if max_iterations and iteration >= max_iterations:
                    logger.info(f"Reached max_iterations ({max_iterations})")
                    break

                self._tick(iteration)
                iteration += 1

                # Always sleep to maintain consistent timing (20 Hz = 0.05s per tick)
                time.sleep(tick_interval)

        except KeyboardInterrupt:
            logger.info("Control loop interrupted by user")
        except Exception as e:
            logger.exception(f"Control loop failed: {e}")
        finally:
            logger.info(f"Completed {iteration} iterations")
            self.stop()

    def _tick(self, iteration: int) -> None:
        """Single iteration of the control loop."""

        # 0. Drive. Until the line-follower (line_follower.py) is wired in, issue a
        #    steady cruise so the robot actually moves — otherwise FakeRobot's
        #    scripted route never advances and every telemetry sample reads zero.
        #    A malfunctioning robot holds position until the hazard is cleared.
        last = getattr(self, "_last_telemetry", None)
        if last is not None and last.is_malfunctioning:
            self.robot.stop()
        else:
            self.robot.drive(self.cruise_speed, 0.0)

        # 1. Read telemetry
        telemetry = self.robot.telemetry()
        self._last_telemetry = telemetry

        # Log telemetry every 20 iterations (once/sec at 20 Hz) for visibility
        if iteration % 20 == 0:
            logger.debug(
                f"[{iteration}] speed={telemetry.speed:.2f} heading={telemetry.heading:.1f}° "
                f"battery={telemetry.battery:.1f}% collision={telemetry.collision}"
            )

        # 2. Post to Data Cloud (throttled by wall-clock time, not tick count, so the
        #    send rate is independent of the loop's tick_interval).
        now = time.time()
        if (
            (self.datacloud_enabled or self.s2s_enabled)
            and now - self._last_datacloud_send >= self.datacloud_send_interval_s
        ):
            self._last_datacloud_send = now
            if self.datacloud_enabled:
                try:
                    get_ingestor().add_record(telemetry)
                except Exception as e:
                    logger.error(f"Data Cloud post failed: {e}")
            if self.s2s_enabled:
                try:
                    get_s2s_ingestor().add_record(telemetry)
                except Exception as e:
                    logger.error(f"S2S post failed: {e}")

        # 3. Detect crash/stall (debounced: only once every 5 seconds after detection).
        if telemetry.collision and time.time() > self._collision_cooldown_until:
            logger.warning(f"Collision detected: {telemetry}")
            self._handle_crash(telemetry)
            self._collision_cooldown_until = time.time() + 5.0

        # 4. Hot-reload params if file changed.
        try:
            mtime = self.params_file.stat().st_mtime
            if mtime > self._last_params_mtime:
                params = self.load_params()
                if params:
                    logger.info(f"Reloading params.json")
                    self.robot.set_params(params)
                    self._last_params_mtime = mtime
        except FileNotFoundError:
            pass

    def _handle_crash(self, telemetry) -> None:
        """Raise a Case in Service Cloud when the robot crashes."""
        try:
            case_mgr = get_case_manager()
            description = (
                f"Robot collision/stall at heading {telemetry.heading:.1f}°, "
                f"speed {telemetry.speed:.2f}, battery {telemetry.battery:.1f}%"
            )
            case_id = case_mgr.create_case(
                robot_id=self.robot_id,
                description=description,
                hazard=telemetry.active_hazard,
                heat_number=telemetry.heat_number,
            )
            logger.info(f"Created Case {case_id}")
        except Exception as e:
            logger.error(f"Failed to create Case: {e}")

    def stop(self) -> None:
        """Stop the control loop and robot."""
        logger.info("Stopping control loop")
        self._running = False
        self.robot.stop()

        # Send anything still queued; the S2S worker is a daemon thread and would
        # otherwise lose the last events when the process exits.
        if self.s2s_enabled:
            try:
                ingestor = get_s2s_ingestor()
                ingestor.close()
                logger.info(
                    f"S2S: sent={ingestor.sent_count} dropped={ingestor.dropped_count}"
                )
            except Exception as e:
                logger.error(f"S2S shutdown flush failed: {e}")


def main():
    """Example: run the control loop with FakeRobot on Mac."""
    import os
    from dotenv import load_dotenv

    # Load environment variables from .env
    load_dotenv()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(logging.INFO)

    # Initialize Salesforce auth
    sf_domain = os.getenv("SF_MY_DOMAIN", "").strip()
    sf_client_id = os.getenv("SF_CLIENT_ID", "").strip()
    sf_client_secret = os.getenv("SF_CLIENT_SECRET", "").strip()

    # Skip Salesforce if credentials are missing or still placeholders
    placeholder_values = {"mycompany", "your_client_id_here", "your_client_secret_here", ""}

    if not _auth_is_configured(sf_domain, sf_client_id, sf_client_secret, placeholder_values):
        logger.info("Salesforce credentials not configured; running in offline mode")
        logger.info("  To enable: edit .env with real SF_MY_DOMAIN, SF_CLIENT_ID, SF_CLIENT_SECRET")
    else:
        init_auth(sf_domain, sf_client_id, sf_client_secret)
        init_cases()

    # Initialize Data Cloud if a source/object pair is configured.
    # Note: the ingest host is NOT configured here — it comes back dynamically
    # from the Data Cloud token exchange (instance_url) and can rotate.
    # DATACLOUD_MODE picks the transport: "batch" (Ingestion API, ~3 min latency, the
    # default), "s2s" (Server-to-Server, real-time), or "both" (for side-by-side testing).
    dc_mode = os.getenv("DATACLOUD_MODE", "batch").strip().lower()
    if dc_mode not in {"batch", "s2s", "both"}:
        logger.warning(f"Unknown DATACLOUD_MODE {dc_mode!r}; falling back to 'batch'")
        dc_mode = "batch"

    dc_source = os.getenv("DATACLOUD_SOURCE_NAME", "").strip()
    dc_object = os.getenv("DATACLOUD_OBJECT_NAME", "").strip()
    dc_enabled = dc_mode in {"batch", "both"} and bool(dc_source and dc_object)
    dc_send_interval_ms = int(os.getenv("DATACLOUD_SEND_INTERVAL_MS", "100"))
    if dc_enabled:
        init_datacloud(dc_source, dc_object)
    elif dc_mode in {"batch", "both"}:
        logger.info("Data Cloud batch ingestion not configured; skipping")

    # S2S needs its own External Client App (JWT bearer), key file and Source ID.
    s2s_enabled = False
    if dc_mode in {"s2s", "both"}:
        s2s_client_id = os.getenv("S2S_CLIENT_ID", "").strip()
        s2s_username = os.getenv("S2S_USERNAME", "").strip()
        s2s_key_path = os.getenv("S2S_PRIVATE_KEY_PATH", "").strip()
        s2s_source_id = os.getenv("S2S_APP_SOURCE_ID", "").strip()
        s2s_missing = [
            name
            for name, value in {
                "SF_MY_DOMAIN": sf_domain,
                "S2S_CLIENT_ID": s2s_client_id,
                "S2S_USERNAME": s2s_username,
                "S2S_PRIVATE_KEY_PATH": s2s_key_path,
                "S2S_APP_SOURCE_ID": s2s_source_id,
            }.items()
            if not value or value in placeholder_values or value.startswith("your_")
        ]
        if s2s_missing:
            logger.error(
                f"DATACLOUD_MODE={dc_mode} but S2S is not configured (missing/placeholder: "
                f"{', '.join(s2s_missing)}); S2S telemetry disabled"
            )
        elif not Path(s2s_key_path).expanduser().is_file():
            logger.error(f"S2S private key not found at {s2s_key_path}; S2S telemetry disabled")
        else:
            init_s2s_auth(sf_domain, s2s_client_id, s2s_username, s2s_key_path)
            init_s2s(s2s_source_id)
            s2s_enabled = True
            logger.info(f"S2S real-time ingestion enabled (mode={dc_mode})")

    # Pick the robot implementation. Defaults to the representative simulator;
    # the Pi sets ROBOT_HARDWARE=sphero once the RVR+ is wired up.
    robot_id = os.getenv("ROBOT_ID", "TEST")
    hardware = os.getenv("ROBOT_HARDWARE", "fake").strip().lower()
    if hardware == "sphero":
        from robot import SpheroRobot  # imported lazily: needs the Sphero SDK (Pi only)
        robot = SpheroRobot(robot_id=robot_id)
        logger.info("Using SpheroRobot (real hardware)")
    else:
        robot = FakeRobot(robot_id=robot_id)
        logger.info("Using FakeRobot (representative simulator)")

    # Params file
    params_file = Path(__file__).parent / "params.json"

    # Command subscriber: listen for Robot_Command__e (the /fix loop) and clear the
    # robot's hazard when told to. Needs SF auth; on by default when auth is configured,
    # disable with COMMAND_SUBSCRIBER_ENABLED=false.
    subscriber = None
    cmd_sub_enabled = os.getenv("COMMAND_SUBSCRIBER_ENABLED", "true").strip().lower() in ("1", "true", "yes")
    if cmd_sub_enabled and _auth_is_configured(sf_domain, sf_client_id, sf_client_secret, placeholder_values):
        try:
            from command_subscriber import CommandSubscriber

            def token_provider():
                # Streaming API uses a core SF token + the My Domain instance URL.
                return (
                    get_auth().get_salesforce_token(),
                    f"https://{sf_domain}.my.salesforce.com",
                )

            subscriber = CommandSubscriber(robot=robot, robot_id=robot_id, token_provider=token_provider)
            subscriber.start()
        except Exception as e:
            logger.error(f"Could not start command subscriber: {e}")

    # Create and run the loop
    loop = ControlLoop(
        robot=robot,
        robot_id=robot_id,
        params_file=params_file,
        datacloud_enabled=dc_enabled,
        datacloud_send_interval_ms=dc_send_interval_ms,
        s2s_enabled=s2s_enabled,
    )

    # MAX_ITERATIONS controls run length: a positive number stops after that many
    # ticks (default 100 = ~5 s at 20 Hz, handy for a quick Mac smoke test), while
    # MAX_ITERATIONS=0 runs until Ctrl+C — needed to watch the robot drive a full
    # lap and hit hazards.
    max_iterations = int(os.getenv("MAX_ITERATIONS", "100")) or None
    loop.run(max_iterations=max_iterations)


if __name__ == "__main__":
    main()
