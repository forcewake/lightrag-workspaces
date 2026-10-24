"""Black-box management safety and durable restart/idempotency checks."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from gateway import Config, Worker, WorkerManager, create_app
from workspace_management import WorkspaceManagement, atomic_json, digest, metadata_body


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.config = Config(*(root / part for part in ("control", "inputs", "runtime", "static")))
        self.config.storage_root.mkdir()
        self.config.input_root.mkdir()
        self.config.static_root.mkdir()
        (self.config.storage_root / "default").mkdir()
        (self.config.storage_root / "default" / "kv_store_doc_status.json").write_text('{"original":{"status":"processed"}}')
        (self.config.static_root / "index.html").write_text('<html><head></head><body>UI</body></html>')
        self.original = (self.config.storage_root / "default" / "kv_store_doc_status.json").read_bytes()
        self.manager = WorkerManager(self.config)
        self.app = create_app(self.config, self.manager)
        self.context = self.app.router.lifespan_context(self.app)
        await self.context.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://gateway.test")
        self.environment = patch.dict(os.environ, {"LIGHTRAG_API_KEY": "", "AUTH_ACCOUNTS": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.context.__aexit__(None, None, None)
        self.assertEqual((self.config.storage_root / "default" / "kv_store_doc_status.json").read_bytes(), self.original)

    async def finish(self, operation_id):
        task = self.manager.management.tasks.get(operation_id)
        if task:
            await task
            await asyncio.sleep(0)
        return (await self.client.get("/workspace-operations/" + operation_id)).json()

    async def create(self, name="meeting-notes", key="a-durable-create-key-1", **fields):
        return await self.client.post("/workspaces", json={"id": name, **fields}, headers={"Idempotency-Key": key})

    async def test_create_and_idempotency_leave_existing_corpus_byte_identical(self):
        response = await self.create(displayName="Meetings", description="Approved notes", tags=["work", "work"])
        self.assertEqual(response.status_code, 202)
        operation = await self.finish(response.json()["id"])
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(operation["workspace"]["tags"], ["work"])
        again = await self.create(displayName="Meetings", description="Approved notes", tags=["work", "work"])
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["id"], operation["id"])
        conflict = await self.create(displayName="Other")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "idempotency_conflict")
        inventory = (await self.client.get("/workspaces")).json()
        self.assertEqual({row["id"] for row in inventory["workspaces"]}, {"default", "meeting-notes"})
        self.assertTrue(inventory["capabilities"]["create"])
        self.assertFalse(self.manager.workers)

    async def test_distinct_punctuation_ids_preserved_and_new_key_cannot_adopt_existing(self):
        for name, key in (("Meeting-notes", "unique-create-key-1"), ("Meeting_notes", "unique-create-key-2")):
            response = await self.create(name, key)
            self.assertEqual((await self.finish(response.json()["id"]))["state"], "succeeded")
        duplicate = await self.create("Meeting-notes", "unique-create-key-3")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["code"], "workspace_exists")
        self.assertEqual(len((await self.client.get("/workspaces")).json()["workspaces"]), 3)

    async def test_metadata_persisted_without_identity_or_corpus_changes(self):
        response = await self.client.patch("/workspaces/default/metadata", json={"displayName": "My notes", "tags": ["source"], "description": "Stable ID"})
        self.assertEqual(response.status_code, 200)
        other = WorkspaceManagement(self.config)
        self.assertEqual(other.catalog()["default"]["displayName"], "My notes")
        invalid = await self.client.patch("/workspaces/default/metadata", json={"id": "changed"})
        self.assertEqual(invalid.status_code, 422)
        state = (await self.client.get("/workspaces/default/status")).json()
        self.assertEqual(state["id"], "default")
        self.assertEqual(state["displayName"], "My notes")
        self.assertEqual(state["state"], "stopped")
        self.assertFalse(self.manager.workers)

    async def test_unknown_queries_and_metadata_never_create(self):
        for method, path, kwargs in (("get", "/workspaces/missing/health", {}), ("patch", "/workspaces/missing/metadata", {"json": {"displayName": "x"}})):
            response = await getattr(self.client, method)(path, **kwargs)
            self.assertEqual(response.status_code, 404)
        self.assertFalse((self.config.storage_root / "missing").exists())

    async def test_api_key_auth_covers_management_reads_and_writes_guest_does_not_bypass(self):
        with patch.dict(os.environ, {"LIGHTRAG_API_KEY": "correct-secret", "AUTH_ACCOUNTS": ""}):
            for path in ("/workspaces/default/status", "/maintenance", "/workspace-operations/00000000-0000-0000-0000-000000000000"):
                response = await self.client.get(path, headers={"Authorization": "Bearer forged-guest"})
                self.assertEqual(response.status_code, 401)
            response = await self.create()
            self.assertEqual(response.status_code, 401)
            metadata = await self.client.patch("/workspaces/default/metadata", json={"displayName": "Authorized"}, headers={"X-API-Key": "correct-secret"})
            self.assertEqual(metadata.status_code, 200)
            duplicate = await self.client.patch("/workspaces/default/metadata", json={"displayName": "x"}, headers=[("X-API-Key", "correct-secret"), ("X-API-Key", "wrong")])
            self.assertEqual(duplicate.status_code, 400)

    async def test_no_auth_does_not_create_cors_exception(self):
        # Cross-origin browser JSON requires a preflight; this gateway does not
        # grant a new blanket CORS permission to management operations.
        response = await self.client.options("/workspaces", headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,idempotency-key"})
        self.assertNotIn("access-control-allow-origin", response.headers)
        response = await self.client.post("/workspaces", content='{"id":"cross-site"}', headers={"content-type": "text/plain", "Idempotency-Key": "some-external-request"})
        self.assertEqual(response.status_code, 415)

    async def test_maintenance_persists_and_blocks_all_new_corpus_routes_without_waking(self):
        token = "private-maintenance-owner-token-123456"
        response = await self.client.post("/maintenance", json={"ownerToken": token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["drained"])
        self.assertTrue(response.json()["ownerMatches"])
        for method, path, kwargs in (("get", "/workspaces/default/health", {}), ("post", "/workspaces/default/query", {"json": {"query": "never accepted"}}), ("post", "/workspaces/default/prewarm", {}), ("patch", "/workspaces/default/metadata", {"json": {"displayName": "blocked"}})):
            blocked = await getattr(self.client, method)(path, **kwargs)
            self.assertEqual(blocked.status_code, 503, path)
            self.assertEqual(blocked.json()["code"], "maintenance_active")
            self.assertIn("Retry-After", blocked.headers)
        self.assertEqual((await self.create()).status_code, 503)
        self.assertEqual((await self.client.get("/livez")).status_code, 200)
        self.assertEqual((await self.client.get("/readyz")).status_code, 503)
        self.assertEqual((await self.client.get("/workspaces/default/webui/")).status_code, 200)
        fresh = WorkspaceManagement(self.config)
        self.assertTrue(fresh.maintenance(token)["ownerMatches"])
        self.assertNotIn(token, (fresh.root / "maintenance.json").read_text())
        wrong = await self.client.post("/maintenance/release", json={"ownerToken": "a-different-owner-token-1234567890"})
        self.assertEqual(wrong.status_code, 409)
        released = await self.client.post("/maintenance/release", json={"ownerToken": token})
        self.assertFalse(released.json()["active"])
        self.assertEqual((await self.client.get("/readyz")).status_code, 200)
        self.assertFalse(self.manager.workers)

    async def test_drain_waits_for_start_admission_lock_and_pipeline_idle(self):
        await self.manager.lock.acquire()
        draining = asyncio.create_task(self.manager.maintenance_status())
        await asyncio.sleep(0)
        self.assertFalse(draining.done())
        self.manager.lock.release()
        self.assertTrue((await draining)["drained"])
        worker = SimpleNamespace(workspace_id="default", references=0)
        self.manager.workers["default"] = worker
        self.manager._idle = AsyncMock(return_value=False)
        self.assertFalse((await self.manager.maintenance_status())["drained"])
        self.manager.workers.clear()

    async def test_second_gateway_cannot_manage_same_control_root(self):
        other = WorkspaceManagement(self.config)
        with self.assertRaisesRegex(RuntimeError, "Another gateway"):
            other.start()

    async def test_restart_after_publish_reconciles_same_receipt_without_recreating(self):
        response = await self.create()
        op = await self.finish(response.json()["id"])
        path = self.manager.management._path(op["id"])
        saved = json.loads(path.read_text())
        saved.update(state="running", retryable=True)
        atomic_json(path, saved)
        stale = (await self.client.get("/workspace-operations/" + op["id"])).json()
        self.assertEqual(stale["errorCode"], "operation_interrupted")
        receipt = self.config.storage_root / "meeting-notes" / ".workspace-created"
        original = receipt.read_bytes()
        retry = await self.create()
        self.assertEqual(retry.json()["id"], op["id"])
        self.assertEqual((await self.finish(op["id"]))["state"], "succeeded")
        self.assertEqual(receipt.read_bytes(), original)

    async def test_partial_postgres_failure_not_discoverable_and_same_key_retry_reconciles(self):
        self.config.storage_backend = "postgres"
        self.config.embedding_model = "synthetic"
        self.config.postgres_database = "test_database"
        barrier = asyncio.Event()
        async def interrupted(*args):
            await barrier.wait()
            raise OSError("secret postgres credential must never appear")
        with patch("workspace_management.create_postgres_namespace", side_effect=interrupted):
            response = await self.create("unpublished")
            await asyncio.sleep(0)
            self.assertFalse((self.config.storage_root / "unpublished").exists())
            self.assertEqual(self.manager.management.operation(response.json()["id"])["state"], "running")
            barrier.set()
            result = await self.finish(response.json()["id"])
            self.assertEqual(result["state"], "failed")
            self.assertTrue(result["retryable"])
            self.assertNotIn("credential", json.dumps(result))
        self.assertFalse((self.config.storage_root / "unpublished").exists())
        response = await self.create("unpublished", "another-key-for-same-id")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "workspace_reserved")

    async def test_existing_inputs_and_symlink_paths_never_adopted(self):
        folder = self.config.input_root / "existing-inputs"
        folder.mkdir()
        (folder / "original.txt").write_text("original source")
        response = await self.create("existing-inputs")
        result = await self.finish(response.json()["id"])
        self.assertEqual(result["errorCode"], "workspace_path_conflict")
        self.assertFalse((self.config.storage_root / "existing-inputs").exists())
        self.assertEqual((folder / "original.txt").read_text(), "original source")
        (self.config.storage_root / "linked").symlink_to(self.config.storage_root / "default")
        response = await self.create("linked", "a-different-key-for-link")
        self.assertEqual(response.status_code, 409)

    async def test_status_retains_specific_timeout_and_reports_live_startup(self):
        ready = asyncio.get_running_loop().create_future()
        worker = Worker("default", 19621, SimpleNamespace(returncode=None), "http://127.0.0.1:19621", ready=ready)
        self.manager.workers["default"] = worker
        state = (await self.client.get("/workspaces/default/status")).json()
        self.assertEqual(state["state"], "starting")
        self.assertIsInstance(state["startupElapsedSeconds"], float)
        ready.set_exception(HTTPException(503, "private server detail"))
        self.manager.failures["default"] = {"state": "failed", "errorCode": "workspace_start_timeout", "retryable": True, "retryAfterSeconds": 5, "startupElapsedSeconds": 240.0}
        state = (await self.client.get("/workspaces/default/status")).json()
        self.assertEqual(state["errorCode"], "workspace_start_timeout")
        self.assertEqual(state["startupElapsedSeconds"], 240.0)
        self.assertNotIn("private", json.dumps(state))
        self.manager.workers.clear()

    async def test_password_auth_reuses_pinned_validator_and_rejects_guest(self):
        handler = SimpleNamespace(validate_token=lambda token: {"role": "user" if token == "verified-user" else "guest"})
        with patch.dict(os.environ, {"AUTH_ACCOUNTS": "synthetic:password", "LIGHTRAG_API_KEY": ""}), patch("workspace_management._upstream_auth_handler", return_value=handler):
            accepted = await self.client.patch("/workspaces/default/metadata", json={"displayName": "Authenticated"}, headers={"Authorization": "Bearer verified-user"})
            self.assertEqual(accepted.status_code, 200)
            denied = await self.client.patch("/workspaces/default/metadata", json={"displayName": "Forbidden"}, headers={"Authorization": "Bearer guest"})
            self.assertEqual(denied.status_code, 401)

    async def test_validation_is_bounded_and_exact(self):
        for body in ({"id": "../outside"}, {"id": "new", "tags": ["tag"] * 21}, {"id": "new", "description": "x" * 2001}, {"id": "new", "displayName": ""}, {"id": "new", "defaultWorkspace": "new"}):
            response = await self.client.post("/workspaces", json=body, headers={"Idempotency-Key": "valid-create-key-1"})
            self.assertEqual(response.status_code, 422)
        response = await self.client.post("/workspaces", json={"id": "new"})
        self.assertEqual(response.status_code, 422)
        response = await self.client.post("/workspaces", content=b" " * 16385, headers={"content-type": "application/json", "Idempotency-Key": "valid-create-key-1"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual([p.name for p in self.config.storage_root.iterdir() if not p.name.startswith(".")], ["default"])

    async def test_database_unavailable_is_503_but_liveness_stays_200_no_cold_start(self):
        self.manager.postgres_inventory = SimpleNamespace(refresh=AsyncMock(side_effect=lambda rows: rows), probe=AsyncMock(return_value=False), close=AsyncMock())
        self.manager._spawn = AsyncMock()
        response = await self.client.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "database_unavailable")
        self.assertEqual((await self.client.get("/livez")).status_code, 200)
        self.manager._spawn.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
