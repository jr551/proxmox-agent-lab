from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from proxmox_agent_lab import journal, pocketbase  # noqa: E402


class _Response:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class PocketBaseTests(unittest.TestCase):
    def test_create_event_uses_native_token_header(self) -> None:
        calls: list[tuple[str, str, dict[str, object]]] = []

        def opener(req: object, *, timeout: float) -> _Response:
            request = req
            body = json.loads(request.data.decode())  # type: ignore[attr-defined]
            self.assertEqual(request.get_header("Authorization"), "test-token")  # type: ignore[attr-defined]
            calls.append((request.get_method(), request.full_url, body))  # type: ignore[attr-defined]
            return _Response({"id": "event-1"})

        client = pocketbase.Client("https://pb.example", "test-token", "events", opener=opener)
        record = {
            "event_id": "event-1", "controller": "test", "timestamp": "2026-08-10T12:00:00Z",
            "event": "lease-begin", "lease": "lease-1", "vmid": 9001,
        }
        self.assertEqual(client.create_event(record)["id"], "event-1")
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/api/collections/events/records"))
        self.assertEqual(calls[0][2]["data"], record)

    def test_event_exists_uses_exact_filter(self) -> None:
        urls: list[str] = []

        def opener(req: object, *, timeout: float) -> _Response:
            urls.append(req.full_url)  # type: ignore[attr-defined]
            return _Response({"items": [{"id": "record-1"}]})

        client = pocketbase.Client("https://pb.example", "test-token", "events", opener=opener)
        self.assertTrue(client.event_exists("event-1"))
        self.assertEqual(
            parse_qs(urlparse(urls[0]).query)["filter"][0],
            'event_id = "event-1"',
        )

    def test_sqlite_migration_preserves_records_and_restarts_safely(self) -> None:
        source_records = [
            {"timestamp": "2026-08-10T12:00:00Z", "event": "lease-begin", "lease": "lease-1", "vmid": 9001},
            {"timestamp": "2026-08-10T12:01:00Z", "event": "lease-end", "lease": "lease-1", "vmid": 9001},
        ]
        stored_ids: set[str] = set()
        imported: list[tuple[dict[str, object], str]] = []
        client = mock.Mock()
        client.event_exists.side_effect = stored_ids.__contains__

        def create(source: dict[str, object], *, event_id: str, **_: object) -> None:
            stored_ids.add(event_id)
            imported.append((source, event_id))

        client.create_imported_event.side_effect = create
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for record in source_records:
                journal.append(root, "sqlite", record)
            first = journal.migrate_sqlite_to_pocketbase(root, client, controller="test")
            second = journal.migrate_sqlite_to_pocketbase(root, client, controller="test")

        self.assertEqual((first["source_events"], first["imported"], first["already_present"]), (2, 2, 0))
        self.assertEqual((second["source_events"], second["imported"], second["already_present"]), (2, 0, 2))
        self.assertEqual([record for record, _ in imported], source_records)
        self.assertNotEqual(imported[0][1], imported[1][1])


if __name__ == "__main__":
    unittest.main()
