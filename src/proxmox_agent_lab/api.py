"""Proxmox HTTPS transport and bounded task waiting.

This module owns the API client only: TLS setup, request construction, error
mapping, and polling a task to completion. It knows nothing about leases,
audit, or command parsing -- the configuration, API root, token identity, and
the secret provider are supplied by the caller (``cli`` binds the process-wide
configuration and keeps the names patchable for tests).
"""

from __future__ import annotations

import json
import ssl
import time
from typing import Any
from urllib import error, parse, request

from . import secrets_store
from .errors import LabError


_TOKEN_CACHE: str | None = None


def token_secret(config: Any, *, host: str, node: str) -> str:
    """The Proxmox API token secret, from whichever backend is configured.

    Cached for the life of the process. Reading the store may spawn a
    subprocess, and this is called on every single API request -- building a
    VM makes hundreds, and paying a process spawn for each was pure waste.
    """
    global _TOKEN_CACHE
    if _TOKEN_CACHE is not None:
        return _TOKEN_CACHE
    if not host or not node:
        raise LabError(
            "This install is not configured yet. Run 'proxmox-lab init' to "
            "create a config file, then fill in [proxmox] host and node."
        )
    try:
        _TOKEN_CACHE = secrets_store.get(config, "proxmox-token")
    except secrets_store.SecretError as exc:
        raise LabError(str(exc)) from None
    return _TOKEN_CACHE


class ProxmoxAPI:
    """Minimal HTTPS client for the Proxmox API.

    ``token_secret`` is a zero-argument callable returning the API token so
    the credential is fetched lazily and stays out of this module.
    """

    def __init__(
        self,
        *,
        config: Any,
        api_root: str,
        token_user: str,
        token_name: str,
        token_secret: Any,
    ) -> None:
        self._config = config
        self._api_root = api_root
        self._token_user = token_user
        self._token_name = token_name
        self._token_secret = token_secret
        self._ssl = ssl.create_default_context(
            cafile=config.proxmox.get("ca_file") or None
        )
        if not bool(config.proxmox.verify_tls):
            # A fresh Proxmox install has a self-signed certificate, so this
            # is off by default. Set [proxmox] verify_tls once you have put a
            # trusted certificate on the host.
            self._ssl.check_hostname = False
            self._ssl.verify_mode = ssl.CERT_NONE

    def call(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        timeout: int = 30,
    ) -> Any:
        from .host_policy import check_api
        check_api(self._config, method, path, data)
        method = method.upper()
        if not path.startswith("/"):
            path = "/" + path
        url = self._api_root + path
        payload: bytes | None = None
        if method in ("GET", "DELETE") and data:
            url += "?" + parse.urlencode(data, doseq=True)
        elif data:
            payload = parse.urlencode(data, doseq=True).encode()
        token = self._token_secret()
        req = request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Authorization": (
                    f"PVEAPIToken={self._token_user}!{self._token_name}={token}"
                ),
                "Accept": "application/json",
            },
        )
        try:
            with request.urlopen(req, context=self._ssl, timeout=timeout) as response:
                body = json.load(response)
        except error.HTTPError as exc:
            if exc.code == 596:
                raise LabError(
                    f"Proxmox HTTP 596 for {method} {path}: guest agent is not "
                    "responding; the guest may be hung or its storage offline. "
                    "Try console screenshot or serial instead."
                ) from None
            detail = exc.read().decode(errors="replace")[:1000]
            raise LabError(f"Proxmox HTTP {exc.code} for {method} {path}: {detail}")
        except (error.URLError, TimeoutError, OSError) as exc:
            raise LabError(f"Proxmox unavailable for {method} {path}: {exc}")
        return body.get("data")

    def reachable(self) -> bool:
        try:
            self.call("GET", "/version", timeout=4)
            return True
        except LabError:
            return False


def wait_task(api: ProxmoxAPI, node: str, upid: str,
              timeout: int = 180) -> dict[str, Any]:
    """Poll a Proxmox task until it stops or the deadline passes."""
    deadline = time.monotonic() + timeout
    encoded = parse.quote(upid, safe="")
    while time.monotonic() < deadline:
        status = api.call("GET", f"/nodes/{node}/tasks/{encoded}/status")
        if status.get("status") == "stopped":
            if status.get("exitstatus") != "OK":
                raise LabError(
                    f"Proxmox task failed: {status.get('exitstatus', 'unknown')}"
                )
            return status
        time.sleep(2)
    raise LabError(f"Timed out waiting for Proxmox task {upid}")
