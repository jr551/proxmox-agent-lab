"""qemu-guest-agent primitives: exec, file write, readiness, bootstrap.

These wrap the Proxmox ``agent`` endpoints and the serial bootstrap path
behind them. `lab` is the shared CLI facade, as in every feature module;
serial access comes from `serial` so nothing here reaches back into console
handlers.
"""
from __future__ import annotations

from . import serial as serial_module
from typing import Any
import base64
import secrets
import time


def agent_exec(lab: Any, api: Any, vmid: int, command: list[str], *,
               input_data: str | None = None, timeout: int = 300) -> dict[str, Any]:
    """Run a command through qemu-guest-agent and wait for its result."""
    payload: dict[str, Any] = {"command": command}
    if input_data is not None:
        payload["input-data"] = base64.b64encode(input_data.encode()).decode()
    started = api.call(
        "POST", f"/nodes/{lab.NODE}/qemu/{vmid}/agent/exec", payload
    )
    pid = started.get("pid") if isinstance(started, dict) else None
    if pid is None:
        raise lab.LabError(f"guest agent did not return a pid: {started}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = api.call(
            "GET", f"/nodes/{lab.NODE}/qemu/{vmid}/agent/exec-status", {"pid": pid}
        )
        if status.get("exited"):
            def decode(field: str) -> str:
                # Proxmox already decodes what qemu-guest-agent base64s, so
                # out-data/err-data arrive as plain text. Decoding again
                # corrupts any output that is *coincidentally* valid base64 --
                # a bare timestamp, a hex digest -- while everything else
                # raises and silently falls through looking correct.
                raw = status.get(field, "")
                return raw if isinstance(raw, str) else ""

            exitcode = status.get("exitcode")
            signal = status.get("signal")
            if exitcode is None and signal is not None:
                # qemu-guest-agent reports either exitcode or signal, never
                # both. Every caller checks `exitcode not in (0, None)` to
                # decide success -- leaving this as None would make a
                # signal-killed process (OOM, crash, an external kill) look
                # like the "no code available" case serial legitimately
                # has, instead of the failure it actually is. 128+signal is
                # the standard shell convention for "killed by signal N".
                exitcode = 128 + int(signal)
            return {
                "exitcode": exitcode,
                "signal": signal,
                "stdout": decode("out-data"),
                "stderr": decode("err-data"),
                "truncated": bool(
                    status.get("out-truncated") or status.get("err-truncated")
                ),
            }
        time.sleep(1)
    raise lab.LabError(f"guest command did not finish within {timeout}s")


def agent_ready(lab: Any, api: Any, vmid: int) -> bool:
    try:
        api.call("POST", f"/nodes/{lab.NODE}/qemu/{vmid}/agent/ping")
        return True
    except lab.LabError:
        return False


def wait_agent_ready(lab: Any, api: Any, vmid: int, timeout: int,
                     interval: float = 5) -> bool:
    """Poll until the guest agent responds, or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if agent_ready(lab, api, vmid):
            return True
        time.sleep(interval)
    return False


def write_guest_file(lab: Any, api: Any, vmid: int, path: str,
                     content: str) -> None:
    """Write a file into a guest without the content touching the ledger."""
    api.call(
        "POST",
        f"/nodes/{lab.NODE}/qemu/{vmid}/agent/file-write",
        {
            "file": path,
            "content": base64.b64encode(content.encode()).decode(),
            "encode": 0,
        },
    )


def exec_guest(lab: Any, api: Any, vmid: int, argv: list[str],
               timeout: int = 300) -> dict[str, Any]:
    """Run a command through the guest agent (argv form)."""
    return agent_exec(lab, api, vmid, argv, timeout=timeout)


def exec_guest_script(lab: Any, api: Any, vmid: int, script: str,
                      timeout: int = 300) -> dict[str, Any]:
    """Run a shell script through the guest agent via bash.

    bash, not /bin/sh: the scripts this runs declare ``#!/bin/bash`` and open
    with ``set -euo pipefail``. dash only gained ``pipefail`` in 0.5.12
    (Debian 13, Ubuntu 24.04), so on an older guest image /bin/sh aborts on
    the second line. Run them under the interpreter they declare.
    """
    return exec_guest(lab, api, vmid, ["/bin/bash", "-c", script], timeout=timeout)


def ensure_agent(lab: Any, api: Any, vmid: int, cloud_user: str,
                 password: str, timeout: int) -> None:
    """Wait for the agent; bootstrap over serial when it never appears."""
    if wait_agent_ready(lab, api, vmid, timeout):
        return
    bootstrap_guest_agent(lab, api, vmid, cloud_user, password)


def clear_bootstrap_password(lab: Any, api: Any, vmid: int) -> bool:
    """Delete the one-time cipassword from the VM config. Returns cleared."""
    try:
        api.call(
            "PUT",
            f"/nodes/{lab.NODE}/qemu/{vmid}/config",
            {"delete": "cipassword"},
        )
        return True
    except lab.LabError:
        return False


def prepare_cloudinit_worker(
    lab: Any,
    api: Any,
    vmid: int,
    template_vmid: int,
    config_updates: dict[str, Any],
    *,
    agent_timeout: int = 120,
    start_timeout: int = 180,
) -> tuple[str, str]:
    """Shared cloud-init bootstrap: password, config, start, agent wait.

    Generates a one-time password, resolves the template's cloud user,
    applies ``config_updates`` plus the cloud-init identity, starts the guest,
    and waits for the agent (bootstrapping over serial when needed). The
    caller must call ``clear_bootstrap_password`` when the post-boot
    provisioning has finished. Returns ``(cloud_user, password)``.
    """
    password = secrets.token_urlsafe(18)
    template_config = api.call(
        "GET", f"/nodes/{lab.NODE}/qemu/{template_vmid}/config"
    )
    cloud_user = template_config.get("ciuser") or "debian"
    payload: dict[str, Any] = {
        "ciuser": cloud_user,
        "cipassword": password,
        "agent": "enabled=1",
        "onboot": 0,
        **config_updates,
    }
    api.call("PUT", f"/nodes/{lab.NODE}/qemu/{vmid}/config", payload)
    start = api.call("POST", f"/nodes/{lab.NODE}/qemu/{vmid}/status/start")
    lab.wait_task(api, start, timeout=start_timeout)
    ensure_agent(lab, api, vmid, cloud_user, password, agent_timeout)
    return cloud_user, password


def bootstrap_guest_agent(lab: Any, api: Any, vmid: int, user: str,
                          password: str) -> None:
    """Install qemu-guest-agent through the serial.

    Generic cloud images have no guest agent, so there is no way in until one
    exists. The serial console is the only channel that needs nothing
    preinstalled.
    """
    with serial_module.TermSession(lab, api, "qemu", vmid, timeout=30) as term:
        try:
            term.login(user, password)
        except (TimeoutError, RuntimeError) as exc:
            raise lab.LabError(f"serial login to the gateway failed: {exc}")
        term.run(
            "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq "
            "&& sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
            "qemu-guest-agent",
            timeout=600,
        )
        term.run("sudo systemctl enable --now qemu-guest-agent", timeout=120)
    if wait_agent_ready(lab, api, vmid, 180):
        lab.audit("guest-agent-bootstrapped", vmid=vmid, via="serial")
        return
    raise lab.LabError(
        "installed qemu-guest-agent over serial but the agent still does not "
        "answer; check 'console text --vmid %s'" % vmid
    )
