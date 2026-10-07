"""
Unit tests for the control loop, FakeRobot simulator, and auth cache.

Run with: python -m pytest tests/test_control_loop.py -v
"""

import json
import math
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from robot import FakeRobot, Telemetry
from sf_auth import SalesforceAuth
from control_loop import ControlLoop


# ---------- Telemetry shape ----------


def test_fake_robot_telemetry_populates_every_field():
    """Snapshot has values for every documented sensor group, not just the old 8."""
    robot = FakeRobot(robot_id="TEST", seed=42)
    robot.drive(0.5, 45.0)
    t = robot.telemetry()

    assert t.robot_id == "TEST"
    assert 0.0 <= t.speed <= 1.0
    assert 0.0 <= t.heading <= 360.0
    assert 0.0 <= t.battery <= 100.0
    assert isinstance(t.collision, bool)
    assert isinstance(t.is_malfunctioning, bool)

    # New fields are populated (not left at defaults for a running robot)
    assert t.colour_name in {"blue", "red", "green", "yellow", "black", "white", "unknown"}
    assert 0 <= t.colour_r <= 255 and 0 <= t.colour_g <= 255 and 0 <= t.colour_b <= 255
    assert 14.0 <= t.voltage <= 16.5
    assert 0.0 <= t.motor_current_left and 0.0 <= t.motor_current_right
    assert 20.0 <= t.temperature_c <= 40.0
    assert 0.0 <= t.ambient_light <= 1000.0


# ---------- Simulator realism ----------


def test_fake_robot_battery_drains_over_time():
    """Battery decreases monotonically as sim time elapses."""
    robot = FakeRobot(robot_id="TEST", seed=1)
    first = robot.telemetry().battery
    # Rewind the started_at timestamp to simulate 10 minutes of runtime
    robot._started_at -= 600
    second = robot.telemetry().battery
    assert second < first


def test_fake_robot_follows_figure_8():
    """Locator XY traces a closed path — min != max on both axes."""
    robot = FakeRobot(robot_id="TEST", seed=1)
    robot.drive(0.5, 0.0)
    xs, ys = [], []
    for _ in range(50):
        robot._last_tick_ts -= 0.5   # fast-forward 0.5 s between ticks
        t = robot.telemetry()
        xs.append(t.locator_x)
        ys.append(t.locator_y)
    # Both axes see motion of at least half the arena span
    assert max(xs) - min(xs) > 0.3
    assert max(ys) - min(ys) > 0.3


def test_imu_accel_correlates_with_velocity_change():
    """Non-trivial acceleration appears when the robot is actually moving."""
    robot = FakeRobot(robot_id="TEST", seed=1)
    robot.drive(0.6, 0.0)
    accels = []
    for _ in range(30):
        robot._last_tick_ts -= 0.1
        accels.append(abs(robot.telemetry().accel_x))
    assert max(accels) > 0.05  # not just noise


# ---------- Scripted hazards + fake fix ----------


def test_scripted_hazard_fires_once_per_lap_and_sets_malfunctioning():
    robot = FakeRobot(robot_id="TEST", seed=1)
    robot.drive(0.5, 0.0)
    saw_hazard = False
    for _ in range(600):
        robot._last_tick_ts -= 0.1
        t = robot.telemetry()
        if t.active_hazard in {"red_card", "yellow_card", "green_card"}:
            saw_hazard = True
            assert t.is_malfunctioning is True
            break
    assert saw_hazard, "FakeRobot should hit a scripted hazard inside one lap"


def test_clear_hazard_resumes_the_robot():
    robot = FakeRobot(robot_id="TEST", seed=1)
    robot.apply_hazard("red_card")
    assert robot.telemetry().is_malfunctioning is True
    robot.clear_hazard()
    t = robot.telemetry()
    assert t.active_hazard is None
    assert t.is_malfunctioning is False


# ---------- Colour sensor ----------


def test_colour_sensor_mostly_sees_blue_when_on_tape():
    robot = FakeRobot(robot_id="TEST", seed=1)
    robot.drive(0.5, 0.0)
    seen = []
    # Sample a short burst at the top of a lap, before any hazard waypoint
    for _ in range(5):
        robot._last_tick_ts -= 0.05
        seen.append(robot.telemetry().colour_name)
    assert seen.count("blue") >= 4


# ---------- Params + control loop ----------


def test_fake_robot_params_load():
    robot = FakeRobot(robot_id="TEST")
    robot.set_params({"max_speed": 0.8, "sensor_smoothing": True, "motor_invert": True})
    assert robot._params["max_speed"] == 0.8
    assert robot._params["sensor_smoothing"] is True
    assert robot._params["motor_invert"] is True


def test_control_loop_params_reload():
    with TemporaryDirectory() as tmpdir:
        params_file = Path(tmpdir) / "params.json"
        params_file.write_text(json.dumps({"max_speed": 0.6, "sensor_smoothing": False}))

        robot = FakeRobot(robot_id="TEST")
        loop = ControlLoop(
            robot=robot,
            robot_id="TEST",
            params_file=params_file,
            datacloud_enabled=False,
        )

        loop._tick(0)
        assert robot._params["max_speed"] == 0.6

        time.sleep(0.1)  # ensure mtime differs
        params_file.write_text(json.dumps({"max_speed": 0.9, "sensor_smoothing": True}))

        loop._tick(1)
        assert robot._params["max_speed"] == 0.9
        assert robot._params["sensor_smoothing"] is True


# ---------- Auth cache ----------


def test_salesforce_auth_token_cache():
    auth = SalesforceAuth(my_domain="test", client_id="test_id", client_secret="test_secret")
    auth._sf_token_cache = {"access_token": "test_token_123", "expires_at": time.time() + 3600}
    assert auth.get_salesforce_token() == "test_token_123"
