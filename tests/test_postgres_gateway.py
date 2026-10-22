"""PostgreSQL discovery uses verified registration and live read-only counts."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import Config, WorkerManager, create_app, workspace_metadata, workspace_startup_memory
from postgres_inventory import PostgresInventory


DATABASE_MARKER_ID = "d4144cf5-b44b-4549-ae64-936398ac5195"


def manifest(workspace_id, frozen=False):
    return {"schema_version": 1, "workspace_id": workspace_id,
            "postgres_workspace": "ws_" + hashlib.sha256(workspace_id.encode()).hexdigest()[:32],
            "status": "ready", "embedding_dim": 3072, "embedding_model": "synthetic-model",
            "database": "synthetic_database", "db_instance_id": DATABASE_MARKER_ID,
            "preserve_backlog": frozen, "read_only": False,
            "source_manifest_sha256": "a" * 64, "counts": {"documents": 999}}


class Connection:
    def __init__(self, manifests):
        self.manifests = manifests
        self.marker_uuid = DATABASE_MARKER_ID
        self.rows = [{"workspace": item["workspace_id"], "postgres_workspace": item["postgres_workspace"],
                      "db_instance_id": DATABASE_MARKER_ID, "status": "ready", "embedding_model": "synthetic-model",
                      "embedding_dim": 3072} for item in manifests.values()]
        self.counts = [{"workspace": item["postgres_workspace"], "status": "processed", "count": index + 3}
                       for index, item in enumerate(manifests.values())]
        self.executed = []
        self.queries = []

    @asynccontextmanager
    async def transaction(self, **kwargs):
        assert kwargs == {"readonly": True}
        yield

    async def execute(self, sql):
        self.executed.append(sql)

    async def fetchrow(self, sql):
        self.queries.append((sql, []))
        return {"db_instance_id": self.marker_uuid, "schema_version": 1, "database": "synthetic_database"}

    async def fetch(self, sql, values):
        self.queries.append((sql, values))
        return self.rows if "lightrag_workspace_registry" in sql else self.counts


class Pool:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    @asynccontextmanager
    async def acquire(self, **kwargs):
        assert kwargs == {"timeout": 5}
        yield self.connection

    async def close(self):
        self.closed = True


class PostgresDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = self.root / "control"
        self.storage.mkdir()
        self.manifests = {name: manifest(name, name == "frozen-queue")
                          for name in ("meeting-notes", "meeting_notes", "frozen-queue")}
        for name, value in self.manifests.items():
            folder = self.storage / name
            folder.mkdir()
            (folder / ".postgres-workspace.json").write_text(json.dumps(value))
            # If discovery accidentally opens an old file, it is invalid JSON.
            (folder / "kv_store_doc_status.json").write_text("do not read legacy JSON")
        (self.storage / "unregistered").mkdir()
        self.config = Config(self.storage, self.root / "inputs", self.root / "runtime", self.root / "static",
                             default_workspace="meeting-notes", storage_backend="postgres",
                             embedding_model="synthetic-model", postgres_database="synthetic_database")
        self.validation = patch("gateway.load_workspace_manifest", side_effect=lambda path, name: self.manifests[name])
        self.validation.start()
        self.addCleanup(self.validation.stop)
        self.manager = WorkerManager(self.config)
        self.connection = Connection(self.manifests)
        self.manager.postgres_inventory.pool = Pool(self.connection)
        self.addAsyncCleanup(self.manager.stop)

    async def test_only_registered_exact_ids_are_discovered_without_legacy_json_reads(self):
        items = workspace_metadata(self.config)
        self.assertEqual({item["id"] for item in items}, set(self.manifests))
        inventory = await self.manager.inventory()
        by_id = {item["id"]: item for item in inventory}
        self.assertEqual(by_id["meeting-notes"]["processed"], 3)
        self.assertEqual(by_id["meeting_notes"]["processed"], 4)
        self.assertNotEqual(self.manifests["meeting-notes"]["postgres_workspace"],
                            self.manifests["meeting_notes"]["postgres_workspace"])
        for item in inventory:
            self.assertTrue(item["available"])
            self.assertNotIn("_postgresManifest", item)
            self.assertNotIn("postgresWorkspace", item)
            self.assertNotIn("synthetic_database", json.dumps(item))
        # Counts and identity reads use only SELECT plus local bounds.
        self.assertEqual(len(self.connection.executed), 2)
        self.assertTrue(all(sql.startswith("SET LOCAL ") for sql in self.connection.executed))
        self.assertTrue(all(sql.startswith("SELECT ") for sql, _ in self.connection.queries))

    async def test_only_the_explicit_preserved_backlog_is_read_only(self):
        writable_namespace = self.manifests["meeting-notes"]["postgres_workspace"]
        self.connection.counts.append({"workspace": writable_namespace, "status": "pending", "count": 2})
        items = {item["id"]: item for item in await self.manager.inventory()}
        self.assertFalse(items["meeting-notes"]["readOnly"])
        self.assertEqual(items["meeting-notes"]["pending"], 2)
        self.assertTrue(items["frozen-queue"]["readOnly"])
        self.assertEqual(items["frozen-queue"]["pending"], 0)
        self.assertIn("Earlier unfinished", items["frozen-queue"]["readOnlyReason"])

    async def test_mismatched_registry_namespace_never_becomes_available(self):
        self.connection.rows[0]["postgres_workspace"] = self.manifests["meeting_notes"]["postgres_workspace"]
        items = {item["id"]: item for item in await self.manager.inventory()}
        self.assertFalse(items["meeting-notes"]["available"])
        self.assertTrue(items["meeting-notes"]["readOnly"])
        self.assertTrue(items["meeting_notes"]["available"])
        self.assertFalse(self.manager.workers)

    async def test_database_identity_mismatch_blocks_all_registered_workspaces(self):
        self.connection.marker_uuid = "3f961688-8c98-430d-a3a4-d0d47edb96a7"
        items = await self.manager.inventory()
        self.assertTrue(all(not item["available"] for item in items))
        self.assertTrue(all(item["documents"] == 0 for item in items))

    async def test_database_failure_is_closed_and_never_exposes_connection_text(self):
        reader = PostgresInventory()
        reader._pool = AsyncMock(side_effect=RuntimeError("postgres://private:secret@host/database"))
        self.manager.postgres_inventory = reader
        with self.assertLogs("postgres_inventory", level="WARNING") as logs:
            items = await self.manager.inventory()
        self.assertTrue(all(not item["available"] and item["readOnly"] for item in items))
        self.assertNotIn("secret", json.dumps(items) + str(logs.output))
        self.assertTrue(all("_postgresManifest" not in item for item in items))

    async def test_pg_cold_start_allowance_does_not_count_legacy_matrices(self):
        folder = self.storage / "meeting-notes"
        with (folder / "vdb_chunks.json").open("wb") as stream:
            stream.truncate(10 * 1024**3)
        self.assertEqual(workspace_startup_memory(self.storage, "meeting-notes", "postgres"), 384 * 1024**2)

    async def test_manifest_validation_failure_is_visible_but_cannot_be_opened(self):
        with patch("gateway.load_workspace_manifest", side_effect=ValueError("registration mismatch")):
            items = await self.manager.inventory()
        self.assertEqual({item["id"] for item in items}, set(self.manifests))
        self.assertTrue(all(not item["available"] and item["readOnly"] for item in items))
        self.assertFalse(self.manager.workers)

    async def test_unregistered_directory_and_unknown_api_selector_never_start_a_worker(self):
        self.manager._spawn = AsyncMock()
        app = create_app(self.config, manager=self.manager)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
            for path, headers in (("/workspaces/unregistered/health", {}),
                                  ("/health", {"LIGHTRAG-WORKSPACE": "unknown"})):
                response = await client.get(path, headers=headers)
                self.assertEqual(response.status_code, 404)
        self.manager._spawn.assert_not_awaited()
        self.assertFalse((self.storage / "unknown").exists())

    async def test_model_or_target_database_mismatch_does_not_use_stale_counts(self):
        self.config.embedding_model = "different-model"
        items = await self.manager.inventory()
        self.assertTrue(all(not item["available"] and item["documents"] == 0 for item in items))
        self.config.embedding_model = "synthetic-model"
        self.config.postgres_database = "another-database"
        items = await self.manager.inventory()
        self.assertTrue(all(not item["available"] and item["documents"] == 0 for item in items))

    async def test_pool_creation_is_bounded_and_lazy(self):
        reader = PostgresInventory()
        pool = Pool(self.connection)
        create_pool = AsyncMock(return_value=pool)
        with patch.dict(sys.modules, {"asyncpg": SimpleNamespace(create_pool=create_pool)}), \
                patch("postgres_inventory.postgres_connection_kwargs", return_value={"host": "synthetic"}):
            self.assertIs(await reader._pool(), pool)
            self.assertIs(await reader._pool(), pool)
        create_pool.assert_awaited_once()
        self.assertEqual(create_pool.await_args.kwargs["max_size"], 2)
        self.assertEqual(create_pool.await_args.kwargs["command_timeout"], 5)
        await reader.close()
        self.assertTrue(pool.closed)


if __name__ == "__main__":
    unittest.main()
