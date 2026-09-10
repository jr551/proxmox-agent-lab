# Portable I/O workloads

`proxmox-lab io-workload` records and replays a small, deterministic workload
against one explicitly named regular scratch file.  It is useful when a guest
fails while doing ordinary file I/O and a later clean guest needs to repeat the
same ordered reads, writes, and flushes.

It records the runner's own operations.  It does **not** transparently capture
arbitrary application, filesystem, driver, or kernel I/O, and it does not
attempt to reproduce scheduler races or wall-clock timing.  A trace is portable
because it contains no hostnames, guest IDs, filesystem paths, or raw payloads.
It stores offsets, lengths, deterministic payload seeds, and SHA-256 checksums.

The feature is guest/OS neutral.  With the package installed in a Python 3.11+
guest, the same commands are available as `python -m
proxmox_agent_lab.ioworkload`; use the existing lease-owned guest channels to
copy or invoke that installed runner.  The host CLI does not silently deploy to
or execute in a guest.

## Record a workload

Choose a new absolute trace path and a new absolute scratch-file path in a
directory dedicated to the disposable guest.  The command creates both with
exclusive creation: it never replaces an existing file.

```bash
proxmox-lab io-workload record \
  --trace /work/case-17.trace.jsonl \
  --scratch /work/case-17.io-scratch \
  --seed 701 --file-size $((16 * 1024 * 1024)) \
  --operations 300 --block-size $((64 * 1024))
```

The scratch file starts as a zero-filled file.  Its size and complete initial
SHA-256 hash are written into the header before operations begin.  Before each
operation, the runner appends and fsyncs an `intent` record.  It then appends a
successful or failed `outcome` record.  A guest crash can therefore leave one
last intent without an outcome; that is intentional evidence of an in-flight
operation.  A normal I/O error is retained in the trace and reported as a
failed capture.

The limits are deliberately conservative: 64 MiB scratch file, 8 MiB per
operation, 100,000 operations, 1 GiB planned I/O, one hour, and 128 MiB trace
input.  These bounds protect replay from accidental or malicious input.

`generate` writes the same deterministic intent-only plan without opening a
scratch file.  It can be useful to make a planned workload first:

```bash
proxmox-lab io-workload generate --trace /work/planned.trace.jsonl \
  --seed 701 --file-size 16777216 --operations 300 --block-size 65536
```

## Inspect and replay

Validate an evidence file without touching a scratch file:

```bash
proxmox-lab io-workload analyze --trace /work/case-17.trace.jsonl
```

Replay always needs a separate result path and a positive acknowledgement that
the supplied scratch file is disposable.  The safest form creates a brand-new
target from the recorded zero-filled baseline:

```bash
proxmox-lab io-workload replay \
  --trace /work/case-17.trace.jsonl \
  --scratch /work/replay-17.io-scratch --create-scratch \
  --result /work/replay-17.result.json --confirm-disposable
```

For an existing scratch file, omit `--create-scratch`; replay first requires
its size and hash to exactly match the trace's initial state.  This prevents a
trace from depending on unseen prior bytes.  It still requires
`--confirm-disposable`, because a matching scratch file will be modified.

The input trace is opened read-only and replay results use create-exclusive
output, so recording evidence cannot be overwritten by a replay.  Replay
refuses symlinks, devices, raw disks, relative/traversal paths, unknown record
types, malformed checksums, unbounded fields, and operations outside the
declared scratch-file size.  Each captured read checksum is compared during
replay; a difference produces a failed replay result rather than claiming a
match.

Use an ordinary regular file on a lease-owned disposable guest volume.  This
tool deliberately has no raw-disk mode and does not make any claim about real
hardware validation merely from its offline replay tests.
