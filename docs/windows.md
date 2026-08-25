# Installing and setting up Windows guests

## Purpose

Create a Windows Server VM that is reachable over the guest agent, RDP and SSH, starting from a retained installation template. Each template is UEFI/Q35 with the official Microsoft evaluation ISO and VirtIO driver ISO attached; templates contain no product key and bypass no licensing.

For a persistent template, record its VMID under `[windows]` in the config. For a one-off run, pass `--template-vmid <id>` instead.

## Prerequisites

- A lease for every mutation: `proxmox-lab lease-begin --purpose "windows lab"` / `lease-end`.
- Retained installation templates per Windows version (VMIDs `tpl-winserver-2025` / `tpl-winserver-2022` per `docs/site-notes.example.md`). Each: UEFI/Q35, `local:iso` evaluation ISO + VirtIO driver ISO, verified clone boot (see `docs/site-notes.md`).
- Guest disk on fast storage (`local-lvm`), installer ISO on bulk storage (`usb-bulk`) — see `docs/storage.md`. `storage status` reports `class` and `--slow-storage-accepted` gates bulk placement.
- Knowledge of the VirtIO driver branch: `2k25` for Server 2025, `2k22` for Server 2022 (overridable via `--driver-branch`).

## Commands

All flags below verified against `src/proxmox_agent_lab/windows.py:register()`.

### Interactive install (default)

Built for multimodal models: clone, boot, then look at the screen and drive it.

```bash
L=$(proxmox-lab lease-begin --purpose "windows lab" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

proxmox-lab windows install --lease "$L" --vmid 9010 --version 2025
proxmox-lab console keys --lease "$L" --vmid 9010 enter   # "press any key"
proxmox-lab console screenshot --vmid 9010 --settle 5
```

Then loop: screenshot, decide, `console click` or `console type`, screenshot again. At the disk-selection step the VirtIO SCSI controller shows no disks until its driver is loaded — use *Load driver* and browse the second CD-ROM (`E:\vioscsi\2k25\amd64`).

Common `windows install` flags (verified):

| Flag | Effect |
|---|---|
| `--version 2025\|2022` | Selects evaluation media (default 2025) |
| `--template-vmid <id>` | Override config template VMID |
| `--name <name>` | Guest hostname/label |
| `--cores 4`, `--memory 8192` | vCPU/RAM (defaults 4 / 8192) |
| `--storage <store>` | Full-clone target, e.g. `usb-bulk` for an 80 GiB disk |
| `--policy delete\|retain` | Lease ownership (default `delete`) |
| `--full-clone` | Full clone instead of linked clone |
| `--image-index 2` | WIM index (2 = Standard with Desktop Experience) |
| `--driver-branch 2k25\|2k22` | VirtIO driver path override |
| `--locale en-GB`, `--timezone "GMT Standard Time"`, `--hostname`, `--owner` | Answer-file fields (unattended path) |
| `--clone-timeout 1800` | Clone task timeout |

### Unattended install

```bash
proxmox-lab windows install --lease "$L" --vmid 9010 --version 2025 \
  --unattended --password-stdin <<< 'ChosenPassw0rd!'
```

Generates `autounattend.xml`, wraps it in a small ISO, uploads it to `local`, attaches it as `ide3`, and boots. The answer file partitions the disk as GPT, injects the VirtIO storage, network and balloon drivers during WinPE, skips OOBE, sets the Administrator password, installs qemu-guest-agent, and enables Remote Desktop.

Windows Setup can only read an answer file from removable media, which is why this one file uses an ISO. Every other file transfer uses the S3 scratch bucket — see [storage.md](storage.md).

Unattended-only flags (verified): `--unattended`, `--password-stdin`, `--image-index`, `--locale`, `--timezone`, `--hostname`, `--driver-branch`.

Without `--password-stdin` a complex password is generated and printed once. It is never written to the audit ledger.

#### The language page is not covered by the answer file

Measured on Server 2022 eval media: Setup opens on **"Language to install"** and waits there indefinitely, even with a valid answer file attached. It is not that the file is ignored — press Enter once and the install goes straight to copying files, with no image selection, no EULA, no partitioning prompt and no driver prompt. Everything after that first page is automated.

**Enter does not clear that page.** Thirty of them changed nothing: focus sits on the "Language to install" combo box, and a combo box consumes Enter. The key that works is the Next button's accelerator, **Alt+N** — one press and the installer went straight to copying files.

So `--unattended` sends Alt+N (and a harmless Enter) after boot until the disk starts filling, then stops. Nothing is typed at an installer that is already running.

That accelerator is the English one. A localised installer labels the button differently, so a non-`en-US` `--locale` may still need a click on Next.

If an install ever does sit still, `wait-agent` says so within five minutes instead of leaving you guessing:

```
warning: 300s in, the guest has written only 0 MB. It may be waiting
for input -- Setup's language page is the usual culprit ... Still waiting.
```

It **warns and keeps waiting** rather than giving up, because the signal is not trustworthy enough to abandon an install on. Two versions of it were tried and both called a healthy guest stuck: disk writes drop to almost nothing during "Getting devices ready", and the coarse frame comparison used to double-check cannot see a spinner only a few pixels across. `--stall-after 0` turns the warning off.

Diagnosing this took three false starts — the answer file was moved to an IDE CD, a SATA CD and a FAT12 floppy, and WinPE could read it from all three (`if exist F:\autounattend.xml` confirmed). The medium was never the problem. Take a screenshot before concluding the answer file is at fault.

#### The answer ISO is a credential

`autounattend.xml` carries the Administrator password in **plain text** — `<PlainText>true</PlainText>` is what makes Setup read it, so there is no way around that. The ISO therefore sits on `local:iso` holding a working password for the guest.

Setup has consumed it by the time the guest agent answers, so `windows finish` detaches the ISO and deletes the volume. Two consequences worth knowing:

- **Run `windows finish`.** Skip it and the ISO stays, indefinitely, readable by anyone with access to that storage.
- If an install is abandoned before `finish`, delete the ISO by hand: `proxmox-lab api --lease "$L" --method DELETE --path /nodes/<node>/storage/local/content/local:iso/autounattend-<vmid>.iso`

This was found by looking: an answer ISO from an earlier install was still on the node with a usable password inside it.

#### Why the guest agent needs vioserial

The agent talks to the host over a **virtio-serial** channel, so the answer file injects `vioserial` alongside the storage, network and balloon drivers.

Leave it out and the failure is thoroughly misleading: the MSI installs, `sc query QEMU-GA` reports `RUNNING`, `agent: enabled=1` is set on the VM — and the host still cannot reach it, because the service has no channel. The giveaway is inside the guest:

```
> wmic path Win32_PnPEntity where "ConfigManagerErrorCode<>0" get Name,ConfigManagerErrorCode
28    PCI Simple Communications Controller
```

Error 28 is "drivers not installed". Installing that one driver made the agent answer immediately:

```
pnputil /add-driver E:\vioserial\2k22\amd64\*.inf /install
net stop QEMU-GA & net start QEMU-GA
```

Two wrong guesses preceded this. Drive letters were blamed (they were right: `D:` Windows, `E:` virtio-win, `F:` UNATTEND) and then the Red Hat signing certificate (importing it into the Root store was worth doing and the store confirmed it worked — but the agent still could not be reached). The evidence that finally narrowed it down was **ping and port 3389 answering while 445 stayed closed**, which proved the answer file's first-logon commands ran and left only the transport unexplained.

### After Setup

```bash
proxmox-lab windows wait-agent --vmid 9010 --timeout 3600
proxmox-lab windows finish --lease "$L" --vmid 9010
```

`wait-agent` flags (verified): `--vmid` (required), `--lease`, `--timeout 3600`, `--interval 15`, `--stall-after 300` (0 disables).

`finish` enables Remote Desktop, installs and starts OpenSSH Server (`--no-openssh` to skip), opens the firewall, and reports the guest addresses. Flags (verified): `--lease` (required), `--vmid` (required), `--no-openssh`, `--timeout 900`.

If `wait-agent` times out, take a screenshot: Setup is usually sitting on a prompt.

Then `push`/`pull` work against the guest with `--windows`.

### Cleanup

Windows clones are ordinary lease resources with policy `delete`. They are stopped and destroyed by `lease-end` like anything else. Use `--policy retain` only for a machine the user asked to keep, and expect to justify it in the audit event.

Additional install flags for cleanup/placement: `--no-start` (`--no-start` sets `start=False`), `--no-boot-key` (suppress auto-tap at UEFI boot prompt), `--clone-timeout`, `--storage`, `--full-clone`.

## Troubleshooting

Keep all paragraphs below — they have cost real leases. No information is removed; troubleshooting stays verbatim with commands pointing to verified flags.

- **Language page stall:** See *The language page is not covered by the answer file* above. Take a screenshot first; send `Alt+N` (or click Next for non-en-US `--locale`). Do not assume a bad answer file. Verify media was readable (`if exist F:\autounattend.xml` in WinPE). `windows wait-agent --stall-after 300` warns but keeps waiting; `--stall-after 0` disables the heuristic.

- **No disks at disk-selection:** VirtIO SCSI driver not loaded. Use *Load driver* → `E:\vioscsi\<2k25|2k22>\amd64`. The branch defaults from `--version`; override with `--driver-branch`.

- **Guest agent unreachable despite `RUNNING`:** Missing `vioserial` driver. Check `wmic ... ConfigManagerErrorCode` for PCI Simple Communications Controller error 28. Install `E:\vioserial\<branch>\amd64\*.inf` and restart `QEMU-GA`. See *Why the guest agent needs vioserial*.

- **Answer ISO left behind:** Run `windows finish --lease "$L" --vmid <id>` which detaches and deletes `autounattend-<vmid>.iso` on `local:iso`. If abandoned, delete via `proxmox-lab api --lease "$L" --method DELETE --path /nodes/<node>/storage/local/content/local:iso/autounattend-<vmid>.iso`.

- **Wrong template or resources:** Pass `--template-vmid`, `--cores`, `--memory`, `--full-clone`, `--storage` explicitly; record VMID under `[windows]` in config for reuse.

- **General hang vs waiting:** Screenshot before concluding. Use `guest disk-activity` and `memflow` as per [reactos.md](reactos.md) §6 for independent write signals.
