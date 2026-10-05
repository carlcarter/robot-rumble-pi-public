# Robot Rumble — Python Control Code

The robot's brain: telemetry loop, Service Cloud integration, Data Cloud ingestion, and Slack Code repair.

## Local development on Mac

### Prerequisites

- Python 3.11+ (tested on 3.11, 3.12, 3.13)
- `uv` (optional but recommended: `pip install uv`)

### Setup

```bash
# Create a venv
uv venv
source .venv/bin/activate

# Install dependencies
uv pip install -r requirements.txt

# Copy and edit the config
cp .env.example .env
# Edit .env with your Salesforce credentials
```

### Run locally

```bash
python control_loop.py
```

This runs the control loop with **FakeRobot** (no hardware needed) for 100 iterations (~5 seconds). You'll see:
- Simulated telemetry being logged
- Simulated crashes and Service Cloud Cases being created (if Salesforce creds are set)
- Params.json hot-reload working

### What each module does

- **robot.py** — Robot interface + FakeRobot simulation
- **control_loop.py** — Main loop: telemetry → crash detection → Salesforce integration
- **sf_auth.py** — OAuth 2.0 client credentials flow (Salesforce + Data Cloud token swap)
- **sf_service.py** — Service Cloud Case operations
- **datacloud.py** — Data Cloud Ingestion API batching and posting
- **params.json** — The robot's editable "bugs" (this is what Slack Code modifies)

### Testing the Slack Code loop

**Goal**: edit `params.json`, restart the loop, see the robot's behaviour change. (In production, the Pi reloads it without restarting.)

```bash
# In one terminal
python control_loop.py

# In another, edit params.json
sed -i.bak 's/"motor_invert": false/"motor_invert": true/' params.json

# The loop detects the file change and calls robot.set_params()
# You'll see in the logs: "Reloading params.json"
```

### Offline mode

If `SF_CLIENT_ID` and `SF_CLIENT_SECRET` are not set, the loop runs in **offline mode** — telemetry is simulated and logged, but Salesforce API calls are skipped. Perfect for Mac testing without needing credentials every time.

### Next: deploy to Pi

1. Push this repo to GitHub
2. On the Pi, `git clone` it (outside `/root` or other Google Drive folders)
3. Set up the venv and install dependencies (same steps as above)
4. Edit `.env` with the Pi's credentials and Salesforce account
5. Change `robot.py` to import `SpheroRobot` instead of `FakeRobot` once the RVR+ arrives
6. Run `control_loop.py` with systemd or supervisor for persistence

## Architecture notes

- **No Sphero SDK dependency on Mac.** When the RVR+ arrives and `SpheroRobot` is implemented in `robot.py`, the Sphero SDK is installed only on the Pi (it needs `/dev/ttyS0` and asyncio event loop setup that's Pi-specific).
- **Params as editable JSON.** The "bugs" live in `params.json` in a GitHub repo. Slack Code edits it; the Pi pulls and hot-reloads. This is safer and more testable than code branches.
- **Debounced crashes.** One physical collision raises exactly one Case. The debounce window is 5 seconds.
- **Data Cloud eventual consistency.** Standard ingestion is ~3.5 min latency. For a live dashboard, confirm with your Salesforce admin that the org supports real-time data graphs (~500ms).
