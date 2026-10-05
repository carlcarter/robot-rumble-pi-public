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


logger = logging.getLogger(__name__)


class ControlLoop:
    """Main robot control loop."""

    def __init__(
        self,
        robot: Robot,
        robot_id: str,
        params_file: Path,
        datacloud_ingest_url: Optional[str] = None,
    ):
        """
        Args:
            robot: Robot instance (FakeRobot or SpheroRobot)
            robot_id: e.g. "EDI" or "LDN"
            params_file: path to params.json
            datacloud_ingest_url: Ingestion API endpoint (optional; skip if no Data Cloud)
        """
        self.robot = robot
        self.robot_id = robot_id
        self.params_file = params_file
        self.datacloud_ingest_url = datacloud_ingest_url

        self._running = False
        self._last_params_mtime = 0.0
        self._collision_cooldown_until = 0.0

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

        logger.info(f"Control loop starting. Robot: {self.robot_id}")

        try:
            while self._running:
                if max_iterations and iteration >= max_iterations:
                    break

                self._tick(iteration)
                iteration += 1

                if max_iterations is None:
                    # Run forever (until KeyboardInterrupt or stop() called).
                    time.sleep(tick_interval)

        except KeyboardInterrupt:
            logger.info("Control loop interrupted by user")
        except Exception as e:
            logger.exception(f"Control loop failed: {e}")
        finally:
            self.stop()

    def _tick(self, iteration: int) -> None:
        """Single iteration of the control loop."""

        # 1. Read telemetry
        telemetry = self.robot.telemetry()

        # 2. Post to Data Cloud (throttled, every ~20 ticks at 20 Hz = once/sec).
        if self.datacloud_ingest_url and iteration % 20 == 0:
            try:
                ingestor = get_ingestor()
                ingestor.add_record(telemetry)
            except Exception as e:
                logger.error(f"Data Cloud post failed: {e}")

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


def main():
    """Example: run the control loop with FakeRobot on Mac."""
    import os
    from dotenv import load_dotenv

    # Load environment variables from .env
    load_dotenv()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Initialize Salesforce auth
    sf_domain = os.getenv("SF_MY_DOMAIN", "mycompany")
    sf_client_id = os.getenv("SF_CLIENT_ID")
    sf_client_secret = os.getenv("SF_CLIENT_SECRET")

    if not sf_client_id or not sf_client_secret:
        logger.warning("Salesforce credentials not set; running in offline mode")
    else:
        init_auth(sf_domain, sf_client_id, sf_client_secret)
        init_cases()

    # Initialize Data Cloud if endpoint provided
    dc_url = os.getenv("DATACLOUD_INGEST_URL")
    if dc_url:
        init_datacloud(dc_url)

    # Create a fake robot for testing
    robot_id = os.getenv("ROBOT_ID", "TEST")
    robot = FakeRobot(robot_id=robot_id)

    # Params file
    params_file = Path(__file__).parent / "params.json"

    # Create and run the loop
    loop = ControlLoop(
        robot=robot,
        robot_id=robot_id,
        params_file=params_file,
        datacloud_ingest_url=dc_url,
    )

    # For Mac testing: run 100 iterations (5 seconds at 20 Hz)
    loop.run(max_iterations=100)


if __name__ == "__main__":
    main()
