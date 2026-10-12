#!/usr/bin/env bash
#
# dry-run-setup.sh — rehearse the Robot Rumble software stack on a bare Pi.
#
# No robot, camera or wiring needed. Creates the venv, installs deps, runs the
# offline test suite, and runs the FakeRobot control loop for a few seconds.
# Idempotent: safe to re-run. See docs/pi-dry-run.md.
#
# Usage:  bash scripts/dry-run-setup.sh
#
set -euo pipefail

VENV="${VENV:-$HOME/rvr}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  OK  %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  !!  %s\033[0m\n' "$*"; }

# --- 0. Board + architecture sanity -----------------------------------------
say "Checking board and architecture"

ARCH="$(uname -m)"
if [[ "$ARCH" == "aarch64" || "$ARCH" == "arm64" ]]; then
  ok "64-bit OS ($ARCH) — prebuilt wheels available, pip install will be quick."
elif [[ "$ARCH" == armv7l || "$ARCH" == armv6l ]]; then
  warn "32-bit OS ($ARCH). 'cryptography' has no wheel here and will compile from"
  warn "source (needs Rust; slow on a Pi). Strongly prefer the 64-bit Pi OS image."
  warn "Continuing anyway in 5s — Ctrl+C to stop and reflash."
  sleep 5
else
  warn "Unexpected arch '$ARCH' — not a Pi? Continuing, but this script targets Pi OS."
fi

MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo 'unknown')"
case "$MODEL" in
  *"Pi 4"*|*"Pi 5"*|*"Compute Module 4"*|*"Compute Module 5"*)
    ok "Board: $MODEL — this is a real deploy target. Keep this card for the venue." ;;
  *"Pi 3"*)
    warn "Board: $MODEL — fine for a SOFTWARE rehearsal, but ship on a Pi 4." ;;
  unknown)
    warn "Could not read board model (not on a Pi?). Carrying on." ;;
  *)
    warn "Board: $MODEL — not the documented Pi 4 target; carrying on." ;;
esac

# --- 1. Python ---------------------------------------------------------------
say "Checking Python"
if ! command -v python3 >/dev/null; then
  warn "python3 not found. Install it:  sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
  exit 1
fi
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
ok "python3 $PYVER"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)' || {
  warn "Python $PYVER is older than 3.11 — the dependency pins expect 3.11+."
  warn "Use Raspberry Pi OS Bookworm (3.11) or Trixie (3.13)."
  exit 1
}

# venv module present? (python3-venv is a separate apt package on Pi OS)
if ! python3 -c 'import venv' 2>/dev/null; then
  warn "The 'venv' module is missing. Install it:  sudo apt install -y python3-venv"
  exit 1
fi

# --- 2. Virtual environment --------------------------------------------------
say "Setting up virtualenv at $VENV"
if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
  ok "Created $VENV"
else
  ok "Reusing existing $VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --quiet --upgrade pip
ok "pip upgraded"

# --- 3. Dependencies ---------------------------------------------------------
say "Installing dependencies (requirements.txt)"
pip install --quiet -r requirements.txt
ok "Core deps installed"

# Flask is optional (hazard webhook only) and not pinned in requirements.txt.
if [[ "${WITH_FLASK:-0}" == "1" ]]; then
  say "Installing Flask (hazard webhook — WITH_FLASK=1)"
  pip install --quiet flask
  ok "Flask installed"
fi

# --- 4. Offline test suite ---------------------------------------------------
say "Running offline test suite (no org needed)"
if python -m pytest -q; then
  ok "All offline tests passed"
else
  warn "Tests failed — see output above. Fix before trusting a live run."
  exit 1
fi

# --- 5. Offline control loop -------------------------------------------------
# No .env / no SF creds => offline mode: FakeRobot, nothing posted, no subscriber.
say "Running the control loop offline (FakeRobot, ~5 s, MAX_ITERATIONS=100)"
MAX_ITERATIONS="${MAX_ITERATIONS:-100}" ROBOT_HARDWARE=fake python control_loop.py
ok "Control loop ran against FakeRobot"

# --- Done --------------------------------------------------------------------
cat <<EOF

$(say "Dry run complete")
  The software stack runs on this board. Next, to exercise the live Salesforce
  pipes from the Pi:

    source $VENV/bin/activate
    cp .env.example .env && nano .env     # fill SF_* (+ S2S_*), DATACLOUD_MODE=s2s
    python s2s_smoke_test.py              # auth + delivery check
    MAX_ITERATIONS=0 python control_loop.py

  Full walkthrough: docs/pi-dry-run.md
EOF
