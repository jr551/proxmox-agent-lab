"""Offline tests for guest lifecycle over the proxmox seam (rework wave 4).

`guest.py` is the slim lifecycle layer over `proxmox.py`: create/clone stamp
the §F metadata contract (exact `qm clone` + `qm set`, or one `qm create`/
`pct create` call), every mutation gates on the SQLite registry before any
seam call, and destroy refuses anything registry-vouched. Everything runs
against `FakeSSH` behind a real `Proxmox` (so remote argv is asserted) or a
stub seam — no network, no real ssh, no wall-clock dependence.
"""

from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path

import sys  # noqa: E402

# Shared bootstrap: fixture configuration plus a per-process state directory,
# applied before any proxmox_agent_lab import. `support` sits beside this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401
import json  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import errors  # noqa: E402
from proxmox_agent_lab import guest as lab_guest  # noqa: E402
from proxmox_agent_lab import leases as leases_module  # noqa: E402
from proxmox_agent_lab import proxmox as proxmox_module  # noqa: E402
from proxmox_agent_lab import store as store_module  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402

LEASE = "abs-guest-lease"
OTHER_LEASE = "abs-other-lease"
EXPIRY = 1_800_000_000


class GuestCase(unittest.TestCase):
    """A temp state root, a lab facade double, and a FakeSSH-backed seam."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.lab = mock.Mock()
        self.lab.STATE_ROOT = self.root
        self.lab.CONFIG = config_module.get()
        self.fake = FakeSSH()
        self.prox = proxmox_module.Proxmox(
            self.fake, "pve", sleep=lambda _seconds: None
        )
        patcher = mock.patch.object(
            lab_guest, "_make_proxmox", lambda config: self.prox
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- fixtures -----------------------------------------------------------

    def store(self) -> store_module.Store:
        return store_module.Store(self.root / "lab.db")

    def open_lease(self, lease_id: str = LEASE) -> None:
        with self.store() as store:
            store.create_lease(
                lease_id, kind="ordinary", purpose="test", expires_at=EXPIRY
            )

    def register_guest(
        self, lease_id: str, kind: str, vmid: int, *,
        name: str | None = None, policy: str = "disposable",
    ) -> None:
        with self.store() as store:
            store.register_resource(
                lease_id, kind, vmid, name=name, policy=policy
            )

    def resources(self, lease_id: str = LEASE) -> list[dict]:
        with self.store() as store:
            return store.resources_for(lease_id)

    # -- dispatch through the registered CLI surface -------------------------

    def run_cmd(self, *argv: str):
        """Parse `argv` through `guest.register` and dispatch.

        Asserts the established output contract on the way: what the handler
        printed on stdout is exactly the payload it returned.
        """
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        lab_guest.register(sub, self.lab)
        args = parser.parse_args(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            payload = args.func(args)
        self.assertEqual(json.loads(out.getvalue()), payload)
        return payload

    def argvs(self) -> list[list[str]]:
        return [call["argv"] for call in self.fake.calls]


class SurfaceTests(GuestCase):
    def test_register_exposes_exactly_the_eight_guest_subcommands(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        lab_guest.register(sub, self.lab)
        self.assertEqual(sorted(sub.choices), ["guest"])
        [action] = [
            item for item in sub.choices["guest"]._actions
            if isinstance(item, argparse._SubParsersAction)
        ]
        self.assertEqual(
            sorted(action.choices),
            ["clone", "create", "destroy", "list", "probe", "run",
             "start", "stop"],
        )


class MetadataContractTests(GuestCase):
    def test_metadata_for_delegates_the_format_strings(self) -> None:
        tags, description = lab_guest.metadata_for(LEASE, EXPIRY)
        self.assertEqual(tags, leases_module.metadata_tags(LEASE))
        self.assertEqual(description,
                         leases_module.metadata_description(LEASE, EXPIRY))
        parts = tags.split(";")
        self.assertEqual(parts[0], "proxmoxagentlab")
        self.assertEqual(parts[-1], f"lease-{LEASE}")
        self.assertNotIn("pxl", parts)
        self.assertEqual(description,
                         f"pxl-lease={LEASE} pxl-expiry={EXPIRY}")

    def test_stamp_guest_writes_both_fields_in_one_call(self) -> None:
        seam = mock.Mock()
        lab_guest.stamp_guest(seam, "lxc", 102, LEASE, EXPIRY)
        seam.set_metadata.assert_called_once_with(
            "lxc", 102,
            tags=leases_module.metadata_tags(LEASE),
            description=f"pxl-lease={LEASE} pxl-expiry={EXPIRY}",
        )
        self.assertEqual(seam.mock_calls[0][0], "set_metadata")


class RequireOwnedTests(GuestCase):
    def test_returns_the_registry_row(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101, name="alpha")
        row = lab_guest.require_owned(self.lab, LEASE, "qemu", 101)
        self.assertEqual(row["name"], "alpha")
        self.assertEqual(row["kind"], "qemu")
        self.assertEqual(int(row["vmid"]), 101)
        self.assertEqual(row["policy"], "disposable")
        # kind=None matches whichever kind the registry recorded.
        self.assertEqual(
            lab_guest.require_owned(self.lab, LEASE, None, 101)["vmid"],
            row["vmid"],
        )
        self.assertEqual(self.argvs(), [])

    def test_absent_rows_are_refused_naming_lease_register(self) -> None:
        self.open_lease()
        with self.assertRaises(errors.LabError) as caught:
            lab_guest.require_owned(self.lab, LEASE, "qemu", 101)
        self.assertIn("lease-register", str(caught.exception))
        # the wrong kind is just as absent -- lookup is keyed on all three
        self.register_guest(LEASE, "lxc", 101)
        with self.assertRaises(errors.LabError) as caught:
            lab_guest.require_owned(self.lab, LEASE, "qemu", 101)
        self.assertIn("lease-register", str(caught.exception))

    def test_a_destroyed_row_is_no_longer_owned(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101)
        with self.store() as store:
            store.mark_destroyed(LEASE, "qemu", 101)
        with self.assertRaises(errors.LabError) as caught:
            lab_guest.require_owned(self.lab, LEASE, None, 101)
        self.assertIn("lease-register", str(caught.exception))

    def test_registry_vouched_rows_are_refused_naming_the_vouching(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101, policy="retain")
        with self.assertRaises(errors.LabError) as caught:
            lab_guest.require_owned(self.lab, LEASE, None, 101)
        message = str(caught.exception)
        self.assertIn("registry-vouched", message)
        self.assertIn("retain", message)
        # the gate is store-only: a refusal raises before any seam call
        self.assertEqual(self.argvs(), [])


class CreateTests(GuestCase):
    def test_create_from_a_template_restamps_the_metadata(self) -> None:
        self.open_lease()
        self.fake.add(r"^qm clone 9000 101")
        self.fake.add(r"^qm set 101")
        self.fake.add(r"^qm status 101", stdout=b"status: stopped\n")
        payload = self.run_cmd(
            "guest", "create", "--lease", LEASE, "--vmid", "101",
            "--name", "alpha", "--template", "9000",
        )
        tags, description = metadata = lab_guest.metadata_for(LEASE, EXPIRY)
        self.assertEqual(metadata, (leases_module.metadata_tags(LEASE),
                                    f"pxl-lease={LEASE} pxl-expiry={EXPIRY}"))
        self.assertEqual(self.argvs()[0],
                         ["qm", "clone", "9000", "101", "--name", "alpha"])
        self.assertEqual(
            self.argvs()[1],
            ["qm", "set", "101", "--tags", tags,
             "--description", description],
        )
        self.assertEqual(payload, {
            "lease_id": LEASE, "vmid": 101, "kind": "qemu", "name": "alpha",
            "state": "stopped", "tags": tags, "pxl_expiry": EXPIRY,
        })
        self.assertEqual(
            [(row["kind"], int(row["vmid"]), row["policy"])
             for row in self.resources()],
            [("qemu", 101, "disposable")],
        )
        self.lab.audit.assert_called_once_with(
            "guest-create", lease=LEASE, vmid=101, kind="qemu", name="alpha",
            template=9000, started=False,
        )

    def test_the_configured_template_vmid_is_the_default(self) -> None:
        self.open_lease()
        self.fake.add(r"^qm clone 9025 101")
        self.fake.add(r"^qm set 101")
        self.fake.add(r"^qm status 101", stdout=b"status: stopped\n")
        self.run_cmd("guest", "create", "--lease", LEASE, "--vmid", "101")
        # [pve] template_vmid in the fixture config is 9025
        self.assertEqual(self.argvs()[0],
                         ["qm", "clone", "9025", "101", "--name", "pxl-101"])

    def test_fresh_qemu_create_stamps_in_the_create_call(self) -> None:
        self.open_lease()
        self.fake.add(r"^qm create 101")
        self.fake.add(r"^qm status 101", stdout=b"status: stopped\n")
        self.run_cmd(
            "guest", "create", "--lease", LEASE, "--vmid", "101",
            "--name", "alpha", "--fresh", "--memory", "2048", "--cores", "2",
        )
        tags, description = lab_guest.metadata_for(LEASE, EXPIRY)
        self.assertEqual(self.argvs()[0], [
            "qm", "create", "101", "--name", "alpha",
            "--net0", "virtio,bridge=vmbr0",
            "--memory", "2048", "--cores", "2",
            "--tags", tags, "--description", description,
        ])
        # single-call stamping: create + status, and nothing else
        self.assertEqual(len(self.argvs()), 2)

    def test_an_explicitly_empty_template_is_a_fresh_create(self) -> None:
        self.open_lease()
        self.fake.add(r"^qm create 101")
        self.fake.add(r"^qm status 101", stdout=b"status: stopped\n")
        self.run_cmd(
            "guest", "create", "--lease", LEASE, "--vmid", "101",
            "--template", "",
        )
        self.assertEqual(self.argvs()[0][:2], ["qm", "create"])
        self.assertEqual(len(self.argvs()), 2)

    def test_fresh_lxc_create_stamps_in_the_create_call(self) -> None:
        self.open_lease()
        self.fake.add(r"^pct create 102")
        self.fake.add(r"^pct status 102", stdout=b"status: stopped\n")
        self.run_cmd(
            "guest", "create", "--lease", LEASE, "--vmid", "102",
            "--kind", "lxc", "--fresh", "--name", "ct7",
            "--ostemplate", "local:vztmpl/debian-12.tar.zst",
        )
        tags, description = lab_guest.metadata_for(LEASE, EXPIRY)
        self.assertEqual(self.argvs()[0], [
            "pct", "create", "102", "local:vztmpl/debian-12.tar.zst",
            "--hostname", "ct7", "--tags", tags, "--description", description,
        ])
        # no `pct set` fallback: one call carried the metadata
        self.assertEqual(len(self.argvs()), 2)

    def test_fresh_lxc_create_passes_the_storage_rootfs(self) -> None:
        # Hosts whose 'local' dir storage lacks rootdir need --storage:
        # pct create gets --rootfs <storage>:<gb>.
        self.open_lease()
        self.fake.add(r"^pct create 102")
        self.fake.add(r"^pct status 102", stdout=b"status: stopped\n")
        self.run_cmd(
            "guest", "create", "--lease", LEASE, "--vmid", "102",
            "--kind", "lxc", "--fresh", "--name", "ct8",
            "--ostemplate", "local:vztmpl/debian-12.tar.zst",
            "--storage", "local-lvm", "--disk-gb", "4",
        )
        tags, description = lab_guest.metadata_for(LEASE, EXPIRY)
        self.assertEqual(self.argvs()[0], [
            "pct", "create", "102", "local:vztmpl/debian-12.tar.zst",
            "--hostname", "ct8", "--rootfs", "local-lvm:4",
            "--tags", tags, "--description", description,
        ])

    def test_create_registers_the_resource_before_it_starts(self) -> None:
        self.open_lease()
        self.fake.add(r"^qm clone 9000 101")
        self.fake.add(r"^qm set 101")
        self.fake.add(r"^qm start 101")
        self.fake.add(r"^qm status 101", stdout=b"status: running\n")
        events: list[str] = []
        real_run = self.fake.run

        def recording_run(argv, **kwargs):
            events.append(" ".join(argv))
            return real_run(argv, **kwargs)

        self.fake.run = recording_run
        real_register = store_module.Store.register_resource

        def recording_register(store_self, lease_id, kind, vmid, **kwargs):
            events.append("register")
            return real_register(store_self, lease_id, kind, vmid, **kwargs)

        with mock.patch.object(
            store_module.Store, "register_resource", recording_register
        ):
            self.run_cmd(
                "guest", "create", "--lease", LEASE, "--vmid", "101",
                "--template", "9000", "--start",
            )
        self.assertLess(events.index("register"), events.index("qm start 101"))
        self.assertEqual(self.resources()[0]["policy"], "disposable")

    def test_a_mutation_needs_a_live_lease(self) -> None:
        with self.assertRaises(errors.LabError) as caught:
            self.run_cmd("guest", "create", "--lease", LEASE, "--vmid", "101")
        self.assertIn("lease-begin", str(caught.exception))
        self.assertEqual(self.argvs(), [])
        self.open_lease()
        with self.store() as store:
            store.set_lease_state(LEASE, "ended", ended=True)
        with self.assertRaises(errors.LabError) as caught:
            self.run_cmd("guest", "create", "--lease", LEASE, "--vmid", "101")
        self.assertIn("lease-begin", str(caught.exception))
        self.assertEqual(self.argvs(), [])


class CloneTests(GuestCase):
    def test_clone_accepts_a_vouched_template_without_ownership(self) -> None:
        # 9000 belongs to nobody here: template: 1 in its config vouches.
        self.open_lease()
        self.fake.add(r"^qm config 9000",
                      stdout=b"template: 1\ntags: pxl;lease-abs-other-lease\n")
        self.fake.add(r"^qm clone 9000 101")
        self.fake.add(r"^qm set 101")
        self.fake.add(r"^qm status 101", stdout=b"status: stopped\n")
        payload = self.run_cmd(
            "guest", "clone", "--lease", LEASE, "--vmid", "101",
            "--source", "9000", "--name", "beta",
        )
        self.assertEqual(self.argvs()[1],
                         ["qm", "clone", "9000", "101", "--name", "beta"])
        self.assertEqual(payload, {
            "lease_id": LEASE, "vmid": 101, "source": 9000, "name": "beta",
            "state": "stopped", "upid": None,
        })
        self.assertEqual(
            [(row["kind"], int(row["vmid"]), row["policy"])
             for row in self.resources()],
            [("qemu", 101, "disposable")],
        )

    def test_clone_accepts_a_retain_row_of_another_lease(self) -> None:
        self.open_lease()
        self.open_lease(OTHER_LEASE)
        self.register_guest(OTHER_LEASE, "lxc", 9002,
                            name="golden", policy="retain")
        self.fake.add(r"^pct clone 9002 101")
        self.fake.add(r"^pct set 101")
        self.fake.add(r"^pct status 101", stdout=b"status: stopped\n")
        self.run_cmd(
            "guest", "clone", "--lease", LEASE, "--vmid", "101",
            "--source", "9002", "--name", "beta",
        )
        # the row carries the kind: clone straight off the registry
        self.assertEqual(self.argvs()[0],
                         ["pct", "clone", "9002", "101", "--hostname", "beta"])

    def test_clone_refuses_a_source_nobody_vouches_for(self) -> None:
        self.open_lease()
        with self.assertRaises(errors.LabError) as caught:
            self.run_cmd(
                "guest", "clone", "--lease", LEASE, "--vmid", "101",
                "--source", "9000",
            )
        self.assertIn("registry-vouched", str(caught.exception))
        self.assertEqual(
            [argv for argv in self.argvs() if "clone" in argv], []
        )


class StartStopTests(GuestCase):
    def test_start_reports_the_reached_state(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101, name="alpha")
        self.fake.add(r"^qm start 101")
        self.fake.add(r"^qm status 101", stdout=b"status: running\n")
        payload = self.run_cmd(
            "guest", "start", "--lease", LEASE, "--vmid", "101"
        )
        self.assertEqual(payload, {
            "lease_id": LEASE, "vmid": 101,
            "state": "running", "graceful": None,
        })
        self.lab.audit.assert_called_once_with(
            "guest-start", lease=LEASE, vmid=101, kind="qemu"
        )

    def test_stop_is_graceful_and_never_assumed(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "lxc", 102, name="beta")
        self.fake.add(r"^pct shutdown 102")
        self.fake.add(r"^pct status 102", stdout=b"status: stopped\n")
        payload = self.run_cmd(
            "guest", "stop", "--lease", LEASE, "--vmid", "102",
            "--timeout", "5",
        )
        self.assertEqual(payload, {
            "lease_id": LEASE, "vmid": 102,
            "state": "stopped", "graceful": True,
        })
        self.assertEqual(
            [argv for argv in self.argvs() if argv[1] == "stop"], []
        )
        self.lab.audit.assert_called_once_with(
            "guest-stop", lease=LEASE, vmid=102, kind="lxc", graceful=True
        )

    def test_stop_falls_back_to_a_hard_stop(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101, name="alpha")
        seam = mock.Mock()
        seam.shutdown.return_value = False
        seam.status.return_value = "stopped"
        with mock.patch.object(
            lab_guest, "_make_proxmox", lambda config: seam
        ):
            payload = self.run_cmd(
                "guest", "stop", "--lease", LEASE, "--vmid", "101",
                "--timeout", "5",
            )
        self.assertEqual(payload["graceful"], False)
        seam.stop.assert_called_once_with("qemu", 101)


class DestroyTests(GuestCase):
    def test_destroy_demands_confirm_before_any_seam_call(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101, name="alpha")
        with self.assertRaises(errors.LabError) as caught:
            self.run_cmd("guest", "destroy", "--lease", LEASE, "--vmid", "101")
        self.assertIn("--confirm", str(caught.exception))
        self.assertEqual(self.argvs(), [])

    def test_destroy_refuses_an_unregistered_guest(self) -> None:
        self.open_lease()
        with self.assertRaises(errors.LabError) as caught:
            self.run_cmd(
                "guest", "destroy", "--lease", LEASE, "--vmid", "101",
                "--confirm",
            )
        self.assertIn("lease-register", str(caught.exception))
        self.assertEqual(self.argvs(), [])

    def test_destroy_refuses_retain_rows_naming_the_vouching(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101, policy="retain")
        with self.assertRaises(errors.LabError) as caught:
            self.run_cmd(
                "guest", "destroy", "--lease", LEASE, "--vmid", "101",
                "--confirm",
            )
        message = str(caught.exception)
        self.assertIn("registry-vouched", message)
        self.assertIn("retain", message)
        self.assertEqual(self.argvs(), [])

    def test_destroy_refuses_templates_naming_the_vouching(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101, policy="disposable")
        self.fake.add(r"^qm config 101", stdout=b"template: 1\n")
        with self.assertRaises(errors.LabError) as caught:
            self.run_cmd(
                "guest", "destroy", "--lease", LEASE, "--vmid", "101",
                "--confirm",
            )
        message = str(caught.exception)
        self.assertIn("registry-vouched", message)
        self.assertIn("template: 1", message)
        self.assertEqual(
            [argv for argv in self.argvs() if argv[1] == "destroy"], []
        )

    def test_destroy_refuses_guests_without_the_pxl_tag(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101, policy="disposable")
        self.fake.add(r"^qm config 101", stdout=b"tags: somebody-elses\n")
        with self.assertRaises(errors.LabError) as caught:
            self.run_cmd(
                "guest", "destroy", "--lease", LEASE, "--vmid", "101",
                "--confirm",
            )
        message = str(caught.exception)
        self.assertIn("pxl", message)
        self.assertIn("tag", message)
        self.assertEqual(
            [argv for argv in self.argvs() if argv[1] == "destroy"], []
        )

    def test_destroy_marks_and_audits(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101, name="alpha")
        self.fake.add(
            r"^qm config 101",
            stdout=b"tags: pxl;lease-abs-guest-lease\n"
                   b"description: pxl-lease=abs-guest-lease "
                   b"pxl-expiry=1800000000\n",
        )
        self.fake.add(r"^qm destroy 101")
        payload = self.run_cmd(
            "guest", "destroy", "--lease", LEASE, "--vmid", "101",
            "--confirm",
        )
        self.assertEqual(payload, {
            "lease_id": LEASE, "vmid": 101,
            "destroyed": True, "purged": True,
        })
        self.assertEqual(
            [argv for argv in self.argvs() if argv[1] == "destroy"],
            [["qm", "destroy", "101", "--purge", "1"]],
        )
        self.assertIsNotNone(self.resources()[0]["destroyed_at"])
        self.lab.audit.assert_called_once_with(
            "guest-destroy", lease=LEASE, vmid=101, kind="qemu", purged=True
        )

    def test_destroy_refuses_a_guest_another_live_lease_owns(self) -> None:
        # require_owned only asks whether THIS lease has a row. Dual
        # registration is reachable (an expired-but-active lease still
        # counts), so one lease must not destroy the other's machine.
        self.open_lease()
        self.open_lease(OTHER_LEASE)
        self.register_guest(LEASE, "qemu", 101, name="alpha")
        self.register_guest(OTHER_LEASE, "qemu", 101, name="alpha")
        with self.assertRaises(errors.LabError) as caught:
            self.run_cmd(
                "guest", "destroy", "--lease", LEASE, "--vmid", "101",
                "--confirm",
            )
        self.assertIn(OTHER_LEASE, str(caught.exception))
        self.assertNotIn(
            ["qm", "destroy", "101", "--purge", "1"], self.argvs()
        )
        self.assertEqual(
            [c for c in self.argvs() if c[0] in ("qm", "pct")], []
        )
    def test_destroy_stops_a_running_guest_before_destroying(self) -> None:
        # pct/qm destroy refuse a running guest; destroy must stop it first.
        self.open_lease()
        self.register_guest(LEASE, "lxc", 102, name="beta")
        self.fake.add(
            r"^pct config 102",
            stdout=b"tags: pxl;lease-abs-guest-lease\n"
                   b"description: pxl-lease=abs-guest-lease "
                   b"pxl-expiry=1800000000\n",
        )
        self.fake.add(r"^pct status 102", stdout=b"status: running\n", times=1)
        self.fake.add(r"^pct shutdown 102")
        self.fake.add(r"^pct status 102", stdout=b"status: stopped\n")
        self.fake.add(r"^pct destroy 102")
        payload = self.run_cmd(
            "guest", "destroy", "--lease", LEASE, "--vmid", "102",
            "--confirm",
        )
        self.assertTrue(payload["destroyed"])
        seq = [" ".join(c["argv"][:2]) for c in self.fake.calls]
        self.assertLess(seq.index("pct shutdown"), seq.index("pct destroy"))


class RunTests(GuestCase):
    def test_lxc_run_reports_the_real_exit_code(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "lxc", 102, name="beta")
        self.fake.add(r"^pct exec 102", returncode=3,
                      stdout=b"out\n", stderr=b"err\n")
        payload = self.run_cmd(
            "guest", "run", "--lease", LEASE, "--vmid", "102",
            "echo", "super-secret-token",
        )
        self.assertEqual(self.argvs()[0],
                         ["pct", "exec", "102", "--", "echo",
                          "super-secret-token"])
        self.assertEqual(payload["exit_code"], 3)
        self.assertEqual(payload["stdout"], "out\n")
        self.assertEqual(payload["stderr"], "err\n")
        self.assertIsInstance(payload["duration_ms"], int)
        self.assertEqual(
            {key for key in payload},
            {"lease_id", "vmid", "exit_code", "stdout", "stderr",
             "duration_ms"},
        )

    def test_run_audits_argv0_and_the_exit_code_only(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "lxc", 102, name="beta")
        self.fake.add(r"^pct exec 102", returncode=3)
        self.run_cmd(
            "guest", "run", "--lease", LEASE, "--vmid", "102",
            "echo", "super-secret-token",
        )
        self.lab.audit.assert_called_once_with(
            "guest-run", lease=LEASE, vmid=102, argv0="echo", exit_code=3
        )
        for call in self.lab.audit.call_args_list:
            self.assertNotIn("super-secret-token", repr(call.args))
            self.assertNotIn("super-secret-token", repr(call.kwargs))

    def test_qemu_run_uses_the_agent_channel(self) -> None:
        self.open_lease()
        self.register_guest(LEASE, "qemu", 101, name="alpha")
        self.fake.add(
            r"^qm guest exec 101",
            stdout=b'{"exitcode": 5, "out-data": "aGkK", "err-data": ""}',
        )
        payload = self.run_cmd(
            "guest", "run", "--lease", LEASE, "--vmid", "101", "/bin/true"
        )
        self.assertEqual(self.argvs()[0], [
            "qm", "guest", "exec", "101", "--synchronous", "--timeout", "300",
            "--", "/bin/true",
        ])
        self.assertEqual(payload["exit_code"], 5)
        self.assertEqual(payload["stdout"], "hi\n")
        self.assertEqual(payload["stderr"], "")

    def test_run_refuses_an_unregistered_guest_before_any_seam_call(self) -> None:
        self.open_lease()
        with self.assertRaises(errors.LabError) as caught:
            self.run_cmd(
                "guest", "run", "--lease", LEASE, "--vmid", "101", "true"
            )
        self.assertIn("lease-register", str(caught.exception))
        self.assertEqual(self.argvs(), [])


class ProbeTests(GuestCase):
    def test_probe_reports_an_agent_reachable_qemu_guest(self) -> None:
        self.fake.add(r"^qm status 101", stdout=b"status: running\n")
        self.fake.add(r"^qm guest ping 101")
        self.fake.add(
            r"^qm guest network-get-interfaces 101",
            stdout=b'[{"name": "eth0", "ip-addresses": '
                   b'[{"ip-address-type": "ipv4", '
                   b'"ip-address": "10.0.0.5"}]}]',
        )
        payload = self.run_cmd("guest", "probe", "--vmid", "101")
        self.assertEqual(payload, {
            "vmid": 101, "exists": True, "running": True, "kind": "qemu",
            "agent_ok": True, "ip": "10.0.0.5", "channel": "agent",
        })

    def test_probe_reports_the_pct_channel_for_lxc(self) -> None:
        self.fake.add(r"^pct status 102", stdout=b"status: stopped\n")
        payload = self.run_cmd("guest", "probe", "--vmid", "102")
        self.assertEqual(payload, {
            "vmid": 102, "exists": True, "running": False, "kind": "lxc",
            "agent_ok": False, "ip": None, "channel": "pct",
        })

    def test_probe_reports_a_guest_the_node_does_not_know(self) -> None:
        payload = self.run_cmd("guest", "probe", "--vmid", "4242")
        self.assertEqual(payload, {
            "vmid": 4242, "exists": False, "running": False, "kind": None,
            "agent_ok": False, "ip": None, "channel": None,
        })


class ListTests(GuestCase):
    def test_list_joins_the_registry_with_live_states(self) -> None:
        self.open_lease()
        self.open_lease(OTHER_LEASE)
        self.register_guest(LEASE, "qemu", 101, name="alpha")
        self.register_guest(OTHER_LEASE, "lxc", 102, name="beta")
        self.register_guest(OTHER_LEASE, "qemu", 103, name="gone")
        with self.store() as store:
            store.mark_destroyed(OTHER_LEASE, "qemu", 103)
        self.fake.add(r"^qm status 101", stdout=b"status: running\n")
        self.fake.add(r"^pct status 102", stdout=b"status: stopped\n")
        self.fake.add(
            r"^qm config 101",
            stdout=b"tags: pxl;lease-abs-guest-lease\n"
                   b"description: pxl-lease=abs-guest-lease "
                   b"pxl-expiry=1800000000\n",
        )
        self.fake.add(
            r"^pct config 102",
            stdout=b"tags: pxl;lease-abs-other-lease\n"
                   b"description: pxl-lease=abs-other-lease pxl-expiry=0\n",
        )
        payload = self.run_cmd("guest", "list")
        self.assertEqual(payload, {"guests": [
            {"vmid": 101, "kind": "qemu", "name": "alpha",
             "lease_id": LEASE, "state": "running",
             "tags": "pxl;lease-abs-guest-lease", "pxl_expiry": EXPIRY},
            {"vmid": 102, "kind": "lxc", "name": "beta",
             "lease_id": OTHER_LEASE, "state": "stopped",
             "tags": "pxl;lease-abs-other-lease", "pxl_expiry": 0},
        ]})
        narrowed = self.run_cmd("guest", "list", "--lease", OTHER_LEASE)
        self.assertEqual([guest["vmid"] for guest in narrowed["guests"]],
                         [102])


if __name__ == "__main__":
    unittest.main()
