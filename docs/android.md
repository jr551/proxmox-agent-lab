# 📱 Android devices

Emulated phones as lab guests — a Galaxy S20, a Pixel, or something small and
quick. They appear on the VM's own console, so every command already here
works on them: screenshot it, click it, type at it, share a link to it.

```bash
proxmox-lab android profiles
proxmox-lab android create --lease "$L" --vmid 9050 --profile galaxy-s20
proxmox-lab console screenshot --vmid 9050        # watch Android boot
```

## ⚠️ Read this before choosing ARM

**Proxmox is x86-64 KVM and cannot accelerate a guest of another
architecture.** An `arm64-v8a` image therefore runs under full software
emulation with no KVM at all. On a mid-range desktop that is not merely slow —
boots take many minutes and the UI manages single-digit frames per second.

So the default is `x86_64`, and it is the right choice for almost everyone:

| | `x86_64` (default) | `arm64-v8a` |
|---|---|---|
| Acceleration | nested KVM | none — pure emulation |
| Usable UI | yes | barely |
| ARM-only apps | mostly, via translation | natively |

Google's x86_64 images from **API 30 onward ship ARM-to-x86 translation**, so
most apps with `arm64-v8a`-only native libraries still run. Reach for
`--abi arm64-v8a` only when you need genuine ARM execution and can wait.

**Nested virtualisation must be on** for x86_64 to be fast. On an AMD host:

```sh
echo "options kvm_amd nested=1" > /etc/modprobe.d/kvm-nested.conf
# Intel: options kvm_intel nested=1
```

The build warns via `kvm-ok` if the guest cannot see KVM.

## 📐 Profiles

A profile sets resolution, density, RAM, storage and Android version, and all
of those are real — `wm size` inside a `galaxy-s20` reports 1440×3200.

**The model string is not.** `ro.product.model` is baked into the system
image's `build.prop`, and a read-only property that is already set cannot be
overridden with `emulator -prop`. A stock image keeps reporting
`sdk_gphone_x86_64`, whatever the profile says. Verified: a `minimal` device
reports the right 720×1600 screen and the wrong model. Spoofing identity
properly needs a modified system image, which this does not ship.

So treat a profile as the phone's **shape**, not its identity, and check with
`android status`, which reports what the device actually says.

| Profile | Screen | Phone RAM | API | Model |
|---|---|---:|---:|---|
| `galaxy-s20` | 1440×3200 @560 | 8 GB | 33 | SM-G980F* |
| `galaxy-s20-ultra` | 1440×3200 @560 | 12 GB | 33 | SM-G988B |
| `galaxy-s10` | 1440×3040 @550 | 8 GB | 31 | SM-G973F |
| `pixel-6` | 1080×2400 @420 | 8 GB | 33 | Pixel 6 |
| `small` | 720×1280 @320 | 2 GB | 30 | generic |
| `minimal` | 720×1600 @320 | 1.5 GB | 30 | generic |

\* requested, not reported — see above.

The VM gets the phone's RAM plus overhead for Debian, X and the emulator
process — so an S20 wants about **11 GB** of host memory, and `minimal` about
**3.6 GB**. On a busy host, start with `minimal`.

## 🏗️ Templates

`--as-template` shuts the device down cleanly when it is built and converts it
to a Proxmox template, so future devices clone in seconds instead of
re-downloading the SDK:

```bash
proxmox-lab android create --lease "$L" --vmid 9050 --profile minimal \
  --as-template

# later, an instant device
proxmox-lab api --lease "$L" --method POST \
  --path /nodes/<node>/qemu/9050/clone --data newid=9051 --wait-task
```

A templated device is registered `retain`, so ordinary cleanup leaves it
alone.

If a device is already built and you decide afterwards that it should be a
template, you do not have to rebuild it:

```bash
proxmox-lab android template --lease "$L" --vmid 9050
```

It shuts the device down cleanly first — Proxmox refuses to template a running
guest — and refuses outright if the VMID is already a template.

## 🖥️ Why it needs no Android-specific viewer

The emulator runs under a bare X session as the VM's console, so its window
*is* the screen Proxmox shows. That means:

```bash
proxmox-lab console screenshot --vmid 9050              # the phone's screen
proxmox-lab console click --lease "$L" --vmid 9050 --x 720 --y 1500
proxmox-lab share create --lease "$L" --vmid 9050       # send someone a link
```

all work unchanged. No separate protocol, no scrcpy, nothing to forward.

## 🔌 adb

```bash
proxmox-lab android status --vmid 9050
proxmox-lab android adb --lease "$L" --vmid 9050 devices
proxmox-lab android adb --lease "$L" --vmid 9050 install /tmp/app.apk
proxmox-lab android adb --lease "$L" --vmid 9050 shell getprop ro.product.model
```

Push an APK in with `proxmox-lab push` first, then install it.

## 🧱 The build scripts

Provisioning is three plain shell scripts under
`src/proxmox_agent_lab/android_scripts/`, shipped as files rather than
embedded strings so you can read, diff and run them by hand:

| Script | What it does |
|---|---|
| `01-install-sdk.sh` | JRE, X, openbox, adb; Android command-line tools; the system image |
| `02-create-avd.sh` | Creates the AVD, then pins resolution, density, RAM and storage into `config.ini` |
| `03-launch-emulator.sh` | systemd unit that runs the emulator on the console via `xinit`, sets the device identity, exposes adb |

Placeholders are `__LIKE_THIS__`; rendering refuses to ship a script with any
left unfilled, because a stray token would reach the guest as literal shell.

## ⏱️ What to expect

First build downloads the SDK and a system image — a few GB, several minutes.
After that, Android itself takes a couple of minutes to boot; watch it with
`console screenshot`, or poll `android status` for `boot_completed`.

Use `--as-template` once and clone thereafter.
