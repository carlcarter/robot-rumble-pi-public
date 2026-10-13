"""Tests for the camera uploader — interval, enable/kill-switch, upload payload.

No hardware: capture is forced via fake=True, and requests.post is monkeypatched.
"""

import time

import camera_uploader
from camera_uploader import CameraUploader


class FakeResp:
    def __init__(self, status_code=201, text="ok"):
        self.status_code = status_code
        self.text = text


def test_enabled_flag_toggles():
    u = CameraUploader("EDI", "https://host", "tok", enabled=True, fake=True)
    assert u.enabled is True
    u.set_enabled(False)
    assert u.enabled is False
    u.set_enabled(True)
    assert u.enabled is True


def test_capture_fake_returns_jpeg_bytes():
    u = CameraUploader("EDI", "https://host", "tok", fake=True)
    data = u._capture()
    assert isinstance(data, bytes) and len(data) > 0
    assert data[:2] == b"\xff\xd8"  # JPEG SOI marker


def test_upload_posts_bytes_with_bearer(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return FakeResp(201)

    monkeypatch.setattr(camera_uploader.requests, "post", fake_post)

    u = CameraUploader("EDI", "https://host/", "secret", fake=True)
    u._upload(b"\xff\xd8jpeg\xff\xd9")

    assert captured["url"] == "https://host/upload/EDI"  # trailing slash stripped
    assert captured["data"] == b"\xff\xd8jpeg\xff\xd9"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["headers"]["Content-Type"] == "image/jpeg"


def test_robot_id_uppercased_in_url(monkeypatch):
    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        return FakeResp(201)

    monkeypatch.setattr(camera_uploader.requests, "post", fake_post)
    u = CameraUploader("edi", "https://host", "tok", fake=True)
    u._upload(b"x")
    assert captured["url"] == "https://host/upload/EDI"


def test_run_uploads_when_enabled(monkeypatch):
    posts = []
    monkeypatch.setattr(
        camera_uploader.requests, "post",
        lambda url, **kw: posts.append(url) or FakeResp(201),
    )
    u = CameraUploader("EDI", "https://host", "tok", interval_s=0.5, fake=True, enabled=True)
    u.start()
    time.sleep(1.3)  # ~2 intervals
    u.stop()
    assert len(posts) >= 1


def test_run_skips_upload_when_disabled(monkeypatch):
    posts = []
    monkeypatch.setattr(
        camera_uploader.requests, "post",
        lambda url, **kw: posts.append(url) or FakeResp(201),
    )
    u = CameraUploader("EDI", "https://host", "tok", interval_s=0.5, fake=True, enabled=False)
    u.start()
    time.sleep(1.3)
    u.stop()
    assert posts == []  # kill-switch: nothing uploaded while disabled


def test_kill_switch_stops_then_resumes_uploads(monkeypatch):
    posts = []
    monkeypatch.setattr(
        camera_uploader.requests, "post",
        lambda url, **kw: posts.append(1) or FakeResp(201),
    )
    u = CameraUploader("EDI", "https://host", "tok", interval_s=0.4, fake=True, enabled=True)
    u.start()
    time.sleep(1.0)
    first = len(posts)
    assert first >= 1
    u.set_enabled(False)
    time.sleep(1.0)
    paused = len(posts)
    assert paused == first  # no new uploads while off
    u.set_enabled(True)
    time.sleep(1.0)
    u.stop()
    assert len(posts) > paused  # resumed


def test_upload_failure_does_not_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(camera_uploader.requests, "post", boom)
    u = CameraUploader("EDI", "https://host", "tok", interval_s=0.3, fake=True, enabled=True)
    u.start()
    time.sleep(0.8)  # worker hits the error path; must not crash the thread
    assert u._thread.is_alive()
    u.stop()
