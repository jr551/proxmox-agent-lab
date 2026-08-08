# Agentless introspection with memflow

This is the most advanced feature in the skill, and the only one that does not
work through the Proxmox API token. Use it to see what a running guest is
*really* doing from underneath it — malware triage, rootkit hunting, an
agent-less or untrusted VM you cannot trust from the inside.

[memflow](https://github.com/memflow/memflow) reads a running guest's memory
directly from the hypervisor and reconstructs the guest OS's own view of
itself. Because the guest OS is never asked, a process hidden from inside the
guest still shows up here.

## Why memflow, and why it is light

memflow's QEMU connector reads guest memory from the `qemu-system` process's
address space via `/proc/<pid>/mem`. There is **no patched kernel, no kernel
module, and no reboot** — which is exactly why it works on a stock Proxmox host
where live LibVMI would need a rebuilt hypervisor kernel. The guest-OS layer is
`memflow-win32`, so **process introspection is fully supported for Windows
guests**; raw memory access works for any guest, and Linux OS support is
best-effort.

## Why it is different, and off by default

memflow must run **resident on the hypervisor**, as root, to read that `/proc`
memory. It cannot be driven through the scoped API token the rest of this tool
uses, so it reaches the host over **SSH** — a deliberately separate trust
boundary. Nothing happens until you:

1. set `[memflow] enabled = true` and `ssh_host` (plus `ssh_user`, `ssh_key` as
   needed) in your config;
2. prepare the host with `memflow host-setup`;
3. confirm with `memflow doctor`.

The SSH key is referenced by **file path only** — no key material ever reaches
a command line or the audit ledger. Guest memory can contain anything, so the
ledger records only that a read happened (`memflow-processes`), with a count,
never the process list itself.

## Configure

```toml
[memflow]
enabled  = true
ssh_host = "192.168.1.50"     # the Proxmox host; memflow runs there
ssh_user = "root"             # needs to read /proc/<qemu-pid>/mem
ssh_key  = "~/.ssh/pxl_vmi"   # a key path, never the key itself
```

## Prepare the host

Installing the stack is a host change, gated like every other one. Review it
first (printing changes nothing, so it needs no authorisation):

```bash
proxmox-lab memflow host-setup --print
```

Then run it:

```bash
proxmox-lab memflow host-setup --host-change-authorized
```

That installs a Rust toolchain (if absent), builds the `pxl-memflow` tool
(memflow + memflow-qemu + memflow-win32), and installs it with the
`pxl-memflow-run` helper. No kernel changes, no reboot.

## Prove the host is ready

```bash
proxmox-lab memflow doctor                     # SSH, tool, /proc access
proxmox-lab memflow doctor --vmid 9040         # ...and that this guest reads
```

`doctor` reports each layer as `ok`/`false`. A layer it cannot check is left
unproven — never counted as a pass. If it is not fully healthy it says which
layer failed and stops.

## Introspect a running guest

The read happens inside a lease and requires the guest to be a **running** QEMU
VM (memflow reads live memory):

```bash
proxmox-lab memflow processes --lease "$L" --vmid 9040
```

Compare this against what the guest reports internally
(`guest run --vmid 9040 tasklist` on Windows): a process visible from
underneath but hidden inside the guest is the classic rootkit signature.

## Read memory and registers

```bash
proxmox-lab memflow read --lease "$L" --vmid 9040 \
  --addr 0xfffff80000000000 --len 64        # raw kernel memory, as hex
proxmox-lab memflow registers --lease "$L" --vmid 9040   # vCPU registers
```

`read` returns bytes from the guest's kernel virtual address space. `registers`
reports the live vCPU state (RIP, RSP, CR3, …) via the QEMU monitor — memflow's
`/proc` connector sees RAM only, so the register set comes from QEMU, over the
same SSH channel.

## Write memory (dangerous)

```bash
proxmox-lab memflow write --lease "$L" --vmid 9040 \
  --addr 0x... --hex 9090 --i-understand
```

This patches the **live** memory of a running kernel; the wrong byte crashes or
silently compromises the guest. It is hard-gated: without `--i-understand` it
refuses. The bytes written are never recorded in the audit ledger (only the
address and length are). Use it on disposable guests, and expect to rebuild the
guest afterward.

## Physical RAM: scan and inject (any guest OS)

`read`/`write` above walk the **Windows kernel's** virtual address space. The
physical commands go straight through the QEMU connector to **guest-physical
RAM**, with no OS layer — so they work on **any guest**, Linux included, and can
reach *userspace* memory that the kernel-VA path cannot.

```bash
# find a byte signature anywhere in guest RAM (a marker, a constant, a pattern):
proxmox-lab memflow scan --lease "$L" --vmid 9072 --hex 50584c... --max-hits 16
# read / inject at a physical address:
proxmox-lab memflow phys-read  --lease "$L" --vmid 9072 --addr 0x107ce2a50 --len 32
proxmox-lab memflow phys-write --lease "$L" --vmid 9072 --addr 0x107ce2a60 --hex 00 --i-understand
```

`scan` sweeps the whole guest RAM for the needle and returns the physical
addresses of the matches; a unique needle usually resolves to a single hit.
`phys-write` is **RAM injection** — like `write` it is hard-gated behind
`--i-understand`, and the bytes are never audited (only the address and length
are). Note that physical pages can move under a running guest, so scan and
inject close together, and re-`phys-read` to confirm the write landed.

### PoC: overriding a client's cert pinning by RAM injection

A pinned client refuses a man-in-the-middle proxy because the intercepted TLS
cert is not the one it pinned — which is exactly what defeats
[`netcap intercept`](netcap.md). Because the pin decision is a value in the
client's memory, RAM injection can flip it. End to end, on a disposable Linux
guest:

1. The client enforces its pin (validates TLS against its own trust set), so
   through the MITM proxy it fails with `CERTIFICATE_VERIFY_FAILED` — nothing is
   intercepted.
2. `memflow scan` locates the client's in-memory *enforce* flag by a unique
   marker next to it.
3. `memflow phys-write` flips the flag in the live guest's RAM.
4. The still-running client now skips the pin check, the MITM handshake
   succeeds, and `netcap intercept` captures the previously-unreadable HTTPS in
   plaintext.

This was verified live: `scan` found the flag, `phys-write` flipped it, the
running process observed the change (its log went from `PINNED enforce=1` to
`ALLOWED enforce=0`), and the proxy then decrypted its traffic.

The same two primitives patch a pin check that lives in **compiled code** rather
than a data flag — which is how a real app enforces it. Also verified live: a
C program whose `check()` compares against a pinned constant and refuses on
mismatch. `scan` for the constant's unique `movabs` signature locates the
function in RAM; the reject branch is a `jne` a fixed offset later; `phys-write`
overwrites that `jne` with two `0x90` NOPs so the check always falls through to
accept. The running process flipped from `REJECT` to `ACCEPT` with no restart —
exactly the technique used against a stripped binary, where you would `scan` for
the check's instruction bytes and `phys-write` to patch the branch or force the
return value.

These are **proofs-of-concept** on controlled lab guests. Only ever do this to
guests you control, for authorized work. The runnable sources for both variants,
with the exact command sequence, are in
[examples/cert-pin-poc/](../examples/cert-pin-poc/).

## Extract code for offline analysis

```bash
proxmox-lab memflow dump --lease "$L" --vmid 9040 \
  --addr 0xfffff806df89d73e --len 4096 --out ./code.bin
```

Writes the raw bytes to a local file, ready to disassemble or load into a
decompiler. This is the extraction half of the Ghidra pipeline (analysis in a
disposable LXC is the follow-on `analyze` step).

## Step through code

memflow's `/proc` connector sees RAM only, so live control flow uses QEMU's
built-in **gdbstub** (no patched kernel). `host-setup` installs a small GDB
remote client (`pxl-gdb`) and capstone; the tool enables the stub on demand and
finds it again on later runs.

```bash
proxmox-lab memflow trace --lease "$L" --vmid 9040 --steps 20        # step into
proxmox-lab memflow trace --lease "$L" --vmid 9040 --steps 20 --over # step over calls
proxmox-lab memflow break --lease "$L" --vmid 9040 --addr 0x... --timeout 15
```

`trace` single-steps and returns each instruction disassembled; `--over` sets a
temporary breakpoint past a `call` so subroutines are skipped. `break` sets a
breakpoint, continues, and reports where the guest stopped — best-effort, so if
the address is not reached within `--timeout` it reports `hit: false` rather
than blocking. Attaching pauses the guest only for the operation; it resumes on
detach.

## Analyse code in Ghidra

For deeper analysis, dump a region and disassemble/decompile it with Ghidra
headless in a **disposable LXC** — so the ~1 GB toolchain never touches the
hypervisor and is destroyed with the lease.

```bash
proxmox-lab memflow ghidra-setup --lease "$L" --lxc 9041          # once per lease
proxmox-lab memflow analyze --lease "$L" --vmid 9040 --lxc 9041 \
  --addr 0xfffff802e609d420 --len 4096
```

`ghidra-setup` creates the container if needed, installs a JDK 21 + the latest
Ghidra + the export script, and registers the LXC to the lease (it is
idempotent — safe to re-run). `analyze` reads the region from the target guest,
loads it into Ghidra with the address as its load base, runs `analyzeHeadless`,
and returns the recovered functions and disassembly as JSON. A Ghidra failure
is raised, never reported as an empty success.

## Guest-OS support

memflow-win32 gives full process/module introspection for **Windows** guests.
Raw physical-memory access works for any guest OS, but turning that into a
process list on **Linux** guests is memflow's weaker, best-effort path — treat
Linux process listings as unproven until you have corroborated them.

## When it will not work

`doctor` is honest about missing layers rather than pretending. If the tool is
not installed, the guest is not running, or the QEMU process cannot be read,
it says so and stops — it never reports an unproven read as a success.

## How it fits together

```
controller (laptop) --ssh--> Proxmox host: pxl-memflow-run --> pxl-memflow
                                                                   |
                                                     memflow-qemu reads /proc/<qemu-pid>/mem
                                                                   |
                                                     memflow-win32 parses the guest kernel
                                                                   |
                                                          the guest's live process list
```

Proxmox launches QEMU directly rather than through libvirt, so `pxl-memflow-run`
addresses a guest by the QEMU pid Proxmox records for it
(`/var/run/qemu-server/<vmid>.pid`), which memflow-qemu accepts as its target.
Every memflow call lives in the one `pxl-memflow` binary, so a future memflow
release is a one-place rebuild on the host and the controller side stays a
stable JSON contract.
