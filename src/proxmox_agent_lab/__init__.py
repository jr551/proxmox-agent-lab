"""Lease-managed, fail-closed control of a Proxmox home lab.

Give an AI agent disposable Proxmox guests that clean up after themselves. A
lease bounds the work; when it ends, the guests it created are destroyed.
The host stays up unless `[power] auto_shutdown` is true and nothing else
is running.
"""

__version__ = "0.22.0"
