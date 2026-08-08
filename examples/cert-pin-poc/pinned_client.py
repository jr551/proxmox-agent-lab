#!/usr/bin/env python3
"""Cert-pinning client for the memflow RAM-injection PoC (authorized lab use).

Runs inside a disposable lab guest. It enforces TLS trust against the system
store (its "pin"), so a man-in-the-middle proxy's substitute certificate is
rejected -- until the enforce flag is flipped in the guest's live RAM from the
hypervisor with `memflow scan` + `memflow phys-write`.

The enforce flag lives one byte after a unique 16-byte marker, in a single
bytearray. It is built in place (not by concatenation) so exactly one live copy
of the marker carries the flag -- concatenation would leave transient copies
that share the signature and waste your time.

    export PXL_PROXY=http://<mitm_proxy_ip>:8080
    python3 pinned_client.py
"""
import os
import ssl
import time
import urllib.request

MARKER = b"PXLPIN0POC0MARK!"          # 16-byte needle for `memflow scan`
buf = bytearray(17)                    # single resident copy: marker + flag
buf[:16] = MARKER
buf[16] = 1                            # 1 = enforce the pin; RAM-flip to 0
_KEEP = buf                            # keep it referenced (resident)

PROXY = os.environ.get("PXL_PROXY", "http://127.0.0.1:8080")
URL = os.environ.get("PXL_URL", "https://example.com/")


def attempt() -> int:
    ctx = ssl.create_default_context()  # "pin": trust only the system store
    if buf[16] != 1:                    # flag cleared -> stop enforcing
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}),
        urllib.request.HTTPSHandler(context=ctx),
    )
    return len(opener.open(URL, timeout=10).read())


def main() -> None:
    while True:
        try:
            n = attempt()
            print("ALLOWED enforce=%d bytes=%d" % (buf[16], n), flush=True)
        except Exception as exc:                       # noqa: BLE001
            print("PINNED  enforce=%d blocked=%s" % (buf[16], type(exc).__name__),
                  flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
