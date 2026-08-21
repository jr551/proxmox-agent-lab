# Storage: physical disks, node storage, and guest file transfer

## Physical disks and node storage

```bash
proxmox-lab storage status                 # what exists, and free space
proxmox-lab storage list-disks             # physical disks, and which are unused
```

Adding a disk formats it, which destroys everything on it and cannot be
undone. The command therefore refuses by default and needs the target named
exactly:

```bash
proxmox-lab storage add-disk --lease "$L" \
  --device /dev/sdb --name bulk \
  --expect-serial <serial> --expect-size-gb 1000 \
  --host-change-authorized
```

Guards, all tested:

- `--host-change-authorized` is mandatory, as for any host-level change.
- The device is never auto-selected; `list-disks` shows candidates.
- A disk Proxmox reports as the OS disk is refused outright.
- A disk already carrying a filesystem, LVM member or partition table is
  refused unless `--wipe-confirmed` is also passed.
- `--expect-serial` and `--expect-size-gb` pin the physical device, so a
  `/dev/sdX` name that moved across a reboot cannot redirect the wipe.

On success the disk is formatted (`ext4` by default, `--filesystem xfs`
available), mounted at `/mnt/pve/<name>`, registered as directory storage and
set to hold `images,iso,vztmpl,import,backup,snippets`. Use `--content` to
narrow that.

All of that is the contract, so a partial result **exits non-zero**: if the
storage is created but setting its content types fails, the JSON is still
printed (with `content_configured: false` and the reason) and then the command
fails, because a caller that read exit 0 would go on to upload content the
storage will not accept. The disk is already formatted at that point, so
finish it with `storage set-content` rather than rerunning `add-disk`, which
would erase it again:

```bash
proxmox-lab storage set-content --lease "$L" --name bulk \
  --content images,iso,vztmpl,import,backup,snippets \
  --host-change-authorized
```

A slow bulk disk such as a USB drive is a good home for install media, imported
cloud images and backups. Keep running guest disks on `local-lvm`; a USB disk
is fine for cold storage but will make a booted VM feel slow.

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

A checksum is required. `--allow-unverified` exists but an unverified image is
a supply-chain problem, not a convenience — use it only when the user accepts
that. The published digest and URL are recorded in the audit event.

### Required privileges

Disk operations need `Sys.Audit` and `Sys.Modify` on `/nodes/pve`, which the
least-privilege lab token does not hold. Without them `list-disks` and
`add-disk` return HTTP 403. Either grant those to the token, or perform the
one-time disk setup as root and let the skill use the resulting storage
normally.

## S3 scratch bucket

Don't have an S3-compatible bucket yet? `install.sh` can provision one for
you: choose the `lxc` S3 backend and it prints a root-only
`minio-host-setup.sh` command that creates a minimal, unprivileged MinIO LXC
(S3 API only, no browser console) on the Proxmox host, along with the
bucket and an access key. See [INSTALL.md](INSTALL.md#optional-host-minio-on-proxmox).

## Bucket

| Item | Value |
|---|---|
| Endpoint | `https://s3.example.com` (behind Cloudflare) |
| Bucket | `lab-scratch` |
| Region | `us-east-1` |
| Addressing | path-style |
| Credential source | macOS Keychain, service `proxmox-agent-lab`, accounts `s3-key-id` and `s3-secret-key` |

Only the endpoint, bucket and region are recorded in this repository. The key
ID and secret live in the Keychain, exactly like the Proxmox API token, and
must never be written to a file, a manifest, a command line, or the journal.

Requests are signed with AWS SigV4 using the standard library. The endpoint
sits behind Cloudflare, which rejects urllib's default user agent, so an
explicit `User-Agent` is sent outside the signed header set.

## Getting files into and out of a guest

Presigned URLs are the mechanism. The controller signs a short-lived URL
locally; the guest fetches or uploads it with the `curl` or `Invoke-WebRequest`
it already has. **No credential ever enters the guest.**

```bash
# local file -> guest
proxmox-lab push --lease "$L" --vmid 9001 --file ./payload.tar.gz \
  --dest /tmp/payload.tar.gz

# guest file -> local
proxmox-lab pull --lease "$L" --vmid 9001 \
  --remote /var/log/cloud-init.log --out ./cloud-init.log
```

Both drive the transfer through qemu-guest-agent. Add `--windows` for a
Windows guest, which swaps `curl` for `Invoke-WebRequest`.

When there is no guest agent — an LXC container, or a guest mid-install — use
`--url-only` to get a presigned URL and run the fetch yourself over
`console text` or `console type`:

```bash
URL=$(proxmox-lab push --lease "$L" --vmid 9001 --file ./x --url-only \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["fetch_url"])')
proxmox-lab console text --vmid 9001 --send "curl -fsSL -o /tmp/x '$URL'"
```

## Direct bucket operations

```bash
proxmox-lab s3 health
proxmox-lab s3 list --prefix screens/
proxmox-lab s3 put --file ./notes.txt --key notes/notes.txt
proxmox-lab s3 get --key notes/notes.txt --out ./notes.txt
proxmox-lab s3 presign --key notes/notes.txt --method GET --expires 900
proxmox-lab s3 delete --key notes/notes.txt
```

## Rules

- Default presigned lifetime is one hour; the maximum accepted is seven days.
- Never paste a presigned URL into the journal, a commit, or a template. The
  URL carries a valid signature. Audit events record the object key only.
- The bucket is scratch space. Anything that must survive a lease belongs in
  Proxmox storage or this repository, not here.
- `pull` deletes its scratch object after download unless `--keep` is given.


## Long-term lease backups

Guests in a long-term lease are backed up weekly with `vzdump`, in snapshot
mode so they keep running, to the storage named by `[lease]
long_term_backup_storage` — or `[storage] bulk_storage` if that is blank. The
slowest, largest disk is the right place: these are safety copies, not
something you restore from often.

```bash
proxmox-lab backup                # run any that are due (the watchdog does this)
proxmox-lab backup --force        # run now regardless
proxmox-lab backup --keep 4       # keep more generations
```

Only whole successful runs update the lease's `last_backup_at`, so a partial
failure means the backup is retried rather than silently skipped for a week.
