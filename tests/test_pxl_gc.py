"""End-to-end tests for the standalone host-side GC script.

These drive the REAL `src/proxmox_agent_lab/resources/pxl-gc.py` as a
subprocess. Nothing from `proxmox_agent_lab` is imported: the script is
standalone by contract (it runs alone on the Proxmox host).

Stub contract (all three executables live in a temp dir prepended to PATH):

- every stub appends its full argv, one line each, to
  `$PXL_GC_STUB_DIR/calls.log` (`qm config 101`, `poweroff -h now`, ...);
- `qm` / `pct` serve one directory per guest, `$PXL_GC_STUB_DIR/<bin>/<vmid>/`,
  containing `status` (running|stopped), optional `config` (verbatim config
  text) and an optional `refuse-stop` flag that makes `shutdown` a no-op;
  `list` prints a header plus one row per guest, `config`/`status` answer from
  the files, `shutdown`/`stop` flip status to stopped (unless refused),
  `destroy` deletes the guest directory and fails for unknown guests;
- `poweroff` only logs.

The script's clock and paths are injected through PXL_GC_* environment
variables so every test is deterministic and writes only inside its temp dir.
"""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GC_SCRIPT = ROOT / "src" / "proxmox_agent_lab" / "resources" / "pxl-gc.py"

NOW = 1_700_000_000
EXPIRED = 1_690_000_000
FUTURE = NOW + 3_600

QM_STUB = """#!/bin/sh
# Fake qm: log argv, serve guests from $PXL_GC_STUB_DIR/qm/<vmid>/.
set -eu
base="${PXL_GC_STUB_DIR:?}"
d="$base/qm"
echo "qm $*" >> "$base/calls.log"
cmd="${1:-}"
shift || true
case "$cmd" in
  list)
    printf '%8s %-14s %-10s %9s %13s %3s\\n' VMID NAME STATUS MEM BOOTDISK PID
    for g in "$d"/*/; do
      [ -d "$g" ] || continue
      vmid=${g%/}
      vmid=${vmid##*/}
      printf '%8s %-14s %-10s %9s %13s %3s\\n' "$vmid" "vm-$vmid" "$(cat "$g/status")" 512 8.00 0
    done
    ;;
  config)
    vmid="${1:-}"
    if [ ! -f "$d/$vmid/config" ]; then
      echo "Configuration file does not exist" >&2
      exit 2
    fi
    cat "$d/$vmid/config"
    ;;
  status)
    vmid="${1:-}"
    if [ ! -d "$d/$vmid" ]; then echo "VM $vmid does not exist" >&2; exit 2; fi
    echo "status: $(cat "$d/$vmid/status")"
    ;;
  shutdown)
    vmid="${1:-}"
    if [ ! -d "$d/$vmid" ]; then echo "VM $vmid does not exist" >&2; exit 2; fi
    if [ ! -f "$d/$vmid/refuse-stop" ]; then printf 'stopped\\n' > "$d/$vmid/status"; fi
    ;;
  stop)
    vmid="${1:-}"
    if [ ! -d "$d/$vmid" ]; then echo "VM $vmid does not exist" >&2; exit 2; fi
    printf 'stopped\\n' > "$d/$vmid/status"
    ;;
  destroy)
    vmid="${1:-}"
    if [ ! -d "$d/$vmid" ]; then echo "VM $vmid does not exist" >&2; exit 2; fi
    rm -rf "$d/$vmid"
    ;;
  *)
    echo "fake qm: unsupported command $cmd" >&2
    exit 64
    ;;
esac
exit 0
"""

PCT_STUB = """#!/bin/sh
# Fake pct: same state model as the qm stub, pct command set.
set -eu
base="${PXL_GC_STUB_DIR:?}"
d="$base/pct"
echo "pct $*" >> "$base/calls.log"
cmd="${1:-}"
shift || true
case "$cmd" in
  list)
    printf '%-10s %-10s %-10s %s\\n' VMID STATUS LOCK NAME
    for g in "$d"/*/; do
      [ -d "$g" ] || continue
      vmid=${g%/}
      vmid=${vmid##*/}
      printf '%-10s %-10s %-10s %s\\n' "$vmid" "$(cat "$g/status")" "-" "ct-$vmid"
    done
    ;;
  config)
    vmid="${1:-}"
    if [ ! -f "$d/$vmid/config" ]; then
      echo "Configuration file does not exist" >&2
      exit 2
    fi
    cat "$d/$vmid/config"
    ;;
  status)
    vmid="${1:-}"
    if [ ! -d "$d/$vmid" ]; then echo "CT $vmid does not exist" >&2; exit 2; fi
    echo "status: $(cat "$d/$vmid/status")"
    ;;
  shutdown)
    vmid="${1:-}"
    if [ ! -d "$d/$vmid" ]; then echo "CT $vmid does not exist" >&2; exit 2; fi
    if [ ! -f "$d/$vmid/refuse-stop" ]; then printf 'stopped\\n' > "$d/$vmid/status"; fi
    ;;
  stop)
    vmid="${1:-}"
    if [ ! -d "$d/$vmid" ]; then echo "CT $vmid does not exist" >&2; exit 2; fi
    printf 'stopped\\n' > "$d/$vmid/status"
    ;;
  destroy)
    vmid="${1:-}"
    if [ ! -d "$d/$vmid" ]; then echo "CT $vmid does not exist" >&2; exit 2; fi
    rm -rf "$d/$vmid"
    ;;
  *)
    echo "fake pct: unsupported command $cmd" >&2
    exit 64
    ;;
esac
exit 0
"""

POWEROFF_STUB = """#!/bin/sh
set -eu
echo "poweroff $*" >> "${PXL_GC_STUB_DIR:?}/calls.log"
exit 0
"""


def pxl_config(expiry, lease="abc", tags=None, template=False):
    lines = []
    if template:
        lines.append("template: 1")
    lines.append("tags: " + (tags if tags is not None else "pxl;lease-" + lease))
    lines.append("description: pxl-lease=%s pxl-expiry=%d" % (lease, expiry))
    return "\n".join(lines) + "\n"


class PxLGcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.stub = self.root / "stub"
        self.state = self.root / "state"
        self.locks = self.root / "lock"
        self.bin.mkdir()
        self.stub.mkdir()
        self.write_stub("qm", QM_STUB)
        self.write_stub("pct", PCT_STUB)
        self.write_stub("poweroff", POWEROFF_STUB)

    # -- helpers ----------------------------------------------------------

    def write_stub(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def add_guest(self, kind, vmid, status="stopped", config=None, refuse=False):
        guest = self.stub / kind / str(vmid)
        guest.mkdir(parents=True, exist_ok=True)
        (guest / "status").write_text(status + "\n")
        if config is not None:
            (guest / "config").write_text(config)
        if refuse:
            (guest / "refuse-stop").touch()
        return guest

    def run_gc(self, *args, now=NOW, stop_timeout="120"):
        env = dict(os.environ)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        env["PXL_GC_STUB_DIR"] = str(self.stub)
        env["PXL_GC_STATE_DIR"] = str(self.state)
        env["PXL_GC_LOCK_DIR"] = str(self.locks)
        env["PXL_GC_POWEROFF"] = "poweroff -h now"
        env["PXL_GC_NOW"] = str(now)
        env["PXL_GC_STOP_TIMEOUT"] = str(stop_timeout)
        return subprocess.run(
            [sys.executable, str(GC_SCRIPT), *args],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def calls(self):
        path = self.stub / "calls.log"
        return path.read_text() if path.exists() else ""

    def stamp(self):
        return self.state / "clear-stamp"

    def assert_boring(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for line in proc.stdout.splitlines():
            self.assertTrue(line.startswith("pxl-gc: "), line)

    # -- destruction ------------------------------------------------------

    def test_expired_lease_guest_is_destroyed(self):
        guest = self.add_guest("qm", 101, status="running",
                               config=pxl_config(EXPIRED))
        proc = self.run_gc()
        self.assert_boring(proc)
        self.assertIn("101: graceful shutdown", proc.stdout)
        self.assertIn("101: destroy", proc.stdout)
        self.assertIn("101: destroyed", proc.stdout)
        self.assertFalse(guest.exists())
        calls = self.calls()
        self.assertIn("qm shutdown 101 --timeout 120", calls)
        self.assertIn("qm destroy 101 --purge 1", calls)
        self.assertNotIn("poweroff", calls)

    def test_future_expiry_guest_is_untouched(self):
        guest = self.add_guest("qm", 101, status="running",
                               config=pxl_config(FUTURE))
        proc = self.run_gc()
        self.assert_boring(proc)
        self.assertIn("skip 101: unexpired (until %d)" % FUTURE, proc.stdout)
        self.assertTrue(guest.exists())
        self.assertNotIn("shutdown", self.calls())
        self.assertNotIn("destroy", self.calls())

    def test_long_term_guest_is_untouched_and_pins_the_host(self):
        guest = self.add_guest("pct", 101, status="stopped",
                               config=pxl_config(0))
        self.state.mkdir(exist_ok=True)
        self.stamp().write_text(str(NOW - 700) + "\n")
        proc = self.run_gc()
        self.assert_boring(proc)
        self.assertIn("skip 101: long-term (pxl-expiry=0)", proc.stdout)
        self.assertIn("not clear: running=0 pinned=1", proc.stdout)
        self.assertTrue(guest.exists())
        self.assertFalse(self.stamp().exists())
        self.assertNotIn("destroy", self.calls())
        self.assertNotIn("poweroff", self.calls())

    def test_non_pxl_guest_is_untouched(self):
        guest = self.add_guest(
            "qm", 101, status="running",
            config="tags: web;prod\ndescription: my web server\n")
        proc = self.run_gc()
        self.assert_boring(proc)
        self.assertIn("skip 101: not pxl", proc.stdout)
        self.assertTrue(guest.exists())
        self.assertNotIn("shutdown", self.calls())
        self.assertNotIn("destroy", self.calls())

    def test_garbage_expiry_and_missing_metadata_skip_not_delete(self):
        bad_epoch = self.add_guest(
            "qm", 101, status="stopped",
            config="tags: pxl;lease-abc\n"
                   "description: pxl-lease=abc pxl-expiry=banana\n")
        no_desc = self.add_guest("qm", 102, status="stopped",
                                 config="tags: pxl;lease-abc\n")
        proc = self.run_gc()
        self.assert_boring(proc)
        self.assertIn(
            "warn 101: pxl-expiry=banana is not a number; not deleting",
            proc.stdout)
        self.assertIn(
            "warn 102: description has no pxl-lease/pxl-expiry line; "
            "not deleting",
            proc.stdout)
        self.assertTrue(bad_epoch.exists())
        self.assertTrue(no_desc.exists())
        self.assertNotIn("shutdown", self.calls())
        self.assertNotIn("destroy", self.calls())

    def test_unreadable_config_skips_not_deletes(self):
        guest = self.add_guest("qm", 101, status="running", config=None)
        proc = self.run_gc()
        self.assert_boring(proc)
        self.assertIn("warn 101: config unreadable; not deleting", proc.stdout)
        self.assertTrue(guest.exists())
        self.assertNotIn("destroy", self.calls())

    def test_template_is_refused(self):
        guest = self.add_guest("qm", 101, status="stopped",
                               config=pxl_config(EXPIRED, template=True))
        proc = self.run_gc()
        self.assert_boring(proc)
        self.assertIn("skip 101: template", proc.stdout)
        self.assertTrue(guest.exists())
        self.assertNotIn("shutdown", self.calls())
        self.assertNotIn("destroy", self.calls())

    def test_double_run_is_idempotent(self):
        self.add_guest("qm", 101, status="running", config=pxl_config(EXPIRED))
        first = self.run_gc()
        self.assert_boring(first)
        self.assertIn("101: destroyed", first.stdout)
        second = self.run_gc()
        self.assert_boring(second)
        self.assertNotIn("destroy", second.stdout)
        self.assertNotIn("shutdown", second.stdout)
        self.assertEqual(self.calls().count("qm destroy 101 --purge 1"), 1)

    def test_dry_run_touches_nothing(self):
        guest = self.add_guest("qm", 101, status="running",
                               config=pxl_config(EXPIRED))
        proc = self.run_gc("--dry-run")
        self.assert_boring(proc)
        self.assertIn(
            "would stop and destroy 101 (expired %d)" % EXPIRED, proc.stdout)
        self.assertTrue(guest.exists())
        calls = self.calls()
        self.assertIn("qm config 101", calls)
        self.assertNotIn("shutdown", calls)
        self.assertNotIn("destroy", calls)
        self.assertFalse(self.stamp().exists())

    def test_hard_stop_after_grace_period(self):
        guest = self.add_guest("qm", 101, status="running",
                               config=pxl_config(EXPIRED), refuse=True)
        proc = self.run_gc(stop_timeout="0")
        self.assert_boring(proc)
        self.assertIn("101: hard stop (grace period expired)", proc.stdout)
        self.assertIn("101: destroyed", proc.stdout)
        self.assertFalse(guest.exists())
        calls = self.calls()
        self.assertIn("qm stop 101", calls)
        self.assertIn("qm destroy 101 --purge 1", calls)

    # -- power-off-when-idle ---------------------------------------------

    def test_idle_poweroff_requires_two_clear_runs(self):
        first = self.run_gc(now=NOW)
        self.assert_boring(first)
        self.assertIn("clear, first observation stamped at", first.stdout)
        self.assertTrue(self.stamp().exists())
        self.assertNotIn("poweroff", self.calls())

        early = self.run_gc(now=NOW + 599)
        self.assert_boring(early)
        self.assertIn("599s < 600s", early.stdout)
        self.assertNotIn("poweroff", self.calls())

        firing = self.run_gc(now=NOW + 600)
        self.assert_boring(firing)
        self.assertIn(
            "powering off: 0 running guests, 0 unexpired pxl guests, "
            "clear since",
            firing.stdout)
        self.assertIn("poweroff -h now", self.calls())

    def test_running_guest_blocks_poweroff_and_clears_stamp(self):
        self.add_guest("qm", 101, status="running",
                       config="tags: web\ndescription: someone else\n")
        self.state.mkdir(exist_ok=True)
        self.stamp().write_text(str(NOW - 700) + "\n")
        proc = self.run_gc()
        self.assert_boring(proc)
        self.assertIn("not clear: running=1 pinned=0", proc.stdout)
        self.assertIn("clear stamp removed", proc.stdout)
        self.assertFalse(self.stamp().exists())
        self.assertNotIn("poweroff", self.calls())

    # -- robustness -------------------------------------------------------

    def test_missing_qm_is_survived(self):
        (self.bin / "qm").unlink()
        self.add_guest("pct", 201, status="stopped",
                       config=pxl_config(EXPIRED))
        proc = self.run_gc()
        self.assert_boring(proc)
        self.assertIn("qm not found on PATH; skipping qemu guests", proc.stdout)
        self.assertIn("201: destroyed", proc.stdout)
        self.assertFalse((self.stub / "pct" / "201").exists())

    def test_failed_qm_is_survived(self):
        self.write_stub("qm", "#!/bin/sh\n"
                              "echo \"qm $*\" >> \"${PXL_GC_STUB_DIR:?}/calls.log\"\n"
                              "echo boom >&2\n"
                              "exit 1\n")
        proc = self.run_gc()
        self.assert_boring(proc)
        self.assertIn("qm list failed (1): boom", proc.stdout)

    def test_unknown_flag_exits_2(self):
        proc = self.run_gc("--bogus")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("usage: pxl-gc [--dry-run]", proc.stderr)


if __name__ == "__main__":
    unittest.main()
