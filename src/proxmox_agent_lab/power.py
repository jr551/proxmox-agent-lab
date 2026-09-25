"""Host power control: Wake-on-LAN, and shutdown that is verified, never assumed.

``wake`` sends the standard Wake-on-LAN magic packet (6 x 0xFF + 16 copies of
the MAC) to the configured broadcast address over stdlib UDP. ``shutdown_verified``
initiates a host shutdown and then *proves* the power-off by repeated probe
failure across ssh AND TCP :22 -- the host being down is a verified fact, never
assumed from the shutdown request having been sent.

A shutdown that cannot be confirmed within the timeout is reported loudly
(``host_powered_off: false``) and is the caller's non-zero exit. There is no
force-off path: a host that refuses to die is reported, not killed.
"""

from __future__ import annotations

import re
import socket
import time
from typing import Any, Callable

from .errors import LabError


class PowerError(LabError):
    """A power operation could not be carried out as configured."""


#: One shutdown-verification round probes both transports, in this order; the
#: ``probe_fn`` calls therefore alternate ``"ssh"``/``"tcp"`` throughout.
_PROBE_KINDS: tuple[str, str] = ("ssh", "tcp")

#: Seconds ``shutdown_verified`` sleeps between probe rounds.
_PROBE_INTERVAL = 5.0

_MAC_HEX = re.compile(r"[0-9a-fA-F]{12}")


def build_magic_packet(mac: str) -> bytes:
    """Return the Wake-on-LAN magic packet for the hardware address ``mac``.

    The packet is exactly ``b"\\xff" * 6 + mac_bytes * 16``: six 0xFF bytes
    followed by sixteen copies of the six MAC bytes. Separators (``:``, ``-``,
    ``.``) are optional, but anything that does not parse to six hex bytes
    raises ``PowerError`` -- a malformed MAC never produces a wrong packet.
    """
    if not isinstance(mac, str):
        raise PowerError(f"not a MAC address: {mac!r}")
    cleaned = mac.strip().replace(":", "").replace("-", "").replace(".", "")
    if _MAC_HEX.fullmatch(cleaned) is None:
        raise PowerError(f"not a MAC address: {mac!r}")
    mac_bytes = bytes.fromhex(cleaned)
    return b"\xff" * 6 + mac_bytes * 16


def _default_sender(
    sock: Callable[..., socket.socket], packet: bytes, addr: tuple[str, int]
) -> None:
    """Default ``wake`` transport: one UDP datagram to ``addr``.

    ``sock`` is the socket factory (``socket.socket``). The packet goes out on
    a fresh ``(AF_INET, SOCK_DGRAM)`` socket with ``SO_BROADCAST`` set -- the
    broadcast address is not reachable without it -- and the socket is closed
    in a ``finally`` so a failed send never leaks a descriptor.
    """
    handle = sock(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        handle.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        handle.sendto(packet, addr)
    finally:
        handle.close()


def wake(config, *, sender: Callable[..., None] | None = None) -> dict[str, Any]:
    """Send the Wake-on-LAN magic packet for the configured host.

    The packet is addressed to ``config.power.broadcast:config.power.port`` and
    carries the MAC from ``config.power.mac``. ``sender(sock, packet, addr)``
    is the transport seam: it receives the socket factory, the magic packet and
    the ``(broadcast, port)`` address; the default sender builds the UDP socket
    described in ``_default_sender``. No socket is created when a sender is
    injected, so callers and tests can supply their own transport.

    Returns ``{"sent": True, "mac": ..., "broadcast": ..., "port": ...}``. The
    MAC is configuration, not a secret; audit redaction is the auditor's job.
    A malformed configured MAC raises ``PowerError`` before anything is sent.
    """
    mac = config.power.mac
    packet = build_magic_packet(mac)
    broadcast = config.power.broadcast
    port = int(config.power.port)
    send = _default_sender if sender is None else sender
    send(socket.socket, packet, (broadcast, port))
    return {"sent": True, "mac": mac, "broadcast": broadcast, "port": port}


def shutdown_verified(
    *,
    request_fn: Callable[[], Any],
    probe_fn: Callable[[str], bool],
    timeout: float = 60.0,
    min_failures: int = 6,
    window: float = 30.0,
    sleep: Callable[[float], Any] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Request host shutdown, then verify the host actually powered off.

    Host power-off is VERIFIED by repeated probe failure across ssh AND TCP
    :22, never assumed. ``request_fn()`` is called exactly once -- the caller
    passes the detached ``shutdown -h now`` request, which must not block on a
    dying sshd. The probe loop then alternates ``probe_fn("ssh")`` and
    ``probe_fn("tcp")`` calls; one round is one of each. A round counts as a
    failure only when BOTH probes report the host not answering (a probe that
    raises ``OSError``/``TimeoutError`` counts as not answering). Any round
    where a probe still reaches the host resets the failure count to zero --
    a host that answers is plainly still up.

    ``host_powered_off`` is ``True`` only when at least ``min_failures``
    all-fail rounds have occurred AND at least ``window`` seconds have elapsed
    since the shutdown request: six quick probe failures inside the window are
    not proof of power-off. A timeout is reported loudly -- ``host_powered_off``
    is ``False`` and the caller must exit non-zero; a shutdown that cannot be
    confirmed is never silently rounded up to success. There is no force-off
    path.

    Returns ``{"host_powered_off": bool, "failures": int, "elapsed": float}``.
    ``sleep``/``now`` are injectable so tests are instant and deterministic.
    """
    request_fn()
    start = now()
    failures = 0
    while True:
        elapsed = now() - start
        if elapsed >= timeout:
            return {"host_powered_off": False, "failures": failures, "elapsed": elapsed}
        reachable = False
        for kind in _PROBE_KINDS:
            try:
                if probe_fn(kind):
                    reachable = True
            except (OSError, TimeoutError):
                pass  # a probe that cannot complete: the host is not answering
        if reachable:
            failures = 0
        else:
            failures += 1
            elapsed = now() - start
            if failures >= min_failures and elapsed >= window:
                return {
                    "host_powered_off": True,
                    "failures": failures,
                    "elapsed": elapsed,
                }
        sleep(_PROBE_INTERVAL)
