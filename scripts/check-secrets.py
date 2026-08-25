#!/usr/bin/env python3
"""Fail when candidate repository files contain common secret material."""

from __future__ import annotations

import re
import sys
from pathlib import Path


PATTERNS = (
    re.compile(r"-----BEGIN (?:OPENSSH|RSA|EC|DSA) PRIVATE KEY-----"),
    re.compile(r"PVEAPIToken\s*=\s*[A-Za-z0-9._~!@#$%^&*+-]{12,}"),
    re.compile(
        r"(?i)(?:password|token|secret)\s*[:=]\s*"
        r"['\"][A-Za-z0-9_./+!$*-]{12,}['\"]"
    ),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~-]{12,}"),
    # Object-storage credentials. Bare 64-hex strings are deliberately not
    # matched: the image library legitimately records SHA-256 checksums.
    re.compile(r"\bGK[0-9a-f]{24}\b"),
    # WireGuard keys: 32 bytes of base64. Checksums in the image library are
    # hex, so they cannot collide with this shape.
    re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{43}=(?![A-Za-z0-9+/=])"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)X-Amz-(?:Signature|Credential)=[A-Za-z0-9%/_-]{16,}"),
    # A bare (unquoted) assignment of something long. The value must contain a
    # digit and must not be a call or a dotted attribute reference, so
    # `token = CONFIG.proxmox.token_user` is not mistaken for a credential.
    re.compile(
        r"(?i)(?:secret|password|token)[a-z_]*\s*[:=]\s*"
        r"(?![A-Za-z0-9_.]*[(.])"
        r"(?=[A-Za-z0-9/+=_-]*[0-9])[A-Za-z0-9/+=_-]{20,}"
    ),
)
# Public, non-secret constants that the patterns above cannot distinguish from
# credentials. Keep this list short and justified.
ALLOWED = (
    # Microsoft's public assembly key token, present in every unattend.xml.
    "31bf3856ad364e35",
    # The *name* of the audit ledger secret, not its value. It appears in the
    # config template, in KNOWN_SECRETS and in docs; the patterns cannot tell a
    # secret's name from a secret.
    "mariadb-password",
)
SKIP_DIRS = {".git", ".venv", "__pycache__"}
SKIP_NAMES = {"check-secrets.py"}


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    failures: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_NAMES:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for allowed in ALLOWED:
                line = line.replace(allowed, "[PUBLIC-CONSTANT]")
            if any(pattern.search(line) for pattern in PATTERNS):
                failures.append(f"{path.relative_to(root)}:{number}")
    if failures:
        print("Potential secrets found:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print("Secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
