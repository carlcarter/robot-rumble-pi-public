# Dry run on a spare Pi (no robot attached)

Rehearse the full software stack on a bare Raspberry Pi — no RVR+, no camera, no
jumper wires — before the real Pi 4s and robots arrive. This proves the headless
workflow, `pip install` on ARM, the 20 Hz loop, the Salesforce pipes, and the
offline tests on the *target architecture*, not just your Mac.

**Best case: do this on a Pi 4B.** The 4B is the real deploy target (see
[pi-hardware-setup.md](pi-hardware-setup.md)), so a dry run on it isn't a
throwaway — the SD card you set up here becomes a venue Pi once the robot arrives.
Everything below the UART line is identical to the live build; you're just not
wiring a robot yet.

A spare **Pi 3B+** also works as a pure software rehearsal, but ship on the 4B:
the 3B+'s GPIO UART is the flaky mini-UART (fine with no robot attached, wrong
for a live deploy). The script detects the board and tells you which case you're in.

## What this proves (and what it can't)

| Proven on a bare Pi | Needs the real robot (skip here) |
| --- | --- |
| Flash → SSH → venv → `pip install` on ARM | UART wiring (hardware-setup steps 5, 9, 10) |
| `cryptography` / `pyjwt` wheels install on Pi OS | Sphero SDK firmware handshake |
| 20 Hz loop runs on constrained hardware | `SpheroRobot` (still a TODO in `robot.py`) |
| Offline `FakeRobot` loop + all 40 pytest tests | Driving / real IMU-based crash detection |
| Live pipes from the Pi's own network: telemetry → Data Cloud, Case on crash, Pipe 3 command subscriber, hazard webhook, `params.json` hot-reload | |

## Prerequisites

Two things decide whether `pip install` is painless:

1. **Use the 64-bit Pi OS Lite image.** `cryptography==50.0.2` (pulled in for S2S
   JWT auth) has a prebuilt `aarch64` wheel on 64-bit Pi OS — it just installs.
   On a 32-bit image there's no wheel: pip tries to compile from source, which
   means installing Rust and a long build on a 3B+. The setup script checks arch
   and warns you.
2. **Bookworm (Python 3.11) or Trixie (3.13).** Either is fine; the pins need 3.11+.

## Steps

Follow [pi-hardware-setup.md](pi-hardware-setup.md) **steps 1–4 and 7**, then stop
and use the script below instead of steps 5, 8, 9, 10. In short:

1. **Flash** (hardware-setup step 1) — Raspberry Pi OS Lite **64-bit**, set
   hostname/SSH/Wi-Fi in Imager's gear menu. Pick your actual board; the image is
   the same 64-bit Lite either way.
2. **First boot + SSH in** (step 2, points 1–4). **Skip the `raspi-config` serial
   setup** — there's no robot to talk to.
3. **Clone the repo** (step 7): `git clone <your repo URL>` then `cd` into it.
4. **Run the one-shot setup + smoke test:**
   ```bash
   bash scripts/dry-run-setup.sh
   ```
   It creates the venv, installs deps, runs the 40 offline tests, and runs the
   offline `FakeRobot` loop for ~5 s. Green all the way = the software stack is
   good on this board.

## Going further — exercise the live pipes (optional)

The offline run proves the loop. To prove the Salesforce path *from the Pi*:

```bash
source ~/rvr/bin/activate
cp .env.example .env
nano .env     # fill SF_* (+ S2S_* if testing real-time), set DATACLOUD_MODE=s2s
python s2s_smoke_test.py     # isolates auth vs delivery — do this first
MAX_ITERATIONS=0 python control_loop.py   # runs until Ctrl+C
```

From your Mac, watch telemetry land:

```bash
python watch_graph.py EDI 1
```

With valid `SF_*` creds the command subscriber (Pipe 3) starts automatically, so
a `resume edi` from Slack will clear the hazard on this Pi — the full fix loop,
no robot required.

### Hazard webhook (optional)

`hazard.py` is a separate process and needs Flask (not in `requirements.txt`):

```bash
pip install flask
python hazard.py        # serves POST /hazard on :5000
# from another shell / the Stream Deck / Slack workflow:
curl -X POST localhost:5000/hazard -H 'Content-Type: application/json' -d '{"hazard":"fog"}'
```

## When you're done

**On a Pi 4B:** keep this card. To turn it into a live venue Pi later, you only
need the steps you skipped — the `raspi-config` serial/UART setup (hardware-setup
step 2.5), wiring the robot (step 5), installing the Sphero SDK (step 4), and
writing `SpheroRobot` + flipping `ROBOT_HARDWARE=sphero`. Everything else is done.

**On a 3B+:** this was software rehearsal only. Flash a fresh Pi 4 for the venues
and follow the full [pi-hardware-setup.md](pi-hardware-setup.md); don't carry the
3B+ card over.
