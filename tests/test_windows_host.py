"""Running the controller itself on Windows.

Not the Windows *guest* support in test_windows.py -- this is the controller
process running on a Windows machine, which has no `fcntl` and no `os.uname`.
Both are import-time or first-call failures, so without these the CLI does not
start at all there.

Windows is simulated rather than skipped: hiding `fcntl` from the import
system and deleting `os.uname` reproduces exactly the two AttributeError /
ModuleNotFoundError paths that break, and does so on the machine running the
suite.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


class _NoFcntl:
    """Import hook that makes `import fcntl` fail, as it does on Windows."""

    def find_module(self, name, path=None):  # pragma: no cover - legacy API
        return None

    def find_spec(self, name, path=None, target=None):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return None


class _FakeMsvcrt:
    """Just enough of msvcrt to model Windows byte-range locking: a held
    region blocks every handle, including its own holder asking again. Keyed
    by file identity so two handles on one path contend like two processes."""

    LK_LOCK = 1
    LK_NBLCK = 2

    def __init__(self) -> None:
        self.held: set[tuple[int, int]] = set()

    def _key(self, fd: int) -> tuple[int, int]:
        stat = os.fstat(fd)
        return (stat.st_dev, stat.st_ino)

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        key = self._key(fd)
        if key in self.held:
            raise PermissionError(13, "Permission denied")
        self.held.add(key)


class WindowsImportTests(unittest.TestCase):
    def test_the_cli_imports_without_fcntl(self) -> None:
        """On Windows `import fcntl` raises; a bare top-level import kills it."""
        hook = _NoFcntl()
        sys.meta_path.insert(0, hook)
        saved = {k: v for k, v in sys.modules.items()
                 if k.startswith("proxmox_agent_lab")}
        try:
            for key in list(sys.modules):
                if key.startswith("proxmox_agent_lab"):
                    del sys.modules[key]
            sys.modules.pop("fcntl", None)
            module = importlib.import_module("proxmox_agent_lab.cli")
            self.assertIsNone(module.fcntl, "fcntl should be None on Windows")
        finally:
            sys.meta_path.remove(hook)
            for key in list(sys.modules):
                if key.startswith("proxmox_agent_lab"):
                    del sys.modules[key]
            sys.modules.update(saved)

    def test_the_controller_lock_still_works_without_fcntl(self) -> None:
        """The lock stops two controllers on one machine interleaving. Without
        flock it must degrade, not raise."""
        from proxmox_agent_lab import cli as lab

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.lock"
            with mock.patch.object(lab, "fcntl", None):
                with path.open("a+") as handle:
                    lab._lock_file(handle)          # must not raise
                    # flock re-locks its own open description freely; msvcrt
                    # reports the region genuinely held. Both answers are
                    # graceful degradation -- raising is the only failure.
                    self.assertIsInstance(lab._try_lock_file(handle), bool)

    def test_two_handles_contend_for_one_region(self) -> None:
        """The Windows rule the flock simulation hides: a held byte region
        blocks a second handle too, so a non-blocking attempt answers False
        rather than pretending the lock was taken. Modelled with a fake
        msvcrt so the contention semantics are pinned on any platform."""
        from proxmox_agent_lab import cli as lab

        fake = _FakeMsvcrt()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.lock"
            with mock.patch.object(lab, "fcntl", None), \
                 mock.patch.dict(sys.modules, {"msvcrt": fake}):
                with path.open("a+") as first:
                    lab._lock_file(first)           # must not raise
                    with path.open("a+") as second:
                        self.assertFalse(lab._try_lock_file(second))
                    # The first handle's region is untouched by the refused
                    # second attempt.
                    self.assertIn(fake._key(first.fileno()), fake.held)


class WindowsSecretsTests(unittest.TestCase):
    def test_legacy_keystore_does_not_need_os_uname(self) -> None:
        """os.uname is POSIX-only; touching it on Windows is AttributeError."""
        from proxmox_agent_lab import secrets_store

        real_uname = getattr(os, "uname", None)
        try:
            if hasattr(os, "uname"):
                del os.uname  # type: ignore[attr-defined]
            with mock.patch.object(secrets_store.shutil, "which",
                                   return_value=None):
                self.assertEqual(secrets_store.legacy_keystore(), "")
            with mock.patch.object(secrets_store, "_is_darwin",
                                   return_value=False), \
                 mock.patch.object(secrets_store, "_is_windows",
                                   return_value=True), \
                 mock.patch.object(secrets_store.shutil, "which",
                                   side_effect=lambda n: n == "cmdkey"):
                self.assertEqual(secrets_store.legacy_keystore(), "wincred")
        finally:
            if real_uname is not None:
                os.uname = real_uname  # type: ignore[attr-defined]

    def test_the_default_backend_on_windows_is_the_environment(self) -> None:
        from proxmox_agent_lab import secrets_store

        self.assertEqual(secrets_store.detect_backend(), "env")

    def test_a_secrets_file_is_readable_without_posix_mode_bits(self) -> None:
        """The 0o077 check is meaningless on NTFS and refused every read."""
        from proxmox_agent_lab import config as config_module
        from proxmox_agent_lab import secrets_store

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.toml"
            path.write_text('proxmox-token = "abc123"\n')
            path.chmod(0o644)  # world-readable: rejected on POSIX
            cfg = config_module.Config(
                {"secrets": {"backend": "file", "file_path": str(path)}},
                None, Path("/nonexistent"),
            )
            # Patch the narrow platform helper, not os.name: setting os.name
            # globally makes pathlib switch to Windows path semantics
            # mid-process and the temp file stops resolving. Both branches
            # are patched explicitly, so the test runs green on either real
            # platform instead of assuming the suite runs on POSIX.
            with mock.patch.object(secrets_store, "_is_windows",
                                   return_value=False):
                with self.assertRaises(secrets_store.SecretError):
                    secrets_store.get(cfg, "proxmox-token")
            with mock.patch.object(secrets_store, "_is_windows",
                                   return_value=True):
                self.assertEqual(
                    secrets_store.get(cfg, "proxmox-token"), "abc123"
                )


if __name__ == "__main__":
    unittest.main()
