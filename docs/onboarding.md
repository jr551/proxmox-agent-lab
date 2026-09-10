# Installer generation and host pairing (experimental)

`proxmox-lab onboard` prepares a Proxmox installation and pairs the resulting
host with your controller. Two paths share the same authenticated HTTPS
callback:

- **Spare PC:** prepare a Proxmox ISO with an exact disk selection and a
  first-boot setup script. DHCP supplies installation networking. The new host
  advertises a signed LAN beacon and calls the controller automatically.
- **VPS:** generate a script for a fresh **Debian 13 amd64 full VPS**. It
  installs the Proxmox kernel, reboots, installs Proxmox, then enrolls the host.
  The resulting controller permits only unprivileged LXC creation and leaves
  the VPS powered on after guest cleanup.

This is offline-tested experimental setup tooling, not a hardware-verified
installer. Read [VERIFICATION.md](VERIFICATION.md). Keep a provider console
available for VPS boot or networking recovery. A container-based VPS cannot
run this flow; the provider must allow booting a custom kernel and creating
namespaces/cgroups.

## Prepare the controller

Use the checkout CLI or install the feature branch. `openssl` is needed on
the controller to generate the pairing certificate. The ISO builder needs
`proxmox-auto-install-assistant` on a Debian/PVE machine; macOS and Windows can
prepare bundles and receive callbacks, but do not run that Linux ISO tool.

Choose a controller IP address or DNS name reachable from the new host. The
receiver uses TCP **8843** with its own TLS certificate. On a WAN, forward that
port to the receiver without terminating TLS elsewhere. This callback reports
credentials and host readiness; it does not create a reverse management tunnel.
The controller must also be able to reach the host's HTTPS API on port **8006**,
through your VPN or appropriately restricted provider firewall.

Bundles expire after 24 hours (`--hours`, maximum 168). Store them outside the
repository: they contain pairing credentials and may contain Wi-Fi credentials.
The generator creates a private directory and refuses to reuse an existing one.
The resulting ISO also contains credentials; protect and delete it after use.

## Spare PC: generate and boot an ISO

Create a root password hash interactively, without passing the password in
argv:

```bash
umask 077
openssl passwd -6 > ~/pxl-root-password.hash
```

Get the target disk's **exact udev `ID_SERIAL`** from the intended machine
(for example using the Proxmox installer's device information tool). Wildcards
and automatic first-disk selection are refused. Booting the generated ISO will
wipe the matching disk; `--wipe-confirmed` acknowledges that choice while
preparing the bundle. It does not write any disks on your controller.

**Boot the target in UEFI mode.** The generated ISO is verified working when
the machine boots it via UEFI: the auto-installer then writes a complete
`proxmox-boot-tool`/GRUB EFI system partition and the installed host boots to
the Proxmox console. A legacy-BIOS (SeaBIOS/CSM) boot of the same ISO was
observed to partition and populate the disk but leave the bootloader
uninstalled — the auto-installer collects `grub-install i386-pc` errors into a
non-fatal warning list and reboots anyway, producing a disk that hangs at
"Booting from Hard Disk…". Enable UEFI on the spare PC before booting the ISO;
do not rely on legacy BIOS for this flow.

```bash
proxmox-lab onboard prepare --mode iso \
  --directory ~/pxl-pc-bundle \
  --controller-host pc.example.com \
  --fqdn lab.example.com \
  --disk-serial "$TARGET_DISK_ID_SERIAL" \
  --root-password-hash-file ~/pxl-root-password.hash \
  --wipe-confirmed
```

Optional settings include `--country`, `--timezone`, `--keyboard`, `--email`,
`--api-host` (the address the controller should use), and
`--ssh-public-key-file` (adds this public key to root's authorized keys).

On the Debian/PVE build machine, run:

```bash
proxmox-lab onboard build-iso --bundle ~/pxl-pc-bundle \
  --source ~/Downloads/proxmox-ve.iso --sha256 "$OFFICIAL_ISO_SHA256"
```

Alternatively, `--url "$OFFICIAL_HTTPS_ISO_URL"` downloads the source ISO with
size/time bounds before checking the supplied SHA-256. Use the checksum
published by Proxmox. The builder validates the answer file with Proxmox's own
assistant, embeds the first-boot script, and writes `proxmox-agent-lab.iso`
inside the bundle. Use a current Proxmox ISO supporting first-boot scripts
(PVE 8.3 or newer). The command reports the generated ISO's SHA-256.

When using another build machine, copy only `pairing.json`, `answer.toml`, and
`host-setup.py` into a private bundle directory there. Keep `receiver.key` on
the controller. Never commit the bundle or its output.

Start the receiver **on your controller**, then write the generated ISO to
installation media with your normal imaging tool and boot the spare PC:

```bash
proxmox-lab onboard serve --bundle ~/pxl-pc-bundle \
  --config-out ~/.config/proxmox-agent-lab/new-lab.toml
```

`serve` receives the callback, authenticates the new node's API using its
returned certificate authority, and writes the controller config only after
verification succeeds. It never overwrites existing config or companion files.
The token is in a separate 0600 secrets file using the existing file backend;
the config contains only that file's path. TLS verification is enabled for
API requests, uploads, and consoles using the enrolled CA.

### Optional Wi-Fi after installation

Add these options to `prepare`:

```bash
--wifi-ssid 'Your network name' \
--wifi-password-file ~/private-wifi-passphrase \
--wifi-interface wlan0
```

This supports WPA2 passphrases and an existing, driver-supported wireless
interface. The passphrase is converted into a WPA PSK before embedding; the
PSK remains a credential. **Wired networking is still required during the
Proxmox ISO installation and first-boot package installation.** This does not
make the stock installer boot over Wi-Fi or supply missing wireless firmware.
WPA3-only and enterprise Wi-Fi are not supported by this initial flow.

The host adds Wi-Fi as a separate DHCP management interface with a higher
route metric, leaving the wired connection intact. It does not bridge Wi-Fi
to guests. The isolated `vmbr1` guest bridge has no implicit NAT, forwarding or
DHCP server; configure guest networking deliberately after enrollment. Use a
stable management address or `--api-host` appropriate for the connection you
will keep. Wake-on-LAN is attempted only for a supported wired NIC; a machine
without it needs another power-on method configured separately.

The host emits signed, credential-free UDP announcements on LAN port 8844
while attempting its HTTPS callback. Optional discovery, in another terminal:

```bash
proxmox-lab onboard discover --bundle ~/pxl-pc-bundle --timeout 60
```

Broadcast discovery does not grant access or replace TLS pairing. Forged,
stale, and unrelated announcements are ignored. It may not cross VLANs or
Wi-Fi client isolation; the direct HTTPS callback works independently.

## Fresh Debian VPS

Choose a full Debian 13 amd64 VPS that can boot the Proxmox kernel. Set DNS so
`--fqdn` / `--api-host` identify the host from the controller. The installer
maps the node's FQDN to its management address in `/etc/hosts` and preserves
other aliases. It does not repartition disks, delete kernels, replace the
management interface configuration, or configure provider firewall rules.
Package installation and the kernel reboot are persistent host changes.

```bash
proxmox-lab onboard prepare --mode vps \
  --directory ~/pxl-vps-bundle \
  --controller-host callback.example.com \
  --fqdn lab-vps.example.com --api-host lab-vps.example.com

proxmox-lab onboard serve --bundle ~/pxl-vps-bundle \
  --config-out ~/.config/proxmox-agent-lab/vps.toml
```

Transfer **only** `host-setup.py` to the intended VPS over authenticated SSH,
then run it there as root:

```bash
python3 host-setup.py --host-change-authorized --reboot-authorized
```

The script checks Debian version, architecture, full virtualization and
namespace support before installing its service. The service uses the signed
Proxmox no-subscription repository, installs a kernel, reboots once, and checks
that the running kernel ends in `-pve` before installing the Proxmox packages.
Failures stop the service; they do not trigger a reboot loop. No templates or
guests are automatically downloaded or created.

After enrollment, `guest_mode = "lxc-only"` blocks QEMU API operations, host
power writes, privileged LXC creation, and cloning (which could inherit a
privileged source). Ordinary leases still clean up their guests. The host
stays on and `lease-end` reports that explicitly. The issued VPS API token
also receives no host power permission.

This is a **controller policy**, not a claim that Proxmox itself has no QEMU
packages or that root cannot bypass it. Proxmox's VM permissions cover both
QEMU and LXC. LXC shares the VPS kernel; do not use this path for untrusted
code, malware, kernel debugging, Windows, Android or device emulation.

## Finish and recover

Point the controller at the generated file:

```bash
export PROXMOX_AGENT_LAB_CONFIG="$HOME/.config/proxmox-agent-lab/new-lab.toml"
proxmox-lab doctor
```

Setup uses the public Proxmox no-subscription repository and preserves disabled
enterprise repository files with a `.pxl-disabled` suffix. The VPS kernel stage
backs up `/etc/hosts` as `hosts.before` in its private state directory.

Setup creates a dedicated API principal, grants guest/storage permissions and
node auditing, adds an isolated guest bridge, and optionally installs your SSH
public key. Use the existing tools to configure storage, templates, guest
networking, shared audit services and the watchdog as needed. A completed
pairing is not a claim that every optional lab subsystem is configured.

The receiver waits up to an hour by default (`--timeout`). The host retries
callbacks for up to 30 minutes within the pairing lifetime. Identical retries
are accepted; a different enrollment using the same bundle is refused. To
resume after a stopped receiver, rerun `serve` and restart the host's
`proxmox-agent-lab-onboarding.service` before bundle expiry. Inspect failures:

```bash
systemctl status proxmox-agent-lab-onboarding
journalctl -u proxmox-agent-lab-onboarding
```

If the callback arrived but API verification failed, fix routing/certificates
and run `onboard accept --bundle … --config-out …`. The private receipt remains
available even after the pairing token expires; rerunning `serve` also reuses
an existing receipt. After successful callback delivery the host deletes its saved
pairing script and token files; remove the original uploaded script or
installation media yourself. The controller bundle remains private until you
remove it. Do not remove the generated config's CA or secrets companion files.

The default VPS power policy is intentional. For a physical host with no
working WoL, configure a supported power-on method before its first lease.

## Upstream references

- [Proxmox automated installation](https://pve.proxmox.com/wiki/Automated_Installation)
- [ISO assistant and first-boot support](https://github.com/proxmox/pve-installer/blob/master/proxmox-auto-install-assistant/src/main.rs)
- [Proxmox on Debian 13](https://pve.proxmox.com/wiki/Install_Proxmox_VE_on_Debian_13_Trixie)
- [Wireless networking constraints](https://pve.proxmox.com/wiki/WLAN)
