#!/usr/bin/env bash
# Create a persistent, unprivileged MinIO LXC on a Proxmox host as a minimal
# S3-compatible scratch bucket for proxmox-agent-lab.
#
# Run this ON the Proxmox host as root:
#   curl -fsSL https://raw.githubusercontent.com/jr551/proxmox-agent-lab/main/minio-host-setup.sh | bash
#
# The container is deliberately separate from the Proxmox host and runs the
# S3 API only -- the browser console is disabled to keep it minimal. It
# listens on the configured LAN bridge only; do not expose its port to the
# Internet. Put HTTPS in front of it before any untrusted network access.
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

# Pin the releases so a re-run never silently upgrades the storage service.
MINIO_VERSION="RELEASE.2025-09-07T16-13-09Z"
MINIO_SHA256="7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f"
MC_VERSION="RELEASE.2025-08-13T08-35-41Z"
MC_SHA256="01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891"

step "MinIO LXC settings"
ask CTID "Container ID" "$(pvesh get /cluster/nextid 2>/dev/null || true)"
[[ "$CTID" =~ ^[1-9][0-9]*$ ]] || die "container ID must be a positive integer"
pct status "$CTID" >/dev/null 2>&1 && die "container $CTID already exists; refusing to modify it"
ask ROOTFS_STORAGE "Container storage" "local-lvm"
ask TEMPLATE_STORAGE "Template storage" "local"
ask ROOTFS_SIZE "Container disk size in GB (bucket data lives here)" "16"
[[ "$ROOTFS_SIZE" =~ ^[1-9][0-9]*$ ]] || die "disk size must be a positive integer"
ask BRIDGE "Network bridge" "vmbr0"
ask NET_IP "IPv4 configuration (dhcp or address/prefix)" "dhcp"
GATEWAY=""
if [ "$NET_IP" != "dhcp" ]; then
    ask GATEWAY "IPv4 gateway" ""
    [ -n "$GATEWAY" ] || die "a static IPv4 configuration needs a gateway"
fi
ask PORT "MinIO S3 API port" "9000"
[[ "$PORT" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$PORT" -le 65535 ] || die "port must be 1-65535"
ask BUCKET "Bucket name" "lab-scratch"
[[ "$BUCKET" =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] \
    || die "bucket name must be 3-63 chars: lowercase letters, digits, hyphens"
ask ACCESS_KEY "MinIO access key (S3 key ID)" "lab-controller"
[[ "$ACCESS_KEY" =~ ^[A-Za-z0-9_-]{3,40}$ ]] \
    || die "access key must be 3-40 chars: letters, digits, - or _"
SECRET_KEY="${PXL_SECRET_KEY:-}"
if [ -z "$SECRET_KEY" ] && [ -t 0 ]; then
    read -r -s -p "  MinIO secret key (leave blank to generate): " SECRET_KEY
    printf '\n'
fi
if [ -z "$SECRET_KEY" ]; then
    command -v openssl >/dev/null 2>&1 || die "openssl is required to generate a secret key"
    SECRET_KEY=$(openssl rand -base64 24)
    GENERATED_SECRET=1
else
    GENERATED_SECRET=0
fi
[ ${#SECRET_KEY} -ge 8 ] || die "secret key must be at least 8 characters"

step "Creating unprivileged Debian container"
pveam update >/dev/null
TEMPLATE=$(pveam available --section system 2>/dev/null | awk '$2 ~ /^debian-13-standard_.*_amd64\.tar\.zst$/ {print $2; exit}')
[ -n "$TEMPLATE" ] || die "no Debian 13 LXC template is available from configured repositories"
pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -Fq "$TEMPLATE" \
    || pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null
NET0="name=eth0,bridge=$BRIDGE,ip=$NET_IP"
[ -n "$GATEWAY" ] && NET0="$NET0,gw=$GATEWAY"
# --onboot: the lab host powers itself off between leases, so this container
# must come back on its own when the host does -- nothing else will start it.
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
    --hostname "minio-$CTID" --unprivileged 1 --features nesting=0 \
    --cores 1 --memory 512 --swap 512 --rootfs "$ROOTFS_STORAGE:$ROOTFS_SIZE" \
    --net0 "$NET0" --onboot 1 --start 0 >/dev/null
pct start "$CTID"
for _ in $(seq 1 30); do
    pct exec "$CTID" -- true >/dev/null 2>&1 && break
    sleep 1
done
pct exec "$CTID" -- true >/dev/null 2>&1 || die "container $CTID did not become ready"

step "Installing MinIO"
SECRET_FILE=$(mktemp)
trap 'rm -f "$SECRET_FILE"' EXIT
chmod 600 "$SECRET_FILE"
printf '%s' "$SECRET_KEY" >"$SECRET_FILE"
pct push "$CTID" "$SECRET_FILE" /root/.minio-secret-key --perms 600
rm -f "$SECRET_FILE"
pct exec "$CTID" -- env PORT="$PORT" BUCKET="$BUCKET" ACCESS_KEY="$ACCESS_KEY" \
    MINIO_VERSION="$MINIO_VERSION" MINIO_SHA256="$MINIO_SHA256" \
    MC_VERSION="$MC_VERSION" MC_SHA256="$MC_SHA256" bash -s <<'SH'
set -euo pipefail
SECRET_KEY=$(cat /root/.minio-secret-key)
rm -f /root/.minio-secret-key
apt-get update -qq
apt-get install -y -qq ca-certificates curl >/dev/null
useradd --system --home /var/lib/minio --shell /usr/sbin/nologin minio 2>/dev/null || true
install -d -o minio -g minio -m 0750 /var/lib/minio/data
install -d -m 0755 /opt/minio

curl --fail --location --proto '=https' --tlsv1.2 \
    -o /opt/minio/minio "https://dl.min.io/server/minio/release/linux-amd64/archive/minio.${MINIO_VERSION}"
echo "${MINIO_SHA256}  /opt/minio/minio" | sha256sum -c -
curl --fail --location --proto '=https' --tlsv1.2 \
    -o /opt/minio/mc "https://dl.min.io/client/mc/release/linux-amd64/archive/mc.${MC_VERSION}"
echo "${MC_SHA256}  /opt/minio/mc" | sha256sum -c -
chmod 0755 /opt/minio/minio /opt/minio/mc

# Root credentials live in a root:minio, 0640 file, never inline in the unit
# file (which systemd normally leaves world-readable).
install -d -m 0750 -o root -g minio /etc/minio
cat >/etc/minio/minio.env <<EOF
MINIO_ROOT_USER=${ACCESS_KEY}
MINIO_ROOT_PASSWORD=${SECRET_KEY}
MINIO_BROWSER=off
EOF
chmod 0640 /etc/minio/minio.env
chown root:minio /etc/minio/minio.env

install -d -m 0755 /etc/systemd/system
cat >/etc/systemd/system/minio.service <<EOF
[Unit]
Description=MinIO
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=minio
Group=minio
EnvironmentFile=/etc/minio/minio.env
ExecStart=/opt/minio/minio server /var/lib/minio/data --address :${PORT}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/var/lib/minio

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now minio.service
for _ in $(seq 1 20); do
    curl -fsS "http://127.0.0.1:${PORT}/minio/health/live" >/dev/null && break
    sleep 1
done
curl -fsS "http://127.0.0.1:${PORT}/minio/health/live" >/dev/null

# Create the bucket now, while the root credentials are still in this shell,
# so the container is immediately usable after this script exits.
/opt/minio/mc alias set local "http://127.0.0.1:${PORT}" "${ACCESS_KEY}" "${SECRET_KEY}" >/dev/null
/opt/minio/mc mb --ignore-existing "local/${BUCKET}" >/dev/null
SH

IP=$(pct exec "$CTID" -- sh -c "hostname -I | awk '{print \$1}'" | tr -d '[:space:]')
[ -n "$IP" ] || die "MinIO is running but its container address could not be determined"
step "MinIO ready"
say "  Endpoint:   ${BOLD}http://$IP:$PORT${RESET}"
say "  Bucket:     ${BOLD}$BUCKET${RESET}"
say "  Region:     ${BOLD}us-east-1${RESET}"
say "  Access key: ${BOLD}$ACCESS_KEY${RESET}"
if [ "$GENERATED_SECRET" -eq 1 ]; then
    say "  Generated secret key (shown once): ${BOLD}$SECRET_KEY${RESET}"
else
    say "  Secret key: the value supplied to this script"
fi
say ""
say "  Re-run controller setup, choose the S3 backend 'existing', and enter"
say "  the endpoint, bucket and region above. Then store the credentials:"
say ""
say "    proxmox-lab secrets set s3-key-id"
say "    proxmox-lab secrets set s3-secret-key"
say ""
warn "This service is HTTP for a trusted LAN only. Do not port-forward $PORT."
warn "Use a TLS reverse proxy before allowing access from untrusted networks."
