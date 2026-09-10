# Host hardware inspection

`proxmox-lab host` reads physical host state that the Proxmox API does not
expose. Both commands use the opt-in host SSH channel (`[memflow] enabled`,
`ssh_host`, `ssh_key`) and are read-only.

```bash
proxmox-lab host sensors    # temperature sensors (hwmon + thermal zones)
proxmox-lab host macs       # network interfaces, MACs, wireless flag
```

`host sensors` reports each `temp*_input` under `/sys/class/hwmon` and each
populated `/sys/class/thermal/thermal_zone*`, in millidegrees and degrees C.
Chips with no readable sensor are omitted.

`host macs` lists every interface except `lo`, its MAC, whether it is
wireless (has a `phy80211`/`wireless` sysfs link), and its operstate. Use it
to find the wireless MAC for `power.wowlan_mac` or to confirm the wired MAC
for `power.mac`.
