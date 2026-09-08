# Storage: physical disks, node storage, and guest file transfer

## Purpose

Physical disks, node storage classes, reclaimed images, verified cloud-image
fetches, S3 scratch space, and guest file transfer — one place for every
host-side storage operation, with destructive actions gated and lease-audited.

## Prerequisites

- An active lease for `add-disk`, `set-content`, `gc --delete`, `download-url`,
  `push`, `pull`, and `backup`.
- `Sys.Audit` and `Sys.Modify` on `/nodes/pve` for `storage list-disks` and
  `storage add-disk`. Perform one-time disk setup as root if the least-privilege
  lab token lacks these.
- S3 credentials stored in the configured secret backend (`s3-key-id` and
  `s3-secret-key`) for scratch-bucket operations.
- A slow bulk disk is fine for ISOs and cold images; keep running guest disks on
  `local-lvm`.

## Minimal example

```bash
L=$(proxmox-lab lease-begin --purpose "storage demo" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
trap 'proxmox-lab lease-end --lease "$L"' EXIT

proxmox-lab storage status
proxmox-lab storage list-disks
proxmox-lab s3 health
proxmox-lab s3 list --prefix screens/

# move a file into and out of a guest
proxmox-lab push --lease "$L" --vmid 9001 --file ./payload.tar.gz \
  --dest /tmp/payload.tar.gz
proxmox-lab pull --lease "$L" --vmid 9001 \
  --remote /var/log/cloud-init.log --out ./cloud-init.log
```

## Expected result

`storage status` reports every storage and whether it is `fast` or `bulk`:

```json
{
  "storages": {
    "local-lvm": {"class": "fast", "free_gb": 450.2},
    "usb-bulk":  {"class": "bulk", "free_gb": 900.1}
  }
}
```

`push`/`pull` return the transferred path and an SHA-256 or byte verification.
Credentials are never sent into the guest; transfers use short-lived presigned
URLs.

`storage gc` (without `--delete`) reports `orphaned_provisioned_gb` and
`orphaned_on_disk_gb`. Only `orphaned_on_disk_gb` is actually reclaimed.

## Commands

| Command | Key flags | Notes |
|---|---|---|
| `storage status` | — | reports `class` (`fast`/`bulk`) and free space |
| `storage list-disks` | — | physical disks; needs `Sys.Audit`+`Sys.Modify` or returns 403 |
| `storage add-disk` | `--lease`, `--device`, `--name`, `--expect-serial`, `--expect-size-gb`, `--host-change-authorized`, `--wipe-confirmed`, `--filesystem` | formats the disk |
| `storage set-content` | `--lease`, `--name`, `--content`, `--host-change-authorized` | fix content types without re-formatting |
| `storage gc` | `--delete`, `--host-change-authorized`, `--dry-run` | reports by default, deletes only with `--delete` |
| `storage download-url` | `--lease`, `--url`, `--filename`, `--storage`, `--content`, `--checksum`, `--allow-unverified` | node-side download, checksum required |
| `s3 health` / `s3 list` / `s3 put` / `s3 get` / `s3 presign` / `s3 delete` | `--prefix`, `--file`, `--key`, `--method`, `--expires` | scratch bucket ops |
| `push` / `pull` | `--lease`, `--vmid`, `--file`, `--dest`, `--remote`, `--out`, `--url-only`, `--windows`, `--sha256`, `--keep` | presigned-URL transfer via guest agent |
| `backup` | `--force`, `--keep`, `--storage`, `--retained` | weekly long-term/retained backup |

## Physical disks and node storage

```bash
proxmox-lab storage status                 # what exists, and free space
proxmox-lab storage list-disks             # physical disks, and which are unused
```

Adding a disk formats it, which destroys everything on it. The command refuses
by default and needs the target named exactly:

```bash
proxmox-lab storage add-disk --lease "$L" \
  --device /dev/sdb --name bulk \
  --expect-serial <serial> --expect-size-gb 1000 \
  --host-change-authorized
```

On success the disk is formatted (`ext4` by default, `--filesystem xfs`
available), mounted at `/mnt/pve/<name>`, registered as directory storage and
set to hold `images,iso,vztmpl,import,backup,snippets`. Use `--content` to
narrow that.

### Fast or bulk

`storage status` reports a `class` for every storage: `bulk` for the one named
in `[storage] bulk_storage`, `fast` for everything else. A guest disk on a
slow USB store is I/O-bound; a warning is printed unless
`--slow-storage-accepted` is passed. An ISO mounted from the bulk store
(`media=cdrom`) is not warned about: that is the recommended arrangement.

### Reclaiming unreferenced images

Failed creates and guests destroyed outside a lease leave disk images behind.
`storage gc` reports by default and deletes nothing:

```bash
proxmox-lab storage gc                      # what is unreferenced?
proxmox-lab storage gc --delete --host-change-authorized
```

It checks every volume against every guest config and snapshot. A snapshot's
`vmstate` volume is read too. If any guest config or snapshot cannot be read,
nothing is classified. `--delete` only removes volumes that the same run found
unreferenced. Read the `orphaned_provisioned_gb` vs `orphaned_on_disk_gb`
numbers before deleting: only the on-disk figure is reclaimed.

### Guest disk activity

Proxmox's `diskwrite` counter can read `0` for a writing qcow2 guest. To
measure real guest I/O, use `guest disk-activity --ground-truth`; see
[disk.md](disk.md).

## S3 scratch bucket

Don't have an S3-compatible bucket yet? `install.sh` can provision one: choose
`lxc` and it prints a root-only `minio-host-setup.sh` command that creates a
minimal MinIO LXC. See [INSTALL.md](INSTALL.md#optional-host-minio-on-proxmox).

> **Trusted LAN only.** The MinIO LXC and the MariaDB audit ledger listen on
the LAN with no TLS. Do not port-forward them; put a TLS reverse proxy in front
before exposing them to an untrusted network. This is the canonical statement;
other pages link here.

### Bucket credentials

Only the endpoint, bucket and region are recorded in this repository. The key
ID and secret live in the configured secret backend and must never be written to
a file, a manifest, a command line, or the journal. Store them with:

```bash
proxmox-lab secrets set s3-key-id
proxmox-lab secrets set s3-secret-key
```

### Direct bucket operations

```bash
proxmox-lab s3 health
proxmox-lab s3 list --prefix screens/
proxmox-lab s3 put --file ./notes.txt --key notes/notes.txt
proxmox-lab s3 get --key notes/notes.txt --out ./notes.txt
proxmox-lab s3 presign --key notes/notes.txt --method GET --expires 900
proxmox-lab s3 delete --key notes/notes.txt
```

### Moving files into and out of a guest

```bash
# local file -> guest
proxmox-lab push --lease "$L" --vmid 9001 --file ./payload.tar.gz \
  --dest /tmp/payload.tar.gz

# guest file -> local
proxmox-lab pull --lease "$L" --vmid 9001 \
  --remote /var/log/cloud-init.log --out ./cloud-init.log
```

Both drive the transfer through `qemu-guest-agent`. Add `--windows` for Windows
guests. When there is no guest agent, use `--url-only` to get a presigned URL
and run the fetch yourself over `console text` or `console type`.

Rules:

- Default presigned lifetime is one hour; the maximum accepted is seven days.
- Never paste a presigned URL into the journal, a commit, or a template.
- The bucket is scratch space; anything that must survive a lease belongs in
  Proxmox storage or the repository, not here.
- `pull` deletes its scratch object after download unless `--keep` is given.

## Fetching cloud images

The node downloads directly, so a multi-gigabyte image never crosses the
controller's link:

```bash
proxmox-lab storage download-url --lease "$L" \
  --url https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/cloud/nocloud_alpine-3.23.4-x86_64-bios-cloudinit-r0.qcow2 \
  --filename alpine-3.23.4-cloudinit.qcow2 \
  --storage bulk --content import \
  --checksum <digest> --checksum-algorithm sha512
```

A checksum is required. `--allow-unverified` exists but an unverified image is a
supply-chain problem; use it only when the user accepts that.

## Long-term lease backups

Guests in a long-term lease are backed up weekly with `vzdump` in snapshot mode
to `[lease] long_term_backup_storage` (defaulting to `[storage] bulk_storage`).
The slowest, largest disk is the right place: these are safety copies.

```bash
proxmox-lab backup                # run any that are due
proxmox-lab backup --force        # run now regardless
proxmox-lab backup --keep 4       # keep more generations
```

Only whole successful runs update `last_backup_at`. Retained guests are backed
up with `backup --retained --force`. See
[long-term-leases.md](long-term-leases.md).

## Cleanup

At the end of the lease, `lease-end` stops and deletes the lease's guests and
their disk images. S3 scratch objects created by `push`/`pull` are deleted
unless `--keep` was passed; old presigned URLs expire on their own.

`storage gc --delete` is a separate, explicit cleanup that removes unreferenced
volumes from node storage. It is not run automatically.

## Common failures

| Symptom | Diagnostic | Next step |
|---|---|---|
| `list-disks`/`add-disk` return 403 | `proxmox-lab doctor` | Grant `Sys.Audit`+`Sys.Modify` on `/nodes/pve`, or set up disks as root |
| `storage add-disk` exits non-zero after formatting | Re-run `storage set-content` | The disk is formatted but content types are not configured; do not re-run `add-disk` |
| `storage gc` reports large `orphaned_provisioned_gb` | `proxmox-lab guest disk-activity --vmid <id> --ground-truth` | Check `orphaned_on_disk_gb` before deciding to delete; sparse qcow2 provisioned size is not used space |
| `push`/`pull` fail silently | `proxmox-lab guest probe --vmid <id>` | The guest may have no agent; use `--url-only` and fetch over `console text` |
| `storage download-url` refuses without checksum | Pass a digest or use `--allow-unverified` and accept the supply-chain risk |

For the full symptom-decision list, see [troubleshooting.md](troubleshooting.md).

## Advanced usage

- Use `s3 put`/`s3 presign` to stage artifacts that must outlive a guest, then
  pull them with a presigned URL.
- `storage gc --dry-run` to preview what `--delete` would remove.
- Set `[storage] bulk_storage` to a large slow disk and keep `upload_storages`
  as an allowlist.

## Verification status

See [VERIFICATION.md](VERIFICATION.md) for what has been observed on real
hardware (`storage status`, `list-disks`, S3 round-trip, file transfer) and
what remains unit-tested only (`storage add-disk` against a second unfamiliar
disk, `storage gc --delete`, `backup --retained` archive writes).

## Safety gate

| Destructive op | Required flag | What it guards |
|---|---|---|
| Format a physical disk (`storage add-disk`) | `--host-change-authorized` + `--expect-serial` + `--expect-size-gb`; `--wipe-confirmed` if a filesystem exists | wiping the wrong `/dev/sdX` after reboot re-ordering |
| Change storage content types | `--host-change-authorized` | host storage registry |
| Delete unreferenced images (`storage gc --delete`) | `--host-change-authorized` + `--delete` | deleting a volume in use |
| Fetch without checksum (`storage download-url`) | `--allow-unverified` | supply-chain; discouraged |
| S3 scratch credentials | never in file/argv/journal; configured secret backend only | presigned URLs audited by key only |

All host-level storage changes share `--host-change-authorized`, like every host
change in [safety-policy.md](safety-policy.md).

## See also

- [CONFIGURATION.md](CONFIGURATION.md#storage) — `[storage]` keys
- [disk.md](disk.md) — offline disk repair and ground-truth I/O measurement
- [long-term-leases.md](long-term-leases.md) — weekly backup target selection
