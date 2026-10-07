"""
Offline tests for the Server-to-Server (S2S) path. No network or Salesforce org needed.

Run with: python -m pytest test_s2s.py -v
"""

import re
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import s2s_events
from robot import Telemetry
from s2s_auth import S2SAuth
from s2s_events import S2SIngestor, build_event


def make_telemetry(**overrides) -> Telemetry:
    fields = dict(
        robot_id="EDI",
        ts=1_790_000_000.123456,
        speed=0.5,
        heading=90.0,
        battery=88.5,
        collision=False,
        active_hazard=None,
        heat_number=2,
    )
    fields.update(overrides)
    return Telemetry(**fields)


# --- Event payload -----------------------------------------------------------


def test_build_event_shape():
    event = build_event(make_telemetry())

    # The three fields the "Data from Other Sources" connector requires.
    assert event["eventId"]
    assert event["eventType"] == "RobotTelemetryRT"
    assert event["dateTime"]

    assert event["category"] == "Other"
    assert event["robotId"] == "EDI"
    assert event["activeHazard"] == ""  # None is sent as empty text
    assert event["heatNumber"] == 2


def test_build_event_datetime_is_millisecond_iso8601():
    event = build_event(make_telemetry())
    # S2S accepts only yyyy-MM-dd'T'HH:mm:ss.SSS'Z'
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", event["dateTime"])


def test_build_event_collision_is_numeric():
    assert build_event(make_telemetry(collision=True))["collision"] == 1
    assert build_event(make_telemetry(collision=False))["collision"] == 0


def test_build_event_ids_are_unique():
    telemetry = make_telemetry()
    assert build_event(telemetry)["eventId"] != build_event(telemetry)["eventId"]


# --- JWT assertion -----------------------------------------------------------


@pytest.fixture
def keypair(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "private.key"
    path.write_bytes(pem)
    return path, key.public_key()


def test_jwt_assertion_claims_and_signature(keypair):
    path, public_key = keypair
    auth = S2SAuth("mydomain", "client123", "robot@example.com", str(path))

    claims = jwt.decode(
        auth._build_assertion(),
        public_key,
        algorithms=["RS256"],
        audience="https://mydomain.my.salesforce.com",
    )

    assert claims["iss"] == "client123"
    assert claims["sub"] == "robot@example.com"
    assert claims["exp"] - time.time() < 300  # short-lived


def test_salesforce_token_is_cached(keypair, monkeypatch):
    path, _ = keypair
    auth = S2SAuth("mydomain", "client123", "robot@example.com", str(path))
    auth._sf_token_cache = {"access_token": "cached", "expires_at": time.time() + 3600}

    def boom(*args, **kwargs):
        raise AssertionError("should not call Salesforce when the token is cached")

    monkeypatch.setattr("s2s_auth.requests.post", boom)
    assert auth.get_salesforce_token() == "cached"


# --- Ingestor ----------------------------------------------------------------


class FakeAuth:
    def __init__(self):
        self.refreshes = 0

    def get_datacloud_token(self, force_refresh=False):
        if force_refresh:
            self.refreshes += 1
        return "token", "tenant.example.com"


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    @property
    def ok(self):
        return self.status_code < 400


@pytest.fixture
def auth(monkeypatch):
    fake = FakeAuth()
    monkeypatch.setattr(s2s_events, "get_s2s_auth", lambda: fake)
    return fake


def scripted_post(monkeypatch, statuses):
    """Replace requests.post with one that returns the given status codes in order."""
    calls = []
    remaining = list(statuses)

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "events": json["events"], "headers": headers})
        return FakeResponse(remaining.pop(0) if remaining else 204)

    monkeypatch.setattr(s2s_events.requests, "post", fake_post)
    return calls


def test_ingestor_sends_to_source_endpoint(auth, monkeypatch):
    calls = scripted_post(monkeypatch, [204])
    ingestor = S2SIngestor("SRC123", flush_interval_s=0.01)

    ingestor.add_record(make_telemetry())
    ingestor.close()

    assert ingestor.sent_count == 1
    assert calls[0]["url"] == "https://tenant.example.com/server/events/SRC123"
    assert calls[0]["headers"]["Authorization"] == "Bearer token"
    assert calls[0]["events"][0]["robotId"] == "EDI"


def test_ingestor_splits_into_batches(auth, monkeypatch):
    calls = scripted_post(monkeypatch, [])
    ingestor = S2SIngestor("SRC123", flush_interval_s=0.01, max_batch=10)

    for _ in range(25):
        ingestor.add_record(make_telemetry())
    ingestor.close()

    assert ingestor.sent_count == 25
    assert all(len(call["events"]) <= 10 for call in calls)


def test_ingestor_refreshes_token_once_on_401(auth, monkeypatch):
    scripted_post(monkeypatch, [401, 204])
    ingestor = S2SIngestor("SRC123", flush_interval_s=0.01)

    ingestor.add_record(make_telemetry())
    ingestor.close()

    assert auth.refreshes == 1
    assert ingestor.sent_count == 1


def test_ingestor_retries_after_server_error(auth, monkeypatch):
    calls = scripted_post(monkeypatch, [500, 204])
    ingestor = S2SIngestor("SRC123", flush_interval_s=0.01)

    ingestor.add_record(make_telemetry())
    ingestor.close()

    assert len(calls) == 2  # same batch sent again
    assert ingestor.sent_count == 1
    assert ingestor.dropped_count == 0


def test_ingestor_drops_malformed_batch_on_400(auth, monkeypatch):
    calls = scripted_post(monkeypatch, [400])
    ingestor = S2SIngestor("SRC123", flush_interval_s=0.01)

    ingestor.add_record(make_telemetry())
    ingestor.close()

    assert len(calls) == 1  # not retried forever
    assert ingestor.sent_count == 0
    assert ingestor.dropped_count == 1


def test_ingestor_caps_buffer_during_outage(auth):
    ingestor = S2SIngestor("SRC123", max_buffer=5)
    ingestor._pending = [{"eventId": str(i)} for i in range(8)]

    ingestor._drain_queue()

    assert [e["eventId"] for e in ingestor._pending] == ["3", "4", "5", "6", "7"]
    assert ingestor.dropped_count == 3


def test_add_record_does_not_block_on_network(auth, monkeypatch):
    def slow_post(*args, **kwargs):
        time.sleep(0.5)
        return FakeResponse(204)

    monkeypatch.setattr(s2s_events.requests, "post", slow_post)
    ingestor = S2SIngestor("SRC123", flush_interval_s=0.01)

    start = time.monotonic()
    for _ in range(20):
        ingestor.add_record(make_telemetry())
    elapsed = time.monotonic() - start

    assert elapsed < 0.2, "add_record must not wait on the HTTP call (control loop runs at 20 Hz)"
    ingestor.close(timeout_s=10)
