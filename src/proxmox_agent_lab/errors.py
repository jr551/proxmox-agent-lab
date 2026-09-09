"""Deliberate, user-facing errors for the lab controller.

``LabError`` is the package's operational-failure type: anything raised on
purpose -- an unreachable host, an expired lease, a refused mutation, a
missing secret -- is a message for the operator, not a programming error, and
``cli.main`` prints it without a traceback. Subsystems define their own
subclasses or sibling error types (``config.ConfigError``,
``secrets_store.SecretError``, ``power.PowerError``,
``mariadb.MariaDBError``) when callers need to distinguish them; the CLI's
expected-error list collects all of them so a routine failure never surfaces
as a crash.
"""

from __future__ import annotations


class LabError(RuntimeError):
    """An operational failure the user can act on."""
