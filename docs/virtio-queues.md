# VirtIO queue inspection

`virtio queues` samples one QEMU virtqueue through the Proxmox monitor API. It
does not use SSH, guest code, or a second QMP transport, and every monitor
request is a read-only `info` command.

```text
proxmox-lab virtio queues \
  --vmid 9001 \
  --path /machine/peripheral/virtio-net-0 \
  --queue 0 \
  --samples 4 \
  --interval 1 \
  --deadline 15 \
  --ring-format split
```

The path and queue are required. Samples, interval, and deadline are bounded
by the command. `--timeout` is accepted as an alias for `--deadline`.
`--element-index N` adds one `info virtio-queue-element PATH QUEUE N` request;
QEMU versions without that HMP command report the element as unavailable.
Element indexes and queue indexes are limited to 65535.

Each result keeps the exact monitor response in every `snapshots[].raw` value.
Recognized fields from `hmp_virtio_queue_status` are exposed in
`snapshots[].parsed.fields`, including `inuse`, `used_idx`,
`signalled_used`, `signalled_used_valid`, `last_avail_idx`,
`shadow_avail_idx`, and the VRing addresses and sizes. New or unrecognised
fields remain under `unknown_fields` or `unknown_lines`; a transcript with no
recognized fields is reported as `status: unavailable`.

Use `--ring-format split` when the queue is a split virtqueue. Split-ring
indices are compared as 16-bit counters, so a plausible high-to-low wrap is
reported with `wrapped: true`. A backwards move that is not a plausible wrap
is reported as a reset and produces no delta. `--ring-format packed` disables
split-ring counter arithmetic and reports progress interpretation as
unavailable. `auto` only selects a format when the transcript explicitly
identifies one; it refuses to guess from fields that may have different packed
semantics.

`interpretation.stalled_candidate` is `true` only when split-ring progress
fields remain unchanged while `inuse` stays non-zero across the samples. This
is a candidate for further investigation, not proof of a hang. A stopped or
unsupported counter, an unknown ring format, a reset, or a missing `inuse`
field yields `null` and an explanation. Queue status does not expose whether
the guest driver reclaimed completions, so the result never claims that it
did.

This feature has offline unit-test coverage only. No real Proxmox host or
QEMU monitor was contacted during development; HMP output varies by QEMU
version and the raw transcript is the evidence to use when a field is
unavailable.
