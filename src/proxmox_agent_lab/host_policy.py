"""Controller-side capability policy for container-only VPS installations."""
from urllib.parse import unquote


def lxc_only(config):
    mode = config.proxmox.get("guest_mode", "all")
    if mode not in ("all", "lxc-only"):
        raise ValueError("[proxmox] guest_mode must be all or lxc-only")
    return mode == "lxc-only"


def check_api(config, method, path, data=None):
    if not lxc_only(config):
        return
    normalized = unquote(path).split("?", 1)[0]
    parts = [part for part in normalized.split("/") if part]
    if "qemu" in parts:
        raise ValueError("This VPS is LXC-only; QEMU operations are disabled")
    if method.upper() != "GET" and len(parts) == 3 and parts[0] == "nodes" and parts[2] == "status":
        raise ValueError("VPS host power operations are disabled")
    if method.upper() in ("POST", "PUT") and "lxc" in parts:
        data = data or {}
        if parts[-1] == "clone":
            raise ValueError("LXC cloning is not supported in VPS mode; create an unprivileged container instead")
        if "unprivileged" in data and str(data["unprivileged"]).lower() not in ("1", "true"):
            raise ValueError("This VPS permits only unprivileged LXC containers")
        if parts[-1] == "lxc" and str(data.get("unprivileged", "0")).lower() not in ("1", "true"):
            raise ValueError("LXC creation on this VPS requires unprivileged=1")


def check_command(config, command):
    # These use host SSH or VM-specific helpers in addition to the API client.
    if command in ("android", "windows", "pe", "memflow", "usb", "virtio", "disk") and lxc_only(config):
        raise ValueError("This command requires VM/host-device capabilities unavailable in LXC-only VPS mode")
