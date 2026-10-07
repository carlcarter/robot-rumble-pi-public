"""
Unit tests for the control loop and components.

Run with: python -m pytest test_control_loop.py -v
"""

import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from robot import FakeRobot, Telemetry
from sf_auth import SalesforceAuth
from control_loop import ControlLoop


def test_fake_robot_telemetry():
    """FakeRobot produces plausible telemetry."""
    robot = FakeRobot(robot_id="TEST")
    robot.drive(0.5, 45.0)

    telem = robot.telemetry()
    assert telem.robot_id == "TEST"
    assert 0.0 <= telem.speed <= 1.0
    assert 0.0 <= telem.heading <= 360.0
    assert 0.0 <= telem.battery <= 100.0
    assert isinstance(telem.collision, bool)


def test_fake_robot_battery_drain():
    """Battery drains during motion."""
    robot = FakeRobot(robot_id="TEST")
    initial = robot.telemetry().battery

    for _ in range(100):
        robot.drive(0.5, 45.0)

    final = robot.telemetry().battery
    assert final < initial, "Battery should drain during motion"


def test_fake_robot_stall_detection():
    """Robot detects stall (no motion for 3+ seconds)."""
    robot = FakeRobot(robot_id="TEST")
    robot.drive(0.01, 45.0)  # Very slow, effectively no progress

    # Simulate 3.5 seconds of no progress
    robot._no_progress_since = time.time() - 3.5

    # Next telemetry should show collision
    telem = robot.telemetry()
    assert telem.collision is True


def test_fake_robot_fog_hazard():
    """Fog hazard injects sensor noise."""
    robot = FakeRobot(robot_id="TEST")
    robot.apply_hazard("fog")

    # Without smoothing, telemetry should be noisy
    headings = [robot.telemetry().heading for _ in range(10)]
    variance = max(headings) - min(headings)
    assert variance > 5.0, "Fog should add significant noise"


def test_fake_robot_params_load():
    """Robot can load and apply params."""
    robot = FakeRobot(robot_id="TEST")
    new_params = {
        "max_speed": 0.8,
        "sensor_smoothing": True,
        "motor_invert": True,
    }
    robot.set_params(new_params)

    assert robot._params["max_speed"] == 0.8
    assert robot._params["sensor_smoothing"] is True
    assert robot._params["motor_invert"] is True


def test_control_loop_params_reload():
    """Control loop detects and reloads params.json."""
    with TemporaryDirectory() as tmpdir:
        params_file = Path(tmpdir) / "params.json"

        # Write initial params
        initial = {"max_speed": 0.6, "sensor_smoothing": False}
        params_file.write_text(json.dumps(initial))

        robot = FakeRobot(robot_id="TEST")
        loop = ControlLoop(
            robot=robot,
            robot_id="TEST",
            params_file=params_file,
            datacloud_enabled=False,
        )

        # Load initial
        loop._tick(0)
        assert robot._params["max_speed"] == 0.6

        # Update params file
        time.sleep(0.1)  # Ensure mtime differs
        updated = {"max_speed": 0.9, "sensor_smoothing": True}
        params_file.write_text(json.dumps(updated))

        # Next tick should reload
        loop._tick(1)
        assert robot._params["max_speed"] == 0.9
        assert robot._params["sensor_smoothing"] is True


def test_salesforce_auth_token_cache():
    """Auth caches tokens to avoid repeated requests."""
    # This is a mock test (doesn't call real Salesforce).
    auth = SalesforceAuth(
        my_domain="test",
        client_id="test_id",
        client_secret="test_secret",
    )

    # Inject a cached token
    auth._sf_token_cache = {
        "access_token": "test_token_123",
        "expires_at": time.time() + 3600,
    }

    # Should return cached token without making an API call
    token = auth.get_salesforce_token()
    assert token == "test_token_123"


if __name__ == "__main__":
    # Run with: python -m pytest test_control_loop.py -v
    print("Run tests with: python -m pytest test_control_loop.py -v")
