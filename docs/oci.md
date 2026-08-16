# Experimental OCI application containers

Proxmox VE 9.1 and later can pull a public OCI image from a registry and turn
its root filesystem into an LXC container. `proxmox-lab oci` exposes that path
for short-lived, **trusted Linux application workloads**.

> [!WARNING]
> This is experimental in both Proxmox VE and this project. An OCI container is
> an **unprivileged LXC**, not a QEMU VM: it shares the Proxmox host kernel.
> It has materially weaker isolation than a VM. Never use it for malware,
> unknown images, untrusted build scripts, kernel or driver work, device
> passthrough, or any task requiring a different OS or kernel.

Use a QEMU VM for those cases. This command does not start Docker and does not
support Docker-in-LXC. Proxmox pulls an OCI archive using its native registry
endpoint and converts it to LXC at creation time.

## Requirements and limits

- Proxmox VE **9.1+** with its OCI support and `skopeo` installed on the host.
- A file-based PVE storage with `vztmpl` content for the pulled archive.
- The direct PVE registry endpoint accepts an image **tag**, not an `@sha256`
  digest reference. Tags are mutable. The command therefore requires
  `--allow-mutable-reference`, audits that fact, and reports the limitation.
- Registry credentials are not supported. Pull only public images; never put a
  registry token in a command line or configuration file.
- A pull refuses to overwrite an existing template volume. Inspect and delete
  that exact host-storage object explicitly before replacing it.
- OCI application images often lack `qemu-guest-agent`, SSH, an init system, or
  a usable login. Do not assume `guest run`, VNC, serial automation, snapshots,
  or graphical tooling will work.
- The container is always unprivileged, has no network interface by default,
  does not start on host boot, and is registered to its lease. Lease cleanup
  stops and deletes it like any other disposable LXC.
- OCI creation supports ordinary leases only. Retained long-term workloads need
  a QEMU guest, whose protection and backup lifecycle is fully supported.

## Workflow

Start one ordinary lease and retain it for the complete session:

```bash
L=$(proxmox-lab lease-begin --purpose "run trusted OCI smoke service" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT
```

Pull a public image. Template storage persists outside the lease, so this is a
host storage change and must be explicitly authorized:

```bash
proxmox-lab oci pull --lease "$L" \
  --reference docker.io/library/busybox:1.37.0 \
  --storage local \
  --host-change-authorized \
  --allow-mutable-reference
```

The result prints a template volume such as
`local:vztmpl/busybox_1.37.0.tar`. Create a lease-owned LXC from it:

```bash
proxmox-lab oci create --lease "$L" --vmid 9002 \
  --template local:vztmpl/busybox_1.37.0.tar \
  --rootfs-storage local-lvm --disk-gb 1 --start
```

`--start` is explicit because starting an OCI entrypoint executes image code.
The response repeats the experimental isolation warning. The VMID must be new
to the lease; the command tags and registers the LXC before normal lifecycle
management resumes.

The pulled OCI archive is not deleted at `lease-end`, because it is persistent
host template storage rather than a lease-owned guest. Remove it only when the
user explicitly asks to change host storage; use the Proxmox template UI or a
lease-authorized raw API deletion after verifying the exact volume.

## Appropriate use

Good fits: deterministic public build tools, a short-lived test service, or a
trusted image whose only output is an artifact or network response.

Bad fits: reverse engineering, malicious or unknown software, arbitrary CI
repositories, Windows/Android/other OS work, GUI installation, USB or network
capture, and experiments that need the stronger isolation or device model of a
QEMU guest.
