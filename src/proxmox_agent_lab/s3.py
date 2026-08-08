"""S3 scratch space for moving files in and out of lab guests.

Credentials live in the macOS Keychain alongside the Proxmox token; only the
endpoint and bucket are recorded in this repository. Requests are signed with
AWS SigV4 using the standard library so the controller keeps running under the
system interpreter.

The presigned-URL path is what makes guest file transfer easy: the controller
signs a short-lived URL locally and the guest fetches or uploads it with the
`curl` or `Invoke-WebRequest` it already has, with no credential ever entering
the guest, the command line, or the audit ledger.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import subprocess
from typing import Any
from urllib import error, parse, request
from xml.etree import ElementTree

from . import config as _config
from . import secrets_store

_CONFIG = _config.get()
ENDPOINT = _CONFIG.s3.endpoint
BUCKET = _CONFIG.s3.bucket
REGION = _CONFIG.s3.region
ENABLED = bool(_CONFIG.s3.enabled)
SERVICE = "s3"
KEY_ID_ACCOUNT = "s3-key-id"
SECRET_ACCOUNT = "s3-secret-key"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
DEFAULT_EXPIRY = 3600
USER_AGENT = "proxmox-agent-lab/1.0"


class S3Error(RuntimeError):
    pass


def _require_enabled() -> None:
    if not ENABLED or not ENDPOINT or not BUCKET:
        raise S3Error(
            "S3 scratch storage is not configured. Set [s3] enabled, endpoint "
            "and bucket in your config, then store the credentials with "
            "'proxmox-lab secrets set s3-key-id' and 's3-secret-key'."
        )


def _keychain(account: str) -> str:
    try:
        return secrets_store.get(_CONFIG, account)
    except secrets_store.SecretError as exc:
        raise S3Error(str(exc)) from None


def credentials() -> tuple[str, str]:
    _require_enabled()
    return _keychain(KEY_ID_ACCOUNT), _keychain(SECRET_ACCOUNT)


def _quote(value: str) -> str:
    return parse.quote(value, safe="/~")


def _host() -> str:
    return parse.urlsplit(ENDPOINT).netloc


def _signing_key(secret: str, stamp: str) -> bytes:
    key = f"AWS4{secret}".encode()
    for part in (stamp, REGION, SERVICE, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


def _canonical_query(params: dict[str, str]) -> str:
    return "&".join(
        f"{parse.quote(key, safe='~')}={parse.quote(value, safe='~')}"
        for key, value in sorted(params.items())
    )


def _now() -> tuple[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")


def object_url(key: str) -> str:
    return f"{ENDPOINT}/{_quote(BUCKET)}/{_quote(key.lstrip('/'))}"


def presign(
    key: str,
    *,
    method: str = "GET",
    expires: int = DEFAULT_EXPIRY,
) -> str:
    """Return a short-lived presigned URL for one object."""
    if not 1 <= expires <= 7 * 24 * 3600:
        raise S3Error("presign expiry must be between 1 second and 7 days")
    access_key, secret = credentials()
    timestamp, stamp = _now()
    canonical_uri = f"/{_quote(BUCKET)}/{_quote(key.lstrip('/'))}"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{stamp}/{REGION}/{SERVICE}/aws4_request",
        "X-Amz-Date": timestamp,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_request = "\n".join(
        (
            method.upper(),
            canonical_uri,
            _canonical_query(params),
            f"host:{_host()}\n",
            "host",
            UNSIGNED_PAYLOAD,
        )
    )
    scope = f"{stamp}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            timestamp,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        )
    )
    signature = hmac.new(
        _signing_key(secret, stamp), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    params["X-Amz-Signature"] = signature
    return f"{ENDPOINT}{canonical_uri}?{_canonical_query(params)}"


def _signed_headers(
    method: str,
    canonical_uri: str,
    query: dict[str, str],
    payload: bytes,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    access_key, secret = credentials()
    timestamp, stamp = _now()
    payload_hash = hashlib.sha256(payload).hexdigest()
    headers = {
        "host": _host(),
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": timestamp,
    }
    headers.update({name.lower(): value for name, value in (extra or {}).items()})
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(
        f"{name}:{headers[name]}\n" for name in sorted(headers)
    )
    canonical_request = "\n".join(
        (
            method.upper(),
            canonical_uri,
            _canonical_query(query),
            canonical_headers,
            signed,
            payload_hash,
        )
    )
    scope = f"{stamp}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            timestamp,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        )
    )
    signature = hmac.new(
        _signing_key(secret, stamp), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return headers


def _call(
    method: str,
    canonical_uri: str,
    *,
    query: dict[str, str] | None = None,
    payload: bytes = b"",
    extra_headers: dict[str, str] | None = None,
    timeout: int = 300,
) -> bytes:
    query = query or {}
    headers = _signed_headers(method, canonical_uri, query, payload, extra_headers)
    url = ENDPOINT + canonical_uri
    if query:
        url += "?" + _canonical_query(query)
    req = request.Request(
        url,
        data=payload if method.upper() in ("PUT", "POST") else None,
        method=method.upper(),
        # The endpoint sits behind Cloudflare, which rejects urllib's default
        # user agent. This header is deliberately outside the signed set.
        headers={**headers, "user-agent": USER_AGENT},
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:800]
        raise S3Error(f"S3 HTTP {exc.code} for {method} {canonical_uri}: {detail}")
    except (error.URLError, TimeoutError, OSError) as exc:
        raise S3Error(f"S3 unreachable for {method} {canonical_uri}: {exc}")


def put_bytes(key: str, payload: bytes, content_type: str | None = None) -> str:
    """Upload one object and return its bucket-relative key."""
    extra = {"content-type": content_type} if content_type else None
    _call(
        "PUT",
        f"/{_quote(BUCKET)}/{_quote(key.lstrip('/'))}",
        payload=payload,
        extra_headers=extra,
    )
    return key.lstrip("/")


def get_bytes(key: str) -> bytes:
    return _call("GET", f"/{_quote(BUCKET)}/{_quote(key.lstrip('/'))}")


def delete_object(key: str) -> None:
    _call("DELETE", f"/{_quote(BUCKET)}/{_quote(key.lstrip('/'))}")


def list_objects(prefix: str = "", limit: int = 200) -> list[dict[str, Any]]:
    body = _call(
        "GET",
        f"/{_quote(BUCKET)}",
        query={
            "list-type": "2",
            "prefix": prefix,
            "max-keys": str(limit),
        },
    )
    root = ElementTree.fromstring(body)
    namespace = {"s3": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    path = "s3:Contents" if namespace else "Contents"
    out: list[dict[str, Any]] = []
    for item in root.findall(path, namespace):

        def text(name: str) -> str:
            node = item.find(f"s3:{name}" if namespace else name, namespace)
            return node.text or "" if node is not None else ""

        out.append(
            {
                "key": text("Key"),
                "size": int(text("Size") or 0),
                "last_modified": text("LastModified"),
            }
        )
    return out


def health() -> dict[str, Any]:
    """Confirm the bucket is reachable and the stored credentials work."""
    objects = list_objects(limit=1)
    return {
        "endpoint": ENDPOINT,
        "bucket": BUCKET,
        "region": REGION,
        "reachable": True,
        "sample_object_count": len(objects),
    }


if __name__ == "__main__":  # manual smoke check
    print(json.dumps(health(), indent=2))
