# Responsible use

Old Computer AI Lab exists to make legitimate systems research safer and more
repeatable. Intended uses include authorized reverse engineering, defensive
malware analysis, incident response, digital forensics, vulnerability
reproduction, interoperability, driver and firmware development, debugging,
and education.

Use it only with systems, software, devices, accounts, and network traffic that
you own or are explicitly authorized to test. Follow applicable law, licenses,
organizational policy, and coordinated-disclosure expectations.

Some optional features can inspect or mutate live guest memory, pass physical
USB devices into guests, capture traffic, or intercept TLS in a guest you
control. Those capabilities have legitimate research value and carry real
risk. Keep them scoped to disposable guests, enable them deliberately, retain
the audit trail, and never install an interception CA outside a lab you control.

The project intentionally enforces leases, resource ownership, explicit
host-change gates, opt-in host access, redacted auditing, and verified cleanup.
These controls reduce mistakes; they do not replace authorization or sound
research judgment.

This document states project intent and does not add restrictions to the MIT
license.
