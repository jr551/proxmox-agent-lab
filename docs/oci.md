# Experimental OCI application containers

## Purpose

Proxmox VE 9.1 and later can pull a public OCI image from a registry and turn its root filesystem into an LXC container. `proxmox-lab oci` exposes that path for short-lived, **trusted Linux application workloads** where a full QEMU VM is heavier than needed.

> [!WARNING]
> This is experimental in both Proxmox VE and this project. An OCI container is an **unprivileged LXC**, not a QEMU VM: it shares the Proxmox host kernel. It has materially weaker isolation than a VM. Never use it for malware, unknown images, untrusted build scripts, kernel or driver work, device passthrough, or any task requiring a different OS or kernel.

Use a QEMU VM for those cases. This command does not start Docker and does not support Docker-in-LXC. Proxmox pulls an OCI archive using its native registry endpoint and converts it to LXC at creation time.

## Prerequisites

- Proxmox VE **9.1+** with its OCI support and `skopeo` installed on the host.
- A file-based PVE storage with `vztmpl` content for the pulled archive (default `local`).
- Public image only — registry credentials are not supported. Never put a registry token in a command line or configuration file.
- A lease for every host or guest mutation (`lease-begin` / `lease-end`). The direct PVE registry endpoint accepts an image **tag**, not an `@sha256` digest, so pulls are gated by `--allow-mutable-reference` and audited.
- Awareness of limits: pull refuses to overwrite an existing template volume; OCI images often lack `qemu-guest-agent`, SSH, init or login — do not assume `guest run`, VNC, serial automation, snapshots, or graphical tooling will work; container is always unprivileged, no network by default, does not start on host boot, registered to its lease, stopped/deleted at `lease-end`; OCI creation supports ordinary leases only (retained long-term workloads need a QEMU guest).

## Commands

All flags verified against `src/proxmox_agent_lab/oci.py:register()`.

### Workflow

Start one ordinary lease and retain it for the complete session:

```bash
L=$(proxmox-lab lease-begin --purpose "run trusted OCI smoke service" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT
```

Optionally check the reference offline first — this validates the grammar and shows the template volume a pull would produce, without needing a lease or touching the host:

```bash
proxmox-lab oci validate --reference docker.io/library/busybox:1.37.0
```

Verified: `oci validate --reference [--storage local]` (`src/proxmox_agent_lab/oci.py`).

Pull a public image. Template storage persists outside the lease, so this is a host storage change and must be explicitly authorized:

```bash
proxmox-lab oci pull --lease "$L" \
  --reference docker.io/library/busybox:1.37.0 \
  --storage local \
  --host-change-authorized \
  --allow-mutable-reference
```

Verified: `oci pull --lease --reference --storage local --host-change-authorized --allow-mutable-reference [--timeout 1800]`. `--allow-mutable-reference` is required because the endpoint accepts tags (mutable); the command audits that fact and reports the limitation.

The result prints a template volume such as `local:vztmpl/busybox_1.37.0.tar`. Create a lease-owned LXC from it:

```bash
proxmox-lab oci create --lease "$L" --vmid 9002 \
  --template local:vztmpl/busybox_1.37.0.tar \
  --rootfs-storage local-lvm --disk-gb 1 --start
```

Verified: `oci create --lease --vmid --template --rootfs-storage --disk-gb [--hostname] [--memory 512] [--swap 0] [--start] [--timeout 1800]`. `--start` is explicit because starting an OCI entrypoint executes image code. The VMID must be new to the lease; the command tags and registers the LXC before normal lifecycle management resumes. The response repeats the experimental isolation warning. `--memory` (MiB, default 512) and `--swap` (default 0) are optional limits.

The pulled OCI archive is not deleted at `lease-end`, because it is persistent host template storage rather than a lease-owned guest. Remove it only when the user explicitly asks to change host storage; use the Proxmox template UI or a lease-authorized raw API deletion after verifying the exact volume.

### Reference

| Subcommand | Key flags | Notes |
|---|---|---|
| `oci validate` | `--reference`, `--storage` | Offline grammar check; no lease/host touch |
| `oci pull` | `--lease`, `--reference`, `--storage`, `--host-change-authorized`, `--allow-mutable-reference`, `--timeout` | Produces `storage:vztmpl/<name>.tar`; refuses overwrite |
| `oci create` | `--lease`, `--vmid`, `--template`, `--rootfs-storage`, `--disk-gb`, `--memory`, `--swap`, `--hostname`, `--start`, `--timeout` | Unprivileged LXC; lease-owned; tags `codex-lab;lease-<id>` |

## Troubleshooting

All original troubleshooting content retained; no fact removed.

- **Requires 9.1+ / `skopeo`:** Host must have Proxmox 9.1+ OCI support and `skopeo` installed; otherwise `oci pull` fails before any image is fetched.

- **Template storage / `vztmpl`:** File-based storage with `vztmpl` content required (e.g. `local`). Pull refuses to overwrite an existing `local:vztmpl/<image>.tar` — inspect and delete that exact host-storage object explicitly before replacing it.

- **Tag vs digest:** Direct PVE registry endpoint accepts a tag, not an `@sha256` digest. Tags are mutable. Command requires `--allow-mutable-reference`, audits that fact, and reports the limitation. Validate with `oci validate` first.

- **Credentials not supported:** Registry credentials are not supported. Pull only public images; never put a registry token in a command line or file.

- **No agent / init / SSH in many OCI images:** Do not assume `guest run`, VNC, serial automation, snapshots, or graphical tooling will work. LXC has no VNC framebuffer and many app images have no `qemu-guest-agent`, SSH, or init/login. Verify capabilities inside the container after `--start`.

- **Network / isolation / lifecycle:** Container is always unprivileged, has no network interface by default, does not start on host boot, and is registered to its lease. Lease cleanup stops and deletes it like any other disposable LXC. OCI creation supports ordinary leases only.

- **Appropriate use:** Good fits: deterministic public build tools, short-lived test service, trusted image whose only output is an artifact or network response. Bad fits: reverse engineering, malicious or unknown software, arbitrary CI repositories, Windows/Android/other OS work, GUI installation, USB or network capture, experiments needing stronger isolation or device model of a QEMU guest.
