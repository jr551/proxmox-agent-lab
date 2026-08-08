#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# A previous run's apt may still hold the dpkg lock; wait rather than fail.
APT='apt-get -o DPkg::Lock::Timeout=600'
$APT update -qq
# xserver + openbox so the emulator has somewhere to draw: its window becomes
# the VM's console, which is what makes the existing screenshot, click and
# share commands work on it unchanged.
# The emulator ships its own QEMU, which links against pulseaudio, nss and
# assorted X libraries whether or not you pass -no-audio. Missing any of them
# fails at exec time with a bare "cannot open shared object file".
$APT install -y -qq curl unzip default-jre-headless xserver-xorg xinit \
  openbox x11-xserver-utils adb cpu-checker mesa-utils \
  libpulse0 libnss3 libasound2t64 libgl1 libx11-xcb1 libxcb-cursor0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
  libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libfontconfig1 \
  libdbus-1-3 libxkbcommon-x11-0 >/dev/null \
  || $APT install -y -qq libpulse0 libnss3 libasound2 libgl1 >/dev/null

if [ ! -d __SDK__/cmdline-tools/latest ]; then
  mkdir -p __SDK__/cmdline-tools
  curl -fsSL -o /tmp/cmdline.zip \
    https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip
  unzip -q -o /tmp/cmdline.zip -d /tmp/cmdline
  rm -rf __SDK__/cmdline-tools/latest
  mv /tmp/cmdline/cmdline-tools __SDK__/cmdline-tools/latest
fi

export ANDROID_SDK_ROOT=__SDK__
export ANDROID_HOME=__SDK__
BIN=__SDK__/cmdline-tools/latest/bin
yes | "$BIN/sdkmanager" --licenses >/dev/null 2>&1 || true
# An interrupted sdkmanager can leave a duplicate package directory, which it
# then warns about on every later call.
rm -rf __SDK__/platform-tools-2
IMAGE_DIR=__SDK__/system-images/android-__API__/__IMAGE_TYPE__/__ABI__
# An interrupted download leaves a stub directory that sdkmanager considers
# installed; avdmanager then fails much later with "contains no system
# images". Check the size and redo it rather than inherit the wreckage.
if [ -d "$IMAGE_DIR" ] && [ "$(du -sm "$IMAGE_DIR" | cut -f1)" -lt 400 ]; then
  echo "system image looks truncated; removing and refetching" >&2
  rm -rf "$IMAGE_DIR"
fi
"$BIN/sdkmanager" --install "platform-tools" "emulator" \
  "system-images;android-__API__;__IMAGE_TYPE__;__ABI__" >/dev/null
[ "$(du -sm "$IMAGE_DIR" | cut -f1)" -ge 400 ] || {
  echo "system image is only $(du -sh "$IMAGE_DIR" | cut -f1) after install" >&2
  exit 1; }

grep -qs ANDROID_SDK_ROOT /etc/profile.d/android.sh 2>/dev/null || cat > /etc/profile.d/android.sh <<'ENVEOF'
export ANDROID_SDK_ROOT=/opt/android-sdk
export ANDROID_HOME=/opt/android-sdk
export PATH="$PATH:/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/opt/android-sdk/cmdline-tools/latest/bin"
ENVEOF

kvm-ok || echo "WARNING: no KVM in this guest; enable nested virtualisation" >&2
echo provisioned
