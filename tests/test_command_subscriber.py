"""
Tests for the Robot_Command__e subscriber dispatch logic — the "fake fix" loop.

No network, no Salesforce org: the dispatch and filter functions are pure, and the
CometD streaming layer is injected with a fake client so the whole receive->dispatch
path is exercised on a laptop.
"""

from robot import FakeRobot
import command_subscriber
from command_subscriber import (
    should_handle,
    dispatch_command,
    handle_event,
    CommandSubscriber,
    STREAMING_CHANNEL,
)


def _event(robot_id, command):
    """Shape mirrors a real platform-event message's data.payload."""
    return {"payload": {"Robot_ID__c": robot_id, "Command__c": command}}


# ---------- Filtering ----------


def test_should_handle_matches_own_robot():
    assert should_handle(_event("EDI", "RESUME"), "EDI") is True


def test_should_handle_is_case_insensitive():
    assert should_handle(_event("edi", "RESUME"), "EDI") is True


def test_should_handle_ignores_other_robot():
    assert should_handle(_event("LON", "RESUME"), "EDI") is False


def test_should_handle_ignores_blank_robot():
    assert should_handle(_event("", "RESUME"), "EDI") is False


# ---------- Dispatch ----------


def test_resume_clears_hazard():
    robot = FakeRobot("EDI")
    robot.apply_hazard("red_card")
    assert robot.telemetry().is_malfunctioning is True

    assert dispatch_command(robot, "RESUME") is True
    assert robot.telemetry().is_malfunctioning is False


def test_reset_hazard_also_clears():
    robot = FakeRobot("EDI")
    robot.apply_hazard("yellow_card")
    assert dispatch_command(robot, "RESET_HAZARD") is True
    assert robot.telemetry().active_hazard is None


def test_halt_stops_the_robot():
    robot = FakeRobot("EDI")
    robot.drive(0.5, 0.0)
    assert dispatch_command(robot, "HALT") is True


def test_unknown_command_is_ignored_not_raised():
    robot = FakeRobot("EDI")
    assert dispatch_command(robot, "EXPLODE") is False


def test_command_is_case_insensitive():
    robot = FakeRobot("EDI")
    robot.apply_hazard("red_card")
    assert dispatch_command(robot, " resume ") is True


# ---------- handle_event: filter + dispatch together ----------


def test_handle_event_applies_only_to_matching_robot():
    robot = FakeRobot("EDI")
    robot.apply_hazard("red_card")

    # Event for LON must not touch EDI
    assert handle_event(_event("LON", "RESUME"), robot, "EDI") is False
    assert robot.telemetry().is_malfunctioning is True

    # Event for EDI clears it
    assert handle_event(_event("EDI", "RESUME"), robot, "EDI") is True
    assert robot.telemetry().is_malfunctioning is False


# ---------- Streaming layer with an injected fake client ----------


class FakeClient:
    """
    Sync Bayeux client stand-in. Delivers one batch of scripted messages on the
    first connect() call, then signals the subscriber to stop so the long-poll
    loop doesn't spin forever.
    """

    def __init__(self, messages, stop_event):
        self._messages = messages
        self._stop_event = stop_event
        self.handshaken = False
        self.disconnected = False
        self.subscribed = None
        self._delivered = False

    def handshake(self):
        self.handshaken = True

    def subscribe(self, channel):
        self.subscribed = channel

    def connect(self):
        if self._delivered:
            self._stop_event.set()
            return []
        self._delivered = True
        # Return events, then stop the loop on the next pass.
        self._stop_event.set()
        return [m for m in self._messages if not m.get("channel", "").startswith("/meta/")]

    def disconnect(self):
        self.disconnected = True


def test_subscriber_listen_dispatches_a_real_message():
    robot = FakeRobot("EDI")
    robot.apply_hazard("red_card")

    message = {"channel": STREAMING_CHANNEL, "data": _event("EDI", "RESUME")}

    sub = CommandSubscriber(
        robot=robot,
        robot_id="EDI",
        token_provider=lambda: ("tok", "https://example.my.salesforce.com"),
    )
    fake = FakeClient([message], sub._stop)
    sub.client_factory = lambda: fake

    sub._listen()

    assert fake.handshaken and fake.disconnected
    assert fake.subscribed == STREAMING_CHANNEL
    assert robot.telemetry().is_malfunctioning is False, "RESUME via the stream should clear the hazard"
