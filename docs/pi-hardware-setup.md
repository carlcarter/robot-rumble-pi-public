# Hardware Setup: Sphero RVR+ and Raspberry Pi 4

This guide walks you through the first-time setup of one **Sphero RVR+ robot** with a **Raspberry Pi 4 (4GB)** running the Robot Rumble control loop. Follow this for each robot you bring online (EDI in Edinburgh, LON in London).

**Timeline:** ~2–3 hours for a fresh Pi, most of that is OS flashing and first boot. Wiring is 15 minutes. SDK install is ~10 minutes. First drive test is ~5 minutes.

**What you'll need:**
- Sphero RVR+ (with one charged battery)
- Raspberry Pi 4 (4GB recommended, not Pi 5 — see "Why Pi 4" below)
- 32GB microSD card
- Raspberry Pi 4 USB-C power supply (5V/3A minimum)
- Anker 20,000mAh USB-C power bank (for the Pi to run headless on the robot)
- Three female-to-female jumper wires (ideally 10–15cm length)
- Raspberry Pi Imager (install on Mac first: https://www.raspberrypi.com/software/)
- SSH access from your Mac to the Pi (Wi-Fi or USB-C Ethernet)
- GitHub access and git CLI (you already have this)
- A workspace with internet access (for the first-time setup script to fetch the Sphero SDK)

---

## Why Pi 4, not Pi 5

The Sphero SDK was written for the Pi 4's UART serial layout. The Pi 5 changed its serial subsystem (RP1 chip; GPIO UART is now `/dev/ttyAMA0` instead of `/dev/serial0`), and the SDK needs edits/workarounds on a Pi 5. The Pi 5 also throttles USB on a 5V/3A power bank (it wants 5V/5A), which drains the battery faster — a problem on a robot. Since speed is unused in this demo, the Pi 4 "just works" and is the safer bet. **Stick with Pi 4.**

---

## Step 1: Flash the Raspberry Pi OS

**On your Mac:**

1. Insert the microSD card into an adapter and plug it into your Mac.
2. Open **Raspberry Pi Imager** (download from https://www.raspberrypi.com/software/ if not installed).
3. Click **"Choose Device"** → **"Raspberry Pi 4"**.
4. Click **"Choose OS"** → scroll down to **"Raspberry Pi OS (other)"** → select **"Raspberry Pi OS Lite (64-bit)"** (the headless image, no desktop).
   - Current versions as of Oct 2026 are **Trixie (Debian 13)** and **Bookworm (Debian 12, legacy)**. Either works; Bookworm is more stable for a first deployment.
5. Click **"Choose Storage"** → select your microSD card.
6. Click the **gear icon** (settings) and configure:
   - **Hostname:** `rvr-edi` (for Edinburgh) or `rvr-lon` (for London) — whatever `ROBOT_ID` you'll use in `.env`.
   - **Enable SSH:** checkmark it; choose "Use password authentication" for simplicity, or paste your Mac's public key if you want key-based auth.
   - **Set username and password:** use `pi` and a strong password (you'll need it for `sudo` later).
   - **Configure wireless LAN:** Enter your Wi-Fi SSID and password (important: must be 2.4GHz or dual-band; some 5GHz-only networks won't work on the Pi 4). Or leave it blank and connect via USB-Ethernet later if Wi-Fi is problematic.
7. Click **"Write"** and wait for the image to be written (~5–10 minutes).
8. When done, eject the microSD card and remove it from your Mac.

---

## Step 2: First Boot and Serial Setup

**On the Pi (SSH from your Mac):**

1. Insert the microSD card into the Pi 4 and plug in the USB-C power supply (5V/3A).
2. Wait ~60 seconds for the first boot. The Pi will run `resize-fs` to expand the filesystem (watch for the activity LED).
3. From your Mac, SSH to the Pi:
   ```bash
   ssh pi@rvr-edi.local
   # (or rvr-lon.local, or the Pi's IP address if .local doesn't resolve)
   # Enter the password you set in step 1, step 6.
   ```

4. Update the OS and install git:
   ```bash
   sudo apt update
   sudo apt install -y git
   sudo apt upgrade -y
   # The upgrade takes ~5–10 minutes.
   ```

5. **Enable the serial port and disable the serial console** (required for UART communication with the RVR):
   ```bash
   sudo raspi-config
   ```
   - Navigate to **"Interface Options"** → **"Serial Port"**.
   - When asked **"Would you like a login shell to be accessible over serial?"**, select **"No"**.
   - When asked **"Would you like the serial port hardware to be enabled?"**, select **"Yes"**.
   - Exit raspi-config; it will ask to reboot. Select **"Yes"** and wait for reboot (~30 seconds).

6. After reboot, SSH back in:
   ```bash
   ssh pi@rvr-edi.local
   ```

7. Verify the serial port is ready:
   ```bash
   ls -la /dev/serial0
   # Should show: /dev/serial0 -> ttyS0
   ```

---

## Step 3: Install Python, venv, and Dependencies

On the Pi (via SSH):

```bash
# Python 3 is already installed; verify it's 3.11 or later
python3 --version

# Create a venv
python3 -m venv ~/rvr
source ~/rvr/bin/activate

# Install core packages (the control loop + Sphero SDK dependencies)
pip install --upgrade pip
pip install requests python-dotenv flask pyserial pytest

# The Sphero SDK is not on PyPI; we'll clone it next.
```

---

## Step 4: Clone the Sphero SDK

On the Pi (via SSH, in the venv):

```bash
cd ~
git clone https://github.com/sphero-inc/sphero-sdk-raspberrypi-python.git
cd ~/sphero-sdk-raspberrypi-python

# Install the SDK's dependencies (run first-time-setup.sh does this)
./first-time-setup.sh
# This installs aiohttp, websocket-client, pyserial-asyncio, and twine.
# It also runs ./tools/pi-uart-check.sh, which asks if you've already configured serial.
# You have (step 2), so just answer "Yes" and it will exit.
```

Test that the Sphero SDK imports correctly:

```bash
python3 -c "from sphero_sdk import SpheroRvrObserver; print('SDK OK')"
```

---

## Step 5: Wire the RVR to the Pi

**Physical connection: three female-to-female jumper wires from the RVR's 4-pin UART port to the Pi's GPIO header.**

The RVR's 4-pin port carries:
- **Pin 1 (5V)** — red wire → Pi GPIO pin **2** (5V power) or **4** (5V power)
- **Pin 2 (TX)** — yellow wire → Pi GPIO pin **10** (RX, UART0)
- **Pin 3 (RX)** — orange wire → Pi GPIO pin **8** (TX, UART0) — **note: TX and RX are crossed**
- **Pin 4 (GND)** — black wire → Pi GPIO pin **6** (GND) or any other GND pin

**Visual reference:**
- [Pi GPIO header pinout](https://pinout.xyz) — search for "UART" to find the TX/RX labels.
- RVR UART port diagram should be in the RVR+ manual or on Sphero's SDK site.

**Important:**
- The TX and RX wires **must be crossed** (RVR TX → Pi RX, RVR RX → Pi TX).
- Power from the RVR's 4-pin port is only 5V, which can power low-power devices (GPIO monitoring, etc.), **but not the Pi itself** — the Pi draws too much current. Power the Pi from its own USB-C bank (step 6).

---

## Step 6: Power the Pi

The Pi **cannot be reliably powered from the RVR's battery** — it draws 3–4W continuously, exceeding the RVR's power port capacity. Instead:

1. Connect the **Anker 20,000mAh USB-C power bank** to the Pi's USB-C power input.
2. Power the RVR separately with its own 36Wh battery (insert into the RVR's battery slot on the underside).
3. When you're testing indoors on the Mac, use the Pi's own USB-C power supply (5V/3A). When deployed on the robot, swap it for the Anker bank.

---

## Step 7: Clone the Robot Rumble Code

On the Pi (via SSH, in the venv):

```bash
cd ~
git clone https://github.com/<your-org>/robot-rumble-code.git
# (or whatever your GitHub repo URL is — ask the team if you don't know it)

cd robot-rumble-code

# Copy the environment template and edit it
cp .env.example .env
nano .env  # or use your favourite editor

# Edit these fields (others can stay as placeholders for now):
# ROBOT_ID=EDI              (or LON for London)
# ROBOT_NAME=Edinburgh      (or London)
# SF_MY_DOMAIN=...          (ask the Salesforce admin)
# SF_CLIENT_ID=...          (from the External Client App)
# SF_CLIENT_SECRET=...      (from the External Client App)
# DATACLOUD_MODE=batch      (or s2s if real-time ingestion is set up)

# Save and exit (Ctrl+X if using nano).
```

---

## Step 8: Test with FakeRobot

Before wiring the real RVR, test that the control loop runs against the simulated robot:

On the Pi (via SSH, in the venv):

```bash
cd ~/robot-rumble-code
python control_loop.py
# It should log telemetry for ~5 seconds (100 iterations at 20 Hz).
# You'll see FakeRobot telemetry printed.
# If Salesforce is not configured, it will log "offline mode" — that's OK.
```

Ctrl+C to stop.

---

## Step 9: First Drive with the Real RVR

Now connect the real RVR:

1. **Power on the RVR** with its power button on the back (you'll see the LEDs light up).
2. **SSH to the Pi** if you've disconnected.
3. **Activate the venv:**
   ```bash
   source ~/rvr/bin/activate
   ```

4. **Run a simple test to ensure the SDK can talk to the RVR:**
   ```bash
   cd ~/sphero-sdk-raspberrypi-python/getting_started/observer/leds
   python3 set_multiple_leds.py
   # The RVR's front LEDs should turn red and blue.
   # If nothing happens, the serial connection failed — check the wiring (step 5) and /dev/serial0.
   ```

5. **Test driving:**
   ```bash
   cd ~/sphero-sdk-raspberrypi-python/getting_started/observer/drive
   python3 drive_linear.py
   # The RVR should roll forward for ~2 seconds, then reverse.
   # (Make sure there's space on the table/floor!)
   ```

Ctrl+C to stop.

---

## Step 10: Run the Full Control Loop

On the Pi (via SSH, in the venv):

```bash
cd ~/robot-rumble-code

# Replace FakeRobot with SpheroRobot (one-line edit)
# (This assumes you've written SpheroRobot in robot.py — see below.)

ROBOT_ID=edi python control_loop.py
# The control loop should now read telemetry from the real RVR.
# Watch the log for any serial errors or telemetry readings.
```

Ctrl+C to stop.

---

## Implementing SpheroRobot

The control loop currently uses `FakeRobot`. To use the real RVR, create a `SpheroRobot` class in [robot.py](robot.py) that implements the same interface (`drive()`, `stop()`, `telemetry()`, `apply_hazard()`, `set_params()`):

**Template (`SpheroRobot` in `robot.py`):**

```python
class SpheroRobot(Robot):
    """Real Sphero RVR+ robot over UART."""
    
    def __init__(self, robot_id: str = "RVR"):
        from sphero_sdk import SpheroRvrObserver
        self.robot_id = robot_id
        self._rvr = SpheroRvrObserver()
        self._rvr.wake()
        self._collision = False
        self._params = {...}  # same as FakeRobot
    
    def drive(self, speed: float, heading: float) -> None:
        # Convert speed (0.0–1.0) to RVR speed (0–255)
        # Call self._rvr.drive_control.drive_forward_seconds or similar
        ...
    
    def telemetry(self) -> Telemetry:
        # Read from self._rvr.sensor_control (accelerometer, etc.)
        # Detect collision if deceleration spike
        ...
    
    # Implement stop, apply_hazard, set_params similarly
```

Then in [control_loop.py](control_loop.py), change line ~287 from:

```python
robot = FakeRobot(robot_id=robot_id)
```

to:

```python
robot = SpheroRobot(robot_id=robot_id)  # or auto-detect based on env var
```

---

## Step 11: Headless networking (do this at your desk, before the event)

The Pi has no screen at the venue, so it must auto-join a known network on boot, and you need a guaranteed way to reach it. Pi OS Bookworm/Trixie use **NetworkManager**. Pre-seed every network you might use, with priorities, over SSH before you travel.

### Pre-seed known networks (highest priority wins)

Add each network; higher `autoconnect-priority` is preferred when more than one is in range:

```bash
# Venue MiFi (intended primary — highest priority)
sudo nmcli connection add type wifi con-name mifi ssid "<MIFI_SSID>"
sudo nmcli connection modify mifi wifi-sec.key-mgmt wpa-psk wifi-sec.psk "<MIFI_PASSWORD>"
sudo nmcli connection modify mifi connection.autoconnect yes connection.autoconnect-priority 20

# Your phone hotspot (fallback)
sudo nmcli connection add type wifi con-name phone ssid "<PHONE_HOTSPOT_SSID>"
sudo nmcli connection modify phone wifi-sec.key-mgmt wpa-psk wifi-sec.psk "<PHONE_PASSWORD>"
sudo nmcli connection modify phone connection.autoconnect yes connection.autoconnect-priority 10

# Home/office wifi (so it reconnects at base) — priority 5, etc.
```

The Pi joins the highest-priority known network in range automatically on boot — no screen needed.

### Phone-hotspot gotchas (these bite on the day)

- **Fix the SSID + password in advance** and type them in now. On iPhone the hotspot name follows the device name (Settings → General → About → Name); you can't change it at the venue without re-seeding the Pi.
- **Force 2.4GHz:** iPhone → Personal Hotspot → turn **"Maximize Compatibility" ON**. The Pi 4's wifi is far more reliable on 2.4GHz (matches the 2.4GHz note in Step 1).
- **The hotspot must be broadcasting before the Pi looks for it.** iPhone hotspots can be lazy about advertising — keep the Personal Hotspot screen open while the Pi boots, or power-cycle the Pi once the hotspot is clearly up.

### Reaching the Pi from your phone

- **Over the shared network:** when the Pi and your phone are on the same MiFi/hotspot, SSH from a phone SSH app (Termius / Blink) to `rvr-edi.local` (or `rvr-lon.local`), or the IP the hotspot's connected-devices list shows. When the Pi is *on your hotspot*, your phone is the router, so the Pi appears in that list.
- **"Am I online?" with no screen:** the planned RGB status-LED feature flashes green once the Pi has network + Salesforce auth — a headless health check (see the project TODO). Until that lands, SSH in to confirm.

### Last-resort: Pi as its own hotspot (fix things when no known network is around)

Configure an **access-point fallback** so that if the Pi can't join any saved network, it raises *its own* wifi AP; you connect your phone directly to the Pi and SSH in to fix it. Options:

- A tool like **`comitup`** or **`AutoHotspot`** (switches to AP mode when no known SSID is found), or
- A manually-defined low-priority NetworkManager AP connection as a last resort.

Document your fallback AP's SSID/password and keep it with the kit. This is the "worst case, 400 miles away, nothing else works" safety net.

### Auto-start on boot

So a power-cycle fully recovers the robot unattended, run the control loop (and camera, if used) as a **systemd service** rather than a manual `python control_loop.py`. Create a unit that `ExecStart`s the venv Python against `control_loop.py` with `Restart=always`, enable it (`sudo systemctl enable --now robot-rumble`), and confirm a reboot brings everything back with no keyboard.

---

## Step 12: Pi Camera 3 (optional — robot's-eye view on the dashboard)

A Pi Camera 3 on the **front of the RVR+** can stream a still photo every few seconds to the Salesforce dashboard. Entirely optional and off by default.

### Fit the camera

- Connect the Pi Camera 3 to the Pi 4's **CSI camera port** (the ribbon connector between the HDMI ports and the 3.5mm jack — *not* the similar-looking display port). Blue side of the ribbon faces the Ethernet/USB ports.
- Mount it on the front of the RVR+ so the ribbon reaches without fouling the drive or the UART lead. Route and secure the ribbon.

### Install the camera stack (apt, not pip)

```bash
sudo apt update && sudo apt install -y python3-picamera2
# picamera2 + libcamera are provided as system packages on Pi OS Bookworm.
# Quick check:
libcamera-hello --timeout 2000   # should briefly show/confirm the camera
```

(`picamera2` is deliberately **not** in `requirements.txt` — it's a Pi-only system package, same convention as the Sphero SDK and pyserial.)

### Configure + run

Set the camera env vars in `.env` (see `.env.example` `CAMERA_*`):

```
CAMERA_ENABLED=true
CAMERA_UPLOAD_URL=https://<your-camera-host>.herokuapp.com
CAMERA_UPLOAD_TOKEN=<must match the host's token>
CAMERA_INTERVAL_S=3
CAMERA_RESOLUTION=640x480
```

The control loop starts the camera uploader automatically when `CAMERA_ENABLED=true` and the URL + token are set. The image host is the separate **`robot-rumble-camera-host`** repo (deployed to Heroku); the dashboard's **Camera host base URL** component setting must point at the same app.

**Remote kill-switch:** if the camera's uploads eat too much venue bandwidth, type `camera off edi` (or `lon`) in the robot's Slack channel to stop uploads without touching the Pi; `camera on edi` resumes. (See the Slack command cheat sheet in the `robot-rumble-sf` repo.)

**Test with no camera:** on a Mac/dev box set `CAMERA_FAKE=true` (plus URL + token) to upload a static test frame and exercise the host → dashboard path without hardware.

---

## Common Gotchas

**Serial connection fails ("timeout", no response from RVR):**
- Check the wiring (step 5): TX/RX **must be crossed**; verify all three wires are connected.
- Verify `/dev/serial0` exists: `ls -la /dev/serial0`.
- Verify the serial console is **disabled**: run `sudo raspi-config` again and double-check Interfaces > Serial Port.
- Restart the Pi: `sudo reboot`.
- Try `python3 -c "import serial; s = serial.Serial('/dev/serial0', 115200); print(s)"` — should open without error.

**SDK firmware check hangs on first import:**
- The Sphero SDK's `SpheroRvrObserver.__init__()` calls `_check_rvr_fw()`, which tries to reach Sphero's content management system over the internet to check for firmware updates. If the network is slow or unavailable, it will timeout.
- On Mac (testing locally), you can mock the firmware check to skip it (or just accept a 5–10 second delay on import).
- On Pi with a spotty network, the first import may take 30 seconds. This is expected; subsequent imports are faster.
- If it hangs forever, the Pi may not have internet. Restart and check Wi-Fi.

**The RVR doesn't wake up when commanded:**
- Call `rvr.wake()` and then wait 2 seconds before sending other commands: `time.sleep(2)`.
- If the RVR's battery is dead, it won't respond (press the power button; the LEDs should light up).
- If the RVR is in "soft sleep" (purple LEDs pulsing), wake it: `rvr.wake()`.

**LEDs don't light up on `set_multiple_leds.py`:**
- Check the wiring (step 5) — serial connection is not established.
- Check that the RVR is powered on (press the power button).
- Verify `/dev/serial0` is the correct device.

---

## Next Steps After First Setup

1. **Commit the working config** to GitHub:
   ```bash
   cd ~/robot-rumble-code
   git add .env  # (only if you're comfortable; secrets will be in plaintext)
   # OR store .env outside the repo and copy it on deployment.
   git status
   ```

2. **Test the Slack Code loop** — wire the Pi's hazard Flask endpoint to Slack, test editing `params.json` via Slack Code, and confirm the robot responds.

3. **Set up the second robot** (LON) — repeat this guide with `ROBOT_ID=LON` and hostname `rvr-lon`.

4. **Deploy to the venues** — the Pi runs 24/7 on the Anker bank; rotate batteries when drained.

---

## Troubleshooting Links

- **Raspberry Pi official docs:** https://www.raspberrypi.com/documentation/
- **Sphero SDK GitHub:** https://github.com/sphero-inc/sphero-sdk-raspberrypi-python
- **Sphero SDK docs:** https://sdk.sphero.com/
- **GPIO Pinout:** https://pinout.xyz/

---

**Questions?** Check the main [README.md](../README.md) or [s2s-setup.md](s2s-setup.md) if you're using real-time Data Cloud ingestion.
