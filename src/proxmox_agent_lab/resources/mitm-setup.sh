#!/usr/bin/env bash
# Prepare a disposable LXC running mitmproxy for SSL inspection and MITM relay.
# Streamed to the Proxmox host by 'proxmox-lab netcap mitm-setup'. Runs as root.
# Idempotent. Same container pattern as the Ghidra analysis box.
set -euo pipefail
LXC=__LXC__
BRIDGE=__BRIDGE__

if ! pct status "$LXC" >/dev/null 2>&1; then
  TMPL=$(pveam list local 2>/dev/null | awk '/debian-1[23]-standard/{print $1}' | head -1)
  if [ -z "$TMPL" ]; then
    pveam update >/dev/null 2>&1 || true
    NAME=$(pveam available --section system 2>/dev/null | awk '/debian-12-standard/{print $2}' | tail -1)
    pveam download local "$NAME" >/dev/null
    TMPL="local:vztmpl/$NAME"
  fi
  pct create "$LXC" "$TMPL" --hostname mitm-lab --cores 2 --memory 1024 \
    --swap 512 --rootfs local-lvm:8 --net0 name=eth0,bridge="$BRIDGE",ip=dhcp \
    --unprivileged 1 --features nesting=1 --onboot 0 --tags codex-lab >/dev/null
fi
pct start "$LXC" >/dev/null 2>&1 || true
for i in $(seq 1 30); do
  pct exec "$LXC" -- getent hosts downloads.mitmproxy.org >/dev/null 2>&1 && break
  sleep 2
done

if ! pct exec "$LXC" -- test -x /opt/mitmproxy/mitmdump 2>/dev/null; then
  pct exec "$LXC" -- bash -c '
    set -e; export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq || true
    apt-get install -y -qq curl ca-certificates openssl >/dev/null
    VER=$(curl -s https://api.github.com/repos/mitmproxy/mitmproxy/releases/latest \
      | grep -o "\"name\": \"mitmproxy [0-9.]*\"" | grep -o "[0-9][0-9.]*" | head -1)
    [ -n "$VER" ] || VER=12.2.3
    URL="https://downloads.mitmproxy.org/$VER/mitmproxy-$VER-linux-x86_64.tar.gz"
    curl -fsSL "$URL" -o /tmp/mitm.tgz
    mkdir -p /opt/mitmproxy
    tar -xzf /tmp/mitm.tgz -C /opt/mitmproxy
    rm -f /tmp/mitm.tgz
  '
fi

# Generate the interception CA by running mitmdump briefly against a dead port.
pct exec "$LXC" -- bash -c '
  if [ ! -f /root/.mitmproxy/mitmproxy-ca-cert.pem ]; then
    timeout 6 /opt/mitmproxy/mitmdump -q -p 8080 --set confdir=/root/.mitmproxy \
      >/dev/null 2>&1 || true
  fi
  test -f /root/.mitmproxy/mitmproxy-ca-cert.pem
'

# The runner invoked by 'netcap intercept'. Rewrite specs arrive as argv from
# the controller (quoted by ssh), so nothing user-supplied is interpolated into
# this script.
pct exec "$LXC" -- bash -c 'cat > /root/pxl-mitm.sh' <<'RUNNER'
#!/usr/bin/env bash
# args: SECONDS PORT PROBE_URL [extra mitmdump options...]
set -u
SEC="${1:-15}"; PORT="${2:-8080}"; PROBE="${3:-}"; shift 3 2>/dev/null || true
CONF=/root/.mitmproxy
rm -f /root/out.har /root/flows.mitm
timeout "$SEC" /opt/mitmproxy/mitmdump -q --listen-port "$PORT" \
  --set confdir="$CONF" --set hardump=/root/out.har \
  -w /root/flows.mitm "$@" >/root/mitm.log 2>&1 &
MPID=$!
sleep 2
if [ -n "$PROBE" ]; then
  CODE=$(https_proxy="http://127.0.0.1:$PORT" http_proxy="http://127.0.0.1:$PORT" \
    curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    --cacert "$CONF/mitmproxy-ca-cert.pem" "$PROBE" 2>/dev/null || echo 000)
  echo "PROBE_STATUS=$CODE"
fi
wait "$MPID" 2>/dev/null || true
exit 0
RUNNER
pct exec "$LXC" -- chmod 0755 /root/pxl-mitm.sh
echo "mitm-lxc-ready $LXC"
