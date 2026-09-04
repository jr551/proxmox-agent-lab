"""Switching the lab machine on, and forcing it off when it will not go.

Powering *on* cannot use the Proxmox API -- the machine is off. Powering
*off* normally does use the API (a graceful node shutdown); the mechanisms
here are the emergency finaliser for when that fails.

Modes:

* `wake-on-lan`    -- a magic packet. Needs only the NIC's MAC address, works
                      on nearly any desktop, and is the default. It cannot
                      force a machine off, so graceful shutdown is the only
                      path down; that is usually what you want anyway.
* `home-assistant` -- call scripts in Home Assistant, e.g. a smart plug or a
                      KVM that presses the power button.
* `wake-on-lan+home-assistant` -- send the magic packet and trigger the Home
                      Assistant script together on every power-on. Useful
                      when WoL alone is not reliable enough to trust by
                      itself (a NIC that occasionally drops out of suspend,
                      a BIOS that forgets the setting) but a smart-plug/KVM
                      fallback is also available; force-off still goes
                      through Home Assistant, since WoL cannot cut power.
* `command`        -- run a local command. The escape hatch for IPMI, a PDU,
                      or anything with a CLI.
* `none`           -- no remote power control; the user switches it on.
"""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
from typing import Any
from urllib import error, request

from .config import Config, ConfigError
from . import secrets_store


class PowerError(RuntimeError):
    pass


def _mac_bytes(mac: str) -> bytes:
    cleaned = mac.replace(":", "").replace("-", "").replace(".", "").strip()
    if len(cleaned) != 12:
        raise PowerError(f"not a MAC address: {mac!r}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise PowerError(f"not a MAC address: {mac!r}") from None


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + _mac_bytes(mac) * 16


def wake_on_lan(mac: str, broadcast: str, port: int = 9) -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Send to the configured broadcast and the global one: a router that
        # drops directed broadcasts often still passes 255.255.255.255.
        targets = {broadcast or "255.255.255.255", "255.255.255.255"}
        errors: list[str] = []
        for target in targets:
            try:
                sock.sendto(packet, (target, port))
            except OSError as exc:
                errors.append(f"could not send to {target}:{port}: {exc}")
        if len(errors) == len(targets):
            raise PowerError("; ".join(errors))


def _home_assistant(config: Config, entity_id: str) -> None:
    url = config.require(
        "power.home_assistant_url", "the base URL of your Home Assistant"
    )
    try:
        token = secrets_store.get(config, "home-assistant-token")
    except secrets_store.SecretError as exc:
        raise PowerError(str(exc)) from None
    payload = json.dumps({"entity_id": entity_id}).encode()
    req = request.Request(
        url.rstrip("/") + "/api/services/script/turn_on",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            if response.status not in (200, 201):
                raise PowerError(f"Home Assistant returned {response.status}")
    except error.HTTPError as exc:
        raise PowerError(f"Home Assistant HTTP {exc.code} for {entity_id}")
    except (error.URLError, TimeoutError, OSError) as exc:
        raise PowerError(f"Home Assistant unreachable: {exc}")


def _run_command(command: str, label: str) -> None:
    result = subprocess.run(
        shlex.split(command), capture_output=True, text=True, timeout=120,
        check=False,
    )
    if result.returncode:
        raise PowerError(
            f"{label} command failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )


def power_on(config: Config) -> dict[str, Any]:
    """Ask the machine to switch on. Does not wait for it."""
    mode = config.power.get("mode", "wake-on-lan")
    if mode == "wake-on-lan":
        mac = config.require(
            "power.mac", "the lab machine's NIC MAC, with Wake-on-LAN enabled in its BIOS"
        )
        wake_on_lan(mac, config.power.get("broadcast", ""),
                    int(config.power.get("wol_port", 9)))
        return {"mode": mode, "sent": "magic packet", "mac": mac}
    if mode == "home-assistant":
        entity = config.require("power.entity_on", "a Home Assistant script entity")
        _home_assistant(config, entity)
        return {"mode": mode, "entity_id": entity}
    if mode == "wake-on-lan+home-assistant":
        mac = config.require(
            "power.mac", "the lab machine's NIC MAC, with Wake-on-LAN enabled in its BIOS"
        )
        entity = config.require("power.entity_on", "a Home Assistant script entity")
        errors: list[str] = []
        try:
            wake_on_lan(mac, config.power.get("broadcast", ""),
                        int(config.power.get("wol_port", 9)))
        except PowerError as exc:
            errors.append(f"wake-on-lan: {exc}")
        try:
            _home_assistant(config, entity)
        except PowerError as exc:
            errors.append(f"home-assistant: {exc}")
        if len(errors) == 2:
            raise PowerError("; ".join(errors))
        return {
            "mode": mode, "sent": "magic packet + home-assistant script",
            "mac": mac, "entity_id": entity, "errors": errors or None,
        }
    if mode == "command":
        command = config.require("power.on_command")
        _run_command(command, "power-on")
        return {"mode": mode, "command": command}
    if mode == "none":
        raise PowerError(
            "power.mode is 'none': switch the machine on yourself, then re-run"
        )
    raise ConfigError(f"unknown power.mode: {mode!r}")


def can_force_off(config: Config) -> bool:
    mode = config.power.get("mode", "wake-on-lan")
    if mode in ("home-assistant", "wake-on-lan+home-assistant"):
        return bool(config.power.get("entity_off"))
    if mode == "command":
        return bool(config.power.get("off_command"))
    return False


def force_off(config: Config) -> dict[str, Any]:
    """Emergency finaliser, only after a graceful shutdown has failed."""
    mode = config.power.get("mode", "wake-on-lan")
    if mode in ("home-assistant", "wake-on-lan+home-assistant"):
        # Wake-on-LAN cannot cut power, so Home Assistant is the only path
        # down in the composite mode too.
        entity = config.require(
            "power.entity_off", "a Home Assistant script that cuts power"
        )
        _home_assistant(config, entity)
        return {"mode": mode, "entity_id": entity}
    if mode == "command":
        command = config.require("power.off_command")
        _run_command(command, "force-off")
        return {"mode": mode, "command": command}
    raise PowerError(
        f"power.mode {mode!r} cannot force the machine off. The graceful "
        "shutdown did not complete; switch it off by hand and check the host."
    )
