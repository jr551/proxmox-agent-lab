# Responsible use

proxmox-agent-lab exists to make legitimate systems research safer and more
repeatable. Intended uses include authorized reverse engineering, defensive
malware analysis, incident response, digital forensics, vulnerability
reproduction, interoperability, driver and firmware development, debugging,
and education.

It drives real hypervisors and real guests as root over ssh. It is built for
hosts you own, and the safest deployment is a host dedicated to the lab; a
shared production host works, because lab guests are labelled and cleanup
refuses anything unlabelled, but you are one bug away from someone's
workload.

Use it only with systems, software, devices, accounts, and network traffic that
you own or are explicitly authorized to test. Follow applicable law, licenses,
organizational policy, and coordinated-disclosure expectations.

Guest memory inspection, USB passthrough, traffic capture and TLS
interception were part of earlier versions and are **not** in this release.
The current surface is guest lifecycle, console access and file transfer —
keep work inside disposable, lease-owned guests, and retain the journal.

The project intentionally enforces leases, resource ownership, explicit
host-change gates, opt-in host access, redacted auditing, and verified cleanup.
These controls reduce mistakes; they do not replace authorization or sound
research judgment. See [docs/safety-policy.md](docs/safety-policy.md) for the
full lease, shutdown, and isolation invariants.

This document states project intent and does not add restrictions to the MIT
license.
