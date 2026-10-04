"""init/doctor/status/journal over the new ssh + SQLite stack (§G).

Covers: ``init`` writes exactly the §G starter schema and discovers the
wired NIC's MAC over the ssh seam (tolerating a dark host); ``doctor`` runs
the §G checklist with FakeSSH -- healthy install, missing config, refused
ssh, warn-only checks -- and fails iff a real check fails; ``status`` prints
the lease ledger even while the host is off.

Everything runs against a ``TemporaryDirectory`` state root and a
``FakeSSH`` seam -- no test spawns a real ssh or opens real controller
state.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
from pathlib import Path
import sys  # noqa: E402
import tempfile
import tomllib
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import bootstrap  # noqa: E402,F401

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import config as config_module  # noqa: E402
from proxmox_agent_lab import diagnostics  # noqa: E402
from proxmox_agent_lab import store as store_module  # noqa: E402
from proxmox_agent_lab.errors import LabError  # noqa: E402
from support.fakessh import FakeSSH  # noqa: E402

LINK_SHOW = (
    b"lo               UNKNOWN        00:00:00:00:00:00 <LOOPBACK,UP,LOWER_UP>\n"
    b"eno1             UP             aa:bb:cc:dd:ee:ff <BROADCAST,MULTICAST,UP,LOWER_UP>\n"
    b"wlan0            DOWN           aa:bb:cc:dd:ee:02 <BROADCAST,MULTICAST>\n"
    b"vmbr0            UP             aa:bb:cc:dd:ee:03 <BROADCAST,MULTICAST,UP,LOWER_UP>\n"
)

CHECK_NAMES = [
    "python_version",
    "config",
    "ssh_target",
    "ssh_connect",
    "remote_tooling",
    "node_identity",
    "state_dir",
    "template_vmid",
    "wol_mac",
    "gc_cron",
    "drift",
]

REMOTE_CHECKS = {
    "remote_tooling", "node_identity", "template_vmid", "gc_cron", "drift",
}


def fake_config(
    *,
    target: str = "fixture-host",
    node: str = "pve",
    template_vmid: int = 9025,
    mac: str = "aa:bb:cc:dd:ee:ff",
    source: str | None = "/fixture/config.toml",
    intended: str = "/fixture/config.toml",
) -> types.SimpleNamespace:
    """The §G config surface diagnostics reads, and nothing else."""
    return types.SimpleNamespace(
        ssh=types.SimpleNamespace(target=target),
        pve=types.SimpleNamespace(node=node, template_vmid=template_vmid),
        power=types.SimpleNamespace(
            mac=mac, broadcast="255.255.255.255", port=9
        ),
        state=types.SimpleNamespace(dir="/fixture/state"),
        lease=types.SimpleNamespace(
            ttl_seconds=7200, idle_shutdown_seconds=28800
        ),
        configured=source is not None,
        source=Path(source) if source else None,
        intended=Path(intended),
        unknown_sections=[],
    )


class DiagnosticsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name) / "state"
        self.ssh = FakeSSH()
        self.lab = self.make_lab()

    def make_lab(self, config=None, config_error=None):
        lab = types.SimpleNamespace(
            STATE_ROOT=self.state_root,
            CONFIG=config if config is not None else fake_config(),
            CONFIG_ERROR=config_error,
            ssh=self.ssh,
        )
        setattr(lab, "__version__", "0.0.0-test")
        lab.mcp_idle_elapsed = lambda: 0.0
        return lab

    def run_json(self, fn, **arg_values):
        """Run a cmd_* handler; return (parsed stdout, LabError|None)."""
        buffer = io.StringIO()
        error = None
        with contextlib.redirect_stdout(buffer), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                fn(self.lab, argparse.Namespace(**arg_values))
            except LabError as exc:
                error = exc
        text = buffer.getvalue()
        return (json.loads(text) if text.strip() else None), error

    def script_healthy_host(
        self, *, hostname: bytes = b"pve\n",         qm_list: bytes = b"",
        pct_list: bytes = b"",
        template_config: bytes = b"template: 1\nname: tpl\n",
    ) -> None:
        """Everything §G asks the host for, answering healthy.

        Rule parameters exist because the fake answers the first matching
        rule: a test that wants a different ``hostname``/``qm list`` must
        script it here, not add a shadowed rule afterwards.
        """
        self.ssh.add(r"^true$")
        self.ssh.add(r"^hostname -s$", stdout=hostname)
        self.ssh.add(r"^qm list$", stdout=qm_list)
        self.ssh.add(r"^pct list$", stdout=pct_list)
        self.ssh.add(
            r"^pvesh get /version --output-format json$",
            stdout=b'{"version": "9.0"}',
        )
        self.ssh.add(r"^pveversion$", stdout=b"pve-manager/9.0.0\n")
        self.ssh.add(
            r"^qm config 9025$",
            stdout=template_config,
        )
        # No crontab/script/log rules: gc.status reads them as absent.

    def check(self, report: dict, name: str) -> dict:
        return next(c for c in report["checks"] if c["name"] == name)


class InitTests(DiagnosticsTestCase):
    def test_init_writes_the_nine_key_starter_schema(self) -> None:
        path = Path(self.tmp.name) / "config.toml"
        with mock.patch.object(
            diagnostics, "_make_ssh", return_value=self.ssh
        ) as make_ssh:
            out, err = self.run_json(
                diagnostics.cmd_init, path=str(path), force=False
            )
        self.assertIsNone(err)
        # The placeholder target is what init probes for MAC discovery.
        make_ssh.assert_called_once_with("proxmox")
        parsed = tomllib.loads(path.read_text())
        sections = {
            name: sorted(parsed[name])
            for name in ("ssh", "pve", "power", "state", "lease")
        }
        self.assertEqual(
            sections,
            {
                "ssh": ["target"],
                "pve": ["node", "template_vmid"],
                "power": ["broadcast", "mac", "port"],
                "state": ["dir"],
                "lease": ["idle_shutdown_seconds", "ttl_seconds"],
            },
        )
        self.assertEqual(parsed["power"]["mac"], "")
        self.assertIsNone(out["mac_discovered"])
        self.assertIn("warning", out)
        self.assertEqual(out["config"], str(path))

    def test_init_default_path_uses_config_intended(self) -> None:
        intended = Path(self.tmp.name) / "deep" / "config.toml"
        self.lab = self.make_lab(config=fake_config(intended=str(intended)))
        with mock.patch.object(
            diagnostics, "_make_ssh", return_value=self.ssh
        ):
            out, err = self.run_json(diagnostics.cmd_init, path=None, force=False)
        self.assertIsNone(err)
        self.assertTrue(intended.is_file())
        self.assertEqual(out["config"], str(intended))

    def test_init_refuses_overwrite_without_force(self) -> None:
        path = Path(self.tmp.name) / "config.toml"
        path.write_text("[ssh]\ntarget = 'lab-host'\n")
        with mock.patch.object(
            diagnostics, "_make_ssh", return_value=self.ssh
        ):
            out, err = self.run_json(
                diagnostics.cmd_init, path=str(path), force=False
            )
        self.assertIsNotNone(err)
        self.assertIn("--force", str(err))
        # The file is untouched and no probe ran.
        self.assertEqual(path.read_text(), "[ssh]\ntarget = 'lab-host'\n")
        self.assertEqual(self.ssh.calls, [])

    def test_init_force_discovers_mac_from_the_replaced_config(self) -> None:
        path = Path(self.tmp.name) / "config.toml"
        path.write_text('[ssh]\ntarget = "lab-host"\n')
        self.ssh.add(r"^true$")
        self.ssh.add(r"^ip -br link show$", stdout=LINK_SHOW)
        with mock.patch.object(
            diagnostics, "_make_ssh", return_value=self.ssh
        ) as make_ssh:
            out, err = self.run_json(
                diagnostics.cmd_init, path=str(path), force=True
            )
        self.assertIsNone(err)
        # The host being overwritten -- not the starter's placeholder -- is
        # what gets probed.
        make_ssh.assert_called_once_with("lab-host")
        self.assertEqual(out["mac_discovered"], "aa:bb:cc:dd:ee:ff")
        self.assertNotIn("warning", out)
        parsed = tomllib.loads(path.read_text())
        self.assertEqual(parsed["power"]["mac"], "aa:bb:cc:dd:ee:ff")

    def test_init_tolerates_a_dark_host(self) -> None:
        path = Path(self.tmp.name) / "config.toml"
        path.write_text('[ssh]\ntarget = "lab-host"\n')
        # No "true" rule: the probe fails, init still writes a usable file.
        with mock.patch.object(
            diagnostics, "_make_ssh", return_value=self.ssh
        ):
            out, err = self.run_json(
                diagnostics.cmd_init, path=str(path), force=True
            )
        self.assertIsNone(err)
        self.assertIsNone(out["mac_discovered"])
        self.assertIn("[power] mac", out["warning"])
        self.assertEqual(
            tomllib.loads(path.read_text())["power"]["mac"], ""
        )


class WiredMacTests(DiagnosticsTestCase):
    def test_prefers_the_up_wired_nic_over_virtual_devices(self) -> None:
        self.assertEqual(
            diagnostics._wired_mac(LINK_SHOW.decode()),
            "aa:bb:cc:dd:ee:ff",
        )

    def test_falls_back_to_a_non_wired_up_interface(self) -> None:
        text = (
            "ib0              DOWN           aa:bb:cc:dd:ee:04\n"
            "ib1              UP             aa:bb:cc:dd:ee:05\n"
        )
        self.assertEqual(diagnostics._wired_mac(text), "aa:bb:cc:dd:ee:05")

    def test_no_candidate_means_none(self) -> None:
        self.assertIsNone(
            diagnostics._wired_mac(
                "lo               UNKNOWN        00:00:00:00:00:00 <LOOPBACK>\n"
            )
        )


class DoctorTests(DiagnosticsTestCase):
    def test_healthy_install_reports_all_checks_ok(self) -> None:
        self.script_healthy_host()
        report, err = self.run_json(
            diagnostics.cmd_doctor, host_checks=False
        )
        self.assertIsNone(err)
        self.assertTrue(report["ok"])
        self.assertEqual(
            [c["name"] for c in report["checks"]], CHECK_NAMES
        )
        self.assertTrue(all(c["ok"] for c in report["checks"]))
        self.assertEqual(report["problems"], [])
        self.assertIn(
            "is a template", self.check(report, "template_vmid")["detail"]
        )

    def test_a_non_template_vmid_warns_without_failing(self) -> None:
        self.script_healthy_host(template_config=b"name: real-vm\nmemory: 8192\n")
        report, err = self.run_json(
            diagnostics.cmd_doctor, host_checks=False
        )
        self.assertIsNone(err)
        self.assertTrue(report["ok"])
        detail = self.check(report, "template_vmid")["detail"]
        self.assertIn("warning", detail)
        self.assertIn("not a template", detail)

    def test_missing_config_fails_and_remote_checks_are_skipped(self) -> None:
        missing = Path(self.tmp.name) / "nope.toml"
        self.lab = self.make_lab(config=config_module.load(missing))
        report, err = self.run_json(
            diagnostics.cmd_doctor, host_checks=False
        )
        self.assertIsNotNone(err)
        self.assertFalse(report["ok"])
        # A config that does not exist yet yields defaults: no [ssh] target.
        self.assertEqual(
            report["problems"],
            [
                "config: no config file at "
                f"{missing}; run 'proxmox-lab init'",
                "ssh_target: [ssh] target is not set",
            ],
        )
        skipped = {
            c["name"] for c in report["checks"] if c.get("skipped")
        }
        self.assertEqual(skipped, REMOTE_CHECKS | {"ssh_connect"})

    def test_unparseable_config_is_reported_as_a_config_failure(self) -> None:
        self.lab = self.make_lab(config_error="synthetic parse failure")
        self.script_healthy_host()
        report, err = self.run_json(
            diagnostics.cmd_doctor, host_checks=False
        )
        self.assertIsNotNone(err)
        self.assertEqual(
            self.check(report, "config")["detail"],
            "could not be read: synthetic parse failure",
        )

    def test_unreachable_host_fails_ssh_but_skips_remote_checks(self) -> None:
        # No rules at all: even the "true" probe is refused by the fake.
        report, err = self.run_json(
            diagnostics.cmd_doctor, host_checks=False
        )
        self.assertIsNotNone(err)
        self.assertEqual(
            report["problems"],
            [
                "ssh_connect: ssh to fixture-host failed or timed out; "
                "run 'ssh-copy-id root@fixture-host'"
            ],
        )
        skipped = {
            c["name"] for c in report["checks"] if c.get("skipped")
        }
        self.assertEqual(skipped, REMOTE_CHECKS)

    def test_node_mismatch_is_a_warning_not_a_failure(self) -> None:
        self.script_healthy_host(hostname=b"othernode\n")
        report, err = self.run_json(
            diagnostics.cmd_doctor, host_checks=False
        )
        self.assertIsNone(err)
        self.assertTrue(report["ok"])
        node = self.check(report, "node_identity")
        self.assertTrue(node["ok"])
        self.assertIn("othernode", node["detail"])

    def test_absent_template_and_mac_warn_without_failing(self) -> None:
        self.script_healthy_host()
        self.lab = self.make_lab(
            config=fake_config(template_vmid=9000, mac="")
        )
        report, err = self.run_json(
            diagnostics.cmd_doctor, host_checks=False
        )
        self.assertIsNone(err)
        self.assertTrue(report["ok"])
        self.assertIn("warning", self.check(report, "template_vmid")["detail"])
        self.assertIn("warning", self.check(report, "wol_mac")["detail"])

    def test_installed_gc_cron_reports_installed(self) -> None:
        self.script_healthy_host()
        payload = base64.b64encode(b"gc-script")
        self.ssh.add(r"^base64 /usr/local/sbin/pxl-gc$", stdout=payload)
        self.ssh.add(
            r"^crontab -l$",
            stdout=(
                b"# pxl-gc\n*/10 * * * * /usr/local/sbin/pxl-gc "
                b">>/var/log/pxl-gc.log 2>&1\n"
            ),
        )
        report, err = self.run_json(
            diagnostics.cmd_doctor, host_checks=False
        )
        self.assertIsNone(err)
        gc_check = self.check(report, "gc_cron")
        self.assertTrue(gc_check["ok"])
        self.assertIn("installed", gc_check["detail"])

    def test_drift_warns_for_a_pxl_guest_unknown_to_the_store(self) -> None:
        self.script_healthy_host(qm_list=b"     1001 guest1 running\n")
        self.state_root.mkdir(parents=True)
        self.ssh.add(
            r"^qm config 1001$",
            stdout=(
                b"tags: pxl;lease-ghost-0001\n"
                b"description: pxl-lease=ghost-0001 pxl-expiry=0\n"
            ),
        )
        report, err = self.run_json(
            diagnostics.cmd_doctor, host_checks=False
        )
        self.assertIsNone(err)
        drift = self.check(report, "drift")
        self.assertTrue(drift["ok"])
        self.assertIn("ghost-0001", drift["detail"])

    def test_store_backed_pxl_guest_is_not_drift(self) -> None:
        self.script_healthy_host(qm_list=b"     1001 guest1 running\n")
        self.state_root.mkdir(parents=True)
        with store_module.Store(self.state_root / "lab.db") as store:
            store.create_lease("lease-0001", expires_at=1234)
            store.register_resource("lease-0001", "qemu", 1001)
        self.ssh.add(
            r"^qm config 1001$",
            stdout=(
                b"tags: pxl;lease-lease-0001\n"
                b"description: pxl-lease=lease-0001 pxl-expiry=1234\n"
            ),
        )
        report, err = self.run_json(
            diagnostics.cmd_doctor, host_checks=False
        )
        self.assertIsNone(err)
        self.assertEqual(
            self.check(report, "drift")["detail"],
            "no pxl-tagged guests disagree with lab.db",
        )


class StatusTests(DiagnosticsTestCase):
    def test_status_reports_leases_while_the_host_is_off(self) -> None:
        self.state_root.mkdir(parents=True)
        with store_module.Store(self.state_root / "lab.db") as store:
            store.create_lease(
                "lease-0001", kind="long_term", purpose="demo", expires_at=0
            )
        # No "true" rule: the host does not answer.
        out, err = self.run_json(diagnostics.cmd_status)
        self.assertIsNone(err)
        self.assertFalse(out["reachable"])
        self.assertIn("power wake", out["note"])
        self.assertEqual(
            out["leases"],
            [
                {
                    "id": "lease-0001",
                    "kind": "long_term",
                    "purpose": "demo",
                    "state": "active",
                    "expires_at": 0,
                    "guests": 0,
                }
            ],
        )
        self.assertNotIn("guests", out)

    def test_status_online_reports_version_uptime_and_guests(self) -> None:
        self.state_root.mkdir(parents=True)
        self.ssh.add(r"^true$")
        self.ssh.add(r"^pveversion$", stdout=b"pve-manager/9.0.0\n")
        self.ssh.add(
            r"^pvesh get /nodes/pve/status --output-format json$",
            stdout=b'{"uptime": 42}',
        )
        self.ssh.add(r"^qm list$", stdout=b"     1001 guest1 running\n")
        self.ssh.add(r"^pct list$", stdout=b"VMID       Status     Name\n")
        out, err = self.run_json(diagnostics.cmd_status)
        self.assertIsNone(err)
        self.assertTrue(out["reachable"])
        self.assertEqual(out["pveversion"], "pve-manager/9.0.0")
        self.assertEqual(out["uptime_seconds"], 42)
        self.assertEqual(
            out["guests"], {"qemu": [1001], "lxc": []}
        )
        self.assertEqual(out["leases"], [])


class JournalTests(DiagnosticsTestCase):
    def test_journal_prints_events_from_the_local_store(self) -> None:
        # journal_module reads config_module.state_dir() -- the bootstrap's
        # per-process PROXMOX_AGENT_LAB_STATE directory shared with every
        # other test in the run, so clear the events table first.
        with store_module.Store(
            config_module.state_dir() / "lab.db"
        ) as store:
            store._conn.execute("DELETE FROM events")
            store.record("test-event", lease="lease-0001", vmid=1001)
        rows, err = self.run_json(
            diagnostics.cmd_journal, lease=None, since=None, limit=50
        )
        self.assertIsNone(err)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "test-event")
        self.assertEqual(rows[0]["lease"], "lease-0001")

    def test_journal_rejects_ledger_era_flags(self) -> None:
        out, err = self.run_json(
            diagnostics.cmd_journal,
            lease=None, since=None, limit=50, summary=True,
        )
        self.assertIsNone(out)
        self.assertIsNotNone(err)
        self.assertIn("--summary", str(err))


if __name__ == "__main__":
    unittest.main()
