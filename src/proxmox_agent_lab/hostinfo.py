"""Read-only host hardware inspection over the opt-in SSH channel.

These commands answer "what is the machine doing physically" -- temperature
sensors and network interfaces -- without needing the Proxmox API to expose
them. They use the same gated host SSH transport as memflow/usb/netcap, so
they are unavailable until that channel is configured.
"""
from __future__ import annotations

import json
from typing import Any

from . import host_transport


def _read_host(lab: Any, script: str) -> str:
    proc = host_transport.host_run(
        lab, ["sh", "-c", script], timeout=30)
    if proc.returncode != 0:
        raise lab.LabError(
            f"host inspection failed: {(proc.stderr or '').strip()[:300]}")
    return proc.stdout


_HWMON_SCRIPT = r'''
for h in /sys/class/hwmon/hwmon*; do
  [ -d "$h" ] || continue
  name=$(cat "$h/name" 2>/dev/null)
  for t in "$h"/temp*_input; do
    [ -f "$t" ] || continue
    v=$(cat "$t" 2>/dev/null)
    [ -n "$v" ] && printf '%s\t%s\t%s\n' "$name" "$(basename "$t")" "$v"
  done
done
for z in /sys/class/thermal/thermal_zone*; do
  [ -d "$z" ] || continue
  type=$(cat "$z/type" 2>/dev/null)
  v=$(cat "$z/temp" 2>/dev/null)
  [ -n "$v" ] && printf 'thermal\t%s\t%s\n' "$type" "$v"
done
exit 0
'''

_NET_SCRIPT = r'''
for i in /sys/class/net/*; do
  n=$(basename "$i")
  [ "$n" = "lo" ] && continue
  mac=$(cat "$i/address" 2>/dev/null)
  [ -n "$mac" ] || continue
  wireless=no
  [ -d "$i/wireless" ] || [ -e "$i/phy80211" ] && wireless=yes
  oper=$(cat "$i/operstate" 2>/dev/null)
  printf '%s\t%s\t%s\t%s\n' "$n" "$mac" "$wireless" "$oper"
done
exit 0
'''


def cmd_sensors(lab: Any, args: Any) -> None:
    out = _read_host(lab, _HWMON_SCRIPT)
    sensors: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        chip, label, raw = parts
        try:
            millideg = int(raw)
        except ValueError:
            continue
        sensors.append({"chip": chip, "sensor": label,
                        "millidegree_c": millideg,
                        "degree_c": round(millideg / 1000.0, 1)})
    print(json.dumps({"host": host_transport.SSH_HOST, "sensors": sensors},
                     indent=2, sort_keys=True))


def cmd_macs(lab: Any, args: Any) -> None:
    out = _read_host(lab, _NET_SCRIPT)
    interfaces: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        name, mac, wireless, oper = parts
        interfaces.append({"interface": name, "mac": mac,
                           "wireless": wireless == "yes",
                           "operstate": oper})
    print(json.dumps({"host": host_transport.SSH_HOST,
                      "interfaces": interfaces}, indent=2, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    from .cli import _bind
    parser = sub.add_parser(
        "host", help="read-only host hardware inspection (sensors, MACs)")
    commands = parser.add_subparsers(dest="host_command", required=True)
    sensors = commands.add_parser(
        "sensors", help="read temperature sensors via the host SSH channel")
    sensors.set_defaults(func=_bind(lab, cmd_sensors))
    macs = commands.add_parser(
        "macs", help="list host network interfaces and MAC addresses")
    macs.set_defaults(func=_bind(lab, cmd_macs))
