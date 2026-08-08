#!/bin/bash
set -euo pipefail
export HOME="${HOME:-/root}"
cat > /usr/local/bin/pxl-android <<'RUNEOF'
#!/bin/bash
export ANDROID_SDK_ROOT=__SDK__
export ANDROID_HOME=__SDK__
export PATH="$PATH:__SDK__/platform-tools:__SDK__/emulator"
# The emulator's own window is the VM's screen, so console screenshot/click
# and share links work on it with no Android-specific plumbing.
exec emulator -avd __NAME__ \
  -no-audio -no-boot-anim -no-snapshot \
  -gpu swiftshader_indirect \
  -prop ro.product.model="__MODEL__" \
  -prop ro.product.manufacturer="__MANUFACTURER__" \
  -prop ro.product.brand="__BRAND__" \
  -prop ro.product.device="__DEVICE__"
RUNEOF
chmod +x /usr/local/bin/pxl-android

cat > /root/.xinitrc <<'XEOF'
openbox &
exec /usr/local/bin/pxl-android
XEOF

cat > /etc/systemd/system/pxl-android.service <<UNIT
[Unit]
Description=Android emulator on the console
After=network-online.target

[Service]
Environment=HOME=/root
ExecStart=/usr/bin/xinit /root/.xinitrc -- :0 vt1 -keeptty
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now pxl-android
# adb over TCP so apps can be installed from the controller.
(__SDK__/platform-tools/adb -a -P __ADB_PORT__ nodaemon server >/var/log/adb.log 2>&1 &) || true
echo launched
