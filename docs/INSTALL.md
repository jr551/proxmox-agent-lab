# Installation

Two machines are involved: the **controller** (this tool, on your laptop or
any always-on box) and the **lab host** (a spare PC running Proxmox).
Everything the controller does travels over one ssh connection as root —
there is no other credential, service, or listener to set up.

**Prerequisites:**

- **Controller:** Python 3.11+ and `pip`.
- **Lab host:** Proxmox VE 8 or 9 on a wired connection with Wake-on-LAN
  (Wi-Fi cannot wake a machine — use the wired port).
- **You:** root ssh access to the host, which you are about to set up.

Budget about an hour, most of it waiting for the Proxmox installer.

---

## 1. Install Proxmox on the spare PC

Download **Proxmox VE 8 or 9** from
[proxmox.com/downloads](https://www.proxmox.com/en/downloads), write it to a
USB stick, and install. Two choices matter:

- **Hostname.** Whatever you pick becomes the *node name* you put in the
  config. `pve` is the default; the short name is what you want, not the FQDN.
- **Network.** Give the machine a **static IP**, or a DHCP reservation. If its
  address moves, nothing here can find it.

## 2. Prepare the machine to be woken and shut down

**Enable Wake-on-LAN in the BIOS.** Reboot into firmware setup and look for
*Wake on LAN*, *Power On By PCI-E*, or *Resume by LAN* — usually under Power
Management. Turn it on.

**Note the MAC address** of the wired NIC. On the Proxmox console:

```sh
ip -br link show
```

Take the MAC of the interface with your LAN IP — usually `enp*` or `eno*`, not
`vmbr0`.

**Check WoL is armed on the NIC.** Some cards need it enabled per boot:

```sh
apt install -y ethtool
ethtool <interface> | grep Wake-on      # want: Wake-on: g
```

If it says `Wake-on: d`, arm it and make it stick:

```sh
ethtool -s <interface> wol g
printf '#!/bin/sh\nethtool -s %s wol g\n' <interface> > /etc/network/if-up.d/wol
chmod +x /etc/network/if-up.d/wol
```

## 3. Trust your controller on the host

Copy your public key to the host once, as root:

```sh
ssh-copy-id root@proxmox
```

`proxmox` is just a name — use your ssh alias, hostname, or IP. If you want an
alias, put it in `~/.ssh/config` first:

```
Host proxmox
    HostName 192.0.2.10
    User root
```

Prove it works with no prompts (this is exactly how the tool will connect):

```sh
ssh -o BatchMode=yes root@proxmox true && echo ok
```

## 4. Install the controller and create the config

```sh
python -m pip install proxmox-agent-lab
proxmox-lab init
```

`init` writes a starter file to
`~/.config/proxmox-agent-lab/config.toml` (use `--path` to choose another
location, `--force` to overwrite). Edit it and set the four values that are
site-specific:

```toml
[ssh]
target = "proxmox"           # the alias/host from step 3

[pve]
node = "pve"                 # the hostname you chose in step 1
template_vmid = 100          # a template to clone guests from

[power]
mac = "aa:bb:cc:dd:ee:ff"    # the MAC you noted in step 2
```

Every key, default and search order is documented in
[CONFIGURATION.md](CONFIGURATION.md).

## 5. Run the doctor

```sh
proxmox-lab doctor
```

`doctor` checks the install end to end and prints a JSON report; it exits
non-zero when any check fails. It works even when the config is missing or
broken — that is the point. What it checks:

1. **Python >= 3.11** — fail on older interpreters.
2. **Config exists and parses** — fail on missing/unparseable; the file it
   looked for is named either way.
3. **ssh as root** to `[ssh] target` — fail on refusal or timeout, with the
   `ssh-copy-id root@<target>` hint.
4. **Remote tooling** — `qm`, `pct`, `pvesh`, `pveversion` present on the
   host; fail if any is missing.
5. **Node identity** — `[pve] node` matches the host's `hostname -s`; warn on
   mismatch.
6. **State** — `[state] dir` writable and `lab.db` opens with the expected
   schema version; fail.
7. **Template** — `[pve] template_vmid` resolves; warn if absent.
8. **Wake-on-LAN MAC** — `[power].mac` set; warn if empty.
9. **GC crontab** — info if the host-side garbage collector is not installed
   (step 6 is optional).
10. **Drift** — pxl-tagged guests whose description lease/expiry metadata
    disagrees with `lab.db`; warn per drifted guest.

Fix what it flags, re-run until it prints `"ok": true`.

## 6. Optional: install the lease garbage collector

The GC is the host's own safety net: if the controller disappears mid-lease,
the host still cleans up after itself and powers off.

```sh
proxmox-lab gc install --host-change-authorized
```

That copies the bundled standalone script to `/usr/local/sbin/pxl-gc`
(mode 0755), creates `/var/lib/pxl-gc`, and injects exactly one root crontab
line, marked for idempotent detection:

```
# pxl-gc
*/10 * * * * /usr/local/sbin/pxl-gc >>/var/log/pxl-gc.log 2>&1
```

On the host side the script is stateless and reads no controller database:
every ten minutes it walks the guests and reads only their metadata — the
`pxl` tag plus the `pxl-lease=` / `pxl-expiry=` line in the description.
A guest whose lease expired is stopped and destroyed; anything without pxl
metadata, any template, and any long-term guest (`pxl-expiry=0`) is never
touched, and metadata that does not parse is warned about and skipped, never
deleted on. When zero guests are running and zero unexpired pxl guests exist
on two consecutive runs at least ten minutes apart, the host shuts itself
down. `--host-change-authorized` is required for install and uninstall;
check what is in place, read-only, with:

```sh
proxmox-lab gc status
```

Remove it again (also host-changing):

```sh
proxmox-lab gc uninstall --host-change-authorized
```

## 7. Where state lives

One file: `<state dir>/lab.db` — SQLite, right where `[state] dir` points
(default `~/.local/share/proxmox-agent-lab`). It holds the leases, the
resources registered to them, and the journal. `$PROXMOX_AGENT_LAB_STATE`
points a throwaway run somewhere else. Nothing is written inside the
installed package.

## 8. First run

The lab is idle until you take a lease. Copy the canonical lease shape from
[../SKILL.md](../SKILL.md) — every task begins with `lease-begin` and ends
with `lease-end`, and that is what guarantees the host cleans up and powers
itself off. Read the journal for what happened:

```sh
proxmox-lab journal --limit 20
```

When something misbehaves: [troubleshooting.md](troubleshooting.md) first,
then [safety-policy.md](safety-policy.md) for the rules the code enforces and
[VERIFICATION.md](VERIFICATION.md) for what has actually been tested.
