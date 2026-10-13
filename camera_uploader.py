"""
Camera capture + upload (Pi Camera 3 on the front of the RVR+).

Captures a JPEG at a fixed interval and POSTs it to the Robot Rumble camera host
(a small Heroku app), which serves the latest frame to the Salesforce dashboard.
Runs on its own daemon thread so it never blocks the 20 Hz control loop — same
shape as CommandSubscriber and the S2S ingestor.

Design notes:
- Capture uses picamera2 (apt-provided on Pi 4 / Bookworm). The import is guarded
  so this module loads on a Mac/CI with no camera; without picamera2 it no-ops
  (unless CAMERA_FAKE is on, which uploads a static test frame so the upload +
  dashboard path can be exercised without hardware).
- Upload is a plain requests.post of JPEG bytes with a Bearer token — NOT the
  Salesforce auth helper (this talks to Heroku, not Salesforce).
- Everything is best-effort: a capture or upload failure is logged, never raised.
- Enable flag is live-toggleable (set_enabled) so the Slack CAMERA_OFF / CAMERA_ON
  commands can stop/start uploads remotely if bandwidth is tight — the thread stays
  alive, it just skips the work while disabled.
"""

import io
import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

# A tiny valid JPEG used when CAMERA_FAKE is on (no hardware). Grey 2x2.
_FAKE_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
    "07090908"
    + "0a" * 100
    + "ffd9"
)


class CameraUploader:
    """Fixed-interval camera capture + upload to the camera host, on a daemon thread."""

    def __init__(
        self,
        robot_id: str,
        upload_url: str,
        upload_token: str,
        interval_s: float = 3.0,
        resolution: tuple = (640, 480),
        enabled: bool = True,
        fake: bool = False,
    ):
        """
        Args:
            robot_id: EDI / LON — the frame is stored per robot on the host.
            upload_url: camera host base, e.g. https://<app>.herokuapp.com
            upload_token: shared secret presented as "Authorization: Bearer <token>".
            interval_s: seconds between captures.
            resolution: (width, height) for capture.
            enabled: initial enabled state (reflects CAMERA_ENABLED). Live-toggleable.
            fake: if True, upload a static test frame instead of using the camera
                (lets the upload + dashboard path be tested with no Pi camera).
        """
        self.robot_id = robot_id.strip().upper()
        self.upload_url = upload_url.rstrip("/")
        self.upload_token = upload_token
        self.interval_s = max(0.5, float(interval_s))
        self.resolution = resolution
        self.fake = fake

        self._enabled = threading.Event()
        if enabled:
            self._enabled.set()

        self._stop = threading.Event()
        self._thread = None
        self._camera = None  # picamera2 instance, created lazily on the thread

    # --- live kill-switch (Slack CAMERA_OFF / CAMERA_ON) --------------------

    def set_enabled(self, value: bool) -> None:
        """Enable/disable uploading at runtime without stopping the thread."""
        if value:
            self._enabled.set()
            logger.info("Camera uploads ENABLED")
        else:
            self._enabled.clear()
            logger.info("Camera uploads DISABLED (kill-switch)")

    @property
    def enabled(self) -> bool:
        return self._enabled.is_set()

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera-uploader", daemon=True)
        self._thread.start()
        logger.info(
            f"Camera uploader started for {self.robot_id} "
            f"(interval={self.interval_s}s, fake={self.fake}, enabled={self.enabled})"
        )

    def stop(self) -> None:
        self._stop.set()
        self._close_camera()

    # --- worker -------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            # Wait out the interval first so a disabled uploader costs nothing.
            self._stop.wait(self.interval_s)
            if self._stop.is_set():
                break
            if not self._enabled.is_set():
                continue  # kill-switch: skip capture + upload, stay alive
            try:
                jpeg = self._capture()
                if jpeg:
                    self._upload(jpeg)
            except Exception as e:
                # Never let the thread die on a camera/network blip.
                logger.error(f"Camera capture/upload failed: {e}")
        self._close_camera()

    def _capture(self) -> bytes:
        """Return JPEG bytes, or b'' if capture isn't possible."""
        if self.fake:
            return _FAKE_JPEG
        cam = self._ensure_camera()
        if cam is None:
            return b""
        buffer = io.BytesIO()
        # picamera2 can capture a JPEG straight to a file-like object.
        cam.capture_file(buffer, format="jpeg")
        return buffer.getvalue()

    def _upload(self, jpeg: bytes) -> None:
        url = f"{self.upload_url}/upload/{self.robot_id}"
        headers = {
            "Authorization": f"Bearer {self.upload_token}",
            "Content-Type": "image/jpeg",
        }
        resp = requests.post(url, data=jpeg, headers=headers, timeout=10)
        if resp.status_code >= 300:
            logger.warning(f"Camera upload HTTP {resp.status_code}: {resp.text[:200]}")

    # --- camera management (lazy; stays off the import path) ----------------

    def _ensure_camera(self):
        if self._camera is not None:
            return self._camera
        try:
            from picamera2 import Picamera2  # apt-provided on the Pi; absent on Mac/CI
        except Exception as e:
            logger.warning(f"picamera2 unavailable ({e}); camera disabled. Use CAMERA_FAKE for testing.")
            return None
        try:
            cam = Picamera2()
            config = cam.create_still_configuration(main={"size": self.resolution})
            cam.configure(config)
            cam.start()
            time.sleep(1.0)  # let auto-exposure settle
            self._camera = cam
            logger.info(f"Pi camera started at {self.resolution}")
            return cam
        except Exception as e:
            logger.error(f"Failed to start Pi camera: {e}")
            return None

    def _close_camera(self) -> None:
        if self._camera is not None:
            try:
                self._camera.stop()
                self._camera.close()
            except Exception:
                pass
            self._camera = None
