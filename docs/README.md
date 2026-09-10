# Documentation index

Use this page to find the right guide for what you are doing.

## Install and configure

- **[INSTALL.md](INSTALL.md)** — blank PC → working lab: Proxmox install,
  Wake-on-LAN, API token, first `proxmox-lab doctor`, watchdog.
- **[CONFIGURATION.md](CONFIGURATION.md)** — every TOML setting, the secrets
  backend, and the lookup order.

## Run your first disposable guest

Start with the canonical lease shape in **[SKILL.md](../SKILL.md)**:

```bash
L=$(proxmox-lab lease-begin --purpose "first run" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT
```

Then follow one of:

- **[RECIPES.md](RECIPES.md)** — copy-paste recipes for browsing, building,
  testing, VPN egress, Android, Windows, and more.
- **[AGENTS.md](AGENTS.md)** — how an agent should choose channels, read
  screens, and avoid common traps.

## Operate a feature

- **[console.md](console.md)** — screenshots, text, keys, clicks, serial, cloud
  vision.
- **[storage.md](storage.md)** — disks, node storage, cloud images, S3, file
  transfer (`push` / `pull`).
- **[network.md](network.md)** — forced VPN egress, gateway build, leak testing.
- **[windows.md](windows.md)** — install Windows Server.
- **[android.md](android.md)** — emulated Android devices.
- **[memflow.md](memflow.md)** — agentless guest memory introspection and
  debugging.
- **[usb.md](usb.md)** — USB passthrough and traffic capture.
- **[netcap.md](netcap.md)** — guest network capture, SSL inspection and MITM.
- **[disk.md](disk.md)** — offline disk repair and inspection.
- **[share.md](share.md)** — disposable console links.
- **[oci.md](oci.md)** — experimental OCI application LXC.
- **[gui-installers.md](gui-installers.md)** — bounded loop for driving installers.
- **[onboarding.md](onboarding.md)** — experimental ISO/VPS onboarding.

- **[commands.md](commands.md)** — generated map of every
  `proxmox-lab` subcommand (regenerate with `scripts/gen-commands.py`).

## Troubleshoot

- **[troubleshooting.md](troubleshooting.md)** — symptom → diagnostic command →
  next decision. Start here before blaming the tool.
- **[VERIFICATION.md](VERIFICATION.md)** — what has been run on real hardware and
  what is unit-tested only.

## Advanced and persistent workloads

- **[long-term-leases.md](long-term-leases.md)** — machines that stay on,
  weekly backups, `lease-destroy` and `lease-release`.
- **[safety-policy.md](safety-policy.md)** — the rules the code enforces.
- **[pe.md](pe.md)** — boot and customise a user-supplied Windows PE ISO.
- **[reactos.md](reactos.md)** — debugging ReactOS over serial and KDB.
- **[macos.md](macos.md)** — macOS guests via OSX-PROXMOX.

## Contribute

- **[architecture.md](architecture.md)** — module map, dependency direction,
  the `register(sub, lab)` callback contract, and the compatibility-alias
  policy.
- **[CONTRIBUTING.md](../CONTRIBUTING.md)** — development setup, required
  checks, and release checklist.
- **[AGENTS.md](../AGENTS.md)** (repository root) — architecture, conventions,
  and important files for contributors.
- **[AUDIT-2026-08-24.md](AUDIT-2026-08-24.md)** — historical security review.

- [Share a lab connection](connections.md): pasteable dev-machine setup and optional host Tailscale.
- [VirtIO queue inspection](virtio-queues.md): read-only virtqueue sampling through the QEMU monitor.
- [Portable I/O workloads](io-workloads.md): record and replay bounded scratch-file I/O traces.
- [Offline crash reports](crash-reports.md): build-pinned address symbolization with a local llvm-symbolizer.
- [Host hardware inspection](host-info.md): temperature sensors and interface MACs over the opt-in SSH channel.
