"""PocketBase transport for the redacted audit ledger.

This module deliberately uses the PocketBase HTTP API through the Python
standard library. The token is supplied by the caller and is never placed in
configuration, records, exception text, or command arguments.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
import json
import re
from typing import Any, Callable
from urllib import error, parse, request


_COLLECTION_RE = re.compile(r"[A-Za-z0-9_]+\Z")


class PocketBaseError(RuntimeError):
    """A bounded PocketBase API or schema error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CollectionSpec:
    name: str
    fields: tuple[dict[str, Any], ...]
    indexes: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": "base",
            "fields": [dict(field) for field in self.fields],
            "indexes": list(self.indexes),
            "listRule": None,
            "viewRule": None,
            "createRule": None,
            "updateRule": None,
            "deleteRule": None,
        }


def audit_collection_spec(name: str) -> CollectionSpec:
    if not _COLLECTION_RE.fullmatch(name):
        raise ValueError(f"unsafe PocketBase collection name: {name!r}")
    quoted = name.replace("`", "``")
    return CollectionSpec(
        name=name,
        fields=(
            {"name": "event_id", "type": "text", "required": True, "max": 64},
            {"name": "controller", "type": "text", "required": True, "max": 200},
            {"name": "timestamp", "type": "date", "required": True},
            {"name": "event", "type": "text", "required": True, "max": 200},
            {"name": "lease", "type": "text", "required": False, "max": 200},
            {"name": "vmid", "type": "number", "required": False, "onlyInt": True},
            {"name": "data", "type": "json", "required": True, "maxSize": 200000},
        ),
        indexes=(
            f"CREATE UNIQUE INDEX `idx_{quoted}_event_id` ON `{quoted}` (`event_id`)",
            f"CREATE INDEX `idx_{quoted}_event` ON `{quoted}` (`event`)",
            f"CREATE INDEX `idx_{quoted}_lease` ON `{quoted}` (`lease`)",
            f"CREATE INDEX `idx_{quoted}_controller` ON `{quoted}` (`controller`)",
        ),
    )


class Client:
    """Small, testable PocketBase REST client for audit records."""

    def __init__(
        self,
        base_url: str,
        token: str,
        collection: str,
        *,
        timeout: float = 10.0,
        opener: Callable[..., Any] = request.urlopen,
    ) -> None:
        base_url = base_url.strip().rstrip("/")
        parsed = parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("PocketBase URL must be an absolute HTTP(S) URL")
        if not token:
            raise ValueError("PocketBase token is empty")
        if not _COLLECTION_RE.fullmatch(collection):
            raise ValueError(f"unsafe PocketBase collection name: {collection!r}")
        if timeout <= 0:
            raise ValueError("PocketBase timeout must be positive")
        self.base_url = base_url
        self.token = token
        self.collection_name = collection
        self.timeout = timeout
        self._opener = opener

    @property
    def _collection_path(self) -> str:
        return "/api/collections/" + parse.quote(self.collection_name, safe="")

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + parse.urlencode(query)
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
        headers = {"Accept": "application/json", "Authorization": self.token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        http_request = request.Request(url, data=body, headers=headers, method=method)
        try:
            response = self._opener(http_request, timeout=self.timeout)
            with response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except error.HTTPError as exc:
            try:
                raw = exc.read()
                try:
                    detail = json.loads(raw.decode()).get("message", "")
                except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                    detail = ""
            finally:
                exc.close()
            suffix = f": {detail}" if detail else ""
            raise PocketBaseError(f"PocketBase HTTP {exc.code}{suffix}", status=exc.code) from None
        except (error.URLError, TimeoutError, OSError) as exc:
            raise PocketBaseError(f"PocketBase request failed: {exc}") from None
        if not raw:
            return {"status": status}
        try:
            result = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PocketBaseError(f"PocketBase returned invalid JSON: {exc}") from None
        if not isinstance(result, dict):
            raise PocketBaseError("PocketBase returned a non-object JSON response")
        return result

    def create_event(self, record: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_id": record["event_id"],
            "controller": record["controller"],
            "timestamp": record["timestamp"],
            "event": record["event"],
            "data": record,
        }
        if record.get("lease") is not None:
            payload["lease"] = record["lease"]
        if record.get("vmid") is not None:
            payload["vmid"] = record["vmid"]
        return self._request("POST", f"{self._collection_path}/records", payload=payload)

    def create_imported_event(
        self, source: dict[str, Any], *, event_id: str, controller: str,
        lease: str | None, vmid: int | None,
    ) -> dict[str, Any]:
        """Store an SQLite event without changing its preserved JSON payload."""
        payload: dict[str, Any] = {
            "event_id": event_id, "controller": controller,
            "timestamp": source["timestamp"], "event": source["event"],
            "data": source,
        }
        if lease is not None:
            payload["lease"] = lease
        if vmid is not None:
            payload["vmid"] = vmid
        return self._request("POST", f"{self._collection_path}/records", payload=payload)

    def event_exists(self, event_id: str) -> bool:
        """Return whether an import event id is already in the collection."""
        result = self._request(
            "GET", f"{self._collection_path}/records",
            query={"page": "1", "perPage": "1", "skipTotal": "true",
                   "filter": f"event_id = {json.dumps(event_id)}", "fields": "id"},
        )
        items = result.get("items", [])
        if not isinstance(items, list):
            raise PocketBaseError("PocketBase records response has invalid items")
        return bool(items)

    def query(self, *, limit: int = 50, lease: str | None = None,
              event: str | None = None, since: str | None = None) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("PocketBase query limit must be positive")
        clauses: list[str] = []
        if lease:
            clauses.append(f"lease = {json.dumps(lease)}")
        if event:
            if "*" not in event:
                clauses.append(f"event = {json.dumps(event)}")
            elif event.endswith("*") and "*" not in event[:-1]:
                clauses.append(f"event ^= {json.dumps(event[:-1])}")
            else:
                clauses.append(f"event ~ {json.dumps(event.replace('*', ''))}")
        if since:
            clauses.append(f"timestamp >= {json.dumps(since)}")
        query = {"page": "1", "perPage": str(limit), "sort": "-timestamp", "fields": "data"}
        if clauses:
            query["filter"] = " && ".join(f"({clause})" for clause in clauses)
        result = self._request("GET", f"{self._collection_path}/records", query=query)
        items = result.get("items", [])
        if not isinstance(items, list):
            raise PocketBaseError("PocketBase records response has invalid items")
        events: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            data = item.get("data", item)
            if isinstance(data, dict):
                events.append(data)
        return events

    def _edge_timestamp(self, sort: str) -> str | None:
        result = self._request(
            "GET", f"{self._collection_path}/records",
            query={"page": "1", "perPage": "1", "sort": sort, "fields": "data"},
        )
        items = result.get("items", [])
        if not items or not isinstance(items[0], dict):
            return None
        data = items[0].get("data", items[0])
        return data.get("timestamp") if isinstance(data, dict) else None

    def summary(self) -> dict[str, Any]:
        result = self._request("GET", f"{self._collection_path}/records",
                               query={"page": "1", "perPage": "1", "skipTotal": "false", "fields": "id"})
        total = result.get("totalItems", 0)
        out: dict[str, Any] = {
            "backend": "pocketbase", "collection": self.collection_name,
            "events": total,
        }
        if not total:
            return out
        out["first_event"] = self._edge_timestamp("timestamp")
        out["last_event"] = self._edge_timestamp("-timestamp")
        # PocketBase's records API has no server-side GROUP BY, and scanning
        # every record just to count event types would not scale on a
        # collection this size. The most recent events are the ones anyone
        # asking for a summary actually cares about, so sample those instead
        # of silently pretending this is an exact, whole-collection count.
        sample_size = min(total, 500)
        recent = self.query(limit=sample_size)
        counts = collections.Counter(
            event.get("event") for event in recent if isinstance(event, dict)
        )
        out["most_common_sample_size"] = sample_size
        out["most_common"] = dict(counts.most_common(10))
        return out

    def get_collection(self) -> dict[str, Any]:
        return self._request("GET", f"/api/collections/{parse.quote(self.collection_name, safe='')}")

    def provision(self) -> dict[str, Any]:
        """Create the audit collection, or validate an existing one."""
        spec = audit_collection_spec(self.collection_name)
        try:
            existing = self.get_collection()
        except PocketBaseError as exc:
            if exc.status != 404:
                raise
            created = self._request("POST", "/api/collections", payload=spec.payload())
            return {"created": True, "collection": created}
        expected_fields = {field["name"]: field for field in spec.fields}
        actual_fields = {field.get("name"): field for field in existing.get("fields", []) if isinstance(field, dict)}
        mismatches: list[str] = []
        for name, expected in expected_fields.items():
            actual = actual_fields.get(name)
            if actual is None:
                mismatches.append(name)
                continue
            if actual.get("type") != expected["type"]:
                mismatches.append(f"{name}.type")
            for key in ("required", "max", "onlyInt", "maxSize"):
                if key in expected and actual.get(key) != expected[key]:
                    mismatches.append(f"{name}.{key}")
        expected_indexes = set(spec.indexes)
        actual_indexes = {index for index in existing.get("indexes", []) if isinstance(index, str)}
        for index in sorted(expected_indexes - actual_indexes):
            mismatches.append(f"index:{index}")
        for rule in ("listRule", "viewRule", "createRule", "updateRule", "deleteRule"):
            if existing.get(rule) is not None:
                mismatches.append(rule)
        if existing.get("type", "base") != "base":
            mismatches.append("type")
        if mismatches:
            raise PocketBaseError("PocketBase audit collection schema mismatch: " + ", ".join(mismatches))
        return {"created": False, "collection": existing}
