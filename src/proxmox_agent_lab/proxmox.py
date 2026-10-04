"""``qm``/``pct``/``pvesh`` wrappers over the injected ssh seam (rework plan §D).

Every remote action is exactly one argv handed to the injected ``ssh`` object
(``.run(argv, *, timeout=None, stdin=None, host_change=False)`` returning a
``CommandResult`` with ``returncode``/``stdout``/``stderr``/``argv`` and
``.ok``). The seam flattens that argv with ``shlex.quote`` per element, so
values land here as plain strings and are never quoted by this module -- the
only strings composed below argv level are guest-side ``sh`` scripts (the
chunked transfer), where interpolated paths are ``shlex.quote``d for the
*guest* shell's parse.

Semantic failures (a non-zero ``qm``/``pct``/``pvesh`` exit, unusable output,
a failed sha256 check, a deadline expiring) raise :class:`ProxmoxError`;
errors from the ssh layer itself (policy refusal, transport failure) propagate
untouched.

The Q4 fallback chains live here and nowhere else: ``pct create`` without
``--tags``/``--description`` followed by ``pct set``, ``pct shutdown`` without
``--timeout``, and ``qm guest exec`` without ``--synchronous`` followed by
``qm guest exec-status`` polling. File transfer is chunked base64 through guest
exec only -- ``qm guest file-write``/``file-read``/``file-pull`` and ``pct
push`` are deliberately not used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shlex
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .errors import LabError

DEFAULT_TIMEOUT = 30.0
EXEC_TIMEOUT = 120.0
CREATE_TIMEOUT = 600.0
CLONE_TIMEOUT = 3600.0
TRANSFER_CHUNK = 49152
POLL_INTERVAL = 2.0

_COMMANDS = {"qemu": "qm", "lxc": "pct"}

_USAGE_ERROR = re.compile(r"(?i)unknown option|unrecognized|usage:")
_STATUS_LINE = re.compile(r"status:\s*(\S+)")
_STORAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ProxmoxError(LabError):
    """A semantic failure in a Proxmox control command."""


@dataclass(frozen=True)
class ExecResult:
    """What one guest exec produced."""

    exit_code: int | None
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip()


def _tool(kind: str) -> str:
    try:
        return _COMMANDS[kind]
    except KeyError:
        raise ProxmoxError(f"unknown guest kind {kind!r} (want qemu or lxc)") from None


def _require(result, action: str):
    if not result.ok:
        raise ProxmoxError(f"{action} failed: {_text(result.stderr)}")
    return result


def _json_value(result, action: str):
    raw = _text(result.stdout)
    try:
        return json.loads(raw) if raw else None
    except ValueError as raised:
        raise ProxmoxError(f"{action}: unparseable JSON output") from raised


def _json_object(result, action: str) -> dict:
    data = _json_value(result, action)
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise ProxmoxError(f"{action}: expected a JSON object in output")
    return data


def _b64decode(value: str | bytes, action: str) -> bytes:
    try:
        return base64.b64decode(value)
    except (ValueError, TypeError) as raised:
        raise ProxmoxError(f"{action}: output is not valid base64") from raised


def _exec_result(state: dict, action: str) -> ExecResult:
    return ExecResult(
        exit_code=state.get("exitcode"),
        stdout=_b64decode(state.get("out-data") or "", action),
        stderr=_b64decode(state.get("err-data") or "", action),
    )


class Proxmox:
    """Proxmox control commands against one node over one injected ssh seam."""

    def __init__(
        self,
        ssh,
        node: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ssh = ssh
        self._node = node
        self._sleep = sleep

    # -- host-level introspection ------------------------------------------

    def node_status(self) -> dict:
        action = "pvesh node status"
        result = self._ssh.run(
            ["pvesh", "get", f"/nodes/{self._node}/status", "--output-format", "json"],
            timeout=DEFAULT_TIMEOUT,
        )
        return _json_object(_require(result, action), action)

    def cluster_nextid(self) -> int:
        """Next free cluster VMID (``pvesh get /cluster/nextid``).

        The caller passes this id to ``guest create``. Create does not pick
        one: a collision destroys a real machine. The idea of asking the
        cluster, rather than guessing, comes from ProxmoxMCP-Plus (MIT);
        this is a ``pvesh get`` on this seam, not their client.
        """
        action = "pvesh cluster nextid"
        result = self._ssh.run(
            ["pvesh", "get", "/cluster/nextid", "--output-format", "json"],
            timeout=DEFAULT_TIMEOUT,
        )
        payload = _json_value(_require(result, action), action)
        if isinstance(payload, str) and payload.strip().isdecimal():
            return int(payload.strip())
        if isinstance(payload, int) and not isinstance(payload, bool) and payload > 0:
            return payload
        raise ProxmoxError(f"{action}: expected a VMID")

    def network_bridges(self) -> list:
        """Linux and OVS bridges (``pvesh`` ``--type any_bridge``).

        ``any_bridge`` is the Proxmox filter that covers ``vmbr*`` and OVS
        bridges. Same credit as :meth:`cluster_nextid`: the question is
        theirs, the argv is ours.
        """
        action = "pvesh network bridges"
        result = self._ssh.run(
            [
                "pvesh", "get", f"/nodes/{self._node}/network",
                "--type", "any_bridge",
                "--output-format", "json",
            ],
            timeout=DEFAULT_TIMEOUT,
        )
        payload = _json_value(_require(result, action), action)
        if not isinstance(payload, list):
            raise ProxmoxError(f"{action}: expected a list")
        return payload

    def pveversion(self) -> str:
        result = self._ssh.run(["pveversion"], timeout=DEFAULT_TIMEOUT)
        return _text(_require(result, "pveversion").stdout)

    def task_status(self, upid: str) -> dict:
        action = f"pvesh task status {upid}"
        result = self._ssh.run(
            [
                "pvesh",
                "get",
                f"/nodes/{self._node}/tasks/{upid}/status",
                "--output-format",
                "json",
            ],
            timeout=DEFAULT_TIMEOUT,
        )
        return _json_object(_require(result, action), action)

    def wait_task(self, upid: str, *, timeout: float = 300.0, poll: float = POLL_INTERVAL) -> dict:
        """Poll a task until ``status == "stopped"``; return the final state.

        The caller reads ``exitstatus`` off the result -- a finished task is
        not necessarily a successful one.
        """
        deadline = time.monotonic() + timeout
        while True:
            state = self.task_status(upid)
            if state.get("status") == "stopped":
                return state
            if time.monotonic() >= deadline:
                raise ProxmoxError(f"task {upid} did not stop within {timeout:g}s")
            self._sleep(poll)

    # -- guest lifecycle ----------------------------------------------------

    def qemu_create(
        self,
        vmid: int,
        *,
        name: str,
        tags: str,
        description: str,
        net0: str = "virtio,bridge=vmbr0",
        scsi0: str | None = None,
        memory: int | None = None,
        cores: int | None = None,
        iso: str | None = None,
        ide0: str | None = None,
        cpu: str | None = None,
        machine: str | None = None,
        vga: str | None = None,
        ostype: str | None = None,
        boot: str | None = None,
    ) -> None:
        argv = ["qm", "create", str(vmid), "--name", name, "--net0", net0]
        if scsi0 is not None:
            argv += ["--scsi0", scsi0]
        if ide0 is not None:
            argv += ["--ide0", ide0]
        if memory is not None:
            argv += ["--memory", str(memory)]
        if cores is not None:
            argv += ["--cores", str(cores)]
        if cpu is not None:
            argv += ["--cpu", cpu]
        if machine is not None:
            argv += ["--machine", machine]
        if vga is not None:
            argv += ["--vga", vga]
        if ostype is not None:
            argv += ["--ostype", ostype]
        if iso is not None:
            # CD first, then the disk, so an empty disk does not hide the
            # installer. virtio-scsi is what current installers expect when
            # the disk is scsi. An IDE disk is a different machine: no
            # scsi controller, and the CD is ide2 beside ide0.
            argv += ["--ide2", f"{iso},media=cdrom"]
            if boot is None:
                if scsi0 is not None:
                    boot = "order=ide2;scsi0"
                elif ide0 is not None:
                    boot = "order=ide2;ide0"
                else:
                    boot = "order=ide2"
            if scsi0 is not None:
                argv += ["--scsihw", "virtio-scsi-pci"]
        if boot is not None:
            argv += ["--boot", boot]
        # Opens the channel guest run / guest probe use. Does not install
        # qemu-guest-agent in the guest. Clones keep the source setting.
        # The flag is an idea from ProxmoxMCP-Plus (MIT), not their code.
        argv += ["--agent", "1", "--tags", tags, "--description", description]
        _require(self._ssh.run(argv, timeout=CREATE_TIMEOUT), f"qm create {vmid}")

    def lxc_create(
        self,
        vmid: int,
        *,
        ostemplate: str,
        hostname: str,
        tags: str,
        description: str,
        rootfs: str | None = None,
    ) -> None:
        """Create an LXC guest, stamping tags+description in the same call.

        ``rootfs`` is the pct volume spec for the container root, e.g.
        ``local-lvm,size=8`` — required on hosts where the ``local`` dir
        storage lacks the ``rootdir`` content type.

        Fallback: when ``pct create`` rejects the metadata flags as unknown
        options (older ``pct``), create without them and stamp with ``pct
        set``. Any other create failure is a real error -- it surfaces the
        real stderr and never triggers a retry.
        """
        action = f"pct create {vmid}"
        base = ["pct", "create", str(vmid), ostemplate, "--hostname", hostname]
        if rootfs is not None:
            base += ["--rootfs", rootfs]
        metadata = ["--tags", tags, "--description", description]
        first = self._ssh.run(base + metadata, timeout=CREATE_TIMEOUT)
        if first.ok:
            return
        if not _USAGE_ERROR.search(_text(first.stderr)):
            raise ProxmoxError(f"{action} failed: {_text(first.stderr)}")
        _require(self._ssh.run(base, timeout=CREATE_TIMEOUT), action)
        _require(
            self._ssh.run(["pct", "set", str(vmid)] + metadata, timeout=DEFAULT_TIMEOUT),
            f"pct set {vmid}",
        )

    def set_metadata(
        self,
        kind: str,
        vmid: int,
        *,
        tags: str | None = None,
        description: str | None = None,
    ) -> None:
        tool = _tool(kind)
        argv = [tool, "set", str(vmid)]
        if tags is not None:
            argv += ["--tags", tags]
        if description is not None:
            argv += ["--description", description]
        _require(self._ssh.run(argv, timeout=DEFAULT_TIMEOUT), f"{tool} set {vmid}")

    def clone(self, kind: str, src: int, dst: int, *, name: str | None = None) -> None:
        """Clone a registered template; metadata is re-stamped afterwards.

        ``name`` maps to ``qm clone --name`` and ``pct clone --hostname``.
        """
        tool = _tool(kind)
        argv = [tool, "clone", str(src), str(dst)]
        if name is not None:
            argv += ["--hostname" if tool == "pct" else "--name", name]
        _require(self._ssh.run(argv, timeout=CLONE_TIMEOUT), f"{tool} clone {src}->{dst}")

    def start(self, kind: str, vmid: int) -> None:
        tool = _tool(kind)
        _require(
            self._ssh.run([tool, "start", str(vmid)], timeout=DEFAULT_TIMEOUT),
            f"{tool} start {vmid}",
        )

    def stop(self, kind: str, vmid: int) -> None:
        tool = _tool(kind)
        _require(
            self._ssh.run([tool, "stop", str(vmid)], timeout=DEFAULT_TIMEOUT),
            f"{tool} stop {vmid}",
        )

    def shutdown(self, kind: str, vmid: int, *, timeout: float = EXEC_TIMEOUT) -> bool:
        """Graceful shutdown, judged by status polling -- never assumed.

        ``pct shutdown --timeout`` is [UNVERIFIED]: on a usage rejection fall
        back to plain ``pct shutdown``. The shutdown command itself is
        best-effort (a guest that is already stopped errors out) -- the return
        value reports only whether ``status`` observed "stopped" before the
        deadline.
        """
        tool = _tool(kind)
        deadline = time.monotonic() + timeout
        first = self._ssh.run(
            [tool, "shutdown", str(vmid), "--timeout", str(int(timeout))],
            timeout=timeout + DEFAULT_TIMEOUT,
        )
        if tool == "pct" and not first.ok and _USAGE_ERROR.search(_text(first.stderr)):
            self._ssh.run(["pct", "shutdown", str(vmid)], timeout=DEFAULT_TIMEOUT)
        while True:
            if self.status(kind, vmid) == "stopped":
                return True
            if time.monotonic() >= deadline:
                return False
            self._sleep(POLL_INTERVAL)

    def destroy(self, kind: str, vmid: int, *, purge: bool = True) -> None:
        tool = _tool(kind)
        argv = [tool, "destroy", str(vmid)]
        if purge and tool == "qm":
            argv += ["--purge", "1"]
        _require(self._ssh.run(argv, timeout=DEFAULT_TIMEOUT), f"{tool} destroy {vmid}")

    def set_cdrom(self, vmid: int, volid: str | None) -> None:
        """Point ide2 at a CD, or eject it. The disk on ide0 is left alone."""
        if volid is None:
            argv = ["qm", "set", str(vmid), "--ide2", "none,media=cdrom"]
        else:
            argv = ["qm", "set", str(vmid), "--ide2", f"{volid},media=cdrom"]
        _require(self._ssh.run(argv, timeout=DEFAULT_TIMEOUT), f"qm set {vmid} cdrom")

    def set_floppy(self, vmid: int, volid: str | None) -> None:
        """Insert a floppy image, or remove the floppy drive's media."""
        if volid is None:
            argv = ["qm", "set", str(vmid), "--delete", "floppy"]
        else:
            argv = ["qm", "set", str(vmid), "--floppy", volid]
        _require(self._ssh.run(argv, timeout=DEFAULT_TIMEOUT), f"qm set {vmid} floppy")

    def make_template(self, kind: str, vmid: int) -> None:
        """Convert a stopped guest into a Proxmox template (``qm``/``pct template``)."""
        tool = _tool(kind)
        _require(
            self._ssh.run([tool, "template", str(vmid)], timeout=DEFAULT_TIMEOUT),
            f"{tool} template {vmid}",
        )

    def snapshot_list(self, kind: str, vmid: int) -> list:
        """Snapshot records from a read-only ``pvesh get``."""
        segment = "lxc" if kind == "lxc" else "qemu"
        action = f"pvesh snapshots {kind} {vmid}"
        result = self._ssh.run(
            [
                "pvesh", "get",
                f"/nodes/{self._node}/{segment}/{vmid}/snapshot",
                "--output-format", "json",
            ],
            timeout=DEFAULT_TIMEOUT,
        )
        payload = _json_value(_require(result, action), action)
        if not isinstance(payload, list):
            raise ProxmoxError(f"{action}: expected a list")
        return payload

    def snapshot_create(
        self, kind: str, vmid: int, name: str, *, description: str | None = None
    ) -> None:
        tool = _tool(kind)
        argv = [tool, "snapshot", str(vmid), name]
        if description:
            argv += ["--description", description]
        _require(
            self._ssh.run(argv, timeout=CLONE_TIMEOUT),
            f"{tool} snapshot {vmid} {name}",
        )

    def snapshot_delete(self, kind: str, vmid: int, name: str) -> None:
        tool = _tool(kind)
        _require(
            self._ssh.run(
                [tool, "delsnapshot", str(vmid), name], timeout=CLONE_TIMEOUT
            ),
            f"{tool} delsnapshot {vmid} {name}",
        )

    def snapshot_rollback(self, kind: str, vmid: int, name: str) -> None:
        tool = _tool(kind)
        _require(
            self._ssh.run(
                [tool, "rollback", str(vmid), name], timeout=CLONE_TIMEOUT
            ),
            f"{tool} rollback {vmid} {name}",
        )

    def storage_status(self) -> list:
        """Configured storages on this node, from a read-only ``pvesh get``."""
        action = "pvesh storage status"
        result = self._ssh.run(
            [
                "pvesh", "get", f"/nodes/{self._node}/storage",
                "--output-format", "json",
            ],
            timeout=DEFAULT_TIMEOUT,
        )
        payload = _json_value(_require(result, action), action)
        if not isinstance(payload, list):
            raise ProxmoxError(f"{action}: expected a list")
        return payload

    def storage_content(self, storage: str, content: str) -> list:
        """Volumes of one content type on one store (``iso`` or ``vztmpl``)."""
        if content not in ("iso", "vztmpl"):
            raise ProxmoxError(f"storage content type {content!r} is not iso or vztmpl")
        if _STORAGE_ID.fullmatch(storage) is None:
            raise ProxmoxError(f"storage name {storage!r} is not a plain name")
        action = f"pvesh storage content {storage} {content}"
        result = self._ssh.run(
            [
                "pvesh", "get",
                f"/nodes/{self._node}/storage/{storage}/content",
                "--content", content,
                "--output-format", "json",
            ],
            timeout=DEFAULT_TIMEOUT,
        )
        payload = _json_value(_require(result, action), action)
        if not isinstance(payload, list):
            raise ProxmoxError(f"{action}: expected a list")
        return payload

    # -- status and probes ----------------------------------------------------

    def status(self, kind: str, vmid: int) -> str:
        tool = _tool(kind)
        result = self._ssh.run([tool, "status", str(vmid)], timeout=DEFAULT_TIMEOUT)
        match = _STATUS_LINE.search(_text(_require(result, f"{tool} status {vmid}").stdout))
        if match is None:
            raise ProxmoxError(f"{tool} status {vmid}: no status line in output")
        return match.group(1)

    def guest_ping(self, vmid: int) -> bool:
        result = self._ssh.run(["qm", "guest", "ping", str(vmid)], timeout=DEFAULT_TIMEOUT)
        return result.ok

    def guest_ip(self, vmid: int) -> str | None:
        """First non-``lo`` IPv4 from the agent's interface list, if any."""
        action = f"qm guest network-get-interfaces {vmid}"
        result = self._ssh.run(
            ["qm", "guest", "network-get-interfaces", str(vmid)],
            timeout=DEFAULT_TIMEOUT,
        )
        payload = _json_value(_require(result, action), action)
        if isinstance(payload, dict):
            interfaces = payload.get("result", payload.get("interfaces"))
        else:
            interfaces = payload
        if isinstance(interfaces, dict):
            interfaces = [interfaces]
        if not isinstance(interfaces, list):
            raise ProxmoxError(f"{action}: no interface list in output")
        for interface in interfaces:
            if not isinstance(interface, dict) or interface.get("name") == "lo":
                continue
            for address in interface.get("ip-addresses") or []:
                if not isinstance(address, dict):
                    continue
                if str(address.get("ip-address-type", "")).lower() == "ipv4":
                    ip = str(address.get("ip-address") or "").strip()
                    if ip and not ip.startswith("127."):
                        return ip
        return None

    def lxc_interfaces(self, vmid: int) -> list:
        """Container interfaces (``pvesh get .../lxc/<vmid>/interfaces``).

        Probe uses this for an LXC address. A failure here is the caller's
        to swallow: a dark container must not fail the probe. Idea from
        ProxmoxMCP-Plus (MIT); the read is ``pvesh get`` on this seam.
        """
        action = f"pvesh lxc interfaces {vmid}"
        result = self._ssh.run(
            [
                "pvesh", "get",
                f"/nodes/{self._node}/lxc/{vmid}/interfaces",
                "--output-format", "json",
            ],
            timeout=DEFAULT_TIMEOUT,
        )
        payload = _json_value(_require(result, action), action)
        if not isinstance(payload, list):
            raise ProxmoxError(f"{action}: expected a list")
        return payload

    # -- guest execution --------------------------------------------------------

    def guest_exec(
        self,
        vmid: int,
        argv: Sequence[str],
        *,
        timeout: float = EXEC_TIMEOUT,
        stdin: bytes | None = None,
    ) -> ExecResult:
        """Run one command in a QEMU guest via the guest agent.

        ``--synchronous`` is [UNVERIFIED]: when the CLI rejects it as an
        unknown option, fall back to a plain exec plus ``exec-status``
        polling of the returned pid.
        """
        argv = list(argv)
        if not argv:
            raise ProxmoxError("guest exec needs a non-empty command")
        action = f"qm guest exec {vmid}"
        first = self._ssh.run(
            ["qm", "guest", "exec", str(vmid), "--synchronous", "--timeout", str(int(timeout)), "--", *argv],
            timeout=timeout,
            stdin=stdin,
        )
        if first.ok:
            return _exec_result(_json_object(first, action), action)
        if not _USAGE_ERROR.search(_text(first.stderr)):
            raise ProxmoxError(f"{action} failed: {_text(first.stderr)}")
        return self._guest_exec_async(vmid, argv, timeout=timeout, stdin=stdin)

    def _guest_exec_async(
        self,
        vmid: int,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
    ) -> ExecResult:
        action = f"qm guest exec {vmid}"
        started = self._ssh.run(
            ["qm", "guest", "exec", str(vmid), "--", *argv],
            timeout=timeout,
            stdin=stdin,
        )
        payload = _json_object(_require(started, action), action)
        pid = payload.get("pid")
        if pid is None:
            raise ProxmoxError(f"{action}: no pid in agent reply")
        deadline = time.monotonic() + timeout
        while True:
            polled = self._ssh.run(
                ["qm", "guest", "exec-status", str(vmid), str(pid)],
                timeout=DEFAULT_TIMEOUT,
            )
            state = _json_object(
                _require(polled, f"qm guest exec-status {vmid} {pid}"),
                f"qm guest exec-status {vmid} {pid}",
            )
            if state.get("exited"):
                return _exec_result(state, action)
            if time.monotonic() >= deadline:
                raise ProxmoxError(
                    f"{action} pid {pid} did not finish within {timeout:g}s"
                )
            self._sleep(POLL_INTERVAL)

    def pct_exec(
        self,
        vmid: int,
        argv: Sequence[str],
        *,
        timeout: float = EXEC_TIMEOUT,
        stdin: bytes | None = None,
    ) -> ExecResult:
        """Blocking ``pct exec``; the guest's real exit code is reported, not raised."""
        argv = list(argv)
        if not argv:
            raise ProxmoxError("pct exec needs a non-empty command")
        result = self._ssh.run(
            ["pct", "exec", str(vmid), "--", *argv],
            timeout=timeout,
            stdin=stdin,
        )
        return ExecResult(result.returncode, result.stdout, result.stderr)

    def _exec(
        self,
        kind: str,
        vmid: int,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
    ) -> ExecResult:
        if _tool(kind) == "pct":
            return self.pct_exec(vmid, argv, stdin=stdin)
        return self.guest_exec(vmid, argv, stdin=stdin)

    # -- chunked transfer (guest exec base64 only) -------------------------------

    def push_bytes(
        self,
        kind: str,
        vmid: int,
        dest: str,
        data: bytes,
        *,
        chunk: int = TRANSFER_CHUNK,
    ) -> str:
        """Push bytes to a guest via chunked base64 over guest exec.

        The only transfer path: each chunk is base64 on stdin to
        ``sh -c 'base64 -d > <dest>'`` (first chunk truncates, later chunks
        append). Returns the sha256 hex digest, recomputed in the guest.
        """
        if chunk <= 0:
            raise ProxmoxError("chunk size must be positive")
        digest = hashlib.sha256(data).hexdigest()
        target = shlex.quote(dest)
        for index, offset in enumerate(range(0, max(len(data), 1), chunk)):
            piece = data[offset : offset + chunk]
            redirect = ">" if index == 0 else ">>"
            result = self._exec(
                kind,
                vmid,
                ["sh", "-c", f"base64 -d {redirect} {target}"],
                stdin=base64.b64encode(piece),
            )
            if not result.ok:
                raise ProxmoxError(
                    f"push to {dest} failed at chunk {index}: {_text(result.stderr)}"
                )
        remote = self._guest_sha256(kind, vmid, dest)
        if remote != digest:
            raise ProxmoxError(
                f"sha256 mismatch pushing {dest}: guest {remote} != {digest}"
            )
        return digest

    def pull_bytes(
        self,
        kind: str,
        vmid: int,
        src: str,
        *,
        chunk: int = TRANSFER_CHUNK,
    ) -> bytes:
        """Pull bytes from a guest via chunked base64 over guest exec.

        Each chunk is ``dd | base64`` inside guest exec until a short chunk;
        the guest's sha256 of the source is compared against the reassembled
        bytes before they are returned.
        """
        if chunk <= 0:
            raise ProxmoxError("chunk size must be positive")
        source = shlex.quote(src)
        parts: list[bytes] = []
        index = 0
        while True:
            script = f"dd if={source} bs={chunk} skip={index} count=1 2>/dev/null | base64"
            result = self._exec(kind, vmid, ["sh", "-c", script])
            if not result.ok:
                raise ProxmoxError(
                    f"pull from {src} failed at chunk {index}: {_text(result.stderr)}"
                )
            piece = _b64decode(result.stdout, f"pull from {src} chunk {index}")
            parts.append(piece)
            if len(piece) < chunk:
                break
            index += 1
        data = b"".join(parts)
        remote = self._guest_sha256(kind, vmid, src)
        if remote != hashlib.sha256(data).hexdigest():
            raise ProxmoxError(f"sha256 mismatch pulling {src}: guest {remote}")
        return data

    def _guest_sha256(self, kind: str, vmid: int, path: str) -> str:
        result = self._exec(kind, vmid, ["sha256sum", path])
        if not result.ok:
            raise ProxmoxError(f"sha256sum {path} failed: {_text(result.stderr)}")
        fields = _text(result.stdout).split()
        if not fields:
            raise ProxmoxError(f"sha256sum {path}: no digest in output")
        return fields[0]


def from_config(config) -> "Proxmox":
    """The Proxmox seam for one configuration (rework plan §G).

    ``ssh`` to ``[ssh] target``, commands against ``[pve] node``. This is the
    one construction site for the seam; lifecycle handlers reach it through
    their module-level ``_make_proxmox`` so tests can substitute a double.
    """
    from . import ssh as ssh_module

    return Proxmox(ssh_module.SSH(config.ssh.target), config.pve.node)
