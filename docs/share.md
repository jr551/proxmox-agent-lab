# 🔗 Disposable console links

Send someone a URL. They see one VM's screen in their browser — no Proxmox
account, no VPN, no client to install. The link expires on its own.

```bash
proxmox-lab share create --lease "$L" --vmid 9001 --minutes 30
```

```json
{
  "url": "https://a1b2-c3d4.ngrok-free.app/v/kJ8x…qP2/",
  "vmid": 9001,
  "expires_in_minutes": 30,
  "single_use": false,
  "warning": "anyone with this URL can use that console until it expires."
}
```

## 🧩 How it works

A small worker VM runs two things: `pxl-share`, a standard-library HTTP server
that serves noVNC and relays the console, and a tunnel that gives it a public
address. The default tunnel is **cloudflared**, whose quick tunnels need no
account and no credential at all. The controller drives it through the guest agent, so there is no
control port to secure.

```
browser ─https─► cloudflared ─► worker VM ─► Proxmox vncwebsocket ─► guest
                            (noVNC + relay)
```

### Why a relay rather than just a link

A Proxmox VNC ticket lives for seconds and is spent by the first connection.
It cannot be baked into a link that someone opens ten minutes later. The
worker mints a fresh one at the moment of connection — which is the entire
reason this needs a worker instead of a URL.

## 🔐 What a link grants

**One VMID, until it expires, and nothing else.** The token in the URL is the
only credential: 24 random bytes, generated per link, and never written to the
audit journal or the server's access log.

| Control | Behaviour |
|---|---|
| Expiry | Set per link; clamped to `[share] max_minutes` (default 8 hours) |
| `--once` | Revoked after the first connection |
| `share revoke --token …` | Kills one link |
| `share revoke --all` | Kills every link |
| `share down` | Stops the worker; **every link dies with it** |
| Lease end | Worker destroyed with everything else |

Sessions live in a file the worker owns; restarting it revokes everything.

## ⚠️ Be clear-eyed about the trust here

**The worker holds a Proxmox API token.** It has to, in order to mint tickets
on demand. Anyone who compromises the worker can open a console on any guest
that token can reach. It is built inside a lease, and destroyed with it — keep
it that way rather than leaving one running indefinitely.

**The URL is the credential.** Anyone who has it, has the console — including
keyboard and mouse. Treat it like a password: don't paste it into a public
issue, and prefer `--once` and short expiries for anything sensitive.

**A quick tunnel gets a new hostname every restart.** Links minted before a
restart stop resolving. That is a feature as much as a nuisance: restarting
the worker revokes everything.

**No tunnel is also fine.** With `tunnel = "none"`, or when the tunnel is
down, links are issued on the worker's LAN address instead and `create`
reports `reachable_from: "lan"`. Most sharing is to someone on the same
network anyway.

## 🔧 Setting it up

Nothing to sign up for. Build the worker inside a lease, then point your config at it:

```bash
proxmox-lab share setup --lease "$L" --vmid 9030 --template <cloud-init-vmid>
```

```toml
[share]
enabled = true
worker_vmid = 9030
tunnel = "cloudflared"   # needs no account; "ngrok" or "none" also work
default_minutes = 30
max_minutes = 480
```

For ngrok instead, set `tunnel = "ngrok"` and store an authtoken with
`proxmox-lab secrets set ngrok-authtoken`. Note that an ngrok **API key** is
not an authtoken; the agent rejects it with `ERR_NGROK_107`.

The worker is small — 1 GB and 2 cores is plenty, since it only shuttles
bytes.

## 🎛️ Using it

```bash
proxmox-lab share status                       # worker, public URL, live links
proxmox-lab share create --lease "$L" --vmid 9001 --minutes 15 --once
proxmox-lab share list                         # tokens shown truncated
proxmox-lab share revoke --lease "$L" --all
proxmox-lab share down --lease "$L"            # cool off; revokes everything
```

`share create` starts the worker if it is not already running.

## 🧪 What is tested

Verified against real hardware: the worker built, a link was minted, and the
RFB handshake came back through the relay **over the public tunnel** — and a
revoked link returned 404 immediately afterwards.

The parts that face the internet also have unit tests: token expiry, single-use
revocation, VMID binding, cross-process visibility of the session store,
WebSocket framing in both directions (a server must not mask its frames, a
client must), and **path traversal on the static assets** — a share link must
never become a way to read files off the worker.
