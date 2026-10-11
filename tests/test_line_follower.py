"""Unit tests for the line follower + colour-patch issue state machine."""

from line_follower import LineFollower, FOLLOWING, ISSUE, REACQUIRING


class FakeTel:
    """Minimal telemetry stub — only colour_name matters to the follower."""

    def __init__(self, colour_name):
        self.colour_name = colour_name


PATCH_ISSUES = {
    "red":    {"hazard": "red_card",    "case_type": "Robot Critical Issue", "subject": "RVR-{robot}: red"},
    "yellow": {"hazard": "yellow_card", "case_type": "Robot Warning Issue",  "subject": "RVR-{robot}: yellow"},
    "green":  {"hazard": "green_card",  "case_type": "Robot Minor Issue",    "subject": "RVR-{robot}: green"},
}

# Fast, deterministic config for tests.
CONFIG = {
    "line_colour": "blue",
    "base_speed": 0.3,
    "search_speed": 0.2,
    "search_sweep_deg": 20.0,
    "search_step": 5.0,
    "patch_confirm_samples": 3,
    "reacquire_samples": 2,
}


def make_follower():
    return LineFollower(config=CONFIG, patch_issues=PATCH_ISSUES)


# --- driving ---------------------------------------------------------------

def test_on_line_drives_straight_at_base_speed():
    f = make_follower()
    speed, offset = f.compute_drive(FakeTel("blue"))
    assert speed == 0.3
    assert offset == 0.0


def test_lost_line_searches_with_growing_offset():
    f = make_follower()
    s1, o1 = f.compute_drive(FakeTel("white"))
    s2, o2 = f.compute_drive(FakeTel("white"))
    assert s1 == 0.2  # slows while searching
    assert abs(o1) == 5.0
    assert abs(o2) == 10.0  # offset grows each lost tick


def test_search_offset_resets_when_line_reacquired():
    f = make_follower()
    f.compute_drive(FakeTel("white"))
    f.compute_drive(FakeTel("white"))
    _, offset = f.compute_drive(FakeTel("blue"))
    assert offset == 0.0


def test_configurable_line_colour():
    f = LineFollower(config={**CONFIG, "line_colour": "black"}, patch_issues=PATCH_ISSUES)
    speed, offset = f.compute_drive(FakeTel("black"))
    assert speed == 0.3 and offset == 0.0
    # blue is no longer the line, so it triggers a search
    speed, _ = f.compute_drive(FakeTel("blue"))
    assert speed == 0.2


# --- issue detection -------------------------------------------------------

def test_patch_requires_confirm_samples():
    f = make_follower()
    assert f.detect_issue(FakeTel("red")) is None   # 1
    assert f.detect_issue(FakeTel("red")) is None   # 2
    issue = f.detect_issue(FakeTel("red"))          # 3 → fires
    assert issue is not None
    assert issue["hazard"] == "red_card"
    assert issue["colour"] == "red"
    assert f.state == ISSUE


def test_patch_run_breaks_on_non_patch_colour():
    f = make_follower()
    f.detect_issue(FakeTel("red"))
    f.detect_issue(FakeTel("red"))
    f.detect_issue(FakeTel("blue"))  # breaks the run
    assert f.detect_issue(FakeTel("red")) is None  # count restarted
    assert f.state == FOLLOWING


def test_flicker_between_patch_colours_does_not_confirm():
    f = make_follower()
    assert f.detect_issue(FakeTel("red")) is None
    assert f.detect_issue(FakeTel("yellow")) is None  # different colour resets
    assert f.detect_issue(FakeTel("red")) is None     # only 1 red in a row again


def test_no_new_issue_while_halted():
    f = make_follower()
    for _ in range(3):
        f.detect_issue(FakeTel("red"))
    assert f.state == ISSUE
    # Still sitting on a patch, but halted — nothing new fires.
    assert f.detect_issue(FakeTel("green")) is None


# --- recovery / re-arm (the behaviour the user asked for) ------------------

def test_resume_suppresses_detection_until_line_reacquired():
    f = make_follower()
    for _ in range(3):
        f.detect_issue(FakeTel("red"))
    assert f.state == ISSUE

    f.on_resume()
    assert f.state == REACQUIRING

    # Still over the red sheet it resumed on — must NOT re-trigger.
    assert f.detect_issue(FakeTel("red")) is None
    assert f.detect_issue(FakeTel("red")) is None
    assert f.state == REACQUIRING

    # Rediscover the blue line for reacquire_samples (2) ticks → back to following.
    assert f.detect_issue(FakeTel("blue")) is None
    assert f.detect_issue(FakeTel("blue")) is None
    assert f.state == FOLLOWING


def test_line_run_resets_if_line_lost_during_reacquire():
    f = make_follower()
    for _ in range(3):
        f.detect_issue(FakeTel("red"))
    f.on_resume()
    f.detect_issue(FakeTel("blue"))   # 1 of 2
    f.detect_issue(FakeTel("white"))  # line lost → counter resets
    f.detect_issue(FakeTel("blue"))   # 1 of 2 again
    assert f.state == REACQUIRING


def test_simulated_trigger_fires_immediately_without_confirm():
    f = make_follower()
    issue = f.trigger("yellow")
    assert issue is not None
    assert issue["hazard"] == "yellow_card"
    assert issue["colour"] == "yellow"
    assert f.state == ISSUE


def test_simulated_trigger_rejects_unknown_colour():
    f = make_follower()
    assert f.trigger("purple") is None
    assert f.state == FOLLOWING


def test_simulated_trigger_noop_while_halted():
    f = make_follower()
    f.trigger("red")
    assert f.state == ISSUE
    # already halted → a second trigger does nothing until resume
    assert f.trigger("green") is None


def test_fires_on_next_patch_after_full_recovery():
    f = make_follower()
    for _ in range(3):
        f.detect_issue(FakeTel("red"))
    f.on_resume()
    f.detect_issue(FakeTel("blue"))
    f.detect_issue(FakeTel("blue"))  # recovered → FOLLOWING
    assert f.state == FOLLOWING
    # Next patch (green) fires normally.
    assert f.detect_issue(FakeTel("green")) is None
    assert f.detect_issue(FakeTel("green")) is None
    issue = f.detect_issue(FakeTel("green"))
    assert issue is not None and issue["hazard"] == "green_card"


def test_same_colour_refires_after_recovery():
    """
    Running all day on a loop: a colour seen earlier must trigger again every time
    the robot hits it (after re-acquiring the line). No 'already seen' memory.
    """
    f = make_follower()

    def fire_red_then_recover():
        for _ in range(3):
            f.detect_issue(FakeTel("red"))
        assert f.state == ISSUE
        f.on_resume()
        f.detect_issue(FakeTel("blue"))
        f.detect_issue(FakeTel("blue"))  # re-acquired → FOLLOWING
        assert f.state == FOLLOWING

    # Hit the *same* red patch three separate times around the loop.
    for lap in range(3):
        fire_red_then_recover()
    # And confirm the third pass genuinely produced an issue each time.
    for _ in range(2):
        assert f.detect_issue(FakeTel("red")) is None
    issue = f.detect_issue(FakeTel("red"))
    assert issue is not None and issue["hazard"] == "red_card"
