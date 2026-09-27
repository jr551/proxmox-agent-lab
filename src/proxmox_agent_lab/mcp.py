"""The MCP server: stdio JSON-RPC 2.0, stdlib only, no SDK.

``proxmox-lab mcp`` serves the pinned 23-tool surface (rework plan §E) over
stdin/stdout. The wire format is newline-delimited JSON -- one complete
JSON-RPC message per line, UTF-8, with no ``Content-Length`` framing. stdout
carries protocol messages only; everything else goes to stderr.

Every tool is a thin wrapper over the SAME ``cmd_*`` handler the slim CLI
binds: the tool's ``arguments`` become the attributes of an
``argparse.Namespace`` the same handler already receives, so the CLI and MCP
surfaces cannot drift. Handlers print their JSON payload (the CLI contract),
so a call is wrapped in ``redirect_stdout`` and the captured document is
re-emitted as the single ``{"type": "text", "text": ...}`` result body.

Errors are JSON-RPC error objects, never results with an error string:

* ``-32700`` malformed JSON, ``-32601`` unknown method,
* ``-32602`` bad params: unknown tool, schema violation, a missing or false
  ``confirm``, an unknown ``console_keys`` key name. The message names the
  offending field, never the value.
* ``-32603`` action failure: dispatch ran and a ``LabError`` (or anything
  else) escaped. The message is redacted through :func:`audit.redact` --
  no typed text, no command strings, no tracebacks.

Two safety invariants live here (§E):

* Every ``tools/call`` -- read-only or mutating, success or failure --
  refreshes ``last_mcp_activity`` (``leases.record_mcp_activity``) and writes
  an audit event carrying tool name + ok + target (vmid/lease) ONLY. Text,
  command and path arguments are never audited.
* The idle shutdown is the second power-off net: a periodic self-wake
  (``select`` on stdin with a ~60s timeout, so it fires even when the client
  has gone silent) plus a check after every ``tools/call`` run
  ``leases.mcp_idle_shutdown_due`` and, when due, the SAME verified
  power-off path ``cleanup-expired`` uses (``shutdown_host`` ->
  ``power.shutdown_verified``). A failed sweep is logged and audited, never
  raised into the protocol loop.

Deliberate transport detail: ``select`` says bytes are waiting, not that a
whole line has arrived, so reads are raw ``os.read`` chunks into a private
buffer split on ``\\n`` -- a partial JSON-RPC line can never wedge the idle
clock.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import io
import json
import os
import selectors
import shlex
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterator

from . import __version__
from .errors import LabError

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "proxmox-agent-lab"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: How often the server wakes itself to re-check the idle-shutdown clock.
IDLE_CHECK_SECONDS = 60.0

_READ_CHUNK = 65536


class _ParamsError(LabError):
    """A ``tools/call`` shape/violation that maps to ``-32602``."""


# -- input schemas ----------------------------------------------------------
# Exactly the §E table: one entry per tool, JSON Schema with `properties`,
# `required` and `additionalProperties: false`. Built once at import time;
# `tools/list` serves this static list verbatim.


def _schema(*, required: tuple[str, ...] = (), **properties: Any) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    schema["additionalProperties"] = False
    return schema


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}

#: Upper bounds for client-supplied numbers that reach a blocking seam call
#: or a store scan. A tool call must not be able to hold an MCP request open
#: for years; the same cap `transfer._timeout_seconds` enforces on the CLI.
_MAX_TIMEOUT = 86400
_MAX_LIMIT = 1000
_KEYS = {"type": "array", "items": {"type": "string"}}
_KIND = {"type": "string", "enum": ["qemu", "lxc"]}
_STATE = {"type": "string", "enum": ["running", "stopped", "all"]}

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "lease_begin",
        "description": "Open a lease (wakes the host first if it is down).",
        "inputSchema": _schema(
            required=("purpose",),
            purpose=_STR,
            long_term=_BOOL,
            ttl_seconds=_INT,
        ),
    },
    {
        "name": "lease_heartbeat",
        "description": "Extend a lease's expiry and refresh guest metadata.",
        "inputSchema": _schema(
            required=("lease",), lease=_STR, ttl_seconds=_INT
        ),
    },
    {
        "name": "lease_end",
        "description": "Close a lease and tear down what it owned.",
        "inputSchema": _schema(
            required=("lease",), lease=_STR, shared_guests_authorized=_BOOL
        ),
    },
    {
        "name": "lease_list",
        "description": "List leases (active unless include_ended).",
        "inputSchema": _schema(include_ended=_BOOL),
    },
    {
        "name": "lease_destroy",
        "description": "Forcibly destroy a lease and its owned guests.",
        "inputSchema": _schema(
            required=("lease", "confirm"), lease=_STR, confirm=_BOOL
        ),
    },
    {
        "name": "lease_register",
        "description": "Adopt an existing guest into a lease.",
        "inputSchema": _schema(
            required=("lease", "kind", "vmid"),
            lease=_STR,
            kind=_KIND,
            vmid=_INT,
            name=_STR,
            allow_existing=_BOOL,
        ),
    },
    {
        "name": "guest_create",
        "description": "Create a lease-owned guest from the template.",
        "inputSchema": _schema(
            required=("lease_id", "vmid"),
            lease_id=_STR,
            vmid=_INT,
            name=_STR,
            memory=_INT,
            cores=_INT,
            start=_BOOL,
            ostemplate=_STR,
            storage=_STR,
            disk_gb=_INT,
        ),
    },
    {
        "name": "guest_clone",
        "description": "Clone a vouched template into a lease-owned guest.",
        "inputSchema": _schema(
            required=("lease_id", "vmid", "source"),
            lease_id=_STR,
            vmid=_INT,
            source=_INT,
            name=_STR,
            full=_BOOL,
        ),
    },
    {
        "name": "guest_start",
        "description": "Start a lease-owned guest.",
        "inputSchema": _schema(
            required=("lease_id", "vmid"), lease_id=_STR, vmid=_INT
        ),
    },
    {
        "name": "guest_stop",
        "description": "Stop a lease-owned guest (graceful, then hard).",
        "inputSchema": _schema(
            required=("lease_id", "vmid"),
            lease_id=_STR,
            vmid=_INT,
            timeout={**_INT, "maximum": _MAX_TIMEOUT},
            force=_BOOL,
        ),
    },
    {
        "name": "guest_destroy",
        "description": "Destroy a lease-owned guest (irreversible).",
        "inputSchema": _schema(
            required=("lease_id", "vmid", "confirm"),
            lease_id=_STR,
            vmid=_INT,
            confirm=_BOOL,
            purge=_BOOL,
        ),
    },
    {
        "name": "guest_probe",
        "description": "How one guest can be reached right now (read-only).",
        "inputSchema": _schema(required=("vmid",), vmid=_INT),
    },
    {
        "name": "guest_list",
        "description": "Registered guests joined with live state (read-only).",
        "inputSchema": _schema(lease_id=_STR, state=_STATE),
    },
    {
        "name": "guest_run",
        "description": "Run a command in a lease-owned guest.",
        "inputSchema": _schema(
            required=("lease_id", "vmid", "command"),
            lease_id=_STR,
            vmid=_INT,
            command=_STR,
            timeout={**_INT, "maximum": _MAX_TIMEOUT},
            stdin=_STR,
        ),
    },
    {
        "name": "push_file",
        "description": "Copy a local file into a lease-owned guest.",
        "inputSchema": _schema(
            required=("lease_id", "vmid", "local_path", "remote_path"),
            lease_id=_STR,
            vmid=_INT,
            local_path=_STR,
            remote_path=_STR,
        ),
    },
    {
        "name": "pull_file",
        "description": "Copy a file out of a lease-owned guest.",
        "inputSchema": _schema(
            required=("lease_id", "vmid", "local_path", "remote_path"),
            lease_id=_STR,
            vmid=_INT,
            local_path=_STR,
            remote_path=_STR,
        ),
    },
    {
        "name": "console_screenshot",
        "description": "Capture a guest's screen as PNG (base64 in result).",
        "inputSchema": _schema(
            required=("lease_id", "vmid"), lease_id=_STR, vmid=_INT
        ),
    },
    {
        "name": "console_type",
        "description": "Type text at a guest's console (never audited).",
        "inputSchema": _schema(
            required=("lease_id", "vmid", "text"),
            lease_id=_STR,
            vmid=_INT,
            text=_STR,
            enter=_BOOL,
        ),
    },
    {
        "name": "console_keys",
        "description": "Send QEMU key names to a guest (ret, f2, ...).",
        "inputSchema": _schema(
            required=("lease_id", "vmid", "keys"),
            lease_id=_STR,
            vmid=_INT,
            keys=_KEYS,
        ),
    },
    {
        "name": "cleanup_expired",
        "description": "Sweep expired leases; may power off an idle host.",
        "inputSchema": _schema(
            required=("lease_id", "confirm"),
            lease_id=_STR,
            confirm=_BOOL,
            all=_BOOL,
        ),
    },
    {
        "name": "journal_query",
        "description": "Read audit events from the local store (read-only).",
        "inputSchema": _schema(
            lease_id=_STR, since=_STR, limit={**_INT, "maximum": _MAX_LIMIT}
        ),
    },
    {
        "name": "doctor",
        "description": "Check the install end to end (read-only).",
        "inputSchema": _schema(host_checks=_BOOL),
    },
    {
        "name": "power_status",
        "description": "Host reachability and what pins it on (read-only).",
        "inputSchema": _schema(),
    },
)

_TOOL_NAMES = {tool["name"] for tool in TOOLS}
_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}

#: The tools that can destroy: `confirm` must be literally `true` (§E).
_CONFIRM_TOOLS = frozenset({"lease_destroy", "guest_destroy", "cleanup_expired"})

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "array": list,
}


# -- namespace adapters ------------------------------------------------------
# Each adapter builds the `args` namespace its cmd_* handler reads. The tool's
# JSON names land directly; renames (lease_id -> lease, ttl_seconds -> ttl)
# and the defaults the CLI's argparse layer would have supplied are spelled
# out here so both surfaces share one implementation.


def _ns(arguments: dict[str, Any], **attrs: Any) -> argparse.Namespace:
    ns = argparse.Namespace()
    for key, value in arguments.items():
        setattr(ns, key, value)
    for key, value in attrs.items():
        setattr(ns, key, value)
    return ns


def _invoke(
    handler: Callable[[Any, argparse.Namespace], Any],
    lab: Any,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Run one cmd_* handler; capture the JSON document it prints.

    Handlers print their payload (the CLI contract) and some return it too;
    the captured stdout document is the tool result either way so the MCP
    body always mirrors what the CLI emits. ``redirect_stdout`` confines the
    print to a local buffer -- the protocol stream is never polluted.
    """
    from . import transfer as transfer_module

    buffer = io.StringIO()
    # Every tool call comes from a remote caller, so transfer paths are
    # confined to the scratch root for its duration -- restored even when the
    # handler raises, so a failure cannot leave confinement off.
    already_confined = transfer_module._CONFINED
    transfer_module.set_unconfined(True)
    try:
        with contextlib.redirect_stdout(buffer):
            result = handler(lab, args)
    finally:
        transfer_module.set_unconfined(already_confined)
    if isinstance(result, dict):
        return result
    text = buffer.getvalue().strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise LabError(
            "handler produced output that is not one JSON document"
        ) from exc
    if not isinstance(payload, dict):
        raise LabError("handler produced a non-object JSON result")
    return payload


def _dispatch_lease_begin(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import leases

    return _invoke(
        leases.cmd_lease_begin,
        lab,
        _ns(a, ttl=a.get("ttl_seconds"), timeout=None),
    )


def _dispatch_lease_heartbeat(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import leases

    return _invoke(
        leases.cmd_lease_heartbeat,
        lab,
        _ns(a, ttl=a.get("ttl_seconds")),
    )


def _dispatch_lease_end(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import cleanup

    return _invoke(cleanup.cmd_lease_end, lab, _ns(a))


def _dispatch_lease_list(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import store as store_module

    include_ended = bool(a.get("include_ended", False))
    with store_module.Store(Path(lab.STATE_ROOT) / "lab.db") as store:
        rows = store.list_leases(include_ended=include_ended)
    return {
        "leases": [
            {
                "id": row["id"],
                "kind": row.get("kind"),
                "purpose": row.get("purpose"),
                "state": row.get("state"),
                "created_at": row.get("created_at"),
                "expires_at": row.get("expires_at"),
                "heartbeat_at": row.get("heartbeat_at"),
            }
            for row in rows
        ]
    }


def _dispatch_lease_destroy(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import cleanup

    return _invoke(cleanup.cmd_lease_destroy, lab, _ns(a))


def _dispatch_lease_register(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import leases

    # `allow_existing` adopts a pre-existing guest: it is registered as a
    # retain-policy row so neither guest_destroy nor a lease teardown claims
    # a machine that predates the lease.
    policy = "retain" if a.get("allow_existing") else "delete"
    return _invoke(
        leases.cmd_lease_register,
        lab,
        _ns(a, policy=policy, ttl=None),
    )


def _dispatch_guest_create(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import guest

    return _invoke(
        guest.cmd_create,
        lab,
        _ns(
            a,
            lease=a["lease_id"],
            kind="qemu",
            start=a.get("start", True),
            template=None,
            fresh=False,
            ostemplate=a.get("ostemplate"),
            storage=a.get("storage"),
            disk_gb=a.get("disk_gb"),
        ),
    )


def _dispatch_guest_clone(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import guest

    return _invoke(
        guest.cmd_clone,
        lab,
        _ns(a, lease=a["lease_id"], full=a.get("full", True)),
    )


def _dispatch_guest_start(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import guest

    return _invoke(guest.cmd_start, lab, _ns(a, lease=a["lease_id"]))


def _dispatch_guest_stop(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import guest

    # force=true maps to the seam's hard stop: a zero graceful timeout.
    timeout = 0 if a.get("force") else int(a.get("timeout") or 120)
    return _invoke(
        guest.cmd_stop, lab, _ns(a, lease=a["lease_id"], timeout=timeout)
    )


def _dispatch_guest_destroy(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import guest

    return _invoke(guest.cmd_destroy, lab, _ns(a, lease=a["lease_id"]))


def _dispatch_guest_probe(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import guest

    return _invoke(guest.cmd_probe, lab, _ns(a))


def _dispatch_guest_list(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import guest

    payload = _invoke(guest.cmd_list, lab, _ns(a, lease=a.get("lease_id")))
    state = a.get("state", "all")
    if state != "all":
        payload["guests"] = [
            item for item in payload.get("guests", [])
            if item.get("state") == state
        ]
    return payload


def _guest_run_argv(command: str) -> list[str]:
    """The guest argv for a `guest_run` command string.

    The CLI takes the command as an argv remainder; over MCP it arrives as
    one string, split here with POSIX quoting rules. For shell syntax the
    caller says so explicitly (``sh -c '...'``) -- this is exec, not a shell.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        raise _ParamsError("guest_run: field 'command' is not valid") from None
    if not argv:
        raise _ParamsError("guest_run: missing required field 'command'")
    return argv


def _guest_run_with_stdin(
    lab: Any, a: dict[str, Any], argv: list[str]
) -> dict[str, Any]:
    """cmd_run's shape plus the one thing argparse cannot carry: stdin.

    Mirrors guest.cmd_run line for line -- same ownership gate, same seam
    call, same argv0-only audit, same result keys -- with the ``stdin``
    argument actually plumbed into guest exec. Kept private here because
    guest.py belongs to another workstream; if cmd_run grows stdin support
    this wrapper folds back into it.
    """
    from . import guest as guest_module
    from . import proxmox as proxmox_module

    lease_id, vmid = str(a["lease_id"]), int(a["vmid"])
    row = guest_module.require_owned(lab, lease_id, None, vmid)
    kind = str(row["kind"])
    timeout = float(a.get("timeout") or guest_module.DEFAULT_RUN_TIMEOUT)
    feed = str(a["stdin"]).encode()
    prox = guest_module._make_proxmox(lab.CONFIG)
    started = time.monotonic()
    if kind == "lxc":
        result = prox.pct_exec(vmid, argv, timeout=timeout, stdin=feed)
    else:
        result = prox.guest_exec(vmid, argv, timeout=timeout, stdin=feed)
    duration_ms = int((time.monotonic() - started) * 1000)
    lab.audit(
        "guest-run",
        lease=lease_id,
        vmid=vmid,
        argv0=argv[0],
        exit_code=result.exit_code,
    )

    def text(raw: Any) -> str:
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).decode("utf-8", "replace")
        return raw if isinstance(raw, str) else ""

    return {
        "lease_id": lease_id,
        "vmid": vmid,
        "exit_code": result.exit_code,
        "stdout": text(result.stdout),
        "stderr": text(result.stderr),
        "duration_ms": duration_ms,
    }


def _dispatch_guest_run(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    argv = _guest_run_argv(a["command"])
    if a.get("stdin") is not None:
        return _guest_run_with_stdin(lab, a, argv)
    from . import guest

    return _invoke(
        guest.cmd_run,
        lab,
        _ns(
            a,
            lease=a["lease_id"],
            command=argv,
            timeout=int(a.get("timeout") or guest.DEFAULT_RUN_TIMEOUT),
        ),
    )


def _dispatch_push_file(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import transfer

    return _invoke(
        transfer.cmd_push,
        lab,
        _ns(
            a,
            lease=a["lease_id"],
            file=a["local_path"],
            dest=a["remote_path"],
            sha256=None,
            timeout=transfer.DEFAULT_TIMEOUT,
        ),
    )


def _dispatch_pull_file(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import transfer

    return _invoke(
        transfer.cmd_pull,
        lab,
        _ns(
            a,
            lease=a["lease_id"],
            remote=a["remote_path"],
            out=a["local_path"],
            sha256=None,
            timeout=transfer.DEFAULT_TIMEOUT,
        ),
    )


def _dispatch_console_screenshot(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import console
    from . import guest as guest_module

    # Over MCP every mutation names its lease (safety invariant 1): the CLI
    # command has no --lease flag, so the registry gate is applied here
    # before the handler runs -- same store lookup console_keys/type use.
    lease_id, vmid = str(a["lease_id"]), int(a["vmid"])
    guest_module.require_owned(lab, lease_id, None, vmid)
    payload = _invoke(console.cmd_screenshot, lab, _ns(a, vmid=vmid, out=None))
    result: dict[str, Any] = {
        "lease_id": lease_id,
        "vmid": vmid,
        "png_base64": None,
        "width": payload.get("width"),
        "height": payload.get("height"),
        "taken_at": dt.datetime.now(dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    if payload.get("path"):
        result["png_base64"] = base64.b64encode(
            Path(str(payload["path"])).read_bytes()
        ).decode("ascii")
    if "supported" in payload:
        result["supported"] = payload["supported"]
    if payload.get("reason"):
        result["reason"] = payload["reason"]
    return result


def _dispatch_console_type(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import console

    return _invoke(
        console.cmd_type,
        lab,
        _ns(
            a,
            lease=a["lease_id"],
            text_stdin=False,
            enter=a.get("enter", True),
            chars_per_second=console.MAX_CHARS_PER_SECOND,
        ),
    )


def _dispatch_console_keys(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import console

    # Key names are validated at the params layer (§E: an unknown name is
    # -32602, naming the field) -- the same key_for() the handler uses, so
    # the check can never drift from what cmd_keys would accept.
    for key in a["keys"]:
        try:
            console.key_for(str(key))
        except LabError:
            raise _ParamsError(
                "console_keys: field 'keys' contains an unknown key name"
            ) from None
    return _invoke(
        console.cmd_keys,
        lab,
        _ns(a, lease=a["lease_id"], screenshot_after=None),
    )


def _dispatch_cleanup_expired(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import cleanup

    payload = _invoke(
        cleanup.cmd_cleanup_expired,
        lab,
        _ns(
            a,
            # lease_id is scope/attribution: the sweep is per contract
            # lease-scoped over the wire; the operator's unscoped sweep is
            # the CLI path.
            lease=a["lease_id"],
            orphans_only=False,
            reclaim_orphans=False,
            include_active=False,
            host_change_authorized=False,
        ),
    )
    payload.setdefault("lease_id", a["lease_id"])
    return payload


def _dispatch_journal_query(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import journal as journal_module

    rows = journal_module.query_events(
        lease=a.get("lease_id"),
        since=a.get("since"),
        limit=int(a.get("limit") or 50),
    )
    return {"events": rows}


def _dispatch_doctor(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    from . import diagnostics

    return _invoke(
        diagnostics.cmd_doctor, lab, _ns(a, host_checks=a.get("host_checks", False))
    )


def _dispatch_power_status(lab: Any, a: dict[str, Any]) -> dict[str, Any]:
    status = lab.power_status()  # the same callable `power status` prints
    return dict(status)


_DISPATCH: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "lease_begin": _dispatch_lease_begin,
    "lease_heartbeat": _dispatch_lease_heartbeat,
    "lease_end": _dispatch_lease_end,
    "lease_list": _dispatch_lease_list,
    "lease_destroy": _dispatch_lease_destroy,
    "lease_register": _dispatch_lease_register,
    "guest_create": _dispatch_guest_create,
    "guest_clone": _dispatch_guest_clone,
    "guest_start": _dispatch_guest_start,
    "guest_stop": _dispatch_guest_stop,
    "guest_destroy": _dispatch_guest_destroy,
    "guest_probe": _dispatch_guest_probe,
    "guest_list": _dispatch_guest_list,
    "guest_run": _dispatch_guest_run,
    "push_file": _dispatch_push_file,
    "pull_file": _dispatch_pull_file,
    "console_screenshot": _dispatch_console_screenshot,
    "console_type": _dispatch_console_type,
    "console_keys": _dispatch_console_keys,
    "cleanup_expired": _dispatch_cleanup_expired,
    "journal_query": _dispatch_journal_query,
    "doctor": _dispatch_doctor,
    "power_status": _dispatch_power_status,
}


# -- params validation --------------------------------------------------------



def _json_type_ok(value: Any, expected: str) -> bool:
    """Type check with JSON semantics: a bool is never an integer."""
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _JSON_TYPES[expected])



def _check_params(name: str, arguments: Any) -> dict[str, Any]:
    """Validate `arguments` against the tool's schema -> the checked dict.

    Hand-rolled because the schemas are deliberately small: `type`,
    `enum`, `required`, `additionalProperties`. The error message names the
    offending field, never its value.
    """
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise _ParamsError(f"{name}: field 'arguments' must be an object")
    schema = _SCHEMAS[name]
    properties = schema["properties"]
    for key in arguments:
        if key not in properties:
            raise _ParamsError(f"{name}: unknown field '{key}'")
    for key in schema.get("required", ()):
        if key not in arguments:
            raise _ParamsError(f"{name}: missing required field '{key}'")
    for key, spec in properties.items():
        if key not in arguments:
            continue
        value = arguments[key]
        expected = spec.get("type")
        if expected in _JSON_TYPES and not _json_type_ok(value, expected):
            raise _ParamsError(
                f"{name}: field '{key}' must be {expected}"
            )
        if "enum" in spec and value not in spec["enum"]:
            raise _ParamsError(
                f"{name}: field '{key}' must be one of "
                + "/".join(spec["enum"])
            )
        if "maximum" in spec and isinstance(value, int) and value > spec["maximum"]:
            # A client must not be able to block an MCP request for years:
            # these numbers flow straight into an ssh subprocess timeout or a
            # store scan. The message names the field, never the value.
            raise _ParamsError(
                f"{name}: field '{key}' must be at most {spec['maximum']}"
            )
        if expected == "array":
            item_type = spec.get("items", {}).get("type")
            if item_type and not all(
                isinstance(item, _JSON_TYPES[item_type]) for item in value
            ):
                raise _ParamsError(
                    f"{name}: field '{key}' items must be {item_type}"
                )
    return dict(arguments)


# -- audit + activity ----------------------------------------------------------


def _log(message: str) -> None:
    """Diagnostics go to stderr only; stdout is protocol bytes."""
    print(f"proxmox-lab mcp: {message}", file=sys.stderr, flush=True)


def _tool_target(arguments: Any) -> Any:
    """The audit target for one call: vmid wins, then lease, then None."""
    if isinstance(arguments, dict):
        vmid = arguments.get("vmid")
        if isinstance(vmid, int) and not isinstance(vmid, bool):
            return vmid
        for key in ("lease", "lease_id"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _audit_call(lab: Any, name: str, arguments: Any, ok: bool) -> None:
    """tool name + ok + target ONLY -- never text, command, or path values."""
    target = _tool_target(arguments)
    try:
        lab.audit(
            "mcp-tool-call",
            actor="mcp",
            tool=name,
            ok=ok,
            target=target,
            lease=target if isinstance(target, str) else None,
            vmid=target if isinstance(target, int) else None,
        )
    except Exception as exc:  # noqa: BLE001 - auditing never fails the call
        _log(f"warning: audit event for {name!r} could not be recorded: {exc}")


def _record_activity(lab: Any, name: str) -> None:
    """Refresh last_mcp_activity; a broken clock never fails the call."""
    try:
        lab.record_mcp_activity(name)
    except AttributeError:
        # A facade without the idle clock (older lab double) still serves
        # calls -- the sweep below fails closed with it.
        _log("warning: lab facade has no record_mcp_activity; idle "
             "shutdown will not observe activity")
    except Exception as exc:  # noqa: BLE001
        _log(f"warning: MCP activity could not be recorded: {exc}")


# -- tools/call ----------------------------------------------------------------


def _result(payload: dict[str, Any]) -> dict[str, Any]:
    """The pinned result shape: one text block carrying the JSON body."""
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, default=str)}
        ]
    }


def _failure_message(name: str, arguments: Any, exc: Exception) -> str:
    """-32603 message: tool name + target, redacted detail, no tracebacks."""
    from . import audit as audit_module

    detail = str(audit_module.redact(str(exc)))
    target = _tool_target(arguments)
    where = f" for vmid {target}" if isinstance(target, int) else ""
    return f"{name} failed{where}: {detail}"


def _run_tool(lab: Any, name: str, arguments: Any) -> dict[str, Any]:
    """Dispatch one validated call; raises _ParamsError/LabError on failure."""
    if name not in _TOOL_NAMES:
        raise _ParamsError(f"{name!r} is not a tool this server exposes")
    checked = _check_params(name, arguments)
    if name in _CONFIRM_TOOLS and checked.get("confirm") is not True:
        if "confirm" not in checked:
            raise _ParamsError(f"{name}: missing required field 'confirm'")
        raise _ParamsError(f"{name}: field 'confirm' must be true")
    return _DISPATCH[name](lab, checked)


def _call_tool(lab: Any, params: Any) -> dict[str, Any]:
    """tools/call handler: params check, dispatch, activity + audit + sweep."""
    if not isinstance(params, dict):
        raise _ParamsError("tools/call: field 'params' must be an object")
    raw_name = params.get("name")
    if not isinstance(raw_name, str):
        raise _ParamsError("tools/call: missing required field 'name'")
    name = raw_name
    arguments = params.get("arguments")

    _record_activity(lab, name)
    ok = False
    try:
        payload = _run_tool(lab, name, arguments)
        ok = True
        return _result(payload)
    finally:
        _audit_call(lab, name, arguments, ok)
        _idle_sweep(lab)


# -- the idle shutdown ----------------------------------------------------------


def _idle_sweep(lab: Any) -> None:
    """Fire the verified power-off when the lab is idle and unowned.

    Runs after every tools/call (always a no-op right after the clock was
    just touched) and from the ~60s self-wake in the read loop, which is the
    path that fires when the client has gone silent. Due-ness is
    ``leases.mcp_idle_shutdown_due``; the power-off is the SAME
    ``cleanup.shutdown_host``/``power.shutdown_verified`` path
    ``cleanup-expired`` uses. A sweep failure is logged and audited, never
    raised into the protocol loop.
    """
    try:
        due = lab.mcp_idle_shutdown_due()
    except AttributeError:
        return
    except Exception as exc:  # noqa: BLE001 - a broken probe is not fatal
        _log(f"warning: idle-shutdown check failed: {exc}")
        return
    if not due:
        return
    try:
        lab.audit("mcp-idle-shutdown-triggered", actor="mcp")
    except Exception:  # noqa: BLE001
        pass
    try:
        powered_off = bool(lab.shutdown_host(None))
    except Exception as exc:  # noqa: BLE001
        _log(f"idle shutdown failed: {exc}")
        try:
            lab.audit(
                "mcp-idle-shutdown-failed", actor="mcp", error=str(exc)
            )
        except Exception:  # noqa: BLE001
            pass
        return
    if not powered_off:
        _log("idle shutdown did not verify power-off; the host stays on")
        try:
            lab.audit("mcp-idle-shutdown-unverified", actor="mcp")
        except Exception:  # noqa: BLE001
            pass


# -- protocol -------------------------------------------------------------------


def _send(sink: Any, message: dict[str, Any]) -> None:
    """One JSON-RPC message: a single complete line, then flush."""
    line = json.dumps(message).encode("utf-8") + b"\n"
    try:
        sink.write(line)
    except TypeError:
        sink.write(line.decode("utf-8"))
    sink.flush()


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _handle_message(lab: Any, message: dict[str, Any]) -> dict | None:
    """One parsed message -> its response, or None for a notification."""
    is_request = "id" in message
    request_id = message.get("id")
    method = message.get("method")
    if not isinstance(method, str):
        if not is_request:
            return None
        return _error(request_id, INVALID_REQUEST, "missing 'method'")
    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        elif method == "tools/list":
            result = {"tools": [dict(tool) for tool in TOOLS]}
        elif method == "tools/call":
            result = _call_tool(lab, message.get("params"))
        else:
            if not is_request:
                return None  # unknown notifications are ignored silently
            return _error(
                request_id, METHOD_NOT_FOUND, f"unknown method '{method}'"
            )
    except _ParamsError as exc:
        if not is_request:
            _log(f"notification tools/call rejected: {exc}")
            return None
        return _error(request_id, INVALID_PARAMS, str(exc))
    except LabError as exc:
        if not is_request:
            _log(f"notification tools/call failed: {exc}")
            return None
        name = (
            message.get("params", {}).get("name")
            if isinstance(message.get("params"), dict)
            else ""
        )
        return _error(
            request_id,
            INTERNAL_ERROR,
            _failure_message(str(name or "?"),
                             (message.get("params") or {}).get("arguments")
                             if isinstance(message.get("params"), dict)
                             else None,
                             exc),
        )
    except Exception:  # noqa: BLE001 - a crash must still answer, cleanly
        for line in traceback.format_exc().rstrip().splitlines():
            _log(line)
        if not is_request:
            return None
        return _error(request_id, INTERNAL_ERROR, "internal error")
    if not is_request:
        return None  # notifications get no response, whatever the outcome
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _handle_line(lab: Any, line: bytes, sink: Any) -> None:
    """One input line -> zero or one response line on the sink."""
    try:
        message = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        _send(sink, _error(None, PARSE_ERROR, "parse error"))
        return
    if not isinstance(message, dict):
        _send(sink, _error(None, INVALID_REQUEST, "not a JSON-RPC object"))
        return
    response = _handle_message(lab, message)
    if response is not None:
        _send(sink, response)


def _read_messages(
    source: Any, *, on_idle: Callable[[], Any]
) -> Iterator[bytes]:
    """Complete input lines; wakes itself roughly every IDLE_CHECK_SECONDS.

    Raw ``os.read`` chunks into a private buffer (never ``readline``: select
    promising bytes does not promise a newline, and a partial line must not
    stall the idle clock). On each select timeout ``on_idle()`` runs the
    idle-shutdown check -- the self-wake that fires when the client has gone
    silent.
    """
    fileno = source.fileno()
    pending = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fileno, selectors.EVENT_READ)
        while True:
            events = selector.select(timeout=IDLE_CHECK_SECONDS)
            if not events:
                on_idle()
                continue
            chunk = os.read(fileno, _READ_CHUNK)
            if not chunk:
                break
            pending += chunk
            while True:
                cut = pending.find(b"\n")
                if cut < 0:
                    break
                line = bytes(pending[:cut]).rstrip(b"\r")
                del pending[: cut + 1]
                if line.strip():
                    yield line


def serve(
    lab: Any = None,
    args: Any = None,
    *,
    stdin: Any = None,
    stdout: Any = None,
) -> None:
    """Serve the MCP surface until stdin closes.

    ``lab`` is the cli module facade (``cli._module()``) -- the same object
    every ``cmd_*`` handler receives, obtained lazily here because ``cli``
    imports this module at registration time. ``stdin``/``stdout`` exist for
    tests driving the server over real pipe pairs; production serves
    ``sys.stdin``/``sys.stdout`` untouched.
    """
    if lab is None:
        from . import cli

        lab = cli._module()
    source = sys.stdin if stdin is None else stdin
    sink = sys.stdout.buffer if stdout is None else stdout
    _log(f"serving {len(TOOLS)} tools over stdio")
    for line in _read_messages(source, on_idle=lambda: _idle_sweep(lab)):
        _handle_line(lab, line, sink)


def register(sub: Any, lab: Any) -> None:
    """Attach the ``mcp`` subcommand -- the server entry point."""
    parser = sub.add_parser(
        "mcp", help="serve the MCP tool surface over stdio (JSON-RPC 2.0)"
    )
    parser.set_defaults(func=lambda args: serve(lab, args))


def main() -> int:
    """Direct entry point equivalent to ``proxmox-lab mcp``."""
    try:
        serve()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":  # python3 -m proxmox_agent_lab.mcp
    sys.exit(main())
