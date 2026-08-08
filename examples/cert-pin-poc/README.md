# PoC: overriding cert pinning by RAM injection

A worked, reproducible version of the proof-of-concept described in
[../../docs/memflow.md](../../docs/memflow.md#poc-overriding-a-clients-cert-pinning-by-ram-injection).
It shows that when a client pins a certificate — refusing the substitute cert a
man-in-the-middle proxy presents — the decision is ultimately a value or an
instruction in the client's memory, and `memflow` can reach it from the
hypervisor and change it while the client runs.

> **Authorized use only.** This is for security research, driver/protocol work
> and malware triage on **disposable lab guests you control**, the stated
> purpose of the `memflow`/`netcap` features. Do not use it against systems or
> traffic you are not authorized to test.

Two variants, both verified live against a Debian guest:

| File | What it pins | How the override lands |
|---|---|---|
| `pinned_client.py` | a runtime **enforce flag** (data) | `scan` for a marker, `phys-write` flips the flag byte |
| `pincheck.c` | a **compiled** comparison (code) | `scan` for the constant's `movabs`, `phys-write` NOPs the reject branch |

Both use only two new primitives: `memflow scan` (find a byte signature in guest
physical RAM) and `memflow phys-write` (inject bytes at a physical address).

## Prerequisites

- The `[memflow]` SSH channel configured and the host prepared
  (`memflow host-setup`) — see [docs/memflow.md](../../docs/memflow.md).
- A running QEMU **lab guest** and an active lease (`$L` below).
- For the TLS half, a `netcap` MITM proxy — see [docs/netcap.md](../../docs/netcap.md).

## Variant 1 — flag in data (`pinned_client.py`)

The client validates TLS against the system trust store (its "pin"), so through
the MITM proxy it fails with `CERTIFICATE_VERIFY_FAILED`. It keeps a unique
16-byte marker followed by a one-byte enforce flag resident in RAM.

```bash
# 1. stand up the MITM proxy in a disposable LXC and note its IP:port
proxmox-lab netcap mitm-setup --lease "$L" --lxc 9073

# 2. in the guest: point it at the proxy and run the client
#    (PXL_PROXY=http://<proxy_ip>:8080), then watch it log "PINNED enforce=1"

# 3. from the controller: find the enforce flag and flip it
MAGIC=$(python3 -c 'print(b"PXLPIN0POC0MARK!".hex())')
proxmox-lab memflow scan --lease "$L" --vmid <id> --hex "$MAGIC"
# the live buffer is the hit whose byte at +16 reads 01; flip that byte:
proxmox-lab memflow phys-write --lease "$L" --vmid <id> --addr <hit+16> --hex 00 --i-understand
```

The running client flips to `ALLOWED enforce=0`, and `netcap intercept` now
decrypts its HTTPS. The buffer is built as a single copy
(`bytearray(17); buf[:16]=MAGIC; buf[16]=1`) so the marker resolves to exactly
one live candidate — building it by concatenation leaves transient copies that
share the signature.

## Variant 2 — comparison in code (`pincheck.c`)

Closer to how a real app enforces a pin: the check is compiled in.

```bash
gcc -O0 -fno-pic -no-pie -fcf-protection=none -o pincheck pincheck.c
objdump --disassemble=check -M intel pincheck   # note the movabs + the jne offset
./pincheck        # prints "REJECT pin-mismatch"
```

`check()` compares against a pinned 64-bit constant, giving its `movabs` a
unique signature. The reject path is a `jne` a fixed offset later (`+0x16` at
`-O0` here — confirm with `objdump`). Overwrite that `jne` with two `0x90` NOPs
so the check always falls through to accept:

```bash
proxmox-lab memflow scan --lease "$L" --vmid <id> --hex 48b88877665544332211
# for each hit, the jne is at hit + 0x16:
proxmox-lab memflow phys-write --lease "$L" --vmid <id> --addr <hit+0x16> --hex 9090 --i-understand
```

The running process flips from `REJECT` to `ACCEPT` with no restart. Against a
stripped binary you would `scan` for the check's own instruction bytes and
`phys-write` to patch the branch or force the return value — same two
primitives.

## Notes learned running this

- **Physical pages can move** under a running guest — `scan` and `phys-write`
  close together, and `phys-read` to confirm the write landed.
- A marker often appears several times (loaded `.text`, the on-disk binary in
  page cache, source text). Disambiguate by what sits next to it (Variant 1) or
  by patching every code copy (Variant 2 — patching a cache copy is harmless).
- Run each client as a systemd transient unit
  (`systemd-run --unit … -p StandardOutput=append:…`); a guest-agent `exec`
  reaps detached children, so a bare `&` will not persist.
