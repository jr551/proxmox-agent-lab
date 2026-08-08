# Network capture, SSL inspection and MITM relay

Three ways to see — and shape — what a guest puts on the wire: watch an
installer phone home, reverse-engineer a protocol, debug a driver, or force an
app down a path it would not normally take. Nothing is installed in the guest
for capture; interception runs in a throwaway container.

Like memflow and usb, the host-resident parts reach the Proxmox host over the
same opt-in **SSH channel**, so they are off until `[memflow] enabled = true`
and `ssh_host` are set (see [memflow.md](memflow.md)). Capture reads a guest's
traffic and interception decrypts it, so both require an active **lease** and
are audited by the *fact* of the capture only — the packets and decrypted flows
go to a local file you name, never to the ledger.

## 1. Passive capture — `capture`

A running QEMU guest's NIC appears on the host as a tap interface
(`tap<vmid>i0`). `capture` runs `tcpdump` on that interface and hands back a
standard **pcap**, so it sees every frame regardless of the guest OS and
touches nothing inside the guest.

```bash
proxmox-lab netcap capture --lease "$L" --vmid 9001 --seconds 15 --out ./guest.pcap
# narrow it with a BPF filter, or cap the packet count:
proxmox-lab netcap capture --lease "$L" --vmid 9001 --filter 'tcp port 443' \
    --count 500 --out ./https.pcap
```

`--nic net0` (the default) picks the interface; pass `--iface` to name a host
interface directly (e.g. an LXC `veth`). Open the pcap in Wireshark. **TLS is
captured as ciphertext** — to read it, terminate TLS in the middle with the
MITM proxy below.

## 2. The MITM proxy — `mitm-setup`

SSL inspection means a proxy the guest trusts. It runs in a **disposable LXC**
— the same container pattern the Ghidra analysis box uses — built once and
destroyed with the lease:

```bash
proxmox-lab netcap mitm-setup --lease "$L" --lxc 9071
```

This creates the container (if absent), installs the standalone
[mitmproxy](https://mitmproxy.org) build, generates its interception CA, and
registers the LXC to the lease. It reports the **proxy IP and port** the guest
should use. Idempotent — re-running a ready box just re-reports its address.
Check it any time with `proxmox-lab netcap doctor --lease "$L" --lxc 9071`.

## 3. Trust the CA — `ca`

Without the proxy's CA installed, the guest rejects the intercepted TLS and you
see only errors. `ca` writes the CA locally (PEM, plus a `.cer` for Windows)
and prints the exact command to trust it on the guest OS:

```bash
proxmox-lab netcap ca --lease "$L" --lxc 9071 --os windows --out ./mitm-ca.pem
```

`--os` accepts `windows`, `linux`, `macos` or `android`; each prints a
ready-to-paste **install helper** (`certutil` / `update-ca-certificates` /
`security add-trusted-cert` / the `adb` system-store push for a rooted or
emulated Android). Push the cert into the guest and run the helper there.

## 4. Intercept, decrypt and rewrite — `intercept`

Runs the proxy for a bounded window, decrypts the HTTPS flows, and returns a
compact JSON summary (method, URL, status, size). Save the raw flows with
`--out` (reopen in mitmproxy) or the HAR with `--har`.

```bash
# point a guest's proxy at the LXC, then:
proxmox-lab netcap intercept --lease "$L" --lxc 9071 --seconds 20 --har ./flows.har
```

**Prove it end to end without wiring up a guest** — `--probe` drives one request
through the proxy from inside the container itself:

```bash
proxmox-lab netcap intercept --lease "$L" --lxc 9071 --probe https://example.com
# -> probe_status 200, one decrypted flow to https://example.com/
```

Because a relay that can read can also **rewrite**, `intercept` is an active
MITM when you ask it to be:

| Flag | Effect |
|---|---|
| `--set-header 'Name: value'` | rewrite/add a **request** header |
| `--set-response-header 'Name: value'` | rewrite/add a **response** header |
| `--replace 'REGEX/REPLACEMENT'` | rewrite matching **response** bodies |
| `--map-remote 'SPEC'` | mitmproxy map-remote (redirect a host/path) |

```bash
# inject a header and rewrite the page the client sees:
proxmox-lab netcap intercept --lease "$L" --lxc 9071 --probe https://example.com \
    --set-response-header 'X-Debug: 1' --replace 'Example Domain/PATCHED'
```

All rewrite flags are repeatable and pass straight through to mitmproxy's
`--modify-headers` / `--modify-body` / `--map-remote`, so their full filter
syntax is available.

## Wiring a guest through the proxy

`mitm-setup` reports the proxy address. Point the guest at it and install the
CA (step 3):

- **Windows / macOS / Linux desktop** — set the system HTTP/HTTPS proxy to
  `<proxy_ip>:8080`.
- **A shell** — `export https_proxy=http://<proxy_ip>:8080 http_proxy=…`.
- **Android emulator** (see [android.md](android.md)) — start it with an HTTP
  proxy, and use the `--os android` helper to place the CA in the system store.

For a guest that cannot be told to use a proxy, put it behind the proxy LXC as
its gateway and redirect `:80`/`:443` (transparent mode) — the same topology as
the [VPN gateway](network.md).

## Safety

- Off until the `[memflow]` SSH channel is configured; capture and interception
  both need an active lease.
- The MITM LXC is a lab guest: tagged `codex-lab`, registered to the lease, and
  destroyed on `lease-end` like everything else.
- Captures and decrypted flows are written only to the local files you name.
  The audit ledger records that a capture happened — never its contents.
- Interception only sees traffic a client deliberately routes through the proxy
  (and can decrypt only once its CA is trusted). Install that CA in guests you
  control, for work you are authorized to do.
