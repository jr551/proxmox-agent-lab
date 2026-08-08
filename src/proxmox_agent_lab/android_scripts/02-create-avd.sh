#!/bin/bash
set -euo pipefail
# The guest agent runs commands with a bare environment and no HOME, which
# `set -u` turns into a hard failure. The AVD location depends on it, so pin
# both rather than hoping.
export HOME="${HOME:-/root}"
export ANDROID_AVD_HOME="${ANDROID_AVD_HOME:-$HOME/.android/avd}"
mkdir -p "$ANDROID_AVD_HOME"
export ANDROID_SDK_ROOT=__SDK__
export ANDROID_HOME=__SDK__
BIN=__SDK__/cmdline-tools/latest/bin
echo no | "$BIN/avdmanager" create avd -n __NAME__ -k "__PACKAGE__" --force >/dev/null

AVD="$ANDROID_AVD_HOME/__NAME__.avd/config.ini"
[ -f "$AVD" ] || AVD="/root/.android/avd/__NAME__.avd/config.ini"
[ -f "$AVD" ] || { echo "no config.ini for AVD __NAME__" >&2; exit 1; }
set_key() { grep -q "^$1=" "$AVD" && sed -i "s|^$1=.*|$1=$2|" "$AVD" || echo "$1=$2" >> "$AVD"; }

set_key hw.lcd.width __WIDTH__
set_key hw.lcd.height __HEIGHT__
set_key hw.lcd.density __DENSITY__
set_key hw.ramSize __RAM__
set_key vm.heapSize __HEAP__
set_key disk.dataPartition.size __STORAGE__M
set_key hw.keyboard yes
set_key hw.gpu.enabled yes
set_key hw.gpu.mode swiftshader_indirect
set_key showDeviceFrame no
echo created
