# Agentless introspection with memflow

Read a running qemu guest from the hypervisor. The helper maps the guest's
qemu process and reads `/proc/<pid>/mem`. Nothing is installed in the guest,
and nothing here uses a second ssh target: `[ssh] target` is already root on
this host.

The commands are CLI-only. They are not MCP tools.

## What has to be true first

1. The guest is qemu, running, and registered to your lease. An lxc, a
   stopped guest, or a guest another lease owns is refused before the helper
   is invoked.
2. The helper is installed. `memflow doctor` says whether it is.

```bash
proxmox-lab memflow host-setup --print
proxmox-lab memflow host-setup --host-change-authorized
proxmox-lab memflow doctor
proxmox-lab memflow doctor --lease "$L" --vmid 9001
```

`host-setup` copies one script to `/usr/local/sbin/pxl-memflow-setup` and
runs it. That script installs a Rust toolchain if the host does not have
one, builds `pxl-memflow`, and installs `/usr/local/bin/pxl-memflow-run`
plus a small gdbstub client for `trace` and `break`. No kernel module, no
reboot. `--print` only shows the script.

## Commands

| Command | Gate |
|---|---|
| `memflow doctor [--lease L --vmid N]` | `--lease` is required with `--vmid` |
| `memflow host-setup [--print] [--host-change-authorized]` | host change, except `--print` |
| `memflow processes --lease L --vmid N` | lease-owned running qemu |
| `memflow read --lease L --vmid N --addr ADDR [--len 64]` | same; `--len` is 1..16MiB |
| `memflow phys-read --lease L --vmid N --addr ADDR [--len 64]` | physical RAM, any guest OS |
| `memflow registers --lease L --vmid N` | vCPU registers via `qm monitor` |
| `memflow scan --lease L --vmid N --hex HEX [--max-hits 8]` | physical RAM; needle at most 256 bytes |
| `memflow dump --lease L --vmid N --addr ADDR --len N --out FILE` | local file, kernel virtual memory |
| `memflow trace --lease L --vmid N [--steps 10] [--over]` | pauses the guest for the trace, then resumes |
| `memflow break --lease L --vmid N --addr ADDR [--timeout 15]` | `hit: false` means the address was not reached |
| `memflow boot-diagnose --lease L --vmid N` | read-only; wedged vs still executing, plus boot-failure text |
| `memflow write` / `memflow phys-write` | `--i-understand` required; payload at most 64KiB |

Windows process listings go through memflow-win32. Linux process listings
are best-effort: treat them as unproven until something inside the guest
agrees. Physical reads do not depend on the guest OS.

## What is recorded

The journal stores the fact of the operation: lease, vmid, address, length,
hit counts, CPU state, failure category. It does not store bytes, process
names, register dumps, or the text a scan matched.

`write` and `phys-write` are refused without `--i-understand`, and the ssh
seam refuses those helper subcommands unless that flag was passed. Use them
only when the user has asked to patch that guest's RAM.

## When it will not work

`doctor` reports each layer it could check. A missing helper, a guest that
is not running, or a guest the lease does not own stops the command. An
unproven layer is not reported as success.
