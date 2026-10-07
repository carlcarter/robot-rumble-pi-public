"""
Robot abstraction layer.

Provides a common interface for FakeRobot (for testing) and SpheroRobot (real hardware).
The control loop and all other code talk only to this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import time
import random


@dataclass
class Telemetry:
    """Robot sensor data, captured at a moment in time."""
    robot_id: str
    ts: float  # unix timestamp (seconds)
    speed: float  # 0.0 to 1.0
    heading: float  # 0.0 to 360.0 degrees
    battery: float  # 0.0 to 100.0 percent
    collision: bool  # True if robot detected a crash/stall
    active_hazard: Optional[str] = None  # "fog" | "rain" | "rush_hour" | "gremlin" | "blackout" | None
    heat_number: int = 0


class Robot(ABC):
    """Base interface all robot implementations follow."""

    @abstractmethod
    def drive(self, speed: float, heading: float) -> None:
        """
        Command the robot to move.

        Args:
            speed: 0.0 (stop) to 1.0 (max). On Mac this is simulated; on Pi it goes to RVR+.
            heading: 0.0 to 360.0 degrees. Absolute heading.
        """
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop all motion immediately."""
        ...

    @abstractmethod
    def telemetry(self) -> Telemetry:
        """Read the robot's current state. Called frequently by the control loop."""
        ...

    @abstractmethod
    def apply_hazard(self, hazard_name: str) -> None:
        """
        Apply a hazard: "fog" (sensor noise), "rain" (traction loss),
        "rush_hour" (obstacle), "gremlin" (random param flip), "blackout" (no telemetry).
        """
        ...

    @abstractmethod
    def set_params(self, params: dict) -> None:
        """
        Load the "bugs" from params.json. The robot's behaviour changes based on these.
        Called whenever the file is reloaded (for Slack Code fixes).
        """
        ...


class FakeRobot(Robot):
    """Simulated robot for Mac development and testing. No hardware required."""

    def __init__(self, robot_id: str = "TEST"):
        self.robot_id = robot_id
        self._speed = 0.0
        self._heading = 0.0
        self._battery = 100.0
        self._started_at = time.time()
        self._no_progress_since = time.time()
        self._collision = False
        self._active_hazard: Optional[str] = None
        self._heat_number = 0

        # Behaviour params (loaded from params.json).
        self._params = {
            "max_speed": 0.6,
            "turn_rate": 1.4,
            "avoidance_radius_cm": 15,
            "sensor_smoothing": False,
            "motor_invert": False,
        }

    def drive(self, speed: float, heading: float) -> None:
        """Simulate motion: battery drains, can stall if speed too low for params."""
        self._speed = min(1.0, max(0.0, speed))
        self._heading = heading % 360.0

        # Simulate battery drain: ~0.5% per second of active motion.
        if self._speed > 0.05:
            self._battery = max(0.0, self._battery - 0.5 / 100.0)
            self._no_progress_since = time.time()
        else:
            # Stall detection: 3s no progress = crash.
            if time.time() - self._no_progress_since > 3.0:
                self._collision = True

    def stop(self) -> None:
        """Stop motion."""
        self._speed = 0.0
        self._no_progress_since = time.time()

    def telemetry(self) -> Telemetry:
        """Return current state, with simulated noise if 'fog' hazard is active."""
        speed = self._speed
        heading = self._heading

        # Stall detection: if no motion for 3+ seconds, mark as collision.
        if time.time() - self._no_progress_since > 3.0:
            self._collision = True

        # "Fog" hazard: inject sensor noise (but apply smoothing if param is set).
        if self._active_hazard == "fog":
            if not self._params.get("sensor_smoothing", False):
                # Without smoothing, noise dominates.
                speed = max(0.0, speed + random.uniform(-0.2, 0.2))
                heading = (heading + random.uniform(-20, 20)) % 360.0

        # "Rain" hazard: reduce effective max speed.
        if self._active_hazard == "rain":
            speed *= 0.5

        return Telemetry(
            robot_id=self.robot_id,
            ts=time.time(),
            speed=max(0.0, min(1.0, speed)),
            heading=heading,
            battery=self._battery,
            collision=self._collision,
            active_hazard=self._active_hazard,
            heat_number=self._heat_number,
        )

    def apply_hazard(self, hazard_name: str) -> None:
        """Simulate a hazard condition."""
        self._active_hazard = hazard_name

        if hazard_name == "gremlin":
            # Randomly flip a parameter to confuse the robot.
            key = random.choice(list(self._params.keys()))
            if isinstance(self._params[key], bool):
                self._params[key] = not self._params[key]
            elif isinstance(self._params[key], float):
                self._params[key] *= -1  # Invert (e.g., turn direction).

        elif hazard_name == "rush_hour":
            # Trigger a collision as if an obstacle appeared.
            self._collision = True

    def set_params(self, params: dict) -> None:
        """Update behaviour params. Slack Code edits params.json, we reload it."""
        self._params.update(params)
