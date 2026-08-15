from __future__ import annotations

import base64
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

    def test_auth_refresh_targets_claimed_collection_and_returns_token(self) -> None:
        def opener(req: object, *, timeout: float) -> _Response:
            request = req
            self.assertEqual(
                request.full_url,
                "https://pb.example/api/collections/_superusers/auth-refresh",
            )  # type: ignore[attr-defined]
            self.assertEqual(
                request.get_header("Authorization"), "old-token"
            )  # type: ignore[attr-defined]
            return _Response({"token": "new-token"})

        client = pocketbase.Client(
            "https://pb.example", "old-token", "events", opener=opener
        )
        self.assertEqual(client.refresh_auth_token("_superusers"), "new-token")

    def test_password_authentication_omits_authorization_header(self) -> None:
        def opener(req: object, *, timeout: float) -> _Response:
            request = req
            self.assertEqual(
                request.full_url,
                "https://pb.example/api/collections/agents/auth-with-password",
            )  # type: ignore[attr-defined]
            self.assertIsNone(request.get_header("Authorization"))  # type: ignore[attr-defined]
            self.assertEqual(
                json.loads(request.data.decode()),  # type: ignore[attr-defined]
                {"identity": "agent@lab.invalid", "password": "password"},
            )
            return _Response({"token": "agent-token"})

        self.assertEqual(
            pocketbase.Client.authenticate_password(
                "https://pb.example",
                "agents",
                "agent@lab.invalid",
                "password",
                opener=opener,
            ),
            "agent-token",
        )

    def test_jwt_claims_select_only_safe_refresh_collections(self) -> None:
        def jwt(claims: dict[str, object]) -> str:
            encoded = base64.urlsafe_b64encode(
                json.dumps(claims).encode()
            ).decode().rstrip("=")
            return f"header.{encoded}.signature"

        renewable = jwt({"collectionId": "_superusers", "exp": 1_000})
        self.assertEqual(
            pocketbase.token_auth_collection(renewable), "_superusers"
        )
        self.assertTrue(
            pocketbase.token_expires_within(renewable, 60, now=950)
        )
        self.assertFalse(
            pocketbase.token_expires_within(renewable, 60, now=939)
        )
        self.assertIsNone(
            pocketbase.token_auth_collection(
                jwt({"collectionId": "../_superusers", "exp": 1_000})
            )
        )

    def test_agent_audit_rule_is_limited_to_the_agent_collection(self) -> None:
        payload = pocketbase.audit_collection_spec(
            "events", agent_collection_id="agents123"
        ).payload()
        self.assertEqual(
            payload["createRule"],
            '@request.auth.collectionId = "agents123"',
        )
        self.assertEqual(payload["listRule"], payload["createRule"])
        self.assertEqual(payload["viewRule"], payload["createRule"])
        self.assertIsNone(payload["updateRule"])
        self.assertIsNone(payload["deleteRule"])

    def test_provision_agent_creates_restricted_password_account(self) -> None:
        client = pocketbase.Client("https://pb.example", "superuser", "events")
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def request(
            method: str,
            path: str,
            *,
            payload: dict[str, object] | None = None,
            **_: object,
        ) -> dict[str, object]:
            calls.append((method, path, payload))
            if method == "GET" and path == "/api/collections/agents":
                raise pocketbase.PocketBaseError("missing", status=404)
            if method == "POST" and path == "/api/collections":
                return {
                    "id": "agents123",
                    "type": "auth",
                    "passwordAuth": {
                        "enabled": True,
                        "identityFields": ["email"],
                    },
                    "authRule": "",
                }
            if method == "GET" and path.endswith("/records"):
                return {"items": []}
            if method == "POST" and path.endswith("/records"):
                return {"id": "record123"}
            self.fail(f"unexpected request: {method} {path}")
            return {}

        with mock.patch.object(client, "_request", side_effect=request), \
             mock.patch.object(
                 pocketbase.Client,
                 "authenticate_password",
                 return_value="agent-token",
             ) as authenticate, \
             mock.patch.object(
                 client,
                 "provision",
                 return_value={"created": True, "collection": {}},
             ) as provision:
            result = client.provision_agent(
                "agents", "agent@lab.invalid", "agent-password"
            )

        self.assertTrue(result["agent_created"])
        self.assertEqual(result["token"], "agent-token")
        self.assertEqual(calls[1][2]["type"], "auth")  # type: ignore[index]
        self.assertEqual(
            calls[3][2]["passwordConfirm"], "agent-password"  # type: ignore[index]
        )
        authenticate.assert_called_once_with(
            "https://pb.example",
            "agents",
            "agent@lab.invalid",
            "agent-password",
            timeout=10.0,
            opener=client._opener,
        )
        provision.assert_called_once_with(agent_collection_id="agents123")

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

    def test_summary_reports_edge_timestamps_and_a_labeled_sample(self) -> None:
        calls: list[str] = []

        def opener(req: object, *, timeout: float) -> _Response:
            url = req.full_url  # type: ignore[attr-defined]
            calls.append(url)
            query = parse_qs(urlparse(url).query)
            sort = query.get("sort", [""])[0]
            per_page = query.get("perPage", [""])[0]
            if query.get("skipTotal") == ["false"]:
                return _Response({"items": [], "totalItems": 3})
            if per_page == "1" and sort == "timestamp":
                return _Response({"items": [
                    {"data": {"timestamp": "2026-08-01T00:00:00Z"}},
                ]})
            if per_page == "1" and sort == "-timestamp":
                return _Response({"items": [
                    {"data": {"timestamp": "2026-08-14T00:00:00Z"}},
                ]})
            # the recent-sample query (perPage == sample size) for most_common
            return _Response({"items": [
                {"data": {"event": "lease-begin"}},
                {"data": {"event": "lease-begin"}},
                {"data": {"event": "lease-end"}},
            ]})

        client = pocketbase.Client("https://pb.example", "test-token", "events", opener=opener)
        result = client.summary()
        self.assertEqual(result["events"], 3)
        self.assertEqual(result["first_event"], "2026-08-01T00:00:00Z")
        self.assertEqual(result["last_event"], "2026-08-14T00:00:00Z")
        self.assertEqual(result["most_common_sample_size"], 3)
        self.assertEqual(result["most_common"], {"lease-begin": 2, "lease-end": 1})

    def test_summary_on_an_empty_collection_skips_the_edge_queries(self) -> None:
        def opener(req: object, *, timeout: float) -> _Response:
            return _Response({"items": [], "totalItems": 0})

        client = pocketbase.Client("https://pb.example", "test-token", "events", opener=opener)
        result = client.summary()
        self.assertEqual(result, {
            "backend": "pocketbase", "collection": "events", "events": 0,
        })


if __name__ == "__main__":
    unittest.main()
