# Robot Rumble — Python control code

The robot's brain for the **Robot Rumble** demo: a pair of Sphero RVR+ robots (**EDI** in Edinburgh, **LON** in London) racing through heats while telemetry and crashes flow into Salesforce.

This repo holds the Python that runs on each Raspberry Pi:

- a 20 Hz control loop driving one RVR+
- real-time telemetry into **Data Cloud** (Server-to-Server events, sub-second)
- Service Cloud **Case** creation on every crash (feeds the Slack alert flow in the SFDX repo)
- a hot-reloadable `params.json` so the robot's behaviour can be tweaked live

The Salesforce side (LWC dashboard, Flows, metadata) lives in the sibling SFDX project at `../robot-rumble-sf`.

## Two pipes, one demo

| Pipe | What flows | Where it lands | Why it matters |
| --- | --- | --- | --- |
| **1 — Telemetry** | 20 Hz samples (speed, heading, battery, hazard, heat) | Data Cloud `RobotLive_DG` → Lightning dashboard | Live scoreboard the audience watches |
| **2 — Crashes** | One Case per collision, debounced to 5 s | Service Cloud Case → Flow → Slack `#rvr-edi` / `#rvr-lon` | Operator alert; drives the "fix the robot" moment |

Pipe 1 uses the Server-to-Server (S2S) events endpoint for sub-second latency. The older batch Ingestion API path is still in the code (`DATACLOUD_MODE=batch`) but adds roughly three minutes of lag and isn't used in the live demo.

## Repo layout

```
.
├── control_loop.py       # Main 20 Hz loop
├── robot.py              # Robot interface + FakeRobot (SpheroRobot TODO on Pi)
├── sf_auth.py            # OAuth client-credentials for Service Cloud + batch Data Cloud
├── sf_service.py         # Case creation
├── datacloud.py          # Batch Ingestion API (legacy)
├── s2s_auth.py           # JWT bearer auth for S2S real-time
├── s2s_events.py         # Non-blocking background sender for S2S events
├── s2s_smoke_test.py     # One-shot auth + send test (per-stage PASS/FAIL)
├── watch_graph.py        # Live view of Data Graph with per-event lag
├── hazard.py             # Flask webhook (Pi only) — receives hazards from Slack/Stream Deck
├── params.json           # The "bugs" — editable live, hot-reloaded by the loop
├── requirements.txt
├── schemas/              # Data Cloud ingestion schemas
├── docs/                 # Setup guides (Pi hardware, S2S)
└── tests/                # Offline unit tests (no org required)
```

## Local setup (Mac)

Prerequisites: Python 3.11+ and `uv` (optional).

```bash
# Create a venv
uv venv
source .venv/bin/activate

# Install dependencies
uv pip install -r requirements.txt
# or: pip install -r requirements.txt

# Copy and fill in the config
cp .env.example .env
# edit .env — see "Configuration" below
```

### Run the loop

```bash
python control_loop.py
```

Without any Salesforce credentials, the loop runs in **offline mode**: `FakeRobot` emits simulated telemetry, nothing is posted to Salesforce, no Cases are created. Great for iterating on Mac.

With `SF_*` set, it hits the real org. With `DATACLOUD_MODE=s2s` and `S2S_*` set, telemetry streams to Data Cloud in real time; you'll see it in `RobotLive_DG` within a second.

### Watch telemetry land

In a second terminal:

```bash
python watch_graph.py EDI 1      # robot EDI, poll every 1 s
```

Each new event prints with its lag from robot timestamp to graph arrival. Expect ~1 s with S2S; minutes with batch.

### S2S smoke test

Before pointing a Pi at the org, confirm auth + event delivery with:

```bash
python s2s_smoke_test.py
```

Each of the four stages prints PASS/FAIL so you can see whether it's the key, the user policy, the token exchange, or the Source ID that's wrong.

## Configuration

Every setting is in `.env` (see `.env.example`). The important ones:

| Variable | Purpose |
| --- | --- |
| `ROBOT_ID` | `EDI` or `LON` — identifies this robot everywhere |
| `SF_MY_DOMAIN` | your org's My Domain prefix (from `<prefix>.my.salesforce.com`) |
| `SF_CLIENT_ID` / `SF_CLIENT_SECRET` | Client-credentials External Client App (Case creation + batch Data Cloud) |
| `DATACLOUD_MODE` | `batch` (default), `s2s` (real-time), or `both` |
| `S2S_CLIENT_ID` / `S2S_USERNAME` / `S2S_PRIVATE_KEY_PATH` / `S2S_APP_SOURCE_ID` | S2S real-time path — see [docs/s2s-setup.md](docs/s2s-setup.md) |

**Secrets never go in the repo.** `.env` and `*.key` / `*.pem` / `*.crt` are gitignored. The S2S signing key lives at `~/.robot-rumble/s2s/private.key` (mode 700).

## Tests

```bash
python -m pytest
```

20 offline tests covering telemetry, params reload, auth token caching, JWT assertion, the S2S ingestor's batching, 401 refresh, 400 drop, retry on 5xx, buffer cap on long outages, and that `add_record()` never blocks the control loop.

No live org is needed. Everything that would make a network call is monkey-patched.

## Deploying to a Pi

See [docs/pi-hardware-setup.md](docs/pi-hardware-setup.md) for the full first-boot walkthrough: flashing the SD card, enabling UART, wiring the RVR+ to the Pi, installing the Sphero SDK, and running the control loop against the real robot. Allow 2–3 hours per Pi for a cold setup.

The `SpheroRobot` implementation is still a TODO in [robot.py](robot.py); the loop currently drives `FakeRobot` everywhere. On the Pi, swap the one `FakeRobot(...)` line in `control_loop.py` once the SDK is talking to the hardware.

## Related

- `../robot-rumble-sf` — SFDX project: Lightning App Page dashboard LWC, Case → Flow → Slack, permission set, custom fields
- Data Cloud Data Graph: `RobotLive_DG` (Real-Time)
- Slack channels: `#rvr-edi`, `#rvr-lon`

## Non-goals

- No historical analytics or replay — the dashboard reads the live graph only
- No multi-tenant or multi-user auth — one logged-in presenter drives the demo
- Pi-side `SpheroRobot` ships separately; this repo is deliberately hardware-agnostic until then
