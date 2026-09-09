
set -euo pipefail
CTID="__CTID__"; STORAGE="__STORAGE__"; BRIDGE="__BRIDGE__"
DBNAME="__DBNAME__"; DBUSER="__DBUSER__"; DBPASS="__DBPASS__"
HOSTIP="$(hostname -I | awk '{print $1}')"

command -v pct >/dev/null || { echo "pct not found: not a Proxmox host" >&2; exit 1; }

if pct status "$CTID" >/dev/null 2>&1; then
  echo "ledger-exists $CTID"
else
  # Match the host's architecture explicitly. `sort | tail -1` alone picks
  # arm64 over amd64 -- it sorts later -- and the container then refuses to
  # start on an amd64 host with a bare "Failed to spawn container".
  ARCH=$(dpkg --print-architecture)
  TPL=""
  for SERIES in debian-13-standard debian-12-standard; do
    TPL=$(pveam available --section system 2>/dev/null \
          | awk -v s="$SERIES" -v a="_${ARCH}.tar" '$2 ~ s && index($2, a) {print $2}' \
          | sort -V | tail -1)
    [ -n "$TPL" ] && break
  done
  [ -n "$TPL" ] || { echo "no debian $ARCH template available" >&2; exit 1; }
  pveam download local "$TPL" >/dev/null 2>&1 || true
  # nesting: Debian 13 ships systemd 257, which will not boot in an
  # unprivileged container without it.
  pct create "$CTID" "local:vztmpl/$TPL" \
    --hostname pxl-ledger --cores 1 --memory 1024 --swap 512 \
    --rootfs "$STORAGE:8" --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
    --features nesting=1 \
    --unprivileged 1 --onboot 1 --tags codex-lab-infra >/dev/null
  echo "ledger-created $CTID"
fi

pct status "$CTID" | grep -q running || pct start "$CTID"
for _ in $(seq 1 60); do pct exec "$CTID" -- true 2>/dev/null && break; sleep 2; done

pct exec "$CTID" -- bash -c '
  set -euo pipefail
  export DEBIAN_FRONTEND=noninteractive
  if ! command -v mariadbd >/dev/null 2>&1 && ! command -v mysqld >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq mariadb-server >/dev/null
  fi
  printf "%s\n" "[mysqld]" "bind-address = 0.0.0.0" \
    > /etc/mysql/mariadb.conf.d/60-pxl.cnf
  systemctl enable --now mariadb >/dev/null 2>&1 || true
  systemctl restart mariadb
'

pct exec "$CTID" -- mariadb -e "
  CREATE DATABASE IF NOT EXISTS \`$DBNAME\`
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
  CREATE USER IF NOT EXISTS '$DBUSER'@'%' IDENTIFIED BY '$DBPASS';
  ALTER USER '$DBUSER'@'%' IDENTIFIED BY '$DBPASS';
  GRANT ALL PRIVILEGES ON \`$DBNAME\`.* TO '$DBUSER'@'%';
  FLUSH PRIVILEGES;"

CTIP=""
for _ in $(seq 1 30); do
  CTIP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}') || true
  [ -n "$CTIP" ] && break; sleep 2
done
[ -n "$CTIP" ] || { echo "container has no IP yet" >&2; exit 1; }

# Publish the ledger on the hypervisor's own address, so every controller uses
# the same host it already talks Proxmox API to. Persisted so it survives a
# reboot; the container is onboot, the rule must be too.
add_rule() {
  iptables -t nat -C "$@" 2>/dev/null || iptables -t nat -A "$@"
}
add_rule PREROUTING -p tcp --dport 3306 -j DNAT --to-destination "$CTIP:3306"
add_rule OUTPUT -o lo -p tcp --dport 3306 -j DNAT --to-destination "$CTIP:3306"
add_rule POSTROUTING -p tcp -d "$CTIP" --dport 3306 -j MASQUERADE
mkdir -p /etc/network/if-up.d
cat > /etc/network/if-up.d/pxl-ledger-dnat <<EOF
#!/bin/sh
iptables -t nat -C PREROUTING -p tcp --dport 3306 -j DNAT --to-destination $CTIP:3306 2>/dev/null || \
  iptables -t nat -A PREROUTING -p tcp --dport 3306 -j DNAT --to-destination $CTIP:3306
iptables -t nat -C OUTPUT -o lo -p tcp --dport 3306 -j DNAT --to-destination $CTIP:3306 2>/dev/null || \
  iptables -t nat -A OUTPUT -o lo -p tcp --dport 3306 -j DNAT --to-destination $CTIP:3306
iptables -t nat -C POSTROUTING -p tcp -d $CTIP --dport 3306 -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -p tcp -d $CTIP --dport 3306 -j MASQUERADE
EOF
chmod +x /etc/network/if-up.d/pxl-ledger-dnat

# The lease guard: enforces cleanup from the host itself, so a controller that
# goes away does not leave guests running for days.
apt-get install -y -qq python3-pymysql >/dev/null 2>&1 || \
  pip3 install --quiet --break-system-packages PyMySQL >/dev/null 2>&1 || true
cat > /etc/pxl-hostguard.json <<EOF
{"host": "$CTIP", "port": 3306, "database": "$DBNAME",
 "user": "$DBUSER", "password": "$DBPASS",
 "grace_minutes": 90, "ledger_ctid": "$CTID",
 "controller_id": "pxl-hostguard"}
EOF
chmod 600 /etc/pxl-hostguard.json
__GUARD_INSTALL__

echo "ledger-ready ctid=$CTID container_ip=$CTIP host_ip=$HOSTIP port=3306"
