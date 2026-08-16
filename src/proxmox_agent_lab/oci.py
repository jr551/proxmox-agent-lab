"""Experimental OCI-image application containers.

Proxmox VE 9.1+ can pull a public OCI image with ``skopeo`` and convert its
archive into an LXC root filesystem. This is intentionally separate from VMs:
an OCI LXC shares the Proxmox host kernel and is not a safe boundary for
untrusted code.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Matches the public-reference grammar accepted by PVE's
# ``oci-registry-pull`` endpoint. PVE currently accepts tags, not digest
# references, so every direct registry pull is explicitly gated as mutable.
_REFERENCE_RE = re.compile(
    r"^(?:(?:[a-zA-Z\d]|[a-zA-Z\d][a-zA-Z\d-]*[a-zA-Z\d])"
    r"(?:\.(?:[a-zA-Z\d]|[a-zA-Z\d][a-zA-Z\d-]*[a-zA-Z\d]))*(?::\d+)?/)?"
    r"[a-z\d]+(?:/[a-z\d]+(?:(?:(?:[._]|__|[-]*)[a-z\d]+)+)?)*:\w[\w.-]{0,127}$"
)
_TEMPLATE_RE = re.compile(
    r"^(?P<storage>[A-Za-z0-9][A-Za-z0-9_.-]*):vztmpl/"
    r"(?P<filename>[A-Za-z0-9][A-Za-z0-9_.-]*\.tar)$"
)
_STORAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

_EXPERIMENTAL_WARNING = (
    "Experimental: PVE OCI images become unprivileged LXC containers. They "
    "share the Proxmox host kernel and do not provide QEMU-grade isolation; "
    "never use them for untrusted code, malware, kernel work, or device "
    "emulation."
)


def _validate_reference(lab: Any, reference: str) -> None:
    if not _REFERENCE_RE.fullmatch(reference):
        raise lab.LabError(
            "OCI reference must be a lower-case registry image with an explicit "
            "tag, for example docker.io/library/busybox:1.37.0. PVE's direct "
            "registry API currently does not accept digest references."
        )


def _template_for_reference(storage: str, reference: str) -> str:
    """Return PVE's normalized archive volume name for a registry reference."""
    filename = reference.rsplit("/", 1)[-1].replace(":", "_") + ".tar"
    return f"{storage}:vztmpl/{filename}"


def _validate_template(lab: Any, template: str) -> None:
    if not _TEMPLATE_RE.fullmatch(template):
        raise lab.LabError(
            "OCI template must be a PVE OCI archive volume such as "
            "local:vztmpl/busybox_1.37.0.tar"
        )


def _lease_tags(lease_id: str) -> str:
    return f"codex-lab;lease-{lease_id}"


def _register_created_lxc(lab: Any, lease_id: str, vmid: int, name: str) -> None:
    """Register only after PVE reports creation completed successfully."""
    with lab.controller_lock():
        fresh = lab.load_lease(lease_id)
        policy = "retain" if lab.is_long_term(fresh) else "delete"
        lab.register_resource(fresh, "lxc", vmid, policy, name)


def cmd_pull(lab: Any, args: Any) -> None:
    """Pull a public, mutable OCI tag into PVE template storage."""
    if not args.host_change_authorized:
        raise lab.LabError(
            "Pulling an OCI archive persists data in host template storage. "
            "Re-run with --host-change-authorized only when the user asked for "
            "that exact host storage change."
        )
    if not args.allow_mutable_reference:
        raise lab.LabError(
            "PVE's direct OCI registry API accepts mutable tags, not digest "
            "references. Re-run with --allow-mutable-reference only after "
            "accepting that supply-chain limitation."
        )
    _validate_reference(lab, args.reference)
    if not _STORAGE_RE.fullmatch(args.storage):
        raise lab.LabError(f"invalid PVE storage name: {args.storage!r}")

    lab.load_lease(args.lease)
    api = lab.ProxmoxAPI()
    template = _template_for_reference(args.storage, args.reference)
    contents = api.call(
        "GET", f"/nodes/{lab.NODE}/storage/{args.storage}/content"
    ) or []
    if any(item.get("volid") == template for item in contents):
        raise lab.LabError(
            f"refusing to overwrite existing host template {template}; inspect "
            "and delete it explicitly before pulling a replacement"
        )
    lab.audit(
        "oci-pull-intent",
        lease=args.lease,
        storage=args.storage,
        reference=args.reference,
        template=template,
        mutable_reference=True,
        experimental=True,
    )
    upid = api.call(
        "POST", f"/nodes/{lab.NODE}/storage/{args.storage}/oci-registry-pull",
        {"reference": args.reference},
    )
    status = lab.wait_task(api, upid, timeout=args.timeout)
    lab.audit(
        "oci-pulled",
        lease=args.lease,
        storage=args.storage,
        reference=args.reference,
        template=template,
        task=upid,
        mutable_reference=True,
        experimental=True,
    )
    print(json.dumps({
        "experimental": True,
        "reference": args.reference,
        "template": template,
        "task_status": status,
        "warning": _EXPERIMENTAL_WARNING,
        "supply_chain_warning": (
            "PVE pulled a tag because its direct registry API does not accept "
            "digest references. Record the registry's published digest outside "
            "this command before using the resulting archive."
        ),
    }, indent=2, sort_keys=True))


def cmd_create(lab: Any, args: Any) -> None:
    """Create one lease-owned unprivileged LXC from a pulled OCI archive."""
    _validate_template(lab, args.template)
    if not _STORAGE_RE.fullmatch(args.rootfs_storage):
        raise lab.LabError(f"invalid PVE storage name: {args.rootfs_storage!r}")
    if args.disk_gb < 1:
        raise lab.LabError("--disk-gb must be at least 1")
    if args.memory < 64:
        raise lab.LabError("--memory must be at least 64 MiB")
    if args.swap < 0:
        raise lab.LabError("--swap may not be negative")

    lease = lab.load_lease(args.lease)
    if lab.is_long_term(lease):
        raise lab.LabError(
            "experimental OCI containers support ordinary leases only; use a "
            "QEMU VM for retained workloads"
        )
    if args.vmid in lease.get("initial_vmids", []):
        raise lab.LabError(f"VMID {args.vmid} existed before this lease")

    hostname = args.hostname or f"oci-{args.vmid}"
    payload = {
        "vmid": args.vmid,
        "hostname": hostname,
        "ostemplate": args.template,
        "rootfs": f"{args.rootfs_storage}:{args.disk_gb}",
        "memory": args.memory,
        "swap": args.swap,
        "unprivileged": 1,
        "onboot": 0,
        "tags": _lease_tags(args.lease),
    }
    api = lab.ProxmoxAPI()
    lab.audit(
        "oci-create-intent",
        lease=args.lease,
        vmid=args.vmid,
        template=args.template,
        rootfs_storage=args.rootfs_storage,
        disk_gb=args.disk_gb,
        unprivileged=True,
        experimental=True,
    )
    upid = api.call("POST", f"/nodes/{lab.NODE}/lxc", payload)
    create_status = lab.wait_task(api, upid, timeout=args.timeout)
    _register_created_lxc(lab, args.lease, args.vmid, hostname)

    start_status = None
    if args.start:
        start_upid = api.call("POST", f"/nodes/{lab.NODE}/lxc/{args.vmid}/status/start")
        start_status = lab.wait_task(api, start_upid, timeout=args.timeout)

    lab.audit(
        "oci-created",
        lease=args.lease,
        vmid=args.vmid,
        template=args.template,
        started=bool(args.start),
        unprivileged=True,
        experimental=True,
    )
    print(json.dumps({
        "experimental": True,
        "kind": "lxc",
        "vmid": args.vmid,
        "template": args.template,
        "unprivileged": True,
        "create_task_status": create_status,
        "start_task_status": start_status,
        "warning": _EXPERIMENTAL_WARNING,
        "interaction_note": (
            "OCI application images commonly lack qemu-guest-agent and an "
            "interactive login. Use a VM when the existing guest-agent, VNC, "
            "serial, or device tooling is required."
        ),
    }, indent=2, sort_keys=True))


def register(sub: Any, lab: Any) -> None:
    oci = sub.add_parser(
        "oci",
        help="experimental, low-isolation OCI image containers (PVE 9.1+)",
    )
    commands = oci.add_subparsers(dest="oci_command", required=True)

    pull = commands.add_parser(
        "pull", help="pull a public OCI tag into template storage (experimental)",
    )
    pull.add_argument("--lease", required=True)
    pull.add_argument("--reference", required=True,
                      help="public image with explicit tag, never a digest")
    pull.add_argument("--storage", default="local",
                      help="file-based PVE storage with vztmpl content")
    pull.add_argument("--host-change-authorized", action="store_true")
    pull.add_argument("--allow-mutable-reference", action="store_true")
    pull.add_argument("--timeout", type=int, default=1800)
    pull.set_defaults(func=lambda args: cmd_pull(lab, args))

    create = commands.add_parser(
        "create", help="create a lease-owned OCI LXC (experimental)",
    )
    create.add_argument("--lease", required=True)
    create.add_argument("--vmid", required=True, type=int)
    create.add_argument("--template", required=True,
                        help="OCI archive volume returned by 'oci pull'")
    create.add_argument("--rootfs-storage", required=True)
    create.add_argument("--disk-gb", required=True, type=int)
    create.add_argument("--hostname")
    create.add_argument("--memory", type=int, default=512,
                        help="memory limit in MiB (default: 512)")
    create.add_argument("--swap", type=int, default=0,
                        help="swap limit in MiB (default: 0)")
    create.add_argument("--start", action="store_true")
    create.add_argument("--timeout", type=int, default=1800)
    create.set_defaults(func=lambda args: cmd_create(lab, args))
