"""
Line follower + colour-patch issue detection.

Pure, hardware-agnostic decision logic: it takes a `Telemetry` snapshot and
decides (a) how to drive to stay on the line, and (b) whether the robot has just
rolled onto a coloured patch that should raise an issue. No hardware, no I/O, no
Salesforce — so it is fully unit-testable and identical for FakeRobot and the
real SpheroRobot.

The RVR+ has a single downward colour sensor (not a left/right pair), so true
gradient following isn't possible. The strategy is **stay-on-line with lost-line
search**: drive forward while the sensor reads the line colour; when the line is
lost, sweep left/right with a growing offset (serpentine) to re-acquire it.

A small state machine governs issue detection and recovery:

    FOLLOWING --(patch confirmed)--> ISSUE (robot halted, Case raised)
       ^                                  |
       |                             (resume from Slack → on_resume())
       |                                  v
       +--(line re-seen N times)--  REACQUIRING

REACQUIRING suppresses all patch detection until the line colour has been seen
again for `reacquire_samples` consecutive ticks. That is what stops the robot
re-triggering on the very patch it was just resumed over: after a resume it must
rediscover the blue tape before any new patch can fire.
"""

from typing import Optional


# State machine states.
FOLLOWING = "following"
ISSUE = "issue"
REACQUIRING = "reacquiring"


# Sensible defaults; every value is overridable from params.json ("line_follow").
DEFAULT_CONFIG = {
    "line_colour": "blue",        # colour_name that means "on the line"
    "base_speed": 0.30,           # 0..1 drive speed while following
    "search_speed": 0.20,         # slower while hunting for a lost line
    "search_sweep_deg": 25.0,     # max heading offset during search
    "search_step": 3.0,           # degrees added to the sweep each lost tick
    "patch_confirm_samples": 3,   # consecutive patch reads before it counts
    "reacquire_samples": 5,       # consecutive line reads to re-arm after resume
}


class LineFollower:
    """
    Decide drive commands and detect colour-patch issues from telemetry.

    `patch_issues` maps a classifier colour name (e.g. "red") to an issue dict,
    e.g. {"hazard": "red_card", "case_type": "...", "subject": "..."}. Only
    colours present in this map can raise an issue; everything else that isn't
    the line colour is treated as "off the line" and triggers the search.
    """

    def __init__(self, config: Optional[dict] = None, patch_issues: Optional[dict] = None):
        self._config = dict(DEFAULT_CONFIG)
        if config:
            self._config.update(config)
        self._patch_issues = dict(patch_issues or {})

        self._state = FOLLOWING
        self._patch_run = 0            # consecutive ticks seeing the current patch colour
        self._patch_run_colour = None  # which colour the current run is counting
        self._line_run = 0             # consecutive ticks seeing the line (for re-acquire)
        self._search_offset = 0.0      # current serpentine offset magnitude
        self._search_sign = 1          # which way the sweep is currently going

    # --- config helpers -----------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def update_config(self, config: Optional[dict] = None, patch_issues: Optional[dict] = None):
        """Apply hot-reloaded params. Safe to call every tick."""
        if config:
            self._config.update(config)
        if patch_issues is not None:
            self._patch_issues = dict(patch_issues)

    @property
    def line_colour(self) -> str:
        return self._config["line_colour"]

    # --- driving ------------------------------------------------------------

    def compute_drive(self, telemetry) -> tuple:
        """
        Return (speed, heading_offset_deg) to stay on the line.

        heading is expressed as an *offset* from current heading: 0 = straight
        on. The caller adds it to the robot's current heading. On the line we go
        straight at base_speed; off the line we slow and sweep with a growing
        offset to re-acquire.
        """
        on_line = telemetry.colour_name == self.line_colour

        if on_line:
            self._search_offset = 0.0
            return (self._config["base_speed"], 0.0)

        # Line lost: serpentine search. Grow the sweep each tick, flipping sign
        # when it hits the cap, so the robot casts side to side to find the line.
        cap = self._config["search_sweep_deg"]
        self._search_offset += self._config["search_step"]
        if self._search_offset >= cap:
            self._search_offset = cap
            self._search_sign *= -1
        return (self._config["search_speed"], self._search_sign * self._search_offset)

    # --- issue detection + recovery ----------------------------------------

    def detect_issue(self, telemetry) -> Optional[dict]:
        """
        Return the issue dict if the robot has just confirmed a patch, else None.

        An issue only fires when:
          - we are in FOLLOWING (not already halted, not still re-acquiring),
          - the sensor reads a mapped patch colour for `patch_confirm_samples`
            consecutive ticks (noise/edge rejection).
        On firing, transitions to ISSUE and returns the issue (with its colour).
        """
        colour = telemetry.colour_name

        # While recovering from a resume, run down the re-acquire counter and
        # suppress all detection until the line is firmly back.
        if self._state == REACQUIRING:
            if colour == self.line_colour:
                self._line_run += 1
                if self._line_run >= self._config["reacquire_samples"]:
                    self._state = FOLLOWING
                    self._line_run = 0
            else:
                self._line_run = 0
            return None

        # While halted, nothing new fires until a resume moves us on.
        if self._state == ISSUE:
            return None

        # FOLLOWING: count consecutive reads of a mapped patch colour.
        if colour in self._patch_issues:
            if colour == self._patch_run_colour:
                self._patch_run += 1
            else:
                self._patch_run_colour = colour
                self._patch_run = 1

            if self._patch_run >= self._config["patch_confirm_samples"]:
                self._state = ISSUE
                self._patch_run = 0
                self._patch_run_colour = None
                issue = dict(self._patch_issues[colour])
                issue["colour"] = colour
                return issue
        else:
            # Any non-patch read breaks the run.
            self._patch_run = 0
            self._patch_run_colour = None

        return None

    def trigger(self, colour: str) -> Optional[dict]:
        """
        Force an issue for `colour` (the simulated safety-net trigger).

        Returns the issue dict and transitions to ISSUE if the colour is a
        configured patch colour and we're in FOLLOWING (not already halted or
        re-acquiring). Returns None otherwise. Bypasses the confirm-samples count
        on purpose — a deliberate trigger needs no debounce.
        """
        if self._state != FOLLOWING or colour not in self._patch_issues:
            return None
        self._state = ISSUE
        self._patch_run = 0
        self._patch_run_colour = None
        issue = dict(self._patch_issues[colour])
        issue["colour"] = colour
        return issue

    def on_resume(self) -> None:
        """
        Called when the robot has been resumed (hazard cleared from Slack).

        Enters REACQUIRING: patch detection stays suppressed until the line
        colour has been seen again for `reacquire_samples` consecutive ticks, so
        the robot cannot re-trigger on the patch it was resumed over — it must
        rediscover the line first, then carry on to the next colour.
        """
        self._state = REACQUIRING
        self._line_run = 0
        self._patch_run = 0
        self._patch_run_colour = None
        self._search_offset = 0.0
