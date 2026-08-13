#!/usr/bin/env bash
# One-touch installer for proxmox-agent-lab.
#
#   curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/install.sh | bash
#
# Installs the package, asks the one-time setup questions, writes a config,
# stores secrets in the OS keyring, and runs the health check. Everything it
# asks for is optional -- skip a prompt with Enter and edit the config later.
#
# Non-interactive:
#   PXL_HOST=192.168.1.50 PXL_NODE=pve PXL_TOKEN_USER=agent@pve \
#   PXL_TOKEN_NAME=lab PXL_TOKEN_SECRET=... PXL_MAC=aa:bb:.. ./install.sh --yes
#   PXL_AUDIT_BACKEND=pocketbase PXL_PB_URL=https://pb.example \
#   PXL_PB_TOKEN_SECRET=... ./install.sh --yes

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
CONFIGURE_EXISTING=0
for argument in "$@"; do
    case "$argument" in
        --yes) ASSUME_YES=1 ;;
        --configure) CONFIGURE_EXISTING=1 ;;
        *) die "unknown option: $argument (expected --yes or --configure)" ;;
    esac
done

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

ask_choice() { # ask_choice VAR "prompt" "default" choices...
    local __var=$1 __prompt=$2 __default=$3 __reply="" __choice
    shift 3
    ask "$__var" "$__prompt" "$__default"
    __reply="${!__var}"
    for __choice in "$@"; do
        [ "$__reply" = "$__choice" ] && return
    done
    die "$__prompt must be one of: $*"
}

confirm() { # confirm "prompt"; true only for an explicit yes
    local __prompt=$1 __reply="${PXL_RECONFIGURE:-}"
    if [ -z "$__reply" ] && [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
        read -r -p "  $__prompt [y/N]: " __reply || true
    fi
    [ "$__reply" = "y" ] || [ "$__reply" = "Y" ] \
        || [ "$__reply" = "yes" ] || [ "$__reply" = "YES" ]
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

CONFIG="${PROXMOX_AGENT_LAB_CONFIG:-$HOME/.config/proxmox-agent-lab/config.toml}"
step "Configuring"
CONFIGURE=1
if [ -f "$CONFIG" ] && [ "$CONFIGURE_EXISTING" -eq 0 ]; then
    if confirm "$CONFIG already exists. Reconfigure it now?"; then
        CONFIGURE=1
    else
        CONFIGURE=0
        say "  ${DIM}keeping $CONFIG${RESET}"
    fi
fi
if [ "$CONFIGURE" -eq 1 ]; then
    say "  ${DIM}Answer what you know; press Enter to keep defaults or skip optional features.${RESET}"
    say ""
    ask HOST       "Proxmox IP address" ""
    ask NODE       "Proxmox node name (its hostname)" "pve"
    ask TOKEN_USER "API token user" "agent@pve"
    ask TOKEN_NAME "API token name" "lab"
    ask MAC        "Wired NIC MAC, for Wake-on-LAN" ""
    ask_choice AUDIT_BACKEND \
        "Audit backend (sqlite, jsonl, or pocketbase)" "sqlite" \
        sqlite jsonl pocketbase
    PB_URL=""
    PB_COLLECTION="proxmox_lab_events"
    PB_TOKEN_NAME="audit-token"
    if [ "$AUDIT_BACKEND" = "pocketbase" ]; then
        ask_choice PB_LOCATION \
            "PocketBase location (existing or proxmox)" "existing" \
            existing proxmox
        if [ "$PB_LOCATION" = "proxmox" ]; then
            say ""
            say "  Run this once as root on the Proxmox host. It creates an"
            say "  unprivileged PocketBase LXC, service account, port, and"
            say "  first superuser; it prints the API URL when finished:"
            say ""
            say "  curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/pocketbase-host-setup.sh | bash"
            say ""
            die "re-run this controller setup with the printed PocketBase API URL"
        fi
        ask PB_URL "PocketBase URL (HTTPS, or trusted-LAN HTTP)" ""
        [ -n "$PB_URL" ] || die "PocketBase URL is required when audit backend is pocketbase"
        ask PB_COLLECTION "PocketBase audit collection" "proxmox_lab_events"
        ask PB_TOKEN_NAME "Secret-store name for the PocketBase token" "audit-token"
    fi

    BROADCAST="255.255.255.255"
    if [ -n "$HOST" ]; then
        BROADCAST="$(printf '%s' "$HOST" | awk -F. 'NF==4{print $1"."$2"."$3".255"}')"
        [ -n "$BROADCAST" ] || BROADCAST="255.255.255.255"
    fi

    mkdir -p "$(dirname "$CONFIG")"
    [ -f "$CONFIG" ] || "$BIN" init --path "$CONFIG" >/dev/null
    "$PYTHON" - "$CONFIG" "$HOST" "$NODE" "$TOKEN_USER" "$TOKEN_NAME" "$MAC" \
        "$BROADCAST" "$AUDIT_BACKEND" "$PB_URL" "$PB_COLLECTION" "$PB_TOKEN_NAME" <<'PY'
import json, pathlib, re, sys
path, host, node, tuser, tname, mac, bcast, backend, pb_url, pb_collection, pb_secret = sys.argv[1:12]
text = pathlib.Path(path).read_text()

def setkey(text, section, key, value, required=False):
    if not value and not required:
        return text
    section_pattern = rf"(?ms)(^\[{re.escape(section)}\]\n.*?)(?=^\[|\Z)"
    section_match = re.search(section_pattern, text)
    if section_match is None:
        raise ValueError(f"configuration template has no [{section}] section")
    body = section_match.group(1)
    updated, count = re.subn(
        rf"(?m)^({re.escape(key)}\s*=\s*)[^\n]*$",
        lambda match: match.group(1) + json.dumps(value),
        body,
        count=1,
    )
    if count != 1:
        raise ValueError(f"configuration template has no [{section}] {key}")
    return text[:section_match.start(1)] + updated + text[section_match.end(1):]

for key, value in (("host", host), ("node", node), ("token_user", tuser),
                   ("token_name", tname)):
    text = setkey(text, "proxmox", key, value)
for key, value in (("mac", mac), ("broadcast", bcast)):
    text = setkey(text, "power", key, value)
text = setkey(text, "audit", "backend", backend, required=True)
if backend == "pocketbase":
    text = setkey(text, "audit", "pocketbase_url", pb_url, required=True)
    text = setkey(text, "audit", "pocketbase_collection", pb_collection, required=True)
    text = setkey(text, "audit", "pocketbase_token_secret", pb_secret, required=True)
pathlib.Path(path).write_text(text)
PY
    chmod 600 "$CONFIG"
    say "  ${GREEN}wrote${RESET} $CONFIG"
fi

# --------------------------------------------------------------- secrets ---
keyring_unavailable() {
    local secret_name=${1:-proxmox-token}
    warn "could not write to the OS keyring."
    say  "     ${DIM}Headless box or no keyring? Choose another backend:${RESET}"
    say  "       [secrets] backend = \"file\"   # a 0600 file, in $CONFIG"
    say  "       [secrets] backend = \"env\"    # export the corresponding PROXMOX_AGENT_LAB_* variable"
    say  "     ${DIM}then: proxmox-lab secrets set $secret_name${RESET}"
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

# ---------------------------------------------------------- audit backend ---
if [ "${AUDIT_BACKEND:-}" = "pocketbase" ]; then
    step "PocketBase audit token"
    PB_TOKEN_VALUE="${PXL_PB_TOKEN_SECRET:-}"
    if "$BIN" secrets list 2>/dev/null | grep -q "\"$PB_TOKEN_NAME\": true"; then
        say "  ${DIM}already stored in your keyring${RESET}"
    elif [ -n "$PB_TOKEN_VALUE" ]; then
        if printf '%s\n' "$PB_TOKEN_VALUE" \
             | "$BIN" secrets set "$PB_TOKEN_NAME" --stdin >/dev/null 2>&1; then
            say "  ${GREEN}stored${RESET}"
        else
            keyring_unavailable "$PB_TOKEN_NAME"
        fi
    elif [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
        say "  ${DIM}Use a PocketBase superuser/API token; it is stored only in your OS keyring.${RESET}"
        if "$BIN" secrets set "$PB_TOKEN_NAME"; then
            say "  ${GREEN}stored${RESET}"
        else
            keyring_unavailable "$PB_TOKEN_NAME"
        fi
    else
        warn "no PocketBase token provided -- run 'proxmox-lab secrets set $PB_TOKEN_NAME'"
    fi
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
