# 📱 Android devices

## Purpose

Emulated phones as lab guests — a Galaxy S20, a Pixel, or something small and quick — that appear on the VM's own console, so every `console` command already here works on them: screenshot it, click it, type at it, share a link to it.

A VM runs a Debian host with the Android SDK emulator on its console (X + openbox + systemd unit). For a real Android network stack with proxy control, see *A real Android-x86 install* at the end (QEMU guest, no Debian).

## Prerequisites

- A lease for every mutation (`lease-begin` / `lease-end`).
- Proxmox node with enough host RAM (phone RAM + ~3 GB Debian/X/emulator overhead). S20 wants ~11 GB; `minimal` wants ~3.6 GB (see Profiles).
- Nested virtualisation enabled for usable `x86_64` performance (KVM). On AMD: `options kvm_amd nested=1`; Intel: `options kvm_intel nested=1`. Check `kvm-ok`; without it `x86_64` falls back to emulation and `arm64-v8a` is already fully emulated and very slow.
- Storage for SDK + system image (a few GB, several minutes on first build). ISO/guest-disk placement per [storage.md](storage.md).

## Commands

All flags verified against `src/proxmox_agent_lab/android.py:register()` and `src/proxmox_agent_lab/console.py`.

### Quick start

```bash
proxmox-lab android profiles
proxmox-lab android create --lease "$L" --vmid 9050 --profile galaxy-s20
proxmox-lab console screenshot --vmid 9050        # watch Android boot
```

### Profiles

A profile sets resolution, density, RAM, storage and Android version, and all of those are real — `wm size` inside a `galaxy-s20` reports 1440×3200.

**The model string is not.** `ro.product.model` is baked into the system image's `build.prop`, and a read-only property that is already set cannot be overridden with `emulator -prop`. A stock image keeps reporting `sdk_gphone_x86_64`, whatever the profile says. Verified: a `minimal` device reports the right 720×1600 screen and the wrong model. Spoofing identity properly needs a modified system image, which this does not ship.

So treat a profile as the phone's **shape**, not its identity, and check with `android status`, which reports what the device actually says.

| Profile | Screen | Phone RAM | API | Model |
|---|---|---:|---:|---|
| `galaxy-s20` | 1440×3200 @560 | 8 GB | 33 | SM-G980F* |
| `galaxy-s20-ultra` | 1440×3200 @560 | 12 GB | 33 | SM-G988B |
| `galaxy-s10` | 1440×3040 @550 | 8 GB | 31 | SM-G973F |
| `pixel-6` | 1080×2400 @420 | 8 GB | 33 | Pixel 6 |
| `small` | 720×1280 @320 | 2 GB | 30 | generic |
| `minimal` | 720×1600 @320 | 1.5 GB | 30 | generic |

\* requested, not reported — see above.

The VM gets the phone's RAM plus overhead for Debian, X and the emulator process — so an S20 wants about **11 GB** of host memory, and `minimal` about **3.6 GB**. On a busy host, start with `minimal`.

### Templates

`--as-template` shuts the device down cleanly when it is built and converts it to a Proxmox template, so future devices clone in seconds instead of re-downloading the SDK:

```bash
proxmox-lab android create --lease "$L" --vmid 9050 --profile minimal \
  --as-template

# later, an instant device
proxmox-lab api --lease "$L" --method POST \
  --path /nodes/<node>/qemu/9050/clone --data newid=9051 --wait-task
```

A templated device is registered `retain`, so ordinary cleanup leaves it alone.

If a device is already built and you decide afterwards that it should be a template, you do not have to rebuild it:

```bash
proxmox-lab android template --lease "$L" --vmid 9050
```

It shuts the device down cleanly first — Proxmox refuses to template a running guest — and refuses outright if the VMID is already a template.

Verified flags: `android create --lease --vmid --profile [--name] [--template] [--api] [--abi x86_64|arm64-v8a] [--cores] [--memory] [--adb-port] [--policy delete|retain] [--as-template] [--clone-timeout] [--setup-timeout]`; `android template --lease --vmid` (`src/proxmox_agent_lab/android.py`).

### Why it needs no Android-specific viewer

The emulator runs under a bare X session as the VM's console, so its window *is* the screen Proxmox shows. That means:

```bash
proxmox-lab console screenshot --vmid 9050              # the phone's screen
proxmox-lab console click --lease "$L" --vmid 9050 --x 720 --y 1500
proxmox-lab share create --lease "$L" --vmid 9050       # send someone a link
```

all work unchanged. No separate protocol, no scrcpy, nothing to forward. Verified: `console screenshot/click/share create` with standard `--lease --vmid` flags.

### adb

```bash
proxmox-lab android status --vmid 9050
proxmox-lab android adb --lease "$L" --vmid 9050 devices
proxmox-lab android adb --lease "$L" --vmid 9050 install /tmp/app.apk
proxmox-lab android adb --lease "$L" --vmid 9050 shell getprop ro.product.model
```

Push an APK in with `proxmox-lab push` first, then install it. Verified: `android status --vmid`; `android adb --lease --vmid [--timeout] <command...>` (`src/proxmox_agent_lab/android.py`).

### The build scripts

Provisioning is three plain shell scripts under `src/proxmox_agent_lab/android_scripts/`, shipped as files rather than embedded strings so you can read, diff and run them by hand:

| Script | What it does |
|---|---|
| `01-install-sdk.sh` | JRE, X, openbox, adb; Android command-line tools; the system image |
| `02-create-avd.sh` | Creates the AVD, then pins resolution, density, RAM and storage into `config.ini` |
| `03-launch-emulator.sh` | systemd unit that runs the emulator on the console via `xinit`, sets the device identity, exposes adb |

Placeholders are `__LIKE_THIS__`; rendering refuses to ship a script with any left unfilled, because a stray token would reach the guest as literal shell.

### What to expect

First build downloads the SDK and a system image — a few GB, several minutes. After that, Android itself takes a couple of minutes to boot; watch it with `console screenshot`, or poll `android status` for `boot_completed`.

Use `--as-template` once and clone thereafter.

### A real Android-x86 install instead of the emulator

Everything above runs the Google SDK emulator inside a Debian VM. For a genuine Android device network stack — most commonly, driving Android's own proxy settings from an external tool of your choice — there is a second, separate path: installing the [android-x86](https://www.android-x86.org/) project's own OS as a real QEMU guest, with no Debian host underneath it.

```bash
proxmox-lab recipe android-x86
```

prints the full machine-readable runbook (verified ISO URL and checksum, VM spec, install stages). Two things worth knowing before using it:

- **Bridge choice determines proxy reachability.** Put the guest's network on whatever bridge your own workstation (or proxy tool) can reach directly — usually the same one the Proxmox host's management IP is on. An isolated lab-only bridge is not routable from outside; verified live that neither `adb` nor a raw TCP connection reaches a guest placed there.
- **The 9.0-r2 setup wizard can loop.** After reaching the Google Services step it may repeatedly return to "Copy apps & data" instead of completing. If that happens, switch to the on-device root shell (`Alt+F1` from the graphical console) and run `settings put secure user_setup_complete 1 && settings put global device_provisioned 1`, then power-cycle with a full stop/start (not just a reset, which left the virtio NIC uninitialized in testing) to land directly on the home screen.

Once booted, `adb shell settings put global http_proxy <host>:<port>` points the device's traffic at any HTTP(S)-capable proxy — verified live by watching a real `CONNECT` request arrive at a plain listener. This project does not wire the recipe to a specific proxy; bring your own.

## Troubleshooting

Keep all existing paragraphs; flags unchanged and verified. No information removed.

- **Read this before choosing ARM — Proxmox is x86-64 KVM and cannot accelerate another architecture.** An `arm64-v8a` image runs under full software emulation with no KVM. On a mid-range desktop boots take many minutes and UI is single-digit FPS. Default is `x86_64`; API 30+ ships ARM-to-x86 translation so most `arm64-v8a`-only apps still run. Reach for `--abi arm64-v8a` only for genuine ARM execution (`src/proxmox_agent_lab/android.py` `--abi` choices `x86_64`, `arm64-v8a`).

  | | `x86_64` (default) | `arm64-v8a` |
  |---|---|---|
  | Acceleration | nested KVM | none — pure emulation |
  | Usable UI | yes | barely |
  | ARM-only apps | mostly, via translation | natively |

- **Nested virtualisation must be on for `x86_64` to be fast.** `options kvm_amd nested=1` (or `kvm_intel`). Build warns via `kvm-ok` if guest cannot see KVM.

- **Model spoofing does not stick:** `ro.product.model` baked into `build.prop`; `emulator -prop` cannot override a set read-only property. `minimal` verified: right screen, wrong model. Treat profile as shape, verify with `android status`.

- **Template conversion needs a stopped guest:** Proxmox refuses to template a running guest; `android template` shuts down cleanly first and refuses if already a template. A templated device is `retain`.

- **Android-x86 bridge/proxy and wizard loop:** See *A real Android-x86 install* — choose routable bridge for proxy, use `settings put secure user_setup_complete 1 && settings put global device_provisioned 1` at `Alt+F1` and full stop/start if wizard loops.
