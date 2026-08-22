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
REQUIRED_VERSION="0.10.0"
CHECK_STAMP="${XDG_STATE_HOME:-$HOME/.local/state}/proxmox-agent-lab/github-update-check.json"
LATEST_TAG=""
CHECK_DUE=1
if [ -f "$CHECK_STAMP" ] && "$PYTHON" - "$CHECK_STAMP" <<'PY' 2>/dev/null
import json, sys, time
try: checked = json.load(open(sys.argv[1], encoding="utf-8"))["checked_at"]
except (OSError, KeyError, TypeError, ValueError): raise SystemExit(1)
raise SystemExit(0 if time.time() - checked < 86400 else 1)
PY
then CHECK_DUE=0; fi

if [ "$CHECK_DUE" -eq 1 ]; then
    # One GitHub request at most per 24 hours. Failure is deliberately cached:
    # an offline update service must never block access to the lab.
    mkdir -p "$VENV" "$(dirname "$CHECK_STAMP")"
    if command -v curl >/dev/null 2>&1; then
        LATEST_TAG=$(curl -fsSL --max-time 4 \
            -H 'Accept: application/vnd.github+json' \
            https://api.github.com/repos/jr551/proxmox-agent-lab/releases/latest \
            2>/dev/null | "$PYTHON" -c \
            'import json,sys; print(json.load(sys.stdin).get("tag_name", ""))' \
            2>/dev/null || true)
    fi
    CURRENT_FOR_CACHE=""
    if [ -x "$BIN" ]; then
        CURRENT_FOR_CACHE=$($VENV/bin/python -c \
            'import proxmox_agent_lab; print(proxmox_agent_lab.__version__)' \
            2>/dev/null || true)
    fi
    "$PYTHON" - "$CHECK_STAMP" "$CURRENT_FOR_CACHE" "$LATEST_TAG" <<'PY'
import json, os, sys, tempfile, time
path, current, tag = sys.argv[1:]
latest = tag.removeprefix("v") or None
def version(value):
    try: return tuple(int(x) for x in value.split("."))
    except (AttributeError, ValueError): return ()
value = {"checked_at": time.time(), "current": current or None,
         "latest": latest, "update_available": version(latest) > version(current),
         "cached": False}
if latest is None: value["error"] = "github update check unavailable"
temporary = path + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(value, handle, sort_keys=True); handle.write("\n")
os.replace(temporary, path)
PY
fi

if [ -x "$BIN" ]; then
    CURRENT=$($VENV/bin/python -c \
        'import proxmox_agent_lab; print("v" + proxmox_agent_lab.__version__)' \
        2>/dev/null || true)
    UPDATE_TARGET="$LATEST_TAG"
    UPDATE_SOURCE=""
    if "$PYTHON" - "$CURRENT" "v$REQUIRED_VERSION" <<'PY'
import sys
def version(value):
    try: return tuple(int(x) for x in value.lstrip("v").split("."))
    except ValueError: return ()
raise SystemExit(0 if version(sys.argv[2]) > version(sys.argv[1]) else 1)
PY
    then
        # This script came from main and can be newer than the once-daily
        # release check. Do not let that cache strand an older executable.
        UPDATE_TARGET="v$REQUIRED_VERSION"
        UPDATE_SOURCE="https://github.com/jr551/proxmox-agent-lab/archive/refs/heads/main.tar.gz"
    elif [ -n "$LATEST_TAG" ]; then
        UPDATE_SOURCE="https://github.com/jr551/proxmox-agent-lab/archive/refs/tags/$LATEST_TAG.tar.gz"
    fi
    if [ -n "$UPDATE_TARGET" ] && "$PYTHON" - "$CURRENT" "$UPDATE_TARGET" <<'PY'
import sys
def version(value):
    try: return tuple(int(x) for x in value.lstrip("v").split("."))
    except ValueError: return ()
raise SystemExit(0 if version(sys.argv[2]) > version(sys.argv[1]) else 1)
PY
    then
        log "proxmox-lab: updating cached environment $CURRENT -> $UPDATE_TARGET"
        "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check \
            --upgrade "$UPDATE_SOURCE" >&2
    else
        log "proxmox-lab: reusing $VENV"
    fi
    printf '%s\n' "$BIN"
    exit 0
fi

SOURCE="${PROXMOX_AGENT_LAB_SOURCE:-proxmox-agent-lab}"
if [ -z "${PROXMOX_AGENT_LAB_SOURCE:-}" ] && [ -n "$LATEST_TAG" ]; then
    SOURCE="https://github.com/jr551/proxmox-agent-lab/archive/refs/tags/$LATEST_TAG.tar.gz"
fi
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
