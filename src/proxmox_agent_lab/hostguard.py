"""The host-side lease guard.

A controller finalises its own leases. The failure this covers is the one that
actually happens: the controller goes away -- laptop closed, process killed,
someone on holiday -- and its guests keep running. This project's own lab had
a lease sit `active` for eight days that way, holding the host awake the whole
time, because nothing on the host itself was watching.

So a small script runs on the Proxmox host under root, on a timer, and reads
the shared ledger the controllers write to. Guests tagged with a lease whose
last lifecycle event says it is over, or whose heartbeat has gone quiet past
the grace window, are stopped.

Stopped, never destroyed. A guest the guard stops can be started again and its
disk inspected; a guest it deleted is gone, and the guard is the component
running unattended with the least context about what the work was worth.
Destroying stays with the controller, which knows the lease's policy.

It then powers the host off, because a lab that cleans up but stays awake has
only solved half the problem -- the eight-day lease also meant eight days of
electricity. The condition for that is deliberately blunt: **no guest running
at all**, infrastructure aside. Not "no lease the ledger knows about" -- a
controller that has not been upgraded yet writes nowhere this guard can read,
and its work would look like an idle host. If something is running, somebody
may be using it.

The power-off does make one ledger consult: the long-term pin. A long-term
lease promises the host stays on even with every guest stopped, and only a
controller new enough to have the feature can create one -- so trusting the
ledger there cannot pull the power out from under an un-upgraded controller.
The same field answers the stop question: a long-term lease never heartbeats
by design, so its silence must not read as abandonment. Only a recorded end
(`lease-end`, `long-term-destroyed`, `long-term-released`) ends one.

Installed by `proxmox-lab journal host-setup` alongside the ledger container.
"""

from __future__ import annotations
from pathlib import Path

# Written to /usr/local/lib/pxl-hostguard.py on the Proxmox host. Standard
# library plus PyMySQL, which the ledger container's host already needs.
GUARD_SCRIPT = (Path(__file__).parent / "resources" / "pxl-hostguard.py").read_text()


# systemd timer rather than a crontab line: it survives a reboot without an
# @reboot entry, logs to the journal, and will not stack runs if one is slow.
GUARD_UNITS = r'''
cat > /etc/systemd/system/pxl-hostguard.service <<'UNIT'
[Unit]
Description=proxmox-agent-lab lease guard
After=network-online.target pve-cluster.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/pxl-hostguard.py
UNIT

cat > /etc/systemd/system/pxl-hostguard.timer <<'UNIT'
[Unit]
Description=Run the proxmox-agent-lab lease guard every 10 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now pxl-hostguard.timer >/dev/null 2>&1 || true
'''
