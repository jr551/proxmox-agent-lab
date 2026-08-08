#!/usr/bin/env sh
# Make `proxmox-lab` available without installing anything permanently.
#
# Prints the path to a working executable on stdout; everything else goes to
# stderr, so it is safe to capture:
#
#     PXL=$(sh bootstrap.sh) && "$PXL" status
#     PXL=$(curl -fsSL <raw-url>/bootstrap.sh | sh) && "$PXL" doctor
#
# For an agent that has only SKILL.md and no checkout, no package and no
# working directory. If the tool is already installed this is a no-op that
# just prints where it is. Otherwise it builds a throwaway virtualenv under
# the temp directory and installs into that.
#
# The venv path is stable, so repeated runs reuse it rather than reinstalling.
# Nothing is written outside the temp directory; your config and lease state
# still live in their normal places, which is what lets the watchdog find
# leases created here.

set -eu

log() { printf '%s\n' "$*" >&2; }

# 1. Already available? Use it.
if command -v proxmox-lab >/dev/null 2>&1; then
    log "proxmox-lab: using the installed copy"
    command -v proxmox-lab
    exit 0
fi

# 2. Running inside a checkout? Use it in place, no install needed.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || echo "")
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/src/proxmox_agent_lab/cli.py" ]; then
    log "proxmox-lab: running from the checkout at $SCRIPT_DIR"
    printf '%s\n' "$SCRIPT_DIR/scripts/proxmox-lab"
    exit 0
fi

# 3. Otherwise build a cached throwaway environment.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" - <<'PY' 2>/dev/null
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then PYTHON=$(command -v "$candidate"); break; fi
done
[ -n "$PYTHON" ] || { log "error: Python 3.11+ is required"; exit 1; }

VENV="${TMPDIR:-/tmp}/proxmox-agent-lab-env"
BIN="$VENV/bin/proxmox-lab"

if [ -x "$BIN" ]; then
    log "proxmox-lab: reusing $VENV"
    printf '%s\n' "$BIN"
    exit 0
fi

SOURCE="${PROXMOX_AGENT_LAB_SOURCE:-proxmox-agent-lab}"
log "proxmox-lab: not installed; building a temporary environment in $VENV"
"$PYTHON" -m venv "$VENV" >&2
"$VENV/bin/python" -m pip install --quiet --upgrade pip >&2 2>/dev/null || true
if ! "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check \
        "$SOURCE" >&2; then
    log "error: could not install '$SOURCE'."
    log "       Set PROXMOX_AGENT_LAB_SOURCE to a path or git URL, e.g."
    log "       PROXMOX_AGENT_LAB_SOURCE=git+https://github.com/jr551/proxmox-agent-lab"
    exit 1
fi

[ -x "$BIN" ] || { log "error: install succeeded but $BIN is missing"; exit 1; }
log "proxmox-lab: ready (temporary install; delete $VENV to remove)"
printf '%s\n' "$BIN"
