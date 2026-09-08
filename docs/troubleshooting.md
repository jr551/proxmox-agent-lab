# Troubleshooting

Work through symptoms in order. Each row names a diagnostic command and the next
decision; an inconclusive result tells you what to try before concluding the tool
is broken.

## Lease, power and cleanup

| Symptom | Diagnostic | Next step |
|---|---|---|
| Unsure whether the lab is set up | `proxmox-lab doctor` | If `proxmox_reachable: false`, the host is likely asleep; continue with `lease-begin`. If `proxmox_token_stored: false`, store the token with `proxmox-lab secrets set proxmox-token`. If missing privileges, grant them to the *token*, not just the user. |
| `lease-begin` times out | `proxmox-lab doctor` and check the configured `[power]` `boot_timeout_seconds` | If `proxmox_reachable: true` but the host did not wake, check BIOS Wake-on-LAN, `ethtool` WoL setting, the broadcast address, or use `power-on --standalone-authorized` only for non-lease boot. A timeout below 90 s is rejected. |
| `lease-end` does not print `host_powered_off: true` | `proxmox-lab journal --limit 20` and `proxmox-lab cleanup-expired --all` | If cleanup failed, `cleanup-expired` retries the finalization. If a long-term lease is active, `lease-end` refuses and points at `lease-destroy`; that is expected. |
| Host stays on after the last lease ended | `proxmox-lab lease-list` and `proxmox-lab guest inventory --orphaned-only` | If a long-term lease is active, the host is meant to stay on. If there are orphaned running guests, run `proxmox-lab cleanup-expired --orphans-only --host-change-authorized`. |
| `lease-end` refuses because of a shared guest | `proxmox-lab lease-list` and `proxmox-lab journal --limit 20` | End or abandon the other lease, unless the user said the guest is yours to delete; then use `--shared-guests-authorized`. |

## Guest access

| Symptom | Diagnostic | Next step |
|---|---|---|
| Unsure which channel works | `proxmox-lab guest probe --vmid <id>` | If `guest_agent: true`, use `guest run`. If `serial_console: true`, use `guest run --password-stdin` or `console text`. If only `vga: true`, use `console screenshot`. If `keyboard_input: false`, VNC keystrokes will not work. |
| `guest run` hangs | `proxmox-lab console screenshot --vmid <id>` and `proxmox-lab guest disk-activity --lease "$L" --vmid <id>` | The guest is usually at a prompt or the command is waiting. Take a screenshot. If the disk is idle, the guest may have crashed. |
| `console type`/`console keys` has no effect | `proxmox-lab guest probe --vmid <id>` | If `keyboard_input: false`, the VM has `vga: serial0`; use `console text` instead. Otherwise re-run with `--screenshot-after 3` and check `screen_changed`. |
| `console text` is empty | `proxmox-lab console screenshot --vmid <id>` and check the guest is running | If the guest is a stopped QEMU, use `--wait-for-guest SECONDS`. If the guest is graphical, use `console screenshot`. |
| `console exec` fails | `proxmox-lab guest probe --vmid <id>` | `console exec` needs `qemu-guest-agent`. If the agent is missing, use `console text` or `console type`. |
| No idea if a screen is stuck | `proxmox-lab console has-gui-locked-up --lease "$L" --vmid <id>` or `proxmox-lab console has-terminal-locked-up --vmid <id>` | A `true` verdict is evidence, not proof — the guest may just be idle. Re-read the screen before deciding. |

## Screen and vision

| Symptom | Diagnostic | Next step |
|---|---|---|
| `console screenshot` is black or wrong | `proxmox-lab console screenshot --vmid <id> --settle 3` or `--via monitor` | Wait longer with `--settle`. If VNC cannot produce a frame, use `--via monitor` (requires `--lease` and the `[memflow]` SSH channel). |
| `console inspect` fails | `proxmox-lab console preflight` | If no vision key is stored, use `console screenshot --for-model`. If a key is stored and every provider fails, `console inspect` still returns a base64 image under `image` unless `--no-image-fallback`. |
| `--for-model` returns `error: exceeded size cap` | `proxmox-lab console screenshot --vmid <id>` without `--for-model` | The screen is too dense for the base64 cap. Read the PNG file from the `path` field. |
| Cannot find the PNG | `proxmox-lab console screenshot --vmid <id> --out ./capture.png` | Specify `--out` or look in the state `screens/` directory reported by `doctor`. |

## Storage and transfer

| Symptom | Diagnostic | Next step |
|---|---|---|
| `storage list-disks` / `add-disk` returns 403 | `proxmox-lab doctor` and `proxmox-lab console preflight` | Grant `Sys.Audit|Sys.Modify` on `/nodes/pve` to the *token*, or perform disk setup as root. |
| `storage add-disk` exits non-zero after formatting | `proxmox-lab storage status` and the JSON output | If `content_configured: false`, run `storage set-content` instead of re-running `add-disk`, which would reformat the disk. |
| `storage gc` shows huge "orphaned" space | `proxmox-lab storage gc` (report only) and `proxmox-lab guest disk-activity --lease "$L" --vmid <id> --ground-truth` | Compare `orphaned_provisioned_gb` to `orphaned_on_disk_gb`; only the on-disk value is real reclaimed space. |
| `push` / `pull` fail | `proxmox-lab guest probe --vmid <id>` | If the guest has no agent, use `--url-only` and run `curl` through `console text`. On Windows, add `--windows`. |
| `storage download-url` refuses without checksum | Re-run with `--checksum <digest>` or use `--allow-unverified` only if the user accepts the supply-chain risk. | An unverified image is a supply-chain problem, not a convenience. |

## Network and VPN

| Symptom | Diagnostic | Next step |
|---|---|---|
| Guest behind VPN has no egress | `proxmox-lab net status` and `proxmox-lab net leak-test --lease "$L" --vmid <id> --user <u> --password-stdin --gateway-vmid <gwid>` | If the guest is on `vmbr0`, run `net attach`. If `leak-test` is **unproven**, install `curl` or use the DNS path; it is not a pass. |
| `net verify` fails on handshake age | `proxmox-lab net status` | The tunnel is dead but the old handshake timestamp remains. Re-provision the gateway and re-run `net verify`. |
| `net verify` passes egress but a non-`wg0` rule is found | `proxmox-lab net verify --lease "$L" --vmid <id>` | Fix the nftables ruleset so only `lab-if → wg0` rules exist, then re-verify. |
| `net host-bridge` says bridge exists but is not visible | `proxmox-lab doctor` | Grant `AgentBridgeUse` to both the user `agent@pve` and the token `agent@pve!lab` on `/sdn/zones/localnetwork/vmbr1`. |

## Leases, orphans and long-term

| Symptom | Diagnostic | Next step |
|---|---|---|
| `doctor` reports running orphans | `proxmox-lab guest inventory --orphaned-only` and `proxmox-lab cleanup-expired --orphans-only --host-change-authorized` | `--orphans-only` stops (never deletes) orphaned guests. If any signal (recent task, uptime, CPU) says the guest is in use, `doctor` reports `orphaned_but_active` and leaves it. Use `--include-active` only when you have proven it is not someone else's live work. |
| `lease-end` on a long-term lease refuses | `proxmox-lab lease-list` | This is expected. Use `lease-destroy --lease <id>` or `lease-release --lease <id>` with `--confirm` after reviewing the preview. |
| Watchdog or idle timer is not shutting the host | `proxmox-lab lease-list` and `proxmox-lab doctor` | If any long-term lease is active, the host is intentionally pinned. If an ordinary lease is stuck in `cleanup_failed`, `cleanup-expired` retries it. |

## Audit and journals

| Symptom | Diagnostic | Next step |
|---|---|---|
| `doctor` reports `audit.spooled_records` | `proxmox-lab journal --limit 20` | The lab host is off and events are queued locally. Run `proxmox-lab journal --flush-spool` when the host is reachable. |
| Suspect a secret leaked to the journal | `proxmox-lab journal --limit 20` | Search the output. If a secret appears, report it; only counts, exit codes and object keys should be recorded. |

## General recovery checklist

When nothing above fits:

1. `proxmox-lab doctor`
2. `proxmox-lab guest probe --vmid <id>`
3. `proxmox-lab console screenshot --vmid <id>`
4. `proxmox-lab journal --limit 20`

Quote the command output that supports your claim. Distinguish *inconclusive*
from *negative*: a probe that returned nothing is not proof of safety.

## See also

- [AGENTS.md](AGENTS.md) — agent operational guidance
- [safety-policy.md](safety-policy.md) — enforced invariants
- [VERIFICATION.md](VERIFICATION.md) — what has been run on real hardware
- [console.md](console.md), [storage.md](storage.md), [network.md](network.md) — feature pages
