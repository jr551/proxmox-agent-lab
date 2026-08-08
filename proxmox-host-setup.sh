#!/usr/bin/env bash
# Make a blank Proxmox machine ready for proxmox-agent-lab.
#
# Run this ON the Proxmox host, as root:
#
#   curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/proxmox-host-setup.sh | bash
#
# It creates a restricted API user and token, grants exactly the privileges
# the tool needs, makes Wake-on-LAN survive reboots, and prints the config
# block to paste on your laptop. Safe to re-run: everything is idempotent, and
# nothing that already exists is modified.
#
# It does NOT touch your network, storage, or existing guests.

set -euo pipefail

BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m'); YELLOW=$(printf '\033[33m')
RESET=$(printf '\033[0m')
[ -t 1 ] || { BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s\n' "$GREEN" "$RESET" "$BOLD" "$*"; printf '%s' "$RESET"; }
warn() { printf '%s!%s  %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '%sx%s  %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

USER_ID="${PXL_USER:-agent@pve}"
TOKEN_ID="${PXL_TOKEN:-lab}"
ROLE="${PXL_ROLE:-PVEVMAdmin}"

[ "$(id -u)" -eq 0 ] || die "run this as root on the Proxmox host"
command -v pveum >/dev/null 2>&1 || die "pveum not found -- is this a Proxmox host?"

NODE=$(hostname -s)
step "Proxmox host: $NODE"
say "  ${DIM}$(pveversion | head -1)${RESET}"

# ------------------------------------------------------------------ user ---
step "API user and token"
if pveum user list --output-format json 2>/dev/null | grep -q "\"$USER_ID\""; then
    say "  ${DIM}user $USER_ID already exists${RESET}"
else
    pveum user add "$USER_ID" --comment "proxmox-agent-lab controller"
    say "  ${GREEN}created${RESET} user $USER_ID"
fi

TOKEN_SECRET=""
if pveum user token list "$USER_ID" --output-format json 2>/dev/null \
     | grep -q "\"$TOKEN_ID\""; then
    warn "token $USER_ID!$TOKEN_ID already exists."
    say  "     ${DIM}Its secret is only shown at creation. To get a new one:${RESET}"
    say  "     ${DIM}pveum user token remove $USER_ID $TOKEN_ID${RESET}"
    say  "     ${DIM}then re-run this script.${RESET}"
else
    # Privilege separation ON: the token gets only what we grant it below.
    TOKEN_SECRET=$(pveum user token add "$USER_ID" "$TOKEN_ID" --privsep 1 \
        --output-format json | sed -n 's/.*"value"[ ]*:[ ]*"\([^"]*\)".*/\1/p')
    [ -n "$TOKEN_SECRET" ] || die "token created but the secret could not be read"
    say "  ${GREEN}created${RESET} token $USER_ID!$TOKEN_ID"
fi

# ----------------------------------------------------------- permissions ---
step "Permissions"
# A privilege-separated token inherits nothing, so grant both the user and the
# token. This mismatch is the most common setup failure.
for target in "--users $USER_ID" "--tokens $USER_ID!$TOKEN_ID"; do
    # shellcheck disable=SC2086
    pveum acl modify /vms $target --roles "$ROLE" >/dev/null
done
say "  ${GREEN}granted${RESET} $ROLE on /vms to the user and the token"
say "  ${DIM}That covers creating, running, consoling and deleting guests.${RESET}"

if [ "${PXL_ALLOW_HOST_ADMIN:-0}" = "1" ]; then
    pveum role add AgentNodeAdmin --privs "Sys.Audit,Sys.Modify,Sys.PowerMgmt,Sys.AccessNetwork" 2>/dev/null \
      || pveum role modify AgentNodeAdmin --privs "Sys.Audit,Sys.Modify,Sys.PowerMgmt,Sys.AccessNetwork"
    for target in "--users $USER_ID" "--tokens $USER_ID!$TOKEN_ID"; do
        # shellcheck disable=SC2086
        pveum acl modify "/nodes/$NODE" $target --roles AgentNodeAdmin >/dev/null
        # shellcheck disable=SC2086
        pveum acl modify /storage $target --roles PVEDatastoreAdmin >/dev/null
    done
    say "  ${GREEN}granted${RESET} node and storage administration"
    say "  ${DIM}(needed for disk setup and the VPN gateway bridge)${RESET}"
else
    say "  ${DIM}Node/storage admin not granted. Re-run with${RESET}"
    say "  ${DIM}PXL_ALLOW_HOST_ADMIN=1 if you want the VPN gateway or${RESET}"
    say "  ${DIM}disk management. It lets the token change host config.${RESET}"
fi

# Powering the node off is how a lease ends; without this the host stays up.
pveum role add AgentPowerOff --privs "Sys.PowerMgmt" 2>/dev/null || true
for target in "--users $USER_ID" "--tokens $USER_ID!$TOKEN_ID"; do
    # shellcheck disable=SC2086
    pveum acl modify "/nodes/$NODE" $target --roles AgentPowerOff >/dev/null
done
say "  ${GREEN}granted${RESET} node power-off (this is how a lease ends)"

# ---------------------------------------------------------- wake-on-lan ----
step "Wake-on-LAN"
IFACE=$(ip -o -4 route show default | awk '{print $5; exit}')
BRIDGE_PORT=""
if [ -n "$IFACE" ] && [ -d "/sys/class/net/$IFACE/bridge" ]; then
    # The default route is via a bridge; the real NIC is the bridge port.
    BRIDGE_PORT=$(ls "/sys/class/net/$IFACE/brif" 2>/dev/null | head -1)
    [ -n "$BRIDGE_PORT" ] && IFACE="$BRIDGE_PORT"
fi

if [ -z "$IFACE" ]; then
    warn "could not identify the wired interface; set [power] mac by hand"
    MAC=""
else
    MAC=$(cat "/sys/class/net/$IFACE/address" 2>/dev/null || echo "")
    say "  interface: ${BOLD}$IFACE${RESET}   MAC: ${BOLD}${MAC:-unknown}${RESET}"
    if ! command -v ethtool >/dev/null 2>&1; then
        say "  ${DIM}installing ethtool${RESET}"
        apt-get install -y -qq ethtool >/dev/null 2>&1 || warn "could not install ethtool"
    fi
    if command -v ethtool >/dev/null 2>&1; then
        SUPPORTED=$(ethtool "$IFACE" 2>/dev/null | sed -n 's/.*Supports Wake-on: *//p')
        CURRENT=$(ethtool "$IFACE" 2>/dev/null | sed -n 's/.*Wake-on: *//p' | tail -1)
        if printf '%s' "$SUPPORTED" | grep -q g; then
            if [ "$CURRENT" = "g" ]; then
                say "  ${DIM}already armed${RESET}"
            else
                ethtool -s "$IFACE" wol g && say "  ${GREEN}armed${RESET} Wake-on-LAN"
            fi
            # Make it survive a reboot.
            HOOK=/etc/network/if-up.d/wol
            printf '#!/bin/sh\n[ "$IFACE" = "%s" ] || exit 0\nethtool -s %s wol g || true\n' \
                "$IFACE" "$IFACE" > "$HOOK"
            chmod +x "$HOOK"
            say "  ${GREEN}persisted${RESET} via $HOOK"
        else
            warn "$IFACE does not support Wake-on-LAN (Supports Wake-on: ${SUPPORTED:-?})."
            say  "     ${DIM}Use a smart plug or IPMI instead -- see CONFIGURATION.md${RESET}"
        fi
    fi
    say "  ${DIM}Also enable Wake-on-LAN in the BIOS, or none of this helps.${RESET}"
fi

# --------------------------------------------------------------- summary ---
IP=$(ip -o -4 addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1)
BROADCAST=$(printf '%s' "$IP" | awk -F. 'NF==4{print $1"."$2"."$3".255"}')

step "Done. Paste this into your laptop's config"
say ""
say "  ${DIM}~/.config/proxmox-agent-lab/config.toml${RESET}"
say ""
cat <<EOF
[proxmox]
host = "${IP:-CHANGE-ME}"
node = "$NODE"
token_user = "$USER_ID"
token_name = "$TOKEN_ID"

[power]
mode = "wake-on-lan"
mac = "${MAC:-CHANGE-ME}"
broadcast = "${BROADCAST:-255.255.255.255}"
EOF
say ""
if [ -n "$TOKEN_SECRET" ]; then
    say "${BOLD}Token secret (shown once):${RESET}"
    say ""
    say "    $TOKEN_SECRET"
    say ""
    say "Store it on your laptop with:"
    say "    ${BOLD}proxmox-lab secrets set proxmox-token${RESET}"
else
    say "Reuse your existing token secret, or recreate the token to get a new one."
fi
say ""
say "Then: ${BOLD}proxmox-lab doctor${RESET}"
