#!/usr/bin/env bash
# Create a persistent, unprivileged PocketBase LXC on a Proxmox host.
#
# Run this ON the Proxmox host as root:
#   curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/pocketbase-host-setup.sh | bash
#
# The container is deliberately separate from the Proxmox host. It listens on
# the configured LAN bridge only; do not expose its HTTP port to the Internet.
# Put HTTPS in front of it before any untrusted network access.
set -euo pipefail

BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m'); YELLOW=$(printf '\033[33m')
RESET=$(printf '\033[0m')
[ -t 1 ] || { BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""; }

say() { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s\n' "$GREEN" "$RESET" "$BOLD" "$*"; printf '%s' "$RESET"; }
warn() { printf '%s!%s  %s\n' "$YELLOW" "$RESET" "$*"; }
die() { printf '%sx%s  %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
ask() {
    local var=$1 prompt=$2 default=${3:-} reply=""
    local env="PXL_${var}"
    reply="${!env:-}"
    if [ -z "$reply" ] && [ -t 0 ]; then
        if [ -n "$default" ]; then
            read -r -p "  $prompt [$default]: " reply || true
        else
            read -r -p "  $prompt: " reply || true
        fi
    fi
    printf -v "$var" '%s' "${reply:-$default}"
}

[ "$(id -u)" -eq 0 ] || die "run this as root on the Proxmox host"
command -v pct >/dev/null 2>&1 || die "pct not found -- is this a Proxmox host?"
command -v pveam >/dev/null 2>&1 || die "pveam not found -- is this a Proxmox host?"

step "PocketBase LXC settings"
ask CTID "Container ID" "$(pvesh get /cluster/nextid 2>/dev/null || true)"
[[ "$CTID" =~ ^[1-9][0-9]*$ ]] || die "container ID must be a positive integer"
pct status "$CTID" >/dev/null 2>&1 && die "container $CTID already exists; refusing to modify it"
ask ROOTFS_STORAGE "Container storage" "local-lvm"
ask TEMPLATE_STORAGE "Template storage" "local"
ask BRIDGE "Network bridge" "vmbr0"
ask NET_IP "IPv4 configuration (dhcp or address/prefix)" "dhcp"
GATEWAY=""
if [ "$NET_IP" != "dhcp" ]; then
    ask GATEWAY "IPv4 gateway" ""
    [ -n "$GATEWAY" ] || die "a static IPv4 configuration needs a gateway"
fi
ask PORT "PocketBase HTTP port" "8090"
[[ "$PORT" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$PORT" -le 65535 ] || die "port must be 1-65535"
ask ADMIN_EMAIL "PocketBase superuser email" "admin@pocketbase.local"
ADMIN_PASSWORD="${PXL_PB_ADMIN_PASSWORD:-}"
if [ -z "$ADMIN_PASSWORD" ] && [ -t 0 ]; then
    read -r -s -p "  PocketBase superuser password (leave blank to generate): " ADMIN_PASSWORD
    printf '\n'
fi
if [ -z "$ADMIN_PASSWORD" ]; then
    command -v openssl >/dev/null 2>&1 || die "openssl is required to generate a superuser password"
    ADMIN_PASSWORD=$(openssl rand -base64 24)
    GENERATED_PASSWORD=1
else
    GENERATED_PASSWORD=0
fi

step "Creating unprivileged Debian container"
pveam update >/dev/null
TEMPLATE=$(pveam available --section system 2>/dev/null | awk '$2 ~ /^debian-13-standard_.*_amd64\.tar\.zst$/ {print $2; exit}')
[ -n "$TEMPLATE" ] || die "no Debian 13 LXC template is available from configured repositories"
pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -Fq "$TEMPLATE" \
    || pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null
NET0="name=eth0,bridge=$BRIDGE,ip=$NET_IP"
[ -n "$GATEWAY" ] && NET0="$NET0,gw=$GATEWAY"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
    --hostname "pocketbase-$CTID" --unprivileged 1 --features nesting=0 \
    --cores 1 --memory 512 --swap 512 --rootfs "$ROOTFS_STORAGE:8" \
    --net0 "$NET0" --start 0 >/dev/null
pct start "$CTID"
for _ in $(seq 1 30); do
    pct exec "$CTID" -- true >/dev/null 2>&1 && break
    sleep 1
done
pct exec "$CTID" -- true >/dev/null 2>&1 || die "container $CTID did not become ready"

step "Installing PocketBase"
SECRET_FILE=$(mktemp)
trap 'rm -f "$SECRET_FILE"' EXIT
chmod 600 "$SECRET_FILE"
printf '%s' "$ADMIN_PASSWORD" >"$SECRET_FILE"
pct push "$CTID" "$SECRET_FILE" /root/.pocketbase-admin-password --perms 600
rm -f "$SECRET_FILE"
pct exec "$CTID" -- env PORT="$PORT" ADMIN_EMAIL="$ADMIN_EMAIL" bash -s <<'SH'
set -euo pipefail
ADMIN_PASSWORD=$(cat /root/.pocketbase-admin-password)
rm -f /root/.pocketbase-admin-password
apt-get update -qq
apt-get install -y -qq ca-certificates curl unzip >/dev/null
useradd --system --home /var/lib/pocketbase --shell /usr/sbin/nologin pocketbase 2>/dev/null || true
install -d -o pocketbase -g pocketbase -m 0750 /opt/pocketbase /var/lib/pocketbase
# Pin the release so a re-run never silently upgrades the database service.
version=0.39.10
archive="/tmp/pocketbase_${version}_linux_amd64.zip"
curl --fail --location --proto '=https' --tlsv1.2 \
  -o "$archive" "https://github.com/pocketbase/pocketbase/releases/download/v${version}/pocketbase_${version}_linux_amd64.zip"
unzip -oq "$archive" -d /opt/pocketbase
rm -f "$archive"
chown -R pocketbase:pocketbase /opt/pocketbase /var/lib/pocketbase
install -d -m 0755 /etc/systemd/system
cat >/etc/systemd/system/pocketbase.service <<EOF
[Unit]
Description=PocketBase
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pocketbase
Group=pocketbase
WorkingDirectory=/var/lib/pocketbase
ExecStart=/opt/pocketbase/pocketbase serve --http=0.0.0.0:${PORT} --dir=/var/lib/pocketbase/pb_data
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/var/lib/pocketbase

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
# Initialize the application database and administrator before the service owns
# it; concurrent first-open risks an avoidable SQLite lock.
/opt/pocketbase/pocketbase superuser create "$ADMIN_EMAIL" "$ADMIN_PASSWORD" --dir=/var/lib/pocketbase/pb_data
systemctl enable --now pocketbase.service
for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null && break
  sleep 1
done
curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null
SH

IP=$(pct exec "$CTID" -- sh -c "hostname -I | awk '{print \$1}'" | tr -d '[:space:]')
[ -n "$IP" ] || die "PocketBase is running but its container address could not be determined"
step "PocketBase ready"
say "  Dashboard: ${BOLD}http://$IP:$PORT/_/${RESET}"
say "  API URL:   ${BOLD}http://$IP:$PORT${RESET}"
say "  Superuser: ${BOLD}$ADMIN_EMAIL${RESET}"
if [ "$GENERATED_PASSWORD" -eq 1 ]; then
    say "  Generated password (shown once): ${BOLD}$ADMIN_PASSWORD${RESET}"
else
    say "  Superuser password: the value supplied to this script"
fi
say ""
say "  In the dashboard, create a nonrenewable superuser impersonation token"
say "  for each controller. On another controller, run install.sh and choose"
say "  PocketBase, then provide the API URL and that controller's token."
say ""
warn "This service is HTTP for a trusted LAN only. Do not port-forward $PORT."
warn "Use a TLS reverse proxy before allowing access from untrusted networks."
