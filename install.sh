#!/usr/bin/env bash
# One-touch installer for proxmox-agent-lab.
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/install.sh | bash
#
# Installs the package, writes a config, stores your API token in the OS
# keyring, and runs the health check. Everything it asks for is optional --
# skip a prompt with Enter and edit the config later.
#
# Non-interactive:
#   PXL_HOST=192.168.1.50 PXL_NODE=pve PXL_TOKEN_USER=agent@pve \
#   PXL_TOKEN_NAME=lab PXL_TOKEN_SECRET=... PXL_MAC=aa:bb:.. ./install.sh --yes

set -euo pipefail

BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m'); YELLOW=$(printf '\033[33m')
RESET=$(printf '\033[0m')
[ -t 1 ] || { BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s\n' "$GREEN" "$RESET" "$BOLD" "$*"; printf '%s' "$RESET"; }
warn() { printf '%s!%s  %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '%sx%s  %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

ask() { # ask VAR "prompt" "default"
    local __var=$1 __prompt=$2 __default=${3:-} __reply=""
    local __env="PXL_${__var}"
    __reply="${!__env:-}"
    if [ -z "$__reply" ] && [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
        if [ -n "$__default" ]; then
            read -r -p "  $__prompt [$__default]: " __reply || true
        else
            read -r -p "  $__prompt: " __reply || true
        fi
    fi
    printf -v "$__var" '%s' "${__reply:-$__default}"
}

# ---------------------------------------------------------------- python ---
step "Checking Python"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        PYTHON=$(command -v "$candidate"); break
    fi
done
[ -n "$PYTHON" ] || die "Python 3.11 or newer is required.
  macOS:  brew install python@3.12
  Debian: sudo apt install python3 python3-venv python3-pip"
say "  ${DIM}$($PYTHON --version) at $PYTHON${RESET}"

# --------------------------------------------------------------- install ---
step "Installing proxmox-agent-lab"
SOURCE="proxmox-agent-lab"
# Running from a checkout? Install that instead of the published package.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[ -f "$SCRIPT_DIR/pyproject.toml" ] && SOURCE="$SCRIPT_DIR"

if command -v pipx >/dev/null 2>&1; then
    say "  ${DIM}using pipx (isolated)${RESET}"
    pipx install --force "$SOURCE" >/dev/null
    BIN="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")/proxmox-lab"
else
    say "  ${DIM}using pip --user${RESET}"
    "$PYTHON" -m pip install --quiet --user --upgrade \
        --disable-pip-version-check --no-warn-script-location "$SOURCE"
    BIN="$("$PYTHON" -c 'import site,os;print(os.path.join(site.USER_BASE,"bin","proxmox-lab"))')"
fi

if ! command -v proxmox-lab >/dev/null 2>&1; then
    if [ -x "$BIN" ]; then
        warn "$(dirname "$BIN") is not on your PATH. Add it:"
        say  "     echo 'export PATH=\"\$PATH:$(dirname "$BIN")\"' >> ~/.zshrc"
    else
        die "installed, but the proxmox-lab command was not found"
    fi
else
    BIN=$(command -v proxmox-lab)
fi
say "  ${GREEN}installed${RESET} $("$BIN" --version 2>/dev/null || echo "proxmox-lab")"

# ---------------------------------------------------------------- config ---
CONFIG="${PROXMOX_AGENT_LAB_CONFIG:-$HOME/.config/proxmox-agent-lab/config.toml}"
step "Configuring"
if [ -f "$CONFIG" ]; then
    say "  ${DIM}$CONFIG already exists, keeping it${RESET}"
else
    say "  ${DIM}Answer what you know; press Enter to skip and edit later.${RESET}"
    say ""
    ask HOST       "Proxmox IP address" ""
    ask NODE       "Proxmox node name (its hostname)" "pve"
    ask TOKEN_USER "API token user" "agent@pve"
    ask TOKEN_NAME "API token name" "lab"
    ask MAC        "Wired NIC MAC, for Wake-on-LAN" ""

    BROADCAST="255.255.255.255"
    if [ -n "$HOST" ]; then
        BROADCAST="$(printf '%s' "$HOST" | awk -F. 'NF==4{print $1"."$2"."$3".255"}')"
        [ -n "$BROADCAST" ] || BROADCAST="255.255.255.255"
    fi

    mkdir -p "$(dirname "$CONFIG")"
    "$BIN" init --path "$CONFIG" >/dev/null
    "$PYTHON" - "$CONFIG" "$HOST" "$NODE" "$TOKEN_USER" "$TOKEN_NAME" "$MAC" "$BROADCAST" <<'PY'
import pathlib, re, sys
path, host, node, tuser, tname, mac, bcast = sys.argv[1:8]
text = pathlib.Path(path).read_text()
def setkey(text, key, value):
    # Replace only the quoted value: these lines carry trailing comments, and
    # anchoring to end-of-line would silently match nothing.
    if not value:
        return text
    return re.sub(rf'(?m)^({re.escape(key)} = ")[^"]*(")',
                  lambda m: m.group(1) + value + m.group(2), text, count=1)
text = setkey(text, "host", host)
text = setkey(text, "node", node)
text = setkey(text, "token_user", tuser)
text = setkey(text, "token_name", tname)
text = setkey(text, "mac", mac)
text = setkey(text, "broadcast", bcast)
pathlib.Path(path).write_text(text)
PY
    chmod 600 "$CONFIG"
    say "  ${GREEN}wrote${RESET} $CONFIG"
fi

# --------------------------------------------------------------- secrets ---
keyring_unavailable() {
    warn "could not write to the OS keyring."
    say  "     ${DIM}Headless box or no keyring? Choose another backend:${RESET}"
    say  "       [secrets] backend = \"file\"   # a 0600 file, in $CONFIG"
    say  "       [secrets] backend = \"env\"    # export PROXMOX_AGENT_LAB_PROXMOX_TOKEN"
    say  "     ${DIM}then: proxmox-lab secrets set proxmox-token${RESET}"
}

step "API token"
if "$BIN" secrets list 2>/dev/null | grep -q '"proxmox-token": true'; then
    say "  ${DIM}already stored in your keyring${RESET}"
elif [ -n "${PXL_TOKEN_SECRET:-}" ]; then
    if printf '%s\n' "$PXL_TOKEN_SECRET" \
         | "$BIN" secrets set proxmox-token --stdin >/dev/null 2>&1; then
        say "  ${GREEN}stored${RESET}"
    else
        keyring_unavailable
    fi
elif [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
    say "  ${DIM}Create one in Proxmox: Datacenter > Permissions > API Tokens.${RESET}"
    say "  ${DIM}Leave privilege separation ON, then grant the *token*${RESET}"
    say "  ${DIM}PVEVMAdmin on /vms. The secret is shown once.${RESET}"
    say ""
    if "$BIN" secrets set proxmox-token; then
        say "  ${GREEN}stored${RESET}"
    else
        keyring_unavailable
    fi
else
    warn "no token provided -- run 'proxmox-lab secrets set proxmox-token'"
fi

# ---------------------------------------------------------------- doctor ---
step "Checking the install"
if "$BIN" doctor; then
    printf '\n%sReady.%s Your lab is one command away:\n\n' "$GREEN$BOLD" "$RESET"
    say "    proxmox-lab status"
    say "    proxmox-lab lease-begin --purpose \"first run\""
    say ""
    say "Docs: https://github.com/jr551/proxmox-agent-lab/blob/main/docs/INSTALL.md"
else
    printf '\n%sAlmost there.%s Fix what doctor listed above, then re-run:\n\n' "$YELLOW$BOLD" "$RESET"
    say "    proxmox-lab doctor"
    exit 1
fi
