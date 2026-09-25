"""Tests for the transfer policy: push/pull over chunked base64 guest exec.

The transport itself lives in ``proxmox.push_bytes``/``pull_bytes``; this
file pins the policy layer over it: the lease-ownership gate precedes every
seam call, the guest kind comes from the registry row, ``--sha256`` is
enforced, a pull lands as a 0600 file and is removed on a digest mismatch,
audit rows carry counts and digests but never content, the ``--timeout``
budget is bounded, and the S3 machinery is gone.

Everything runs against a ``TemporaryDirectory`` state root, a real store
(the ownership gate reads it) and a fake transfer seam -- no test spawns a
real ssh or opens real controller state.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import sys  # noqa: E402
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import store  # noqa: E402
from proxmox_agent_lab import transfer  # noqa: E402
from proxmox_agent_lab.errors import LabError  # noqa: E402
from proxmox_agent_lab.proxmox import ProxmoxError  # noqa: E402


def fake_config() -> SimpleNamespace:
    """The §G surface the transfer reads, and nothing else."""
    return SimpleNamespace(
        ssh=SimpleNamespace(target="fixture-host"),
        pve=SimpleNamespace(node="pve", template_vmid=9000),
        lease=SimpleNamespace(ttl_seconds=7200, idle_shutdown_seconds=28800),
        power=SimpleNamespace(
            mac="aa:bb:cc:dd:ee:ff", broadcast="255.255.255.255", port=9
        ),
    )


class FakeLab:
    """The `lab` surface the handlers use: config, state root, audit."""

    def __init__(self, state_root: Path) -> None:
        self.STATE_ROOT = Path(state_root)
        self.CONFIG = fake_config()
        self.audits: list[tuple[str, dict]] = []

    def audit(self, event: str, **fields) -> None:
        self.audits.append((event, fields))


class FakeProxmox:
    """The transfer seam double: records calls, scripts digests and payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.push_digest: str | None = None
        self.pull_data: bytes = b""
        self.pull_error: BaseException | None = None

    def push_bytes(self, kind, vmid, dest, data):
        self.calls.append(("push_bytes", kind, vmid, dest, data))
        if self.push_digest is not None:
            return self.push_digest
        return hashlib.sha256(data).hexdigest()

    def pull_bytes(self, kind, vmid, src):
        self.calls.append(("pull_bytes", kind, vmid, src))
        if self.pull_error is not None:
            raise self.pull_error
        return self.pull_data


class TransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name)
        self.lab = FakeLab(self.state_root)
        self.seam = FakeProxmox()
        patcher = mock.patch.object(
            transfer, "_make_proxmox", return_value=self.seam
        )
        self.seam_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.payload = b"payload bytes"
        self.source = self.state_root / "payload.bin"
        self.source.write_bytes(self.payload)

    # -- helpers -----------------------------------------------------------

    def register(self, kind="qemu", vmid=101, *, policy="disposable") -> None:
        with store.Store(self.state_root / "lab.db") as database:
            if database.get_lease("lease-1") is None:
                database.create_lease("lease-1", expires_at=int(time.time()) + 7200)
            database.register_resource("lease-1", kind, vmid, policy=policy)

    def capture(self, fn):
        buffer = io.StringIO()
        error = None
        with contextlib.redirect_stdout(buffer), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                fn()
            except LabError as exc:
                error = exc
        text = buffer.getvalue()
        return (json.loads(text) if text.strip() else None), error

    def push_args(self, **overrides) -> SimpleNamespace:
        values = dict(
            lease="lease-1", vmid=101, file=str(self.source),
            dest="/tmp/pxl-a.bin", sha256=None, timeout=300,
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def pull_args(self, **overrides) -> SimpleNamespace:
        values = dict(
            lease="lease-1", vmid=101, remote="/etc/pxl/a.bin",
            out=str(self.state_root / "pulled.bin"), sha256=None, timeout=300,
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    # -- push --------------------------------------------------------------

    def test_push_delivers_the_bytes_and_reports_the_guest_digest(self) -> None:
        self.register()
        # A divergent seam digest proves the JSON carries the seam's return.
        self.seam.push_digest = "guest-verified-digest"
        payload, error = self.capture(
            lambda: transfer.cmd_push(self.lab, self.push_args())
        )
        self.assertIsNone(error)
        self.assertEqual(self.seam.calls, [
            ("push_bytes", "qemu", 101, "/tmp/pxl-a.bin", self.payload),
        ])
        self.assertEqual(payload, {
            "lease_id": "lease-1",
            "vmid": 101,
            "local_path": str(self.source.resolve()),
            "remote_path": "/tmp/pxl-a.bin",
            "bytes": len(self.payload),
            "sha256": "guest-verified-digest",
        })

    def test_kind_comes_from_the_registry_row(self) -> None:
        self.register(kind="lxc", vmid=202)
        _, error = self.capture(
            lambda: transfer.cmd_push(self.lab, self.push_args(vmid=202))
        )
        self.assertIsNone(error)
        self.assertEqual(self.seam.calls, [
            ("push_bytes", "lxc", 202, "/tmp/pxl-a.bin", self.payload),
        ])

    def test_unregistered_guest_is_refused_before_any_seam_call(self) -> None:
        _, error = self.capture(
            lambda: transfer.cmd_push(self.lab, self.push_args())
        )
        self.assertIsInstance(error, LabError)
        self.assertIn("lease-register", str(error))
        _, error = self.capture(
            lambda: transfer.cmd_pull(self.lab, self.pull_args())
        )
        self.assertIsInstance(error, LabError)
        self.assertIn("lease-register", str(error))
        self.seam_factory.assert_not_called()
        self.assertEqual(self.seam.calls, [])
        self.assertEqual(self.lab.audits, [])

    def test_sha256_mismatch_refuses_before_touching_the_guest(self) -> None:
        self.register()
        args = self.push_args(sha256="0" * 64)
        _, error = self.capture(lambda: transfer.cmd_push(self.lab, args))
        self.assertIsInstance(error, LabError)
        self.assertIn("sha256 mismatch", str(error))
        self.seam_factory.assert_not_called()
        self.assertEqual(self.seam.calls, [])
        self.assertEqual(self.lab.audits, [])  # nothing happened, nothing audited

    # -- pull --------------------------------------------------------------

    def test_pull_writes_a_private_file_with_the_verified_digest(self) -> None:
        self.register()
        self.seam.pull_data = self.payload
        out = self.state_root / "pulled.bin"
        payload, error = self.capture(
            lambda: transfer.cmd_pull(self.lab, self.pull_args(out=str(out)))
        )
        self.assertIsNone(error)
        self.assertEqual(out.read_bytes(), self.payload)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertEqual(self.seam.calls, [
            ("pull_bytes", "qemu", 101, "/etc/pxl/a.bin"),
        ])
        self.assertEqual(payload, {
            "lease_id": "lease-1",
            "vmid": 101,
            "local_path": str(out),
            "remote_path": "/etc/pxl/a.bin",
            "bytes": len(self.payload),
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        })

    def test_pull_sha256_mismatch_removes_the_file_and_fails(self) -> None:
        self.register()
        self.seam.pull_data = self.payload
        out = self.state_root / "pulled.bin"
        args = self.pull_args(out=str(out), sha256="0" * 64)
        _, error = self.capture(lambda: transfer.cmd_pull(self.lab, args))
        self.assertIsInstance(error, LabError)
        self.assertIn("sha256 mismatch", str(error))
        self.assertFalse(out.exists())  # the bad pull is removed
        self.assertEqual(self.lab.audits, [])

    def test_pull_seam_mismatch_raises_and_writes_nothing(self) -> None:
        self.register()
        self.seam.pull_error = ProxmoxError(
            "sha256 mismatch pulling /etc/pxl/a.bin: guest " + "0" * 64
        )
        out = self.state_root / "pulled.bin"
        _, error = self.capture(
            lambda: transfer.cmd_pull(self.lab, self.pull_args(out=str(out)))
        )
        self.assertIsInstance(error, LabError)
        self.assertIn("sha256 mismatch", str(error))
        self.assertFalse(out.exists())
        self.assertEqual(self.lab.audits, [])

    # -- audit and surface ---------------------------------------------------

    def test_audit_events_carry_no_content(self) -> None:
        self.register()
        self.seam.push_digest = hashlib.sha256(self.payload).hexdigest()
        expected = hashlib.sha256(self.payload).hexdigest()
        args = self.push_args(sha256=expected)  # a matching digest must pass
        _, error = self.capture(lambda: transfer.cmd_push(self.lab, args))
        self.assertIsNone(error)
        self.assertEqual(self.lab.audits, [("guest-push", {
            "lease": "lease-1",
            "vmid": 101,
            "remote_path": "/tmp/pxl-a.bin",
            "bytes": len(self.payload),
            "sha256": expected,
        })])
        self.lab.audits.clear()
        self.seam.pull_data = self.payload
        args = self.pull_args(sha256=expected)  # and so must a matching pull
        _, error = self.capture(lambda: transfer.cmd_pull(self.lab, args))
        self.assertIsNone(error)
        self.assertEqual(self.lab.audits, [("guest-pull", {
            "lease": "lease-1",
            "vmid": 101,
            "remote_path": "/etc/pxl/a.bin",
            "bytes": len(self.payload),
            "sha256": expected,
        })])

    def test_register_exposes_the_old_arg_names(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        transfer.register(sub, self.lab)
        pushed = parser.parse_args([
            "push", "--lease", "L-1", "--vmid", "101",
            "--file", "f.bin", "--dest", "/tmp/x",
        ])
        self.assertEqual(
            (pushed.lease, pushed.vmid, pushed.file, pushed.dest),
            ("L-1", 101, "f.bin", "/tmp/x"),
        )
        self.assertTrue(callable(pushed.func))
        self.assertEqual(pushed.timeout, 300)
        pulled = parser.parse_args([
            "pull", "--lease", "L-1", "--vmid", "101",
            "--remote", "/tmp/x", "--out", "y.bin",
        ])
        self.assertEqual(
            (pulled.lease, pulled.vmid, pulled.remote, pulled.out),
            ("L-1", 101, "/tmp/x", "y.bin"),
        )
        self.assertTrue(callable(pulled.func))
        self.assertEqual(pulled.timeout, 300)

    def test_the_s3_machinery_is_gone(self) -> None:
        source = Path(transfer.__file__).read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r"(?m)^\s*(?:import|from)\s+[^\n]*\bs3\b", source)
        )

    def test_timeout_bounds_the_transfer(self) -> None:
        self.register()

        def slow(*_args, **_kwargs):
            time.sleep(30)
            return "never"

        self.seam.push_bytes = slow
        args = self.push_args(timeout=0.25)
        _, error = self.capture(lambda: transfer.cmd_push(self.lab, args))
        self.assertIsInstance(error, LabError)
        self.assertIn("did not finish within 0.25s", str(error))
        self.assertEqual(self.lab.audits, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
