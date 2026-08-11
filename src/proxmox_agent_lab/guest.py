"""One way to talk to a guest, whatever the guest happens to support.

There are three ways to run a command inside a lab guest, and which ones work
depends on the guest, not on what the caller wants:

* **qemu-guest-agent** -- real exit codes and separated streams, but generic
  cloud images do not ship it and it is absent during an install.
* **serial console** -- needs no agent, but needs a login and returns a
  transcript rather than structured output.
* **LXC console** -- the container equivalent of the above.

Without this module every caller has to know that taxonomy, probe for it, and
handle each case. `GuestSession` probes once and presents one interface, so
callers -- and agents -- can say "run this in the guest" and get a result.

    with GuestSession(lab, api, vmid, password=pw) as guest:
        print(guest.channel)          # "agent" or "serial"
        result = guest.run("uname -a")
        print(result.stdout, result.exit_code)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import secrets
import shlex
import time
from typing import Any

from . import console


class GuestError(RuntimeError):
    pass


@dataclass
class CommandResult:
    """What a command did, regardless of how it was delivered."""

    stdout: str
    exit_code: int | None
    channel: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        # A serial run cannot always report a code; absent means "no failure
        # observed", which is the best that channel can honestly offer.
        return self.exit_code in (0, None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "channel": self.channel,
            "ok": self.ok,
        }


@dataclass
class GuestCapabilities:
    """What this guest can actually do, discovered rather than assumed."""

    vmid: int
    kind: str
    agent: bool = False
    serial: bool = False
    graphical_console: bool = False
    keyboard_input: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "vmid": self.vmid,
            "kind": self.kind,
            "guest_agent": self.agent,
            "serial_console": self.serial,
            "graphical_console": self.graphical_console,
            "keyboard_input": self.keyboard_input,
            "notes": self.notes,
        }


def probe(lab: Any, api: Any, vmid: int) -> GuestCapabilities:
    """Discover how this guest can be reached. Read-only."""
    kind = "qemu"
    try:
        config = api.call("GET", f"/nodes/{lab.NODE}/qemu/{vmid}/config")
    except lab.LabError:
        try:
            api.call("GET", f"/nodes/{lab.NODE}/lxc/{vmid}/config")
            caps = GuestCapabilities(vmid=vmid, kind="lxc", serial=True)
            caps.notes.append("LXC: use the container console; no VNC")
            return caps
        except lab.LabError:
            raise GuestError(
                f"VMID {vmid} is not a QEMU VM or LXC container on {lab.NODE}"
            ) from None

    caps = GuestCapabilities(vmid=vmid, kind=kind)
    caps.serial = any(str(key).startswith("serial") for key in config)
    display = str(config.get("vga") or "")
    # RFB key events go to the emulated PS/2 keyboard, so they only reach a
    # guest that has a graphical display. On `vga: serial0` you get a picture
    # but typing goes nowhere -- the single most confusing failure here.
    caps.graphical_console = not display.startswith("serial")
    caps.keyboard_input = caps.graphical_console
    if not caps.graphical_console:
        caps.notes.append(
            "display is the serial console: screenshots work, but VNC "
            "keyboard input does not. Drive this guest over serial."
        )
    caps.agent = console.agent_ready(lab, api, vmid)
    if not caps.agent and str(config.get("agent") or "").startswith("enabled"):
        caps.notes.append(
            "agent is enabled in config but not answering: the guest may "
            "still be booting, or qemu-guest-agent is not installed"
        )
    return caps


class GuestSession:
    """A command channel to one guest, chosen automatically.

    `prefer` picks between channels when both work. The agent is the default
    because it reports real exit codes.
    """

    def __init__(
        self,
        lab: Any,
        api: Any,
        vmid: int,
        *,
        user: str | None = None,
        password: str | None = None,
        prefer: str = "agent",
        capabilities: GuestCapabilities | None = None,
    ) -> None:
        self.lab = lab
        self.api = api
        self.vmid = vmid
        self.user = user
        self._password = password
        self.capabilities = capabilities or probe(lab, api, vmid)
        self._term: console.TermSession | None = None

        options = []
        if self.capabilities.agent:
            options.append("agent")
        if self.capabilities.serial and password:
            options.append("serial")
        if not options:
            raise GuestError(self._no_channel_message())
        if prefer in options:
            self.channel = prefer
        else:
            self.channel = options[0]

    def _no_channel_message(self) -> str:
        caps = self.capabilities
        if caps.serial and not self._password:
            return (
                f"VMID {self.vmid} has a serial console but no guest agent. "
                "Pass a console password to use the serial channel, or "
                "install qemu-guest-agent in the guest."
            )
        return (
            f"no way in to VMID {self.vmid}: no guest agent is answering and "
            "no serial console is configured. Add `serial0: socket` to the "
            "VM, or install qemu-guest-agent."
        )

    # -- lifecycle ---------------------------------------------------------

    def _terminal(self) -> console.TermSession:
        if self._term is None:
            self._term = console.TermSession(
                self.lab, self.api, self.capabilities.kind, self.vmid
            )
            self._term.login(self.user or "root", self._password or "")
        return self._term

    def close(self) -> None:
        if self._term is not None:
            self._term.close()
            self._term = None

    def __enter__(self) -> "GuestSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- the interface callers actually want -------------------------------

    def run(self, command: str, timeout: int = 300,
            shell: str = "/bin/sh") -> CommandResult:
        """Run a shell command in the guest."""
        if self.channel == "agent":
            result = console.agent_exec(
                self.lab, self.api, self.vmid, [shell, "-c", command],
                timeout=timeout,
            )
            return CommandResult(
                stdout=result["stdout"], stderr=result["stderr"],
                exit_code=result["exitcode"], channel="agent",
            )
        output, code = self._terminal().run_status(command, timeout=timeout)
        return CommandResult(stdout=output, exit_code=code, channel="serial")

    def read_screen(self) -> dict[str, Any]:
        """Capture the screen. Works even when no command channel does."""
        if not self.capabilities.graphical_console:
            self.capabilities.notes.append(
                "screenshot shows the rendered serial console"
            )
        with console.VncSession(self.lab, self.api, self.vmid) as session:
            rgb = session.client.capture()
            return {
                "width": session.client.width,
                "height": session.client.height,
                "rgb": rgb,
            }

    def describe(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            **self.capabilities.as_dict(),
        }


# --- commands ------------------------------------------------------------


def _lease_owns(lab: Any, lease_id: str, kind: str, vmid: int) -> bool:
    lease = lab.load_lease(lease_id)
    return any(
        item.get("kind") == kind and int(item.get("vmid", -1)) == vmid
        for item in lease.get("resources", [])
    )


def cmd_template(lab: Any, args: Any) -> None:
    """Convert a stopped, lease-owned guest into a cloneable template."""
    import json

    api = lab.ProxmoxAPI()
    if not _lease_owns(lab, args.lease, args.kind, args.vmid):
        raise lab.LabError(
            f"VMID {args.vmid} is not a {args.kind} guest registered to this lease"
        )
    status = api.call(
        "GET", f"/nodes/{lab.NODE}/{args.kind}/{args.vmid}/status/current"
    )
    if status.get("status") != "stopped":
        raise lab.LabError(
            f"VMID {args.vmid} must be stopped before template conversion "
            f"(status={status.get('status')})"
        )
    result = api.call(
        "POST", f"/nodes/{lab.NODE}/{args.kind}/{args.vmid}/template"
    )
    lab.audit("guest-template", lease=args.lease, kind=args.kind,
              vmid=args.vmid, sync=False)
    print(json.dumps(
        {"vmid": args.vmid, "kind": args.kind, "result": result},
        indent=2, sort_keys=True,
    ))


def cmd_clone(lab: Any, args: Any) -> None:
    """Clone a lease-owned template into a new guest, registering it."""
    import json

    api = lab.ProxmoxAPI()
    if not _lease_owns(lab, args.lease, args.kind, args.template):
        raise lab.LabError(
            f"VMID {args.template} is not a {args.kind} guest registered to "
            "this lease"
        )
    data: dict[str, Any] = {"newid": args.newid}
    if args.name:
        data["name"] = args.name
    result = api.call(
        "POST", f"/nodes/{lab.NODE}/{args.kind}/{args.template}/clone", data
    )
    # The clone endpoint is not the guest-creation path, so it does not
    # auto-register; do that under the same lock every other lease mutator
    # uses so concurrent creations cannot clobber the entry.
    with lab.controller_lock():
        fresh = lab.load_lease(args.lease)
        lab.register_resource(
            fresh, args.kind, args.newid, "delete",
            args.name or f"clone-{args.newid}",
        )
    lab.audit("guest-clone", lease=args.lease, kind=args.kind,
              template=args.template, vmid=args.newid, sync=False)
    print(json.dumps(
        {"vmid": args.newid, "kind": args.kind, "template": args.template,
         "result": result},
        indent=2, sort_keys=True,
    ))


def cmd_probe(lab: Any, args: Any) -> None:
    import json

    api = lab.ProxmoxAPI()
    caps = probe(lab, api, args.vmid)
    advice = []
    if caps.agent:
        advice.append("use 'guest run' or 'console exec' -- real exit codes")
    elif caps.serial:
        advice.append("no agent: use 'guest run --password-stdin', or "
                      "'console text --send' for one-off lines")
    if caps.graphical_console:
        advice.append("VNC keyboard and pointer work on this guest")
    else:
        advice.append("VNC input will not reach this guest; drive it over serial")
    print(json.dumps({**caps.as_dict(), "advice": advice}, indent=2,
                     sort_keys=True))


# --- detached runs -------------------------------------------------------

_RUNS_DIR = "guest-runs"
GRUN_EXIT_MARK = "grun-exit"


def _runs_dir(lab: Any) -> Path:
    directory = Path(lab.STATE_ROOT) / _RUNS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _record_run(lab: Any, vmid: int, pid: str, log: str,
                command: str) -> Path:
    import json

    path = _runs_dir(lab) / f"vm{vmid}-{pid}.json"
    path.write_text(json.dumps({
        "vmid": vmid, "pid": pid, "log": log, "command": command,
        "started_at": lab.iso_now(),
    }, indent=2))
    return path


def _find_run(lab: Any, vmid: int, pid: str) -> dict[str, Any]:
    import json

    path = _runs_dir(lab) / f"vm{vmid}-{pid}.json"
    if not path.is_file():
        raise GuestError(
            f"no detached run recorded for VMID {vmid} pid {pid} on this "
            "controller; run it with 'guest run --detach' first"
        )
    return json.loads(path.read_text())


def _agent_sh(lab: Any, api: Any, vmid: int, script: str) -> dict[str, Any]:
    return console.agent_exec(
        lab, api, vmid, ["/bin/sh", "-c", script], timeout=30,
    )


def _pid_alive(lab: Any, api: Any, vmid: int, pid: str) -> bool:
    run = _agent_sh(lab, api, vmid, f"kill -0 {pid} 2>/dev/null; echo $?")
    return run.get("exitcode") == 0 and run.get("stdout", "").strip() == "0"


def cmd_log(lab: Any, args: Any) -> None:
    """Print or follow the log of a detached guest run."""
    import json

    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    record = _find_run(lab, args.vmid, args.pid)
    log = record["log"]
    cursor = 0
    if args.tail:
        run = _agent_sh(lab, api, args.vmid,
                        f"wc -c < {shlex.quote(log)} 2>/dev/null || echo 0")
        total = int(run.get("stdout", "0").strip() or 0)
        cursor = max(0, total - args.tail)
    deadline = time.monotonic() + (args.timeout if args.follow else 0)
    exited = False
    while True:
        # The guest reports the byte count of what it emitted so the cursor
        # stays byte-aligned even when the log contains non-ASCII output.
        token = secrets.token_hex(4)
        marker = f"__logb{token}__"
        run = _agent_sh(
            lab, api, args.vmid,
            f"LC_ALL=C out=$(tail -c +{cursor + 1} {shlex.quote(log)} "
            f"2>/dev/null); printf '%s' \"$out\"; "
            f"printf '\\n{marker}:%s\\n' \"${{#out}}\"",
        )
        output = run.get("stdout", "")
        count = 0
        if output:
            split = output.rsplit(f"\n{marker}:", 1)
            if len(split) == 2:
                data, count_text = split
                if count_text.isdigit():
                    count = int(count_text)
            else:
                data = output
            if data:
                print(data, end="", flush=True)
            cursor += count if count else len(data.encode("utf-8", "replace"))
        alive = _pid_alive(lab, api, args.vmid, args.pid)
        if not alive:
            exited = True
            break
        if not args.follow:
            break
        if args.timeout and time.monotonic() >= deadline:
            break
        time.sleep(1)
    payload = {
        "vmid": args.vmid, "pid": args.pid, "log": log,
        "cursor": cursor, "exited": exited,
    }
    print("\n" + json.dumps(payload, indent=2, sort_keys=True))


def cmd_wait(lab: Any, args: Any) -> None:
    """Wait for a detached guest run to exit, then report its tail."""
    import json

    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    record = _find_run(lab, args.vmid, args.pid)
    log = record["log"]
    started = time.monotonic()
    deadline = started + args.timeout
    while time.monotonic() < deadline:
        if not _pid_alive(lab, api, args.vmid, args.pid):
            break
        time.sleep(5)
    elapsed = round(time.monotonic() - started)
    exited = not _pid_alive(lab, api, args.vmid, args.pid)
    tail = ""
    exit_code = None
    if exited:
        run = _agent_sh(lab, api, args.vmid,
                        f"tail -c 65536 {shlex.quote(log)} 2>/dev/null || true")
        tail = run.get("stdout", "")
        for line in reversed(tail.splitlines()):
            marker = line.strip()
            if marker.startswith(f"{GRUN_EXIT_MARK}:"):
                value = marker.split(":", 1)[1].strip()
                if value.isdigit():
                    exit_code = int(value)
                break
    payload = {
        "vmid": args.vmid, "pid": args.pid, "log": log,
        "exited": exited, "elapsed_seconds": elapsed,
        "exit_code": exit_code, "tail": tail[-2000:],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_run(lab: Any, args: Any) -> None:
    import json
    import sys

    api = lab.ProxmoxAPI()
    lab.load_lease(args.lease)
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else None
    if args.detach:
        command = " ".join(args.command)
        token = secrets.token_hex(4)
        log = f"/tmp/grun-{token}.log"
        # nohup so the process survives agent/console disconnects; the inner
        # sh records its own exit code into the log for 'guest wait'.
        script = (
            f"nohup sh -c '({command}) > {shlex.quote(log)} 2>&1; "
            f"echo {GRUN_EXIT_MARK}:$? >> {shlex.quote(log)}' "
            ">/dev/null 2>&1 & echo $!"
        )
        result = console.agent_exec(
            lab, api, args.vmid, ["/bin/sh", "-c", script],
            timeout=args.timeout,
        )
        if result["exitcode"] not in (0, None):
            raise lab.LabError(
                f"could not start detached run: {result['stderr'][:400]}"
            )
        pid = result.get("stdout", "").strip()
        if not pid.isdigit():
            raise lab.LabError(f"detached run did not report a pid: {pid!r}")
        _record_run(lab, args.vmid, pid, log, command)
        lab.audit("guest-run-detached", lease=args.lease, vmid=args.vmid,
                  pid=pid, sync=False)
        print(json.dumps({
            "vmid": args.vmid, "pid": pid, "log": log, "command": command,
            "next": f"proxmox-lab guest log --lease {args.lease} "
                    f"--vmid {args.vmid} --pid {pid} --follow",
        }, indent=2, sort_keys=True))
        return
    try:
        with GuestSession(lab, api, args.vmid, user=args.user,
                          password=password, prefer=args.prefer) as guest:
            result = guest.run(" ".join(args.command), timeout=args.timeout)
            payload = {"vmid": args.vmid, **result.as_dict()}
    except GuestError as exc:
        raise lab.LabError(str(exc)) from None
    lab.audit("guest-run", lease=args.lease, vmid=args.vmid,
              channel=result.channel, exit_code=result.exit_code, sync=False)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not result.ok:
        raise lab.LabError(f"command exited {result.exit_code}")


def register(sub: Any, lab: Any) -> None:
    def bind(handler: Any) -> Any:
        return lambda args: handler(lab, args)

    guest = sub.add_parser("guest", help="talk to a guest over any channel")
    guest_sub = guest.add_subparsers(dest="guest_command", required=True)

    probe_cmd = guest_sub.add_parser(
        "probe", help="how can this guest be reached? (read-only)"
    )
    probe_cmd.add_argument("--vmid", type=int, required=True)
    probe_cmd.set_defaults(func=bind(cmd_probe))

    run_cmd = guest_sub.add_parser(
        "run", help="run a command, picking the channel automatically"
    )
    run_cmd.add_argument("--lease", required=True)
    run_cmd.add_argument("--vmid", type=int, required=True)
    run_cmd.add_argument("--user", help="console user, for the serial channel")
    run_cmd.add_argument("--password-stdin", action="store_true",
                         help="console password on stdin, enabling serial")
    run_cmd.add_argument("--prefer", choices=("agent", "serial"),
                         default="agent")
    run_cmd.add_argument("--timeout", type=int, default=300)
    run_cmd.add_argument("--detach", action="store_true",
                         help="start in the background and return immediately "
                              "(agent channel; Linux guests)")
    run_cmd.add_argument("command", nargs="+")
    run_cmd.set_defaults(func=bind(cmd_run))

    log_cmd = guest_sub.add_parser(
        "log", help="print or stream the log of a detached guest run"
    )
    log_cmd.add_argument("--lease", required=True)
    log_cmd.add_argument("--vmid", type=int, required=True)
    log_cmd.add_argument("--pid", required=True)
    log_cmd.add_argument("--tail", type=int,
                         help="start from the last N bytes of the log")
    log_cmd.add_argument("--follow", action="store_true",
                         help="stream new output until the run exits or timeout")
    log_cmd.add_argument("--timeout", type=int, default=60,
                         help="seconds to follow (default 60)")
    log_cmd.set_defaults(func=bind(cmd_log))

    wait_cmd = guest_sub.add_parser(
        "wait", help="wait for a detached guest run to exit and report its tail"
    )
    wait_cmd.add_argument("--lease", required=True)
    wait_cmd.add_argument("--vmid", type=int, required=True)
    wait_cmd.add_argument("--pid", required=True)
    wait_cmd.add_argument("--timeout", type=int, default=3600)
    wait_cmd.set_defaults(func=bind(cmd_wait))

    template_cmd = guest_sub.add_parser(
        "template",
        help="convert a stopped lease-owned guest into a cloneable template",
    )
    template_cmd.add_argument("--lease", required=True)
    template_cmd.add_argument("--vmid", type=int, required=True)
    template_cmd.add_argument("--kind", choices=("qemu", "lxc"), default="qemu")
    template_cmd.set_defaults(func=bind(cmd_template))

    clone_cmd = guest_sub.add_parser(
        "clone", help="clone a lease-owned template into a new guest"
    )
    clone_cmd.add_argument("--lease", required=True)
    clone_cmd.add_argument("--template", type=int, required=True)
    clone_cmd.add_argument("--newid", type=int, required=True)
    clone_cmd.add_argument("--name")
    clone_cmd.add_argument("--kind", choices=("qemu", "lxc"), default="qemu")
    clone_cmd.set_defaults(func=bind(cmd_clone))
