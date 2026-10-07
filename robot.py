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

# Colour-sensor samples at the moment a hazard card is "under" the robot.
# RGB values are deliberately offset from the ideal centroid so the classifier
# still has to do a little work.
_HAZARD_CARD_COLOURS = {
    "red_card":    (215, 50,  45),
    "yellow_card": (225, 205, 55),
    "green_card":  (50,  195, 70),
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

    # Where on the figure-8 (phase 0..1) each hazard card sits. Picked so EDI
    # and LON hit hazards at different moments if they share a seed.
    _HAZARD_WAYPOINTS = [
        (0.18, "red_card"),
        (0.45, "yellow_card"),
        (0.72, "green_card"),
    ]
    _HAZARD_WINDOW = 0.015                # how long (in phase units) the card sits

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
        self._fired_hazards_this_lap: set = set()
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
        self._maybe_fire_scripted_hazard()

        # Colour sample depends on where we are on the loop and whether a hazard
        # card is currently "under" the sensor.
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
        self._phase = (self._phase + dt / self._FIGURE_8_PERIOD_S) % 1.0
        # Each wrap starts a new lap; reset the "already fired this lap" set
        if self._phase < 0.01 and self._fired_hazards_this_lap:
            self._fired_hazards_this_lap.clear()
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

    def _maybe_fire_scripted_hazard(self) -> None:
        if self._active_hazard is not None:
            return
        for phase, hazard_name in self._HAZARD_WAYPOINTS:
            if hazard_name in self._fired_hazards_this_lap:
                continue
            if abs(self._phase - phase) < self._HAZARD_WINDOW:
                self._active_hazard = hazard_name
                self._collision_pulse = True
                self._fired_hazards_this_lap.add(hazard_name)
                return

    def _sample_colour(self) -> tuple:
        """Return (r,g,b) the colour sensor sees right now."""
        if self._active_hazard in _HAZARD_CARD_COLOURS:
            base = _HAZARD_CARD_COLOURS[self._active_hazard]
        else:
            # Blue tape, with sensor noise
            base = (42, 65, 215)
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
