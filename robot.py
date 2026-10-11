"""
Robot abstraction layer.

Provides a common interface for FakeRobot (representative stub, no hardware needed)
and SpheroRobot (real RVR+, implemented when hardware lands). The control loop and
every downstream consumer talks only to this interface.

FakeRobot is deliberately *representative*, not just random: it drives a scripted
figure-8, derives IMU from the kinematic model, decays battery/voltage over time,
and emits colour-sensor samples that mostly see blue tape with occasional hazard
excursions. The dashboard should look alive against this stream.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import math
import random
import time

from colour import ColourClassifier


# Hazards that imply the robot should stop. "fog" and "rain" degrade behaviour
# but don't halt it; the colour-card hazards do.
_HALTING_HAZARDS = {"red_card", "yellow_card", "green_card", "rush_hour", "blackout"}

# Blue line tape the robot follows (sensor RGB, before noise). Classifies "blue".
_LINE_TAPE_RGB = (42, 65, 215)

# Colour-sensor RGB for each coloured-paper patch on the course. Deliberately
# offset from the ideal centroid so the classifier still has to do a little work;
# these classify to "red" / "yellow" / "green" respectively.
_PATCH_RGB = {
    "red":    (215, 50,  45),
    "yellow": (225, 205, 55),
    "green":  (50,  195, 70),
}


@dataclass
class Telemetry:
    """Robot sensor data, captured at a moment in time."""

    # --- Identity + state ---
    robot_id: str
    ts: float                             # unix timestamp, seconds
    collision: bool                       # one-sample flag for a crash event
    active_hazard: Optional[str] = None   # named hazard, or None
    is_malfunctioning: bool = False       # derived: active_hazard halts the robot
    heat_number: int = 0

    # --- Headline motion (kept for backwards compatibility) ---
    speed: float = 0.0                    # 0.0 to 1.0
    heading: float = 0.0                  # 0.0 to 360.0 degrees
    battery: float = 100.0                # 0.0 to 100.0 percent

    # --- IMU: linear acceleration (g) ---
    accel_x: float = 0.0
    accel_y: float = 0.0
    accel_z: float = 1.0                  # 1 g gravity when stationary and level

    # --- IMU: angular rate (deg/s) ---
    gyro_x: float = 0.0
    gyro_y: float = 0.0
    gyro_z: float = 0.0

    # --- IMU: attitude (degrees) ---
    attitude_pitch: float = 0.0
    attitude_roll: float = 0.0
    attitude_yaw: float = 0.0

    # --- Locator (encoder-based odometry) ---
    locator_x: float = 0.0                # metres from origin
    locator_y: float = 0.0
    velocity_x: float = 0.0               # metres/second
    velocity_y: float = 0.0

    # --- Colour sensor ---
    colour_r: int = 0                     # 0-255
    colour_g: int = 0
    colour_b: int = 0
    colour_name: str = "unknown"

    # --- Environment ---
    ambient_light: float = 0.0            # lux

    # --- Power and health ---
    voltage: float = 16.0                 # volts; RVR+ nominal is ~16 V
    motor_current_left: float = 0.0       # amps
    motor_current_right: float = 0.0      # amps
    temperature_c: float = 25.0           # degrees C


class Robot(ABC):
    """Base interface all robot implementations follow."""

    @abstractmethod
    def drive(self, speed: float, heading: float) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def telemetry(self) -> Telemetry: ...

    @abstractmethod
    def apply_hazard(self, hazard_name: str) -> None: ...

    @abstractmethod
    def clear_hazard(self) -> None:
        """
        Resume normal operation after a Case has been "fixed" from Slack.

        Clears the active hazard, resets the one-sample collision flag, and
        releases any halt the hazard imposed. The line-follower (if running)
        will pick up driving again on the next tick.
        """
        ...

    @abstractmethod
    def set_params(self, params: dict) -> None: ...


class FakeRobot(Robot):
    """
    Representative simulator — not random noise, a real-looking robot.

    Drives a repeating figure-8 inside a virtual 2 m x 2 m arena. Hazard cards
    appear at scripted waypoints along the path. IMU, locator, voltage,
    temperature and motor currents are all derived from the kinematic model so
    they stay correlated (accel spikes when velocity jumps, current rises with
    commanded speed, etc.).
    """

    # Arena geometry
    _ARENA_SIZE_M = 2.0                   # half-width of the figure-8 lobes
    _NOMINAL_SPEED_MPS = 0.3              # ~linear speed when commanded 0.5
    _FIGURE_8_PERIOD_S = 24.0             # one full lap of the figure-8

    # Battery / voltage decay
    _BATTERY_HOURS_TO_EMPTY = 1.0
    _VOLTAGE_FULL = 16.0
    _VOLTAGE_EMPTY = 14.5

    # Where on the figure-8 (phase 0..1) each coloured-paper PATCH sits on the
    # course. The sensor reads that colour while the robot is over the patch; the
    # control loop's line follower is what decides to raise an issue. The patch
    # window is wide enough to span several ticks so detection's confirm-samples
    # logic is genuinely exercised.
    _PATCH_WAYPOINTS = [
        (0.18, "red"),
        (0.45, "yellow"),
        (0.72, "green"),
    ]
    _PATCH_WINDOW = 0.02                  # how long (in phase units) the patch sits under the sensor

    def __init__(self, robot_id: str = "TEST", seed: Optional[int] = None):
        self.robot_id = robot_id
        self._rng = random.Random(seed)
        self._started_at = time.time()
        self._last_tick_ts = self._started_at
        self._colour_classifier = ColourClassifier()

        # Driving state (what the control loop asks for)
        self._cmd_speed = 0.0
        self._cmd_heading = 0.0

        # Simulation state (derived)
        self._phase = 0.0                 # 0..1 around the figure-8
        self._pose_x = 0.0
        self._pose_y = 0.0
        self._vel_x = 0.0
        self._vel_y = 0.0
        self._prev_vel_x = 0.0
        self._prev_vel_y = 0.0
        self._yaw_deg = 0.0
        self._prev_yaw_deg = 0.0

        # Fault state
        self._collision_pulse = False     # one-tick flag
        self._active_hazard: Optional[str] = None
        self._lap_count = 0
        self._heat_number = 1

        # Behaviour params (Slack-Code-editable; hot-reloaded by control loop)
        self._params = {
            "max_speed": 0.6,
            "turn_rate": 1.4,
            "avoidance_radius_cm": 15,
            "sensor_smoothing": False,
            "motor_invert": False,
        }

    # ---------- Robot interface ----------

    def drive(self, speed: float, heading: float) -> None:
        self._cmd_speed = max(0.0, min(1.0, speed))
        self._cmd_heading = heading % 360.0

    def stop(self) -> None:
        self._cmd_speed = 0.0

    def apply_hazard(self, hazard_name: str) -> None:
        self._active_hazard = hazard_name
        # Legacy hazards still supported for compatibility with hazard.py
        if hazard_name == "rush_hour":
            self._collision_pulse = True

    def clear_hazard(self) -> None:
        self._active_hazard = None
        self._collision_pulse = False
        # Nudge the drive command back on so visually the robot resumes immediately
        if self._cmd_speed == 0.0:
            self._cmd_speed = 0.4

    def set_params(self, params: dict) -> None:
        self._params.update(params)

    def telemetry(self) -> Telemetry:
        """
        Advance the simulation to 'now' and emit a snapshot.

        Called once per control-loop tick (~20 Hz), so the dt is small and the
        kinematic integration is naive-but-fine.
        """
        now = time.time()
        dt = max(1e-3, min(0.5, now - self._last_tick_ts))
        self._last_tick_ts = now

        self._advance_phase(dt)
        self._integrate_motion(dt)

        # Colour sample depends on where we are on the course: the blue line most
        # of the time, a coloured-paper patch when the route is over one. The
        # control loop detects the patch colour and decides to raise an issue —
        # the robot no longer fires hazards itself.
        r, g, b = self._sample_colour()
        colour_name = self._colour_classifier.classify(r, g, b)

        # Battery/voltage decay (seconds of total runtime, not just driven)
        battery_pct = self._battery_percent(now)
        voltage = self._voltage_from_battery(battery_pct)

        # Motor currents correlate with commanded speed, with asymmetry when turning
        base_current = 0.3 + 2.2 * self._cmd_speed
        turn_bias = 0.4 * math.sin(math.radians(self._yaw_deg - self._cmd_heading))
        motor_left = max(0.0, base_current + turn_bias + self._rng.gauss(0, 0.05))
        motor_right = max(0.0, base_current - turn_bias + self._rng.gauss(0, 0.05))
        if self._collision_pulse:
            motor_left = motor_right = 4.0

        temperature = 25.0 + 8.0 * (1 - math.exp(-(now - self._started_at) / 1800.0))
        ambient_light = 450.0 + 150.0 * math.sin((now - self._started_at) / 60.0)

        # IMU derived from velocity + yaw deltas
        accel_x = (self._vel_x - self._prev_vel_x) / dt / 9.81 + self._rng.gauss(0, 0.05)
        accel_y = (self._vel_y - self._prev_vel_y) / dt / 9.81 + self._rng.gauss(0, 0.05)
        accel_z = 1.0 + self._rng.gauss(0, 0.02)
        if self._collision_pulse:
            accel_x -= 2.5  # believable deceleration spike

        gyro_z = ((self._yaw_deg - self._prev_yaw_deg + 180) % 360 - 180) / dt
        gyro_x = self._rng.gauss(0, 1.0)
        gyro_y = self._rng.gauss(0, 1.0)

        # Attitude: yaw tracks heading; pitch/roll gently wobble
        pitch = 1.5 * math.sin((now - self._started_at) * 1.1) + self._rng.gauss(0, 0.3)
        roll = 1.5 * math.sin((now - self._started_at) * 0.9) + self._rng.gauss(0, 0.3)

        snapshot = Telemetry(
            robot_id=self.robot_id,
            ts=now,
            collision=self._collision_pulse,
            active_hazard=self._active_hazard,
            is_malfunctioning=self._is_malfunctioning(),
            heat_number=self._heat_number,
            speed=self._cmd_speed,
            heading=self._yaw_deg,
            battery=battery_pct,
            accel_x=accel_x, accel_y=accel_y, accel_z=accel_z,
            gyro_x=gyro_x, gyro_y=gyro_y, gyro_z=gyro_z,
            attitude_pitch=pitch, attitude_roll=roll, attitude_yaw=self._yaw_deg,
            locator_x=self._pose_x, locator_y=self._pose_y,
            velocity_x=self._vel_x, velocity_y=self._vel_y,
            colour_r=r, colour_g=g, colour_b=b, colour_name=colour_name,
            ambient_light=ambient_light,
            voltage=voltage,
            motor_current_left=motor_left,
            motor_current_right=motor_right,
            temperature_c=temperature,
        )

        # Collision is a one-sample event
        self._collision_pulse = False
        return snapshot

    # ---------- Simulation internals ----------

    def _is_malfunctioning(self) -> bool:
        return self._active_hazard in _HALTING_HAZARDS

    def _advance_phase(self, dt: float) -> None:
        # When halted the phase freezes — the robot stops dead on the tape
        if self._is_malfunctioning() or self._cmd_speed == 0.0:
            return
        prev = self._phase
        self._phase = (prev + dt / self._FIGURE_8_PERIOD_S) % 1.0
        if self._phase < prev:   # wrapped → new lap
            self._lap_count += 1

    def _integrate_motion(self, dt: float) -> None:
        # Figure-8 (lemniscate of Gerono) parametric form, scaled to arena
        t = self._phase * 2 * math.pi
        a = self._ARENA_SIZE_M / 2
        new_x = a * math.sin(t)
        new_y = a * math.sin(t) * math.cos(t)

        # Velocity + yaw from pose delta
        self._prev_vel_x, self._prev_vel_y = self._vel_x, self._vel_y
        self._vel_x = (new_x - self._pose_x) / dt
        self._vel_y = (new_y - self._pose_y) / dt
        self._pose_x, self._pose_y = new_x, new_y

        self._prev_yaw_deg = self._yaw_deg
        if abs(self._vel_x) + abs(self._vel_y) > 1e-3:
            self._yaw_deg = math.degrees(math.atan2(self._vel_y, self._vel_x)) % 360.0

    def _current_patch_colour(self) -> Optional[str]:
        """Which coloured-paper patch (if any) is under the sensor at this phase."""
        for phase, colour in self._PATCH_WAYPOINTS:
            if abs(self._phase - phase) < self._PATCH_WINDOW:
                return colour
        return None

    def _sample_colour(self) -> tuple:
        """Return (r,g,b) the colour sensor sees right now — blue line, or a patch."""
        patch = self._current_patch_colour()
        base = _PATCH_RGB[patch] if patch else _LINE_TAPE_RGB
        noise = lambda: int(self._rng.gauss(0, 6))
        return (
            max(0, min(255, base[0] + noise())),
            max(0, min(255, base[1] + noise())),
            max(0, min(255, base[2] + noise())),
        )

    def _battery_percent(self, now: float) -> float:
        elapsed_h = (now - self._started_at) / 3600.0
        pct = 100.0 - (elapsed_h / self._BATTERY_HOURS_TO_EMPTY) * 100.0
        return max(0.0, min(100.0, pct))

    def _voltage_from_battery(self, pct: float) -> float:
        return self._VOLTAGE_EMPTY + (self._VOLTAGE_FULL - self._VOLTAGE_EMPTY) * (pct / 100.0)


# ============================================================================
# SpheroRobot — real RVR+ over UART.
#
# !!! UNTESTED SCAFFOLDING !!!
# Written against the Sphero SDK docs before the hardware arrived. It will almost
# certainly need adjustment on the real robot — especially the sensor-streaming
# field names and the exact drive call. Do NOT assume this works until it has run
# on an RVR+. The hardware-agnostic logic (line_follower.py, control_loop.py) is
# fully tested; this class is the only unverified part.
#
# Known gotcha (see memory [[robot-rumble-state]]): the Sphero SDK calls
# asyncio.get_event_loop().run_until_complete() at import/init time, which fails
# on Python 3.14 and on modern Pi OS (Debian Trixie, Python 3.13+). Use the Pi 4
# image documented in docs/pi-hardware-setup.md (Bookworm) and the pinned deps.
# ============================================================================
class SpheroRobot(Robot):
    """
    Real Sphero RVR+ driven over the Pi's UART (/dev/serial0) via the Sphero SDK's
    synchronous observer (`SpheroRvrObserver`).

    Design mirrors FakeRobot's contract exactly so control_loop.py and
    line_follower.py are unchanged between sim and hardware. Telemetry is assembled
    from the SDK's sensor-streaming callbacks (colour, IMU, locator, accelerometer)
    plus battery polling; colour is classified with the same ColourClassifier so the
    line-follow / patch-detection logic sees identical colour_name values.
    """

    # RVR nominal speed in m/s at full drive, used to scale our 0..1 speed command
    # onto the SDK's 0..255 byte. Tune on hardware.
    _SDK_MAX_SPEED_BYTE = 255

    def __init__(self, robot_id: str = "EDI"):
        # Import lazily so the module only needs the SDK on the Pi, not in tests/CI.
        from sphero_sdk import SpheroRvrObserver  # noqa: F401

        self.robot_id = robot_id
        self._classifier = ColourClassifier()
        self._started_at = time.time()
        self._heat_number = 1

        # Latest sensor values, updated by streaming callbacks.
        self._sensor = {}
        self._active_hazard: Optional[str] = None
        self._battery_pct = 100.0

        self._rvr = SpheroRvrObserver()
        self._wake_and_stream()

    # ---------- setup ----------

    def _wake_and_stream(self) -> None:
        """Wake the RVR and subscribe to the sensor streams we need."""
        self._rvr.wake()
        time.sleep(2)  # SDK needs a moment after wake
        self._rvr.reset_yaw()

        # Register handlers, then enable streaming. Exact sensor ids / field names
        # per the SDK's RvrStreamingServices — VERIFY ON HARDWARE.
        self._rvr.sensor_control.add_sensor_data_handler(
            service="ColorDetection", handler=self._on_colour
        )
        self._rvr.sensor_control.add_sensor_data_handler(
            service="Locator", handler=self._on_locator
        )
        self._rvr.sensor_control.add_sensor_data_handler(
            service="IMU", handler=self._on_imu
        )
        self._rvr.sensor_control.add_sensor_data_handler(
            service="Accelerometer", handler=self._on_accel
        )
        self._rvr.enable_color_detection(is_enabled=True)
        self._rvr.sensor_control.start(interval=50)  # ms → 20 Hz

    # ---------- streaming callbacks ----------

    def _on_colour(self, data):
        # Expected shape (verify): {"ColorDetection": {"R":..,"G":..,"B":..}}
        c = data.get("ColorDetection", data)
        self._sensor["colour"] = (int(c.get("R", 0)), int(c.get("G", 0)), int(c.get("B", 0)))

    def _on_locator(self, data):
        loc = data.get("Locator", data)
        self._sensor["locator"] = (float(loc.get("X", 0.0)), float(loc.get("Y", 0.0)))

    def _on_imu(self, data):
        imu = data.get("IMU", data)
        self._sensor["imu"] = (
            float(imu.get("Pitch", 0.0)), float(imu.get("Roll", 0.0)), float(imu.get("Yaw", 0.0))
        )

    def _on_accel(self, data):
        a = data.get("Accelerometer", data)
        self._sensor["accel"] = (
            float(a.get("X", 0.0)), float(a.get("Y", 0.0)), float(a.get("Z", 1.0))
        )

    # ---------- Robot interface ----------

    def drive(self, speed: float, heading: float) -> None:
        if self._active_hazard in _HALTING_HAZARDS:
            return  # halted until cleared
        speed_byte = int(max(0.0, min(1.0, speed)) * self._SDK_MAX_SPEED_BYTE)
        # SDK heading is 0..359 integer, 0 = the direction it faced at reset_yaw.
        self._rvr.drive_with_heading(
            speed=speed_byte,
            heading=int(heading) % 360,
            flags=0,
        )

    def stop(self) -> None:
        self._rvr.drive_with_heading(speed=0, heading=0, flags=0)

    def apply_hazard(self, hazard_name: str) -> None:
        self._active_hazard = hazard_name
        if hazard_name in _HALTING_HAZARDS:
            self.stop()

    def clear_hazard(self) -> None:
        self._active_hazard = None

    def set_params(self, params: dict) -> None:
        # No live tuning of the SDK itself yet; follower reads line_follow params.
        # Kept for interface parity and future per-robot tuning.
        self._params = dict(params)

    def telemetry(self) -> Telemetry:
        now = time.time()
        r, g, b = self._sensor.get("colour", (0, 0, 0))
        colour_name = self._classifier.classify(r, g, b)
        lx, ly = self._sensor.get("locator", (0.0, 0.0))
        pitch, roll, yaw = self._sensor.get("imu", (0.0, 0.0, 0.0))
        ax, ay, az = self._sensor.get("accel", (0.0, 0.0, 1.0))

        # Battery polled occasionally (the SDK call is slow); refresh ~every 30s.
        if now - getattr(self, "_last_battery_poll", 0) > 30:
            try:
                self._battery_pct = float(self._rvr.get_battery_percentage().get("percentage", self._battery_pct))
            except Exception:
                pass
            self._last_battery_poll = now

        return Telemetry(
            robot_id=self.robot_id,
            ts=now,
            collision=False,  # TODO: derive from an accel spike threshold on hardware
            active_hazard=self._active_hazard,
            is_malfunctioning=self._active_hazard in _HALTING_HAZARDS,
            heat_number=self._heat_number,
            heading=yaw % 360.0,
            battery=self._battery_pct,
            accel_x=ax, accel_y=ay, accel_z=az,
            attitude_pitch=pitch, attitude_roll=roll, attitude_yaw=yaw,
            locator_x=lx, locator_y=ly,
            colour_r=r, colour_g=g, colour_b=b, colour_name=colour_name,
            voltage=self._voltage_from_pct(self._battery_pct),
        )

    def _voltage_from_pct(self, pct: float) -> float:
        return 14.5 + (16.0 - 14.5) * (pct / 100.0)

    def close(self) -> None:
        try:
            self._rvr.sensor_control.clear()
            self._rvr.close()
        except Exception:
            pass
