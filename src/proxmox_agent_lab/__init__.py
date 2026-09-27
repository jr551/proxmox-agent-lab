"""Lease-managed, fail-closed control of a Proxmox home lab.

Give an AI agent disposable Proxmox guests that clean up after themselves. A
lease bounds the work; when it ends, the guests it created are destroyed and
the host is switched off if — and only if — it is genuinely idle.
"""

__version__ = "0.18.0"
