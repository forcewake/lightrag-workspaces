"""Read-only graph admission and native pool lifecycle, without model calls."""

import asyncio
from contextlib import asynccontextmanager
import copy
import hashlib
import inspect
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import postgres_startup as startup


WORKSPACE = "ws_" + "a" * 32
MANIFEST = {"postgres_workspace": WORKSPACE, "database": "synthetic_migration"}


class GraphConnection:
    def __init__(self):
        # Independent fixture of the schema created by pinned upstream _DDL.
        types = {"workspace": "text", "namespace": "text", "properties": "jsonb",
                 "updated_at": "timestamp with time zone"}
        tables = {"lightrag_graph_nodes": dict(types, id="text"),
                  "lightrag_graph_edges": dict(types, src_id="text", tgt_id="text")}
        self.columns = [{"table_name": table, "column_name": name, "data_type": data_type,
                         "not_null": True, "relkind": "r", "resolves_to_public": True}
                        for table, fields in tables.items() for name, data_type in fields.items()]
        self.constraints = [
            {"table_name": "lightrag_graph_nodes", "kind": "p", "validated": True,
             "deferrable": False, "columns": ["workspace", "namespace", "id"], "index_ready": True},
            {"table_name": "lightrag_graph_edges", "kind": "p", "validated": True,
             "deferrable": False, "columns": ["workspace", "namespace", "src_id", "tgt_id"],
             "index_ready": True},
            *[{"table_name": "lightrag_graph_edges", "kind": "f", "validated": True,
               "deferrable": False, "columns": ["workspace", "namespace", endpoint],
               "delete_action": "c", "references_nodes": True,
               "referenced_columns": ["workspace", "namespace", "id"]}
              for endpoint in ("src_id", "tgt_id")],
        ]
        self.indexes = [{"valid": True, "ready": True, "method": "btree", "unconditional": True,
                         "columns": ["workspace", "namespace", "tgt_id"], "key_count": 3}]
        self.noncanonical = False
        self.queries = []
        self.readonly = []
        self.error = None

    @asynccontextmanager
    async def transaction(self, *, readonly):
        self.readonly.append(readonly)
        yield

    async def execute(self, sql):
        if sql != "SET LOCAL statement_timeout = '10s'":
            raise AssertionError("Graph startup attempted a write")
        self.queries.append(sql)

    async def fetch(self, sql):
        self.queries.append(sql)
        if self.error:
            raise self.error
        if "FROM pg_class c JOIN pg_attribute" in sql:
            return copy.deepcopy(self.columns)
        if "FROM pg_constraint c JOIN pg_class" in sql:
            return copy.deepcopy(self.constraints)
        if "FROM pg_index i JOIN pg_class" in sql:
            return copy.deepcopy(self.indexes)
        raise AssertionError(f"Unexpected catalog query: {sql}")

    async def fetchval(self, sql, workspace, namespace):
        self.queries.append(sql)
        if "FROM public.lightrag_graph_edges" not in sql:
            raise AssertionError("Unexpected data query")
        if workspace != WORKSPACE or namespace != "chunk_entity_relation":
            raise AssertionError("Graph check crossed a workspace or namespace boundary")
        return self.noncanonical


class GraphSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_graph_only_reads_catalogs_and_selected_workspace(self):
        connection = GraphConnection()
        await startup.validate_postgres_graph_schema(connection, WORKSPACE)
        self.assertEqual(len(connection.queries), 4)
        for sql in connection.queries:
            self.assertTrue(sql.strip().startswith("SELECT"))
            self.assertNotIn("pg_advisory", sql)
            self.assertNotIn("FOR UPDATE", sql)

    async def test_absent_legacy_or_shadowed_graph_schema_fails_closed(self):
        cases = [{"data_type": "varchar(255)"}, {"not_null": False},
                 {"resolves_to_public": False}, {"relkind": "v"}]
        for changes in cases:
            connection = GraphConnection()
            connection.columns[0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, "offline migration"):
                await startup.validate_postgres_graph_schema(connection, WORKSPACE)
        for name in ("workspace", "namespace", "id", "properties", "updated_at"):
            connection = GraphConnection()
            connection.columns = [row for row in connection.columns if row["column_name"] != name]
            with self.subTest(missing=name), self.assertRaisesRegex(RuntimeError, "missing or incompatible"):
                await startup.validate_postgres_graph_schema(connection, WORKSPACE)

    async def test_primary_keys_require_workspace_namespace_and_valid_unique_index(self):
        changes = [{"columns": ["workspace", "id"]}, {"index_ready": False},
                   {"deferrable": True}, {"validated": False}]
        for change in changes:
            connection = GraphConnection()
            connection.constraints[0].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, "primary key"):
                await startup.validate_postgres_graph_schema(connection, WORKSPACE)

    async def test_endpoint_foreign_keys_preserve_namespace_and_delete_integrity(self):
        changes = [{"columns": ["workspace", "src_id"]}, {"references_nodes": False},
                   {"referenced_columns": ["workspace", "id"]}, {"validated": False},
                   {"deferrable": True}, {"delete_action": "a"}]
        for endpoint in (2, 3):
            for change in changes:
                connection = GraphConnection()
                connection.constraints[endpoint].update(change)
                with self.subTest(endpoint=endpoint, change=change), self.assertRaisesRegex(RuntimeError, "foreign key"):
                    await startup.validate_postgres_graph_schema(connection, WORKSPACE)
            connection = GraphConnection()
            del connection.constraints[endpoint]
            with self.assertRaisesRegex(RuntimeError, "foreign key"):
                await startup.validate_postgres_graph_schema(connection, WORKSPACE)

    async def test_missing_invalid_or_partial_reverse_index_is_not_silently_rebuilt(self):
        changes = [{"valid": False}, {"ready": False}, {"unconditional": False},
                   {"method": "hash"}, {"columns": ["workspace", "tgt_id"]}, {"key_count": 4}]
        for change in changes:
            connection = GraphConnection()
            connection.indexes[0].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, "reverse-endpoint"):
                await startup.validate_postgres_graph_schema(connection, WORKSPACE)
        connection.indexes = []
        with self.assertRaisesRegex(RuntimeError, "reverse-endpoint"):
            await startup.validate_postgres_graph_schema(connection, WORKSPACE)

    async def test_legacy_edge_data_is_rejected_without_normalizing_it(self):
        connection = GraphConnection()
        connection.noncanonical = True
        with self.assertRaisesRegex(RuntimeError, "noncanonical legacy edges"):
            await startup.validate_postgres_graph_schema(connection, WORKSPACE)
        self.assertTrue(connection.noncanonical)
        self.assertTrue(all(sql.strip().startswith("SELECT") for sql in connection.queries))

    async def test_invalid_namespace_fails_before_reading(self):
        for workspace, namespace in (("unregistered", "chunk_entity_relation"), (WORKSPACE, "other_graph")):
            connection = GraphConnection()
            with self.subTest(workspace=workspace, namespace=namespace), self.assertRaises(RuntimeError):
                await startup.validate_postgres_graph_schema(connection, workspace, namespace)
            self.assertEqual(connection.queries, [])


class GraphStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = GraphConnection()
        connection = self.connection

        class DB:
            workspace = WORKSPACE
            database = MANIFEST["database"]

            async def _run_with_retry(self, operation):
                return await operation(connection)

        self.db = DB()
        self.manager = SimpleNamespace(get_client=AsyncMock(return_value=self.db), release_client=AsyncMock())
        manager = self.manager

        class Graph:
            def __init__(self):
                self.db = None
                self.workspace = ""
                self.namespace = "chunk_entity_relation"
                self.global_config = {"vector_storage": "PGVectorStorage"}

            async def initialize(self):
                raise AssertionError("Native schema initializer must not execute")

            async def finalize(self):
                if self.db is not None:
                    await manager.release_client(self.db)
                    self.db = None

            async def upsert_node(self, *args):
                return "unchanged native write"

        self.Graph, self.DB = Graph, DB
        self.source_hash_patch = patch.object(startup, "GRAPH_INITIALIZE_SHA256",
            hashlib.sha256(inspect.getsource(Graph.initialize).encode()).hexdigest())
        self.source_hash_patch.start()
        self.addCleanup(self.source_hash_patch.stop)
        self.finalize, self.upsert = Graph.finalize, Graph.upsert_node
        lightrag = ModuleType("lightrag")
        lightrag.__version__ = "1.5.7"
        graph_module = ModuleType("lightrag.kg.pgtable_impl")
        graph_module.PGTableGraphStorage = Graph
        postgres_module = ModuleType("lightrag.kg.postgres_impl")
        postgres_module.ClientManager, postgres_module.PostgreSQLDB = self.manager, DB
        shared_module = ModuleType("lightrag.kg.shared_storage")
        self.lock = asyncio.Lock()
        shared_module.get_data_init_lock = lambda: self.lock
        utils = ModuleType("lightrag.utils")
        utils.logger = Mock()
        self.modules = {"lightrag": lightrag, "lightrag.kg.pgtable_impl": graph_module,
                        "lightrag.kg.postgres_impl": postgres_module,
                        "lightrag.kg.shared_storage": shared_module, "lightrag.utils": utils}
        self.patch = patch.dict("sys.modules", self.modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_native_pool_lifecycle_and_explicit_writes_are_preserved(self):
        self.assertTrue(startup.install_postgres_graph_startup(MANIFEST))
        installed = self.Graph.initialize
        self.assertFalse(startup.install_postgres_graph_startup(MANIFEST))
        self.assertIs(self.Graph.initialize, installed)
        graph = self.Graph()
        await graph.initialize()
        self.assertIs(graph.db, self.db)
        self.assertEqual(graph.workspace, WORKSPACE)
        self.manager.get_client.assert_awaited_once_with(vector_storage="PGVectorStorage")
        self.assertEqual(self.connection.readonly, [True])
        self.assertIs(self.Graph.finalize, self.finalize)
        self.assertIs(self.Graph.upsert_node, self.upsert)
        self.assertEqual(await graph.upsert_node("new", {}), "unchanged native write")
        # Repeated initialize must not leak another ClientManager reference.
        await graph.initialize()
        self.manager.get_client.assert_awaited_once()
        self.assertEqual(self.connection.readonly, [True])
        await graph.finalize()
        self.manager.release_client.assert_awaited_once_with(self.db)
        self.assertIsNone(graph.db)

    async def test_incompatible_graph_releases_client_and_retains_uninitialized_state(self):
        startup.install_postgres_graph_startup(MANIFEST)
        self.connection.columns = []
        graph = self.Graph()
        with self.assertRaisesRegex(RuntimeError, "missing or incompatible"):
            await graph.initialize()
        self.manager.release_client.assert_awaited_once_with(self.db)
        self.assertIsNone(graph.db)
        self.assertEqual(graph.workspace, "")
        self.assertFalse(self.lock.locked())

    async def test_cancellation_releases_acquired_client(self):
        startup.install_postgres_graph_startup(MANIFEST)
        self.connection.error = asyncio.CancelledError()
        graph = self.Graph()
        with self.assertRaises(asyncio.CancelledError):
            await graph.initialize()
        self.manager.release_client.assert_awaited_once_with(self.db)
        self.assertIsNone(graph.db)

    async def test_wrong_database_or_workspace_never_binds_graph(self):
        startup.install_postgres_graph_startup(MANIFEST)
        for name, value in (("database", "other_database"), ("workspace", "ws_" + "b" * 32)):
            original = getattr(self.db, name)
            setattr(self.db, name, value)
            self.manager.release_client.reset_mock()
            graph = self.Graph()
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "client differs"):
                await graph.initialize()
            self.manager.release_client.assert_awaited_once_with(self.db)
            self.assertIsNone(graph.db)
            setattr(self.db, name, original)
        self.assertEqual(self.connection.queries, [])

    async def test_wrong_graph_namespace_never_acquires_pool(self):
        startup.install_postgres_graph_startup(MANIFEST)
        graph = self.Graph()
        graph.namespace = "other_graph"
        with self.assertRaisesRegex(RuntimeError, "graph namespace"):
            await graph.initialize()
        self.manager.get_client.assert_not_awaited()

    async def test_already_bound_graph_cannot_switch_workspace(self):
        startup.install_postgres_graph_startup(MANIFEST)
        graph = self.Graph()
        graph.db, graph.workspace = self.db, "other_workspace"
        with self.assertRaisesRegex(RuntimeError, "already bound"):
            await graph.initialize()
        self.manager.get_client.assert_not_awaited()

    async def test_installed_hook_cannot_be_rebound_to_another_workspace(self):
        startup.install_postgres_graph_startup(MANIFEST)
        installed = self.Graph.initialize
        with self.assertRaisesRegex(RuntimeError, "already bound"):
            startup.install_postgres_graph_startup(dict(MANIFEST, postgres_workspace="ws_" + "b" * 32))
        self.assertIs(self.Graph.initialize, installed)

    async def test_source_drift_or_unavailable_source_refuses_before_patching(self):
        original = self.Graph.initialize
        with patch("postgres_startup.inspect.getsource", return_value="changed source"):
            with self.assertRaisesRegex(RuntimeError, "source SHA256 differs"):
                startup.install_postgres_graph_startup(MANIFEST)
        self.assertIs(self.Graph.initialize, original)
        with patch("postgres_startup.inspect.getsource", side_effect=OSError("source missing")):
            with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                startup.install_postgres_graph_startup(MANIFEST)
        self.assertIs(self.Graph.initialize, original)

    async def test_repeated_install_still_checks_original_source_and_version(self):
        startup.install_postgres_graph_startup(MANIFEST)
        installed = self.Graph.initialize
        with patch("postgres_startup.inspect.getsource", return_value="changed source"):
            with self.assertRaisesRegex(RuntimeError, "source SHA256 differs"):
                startup.install_postgres_graph_startup(MANIFEST)
        self.modules["lightrag"].__version__ = "1.5.8"
        with self.assertRaisesRegex(RuntimeError, "pinned LightRAG"):
            startup.install_postgres_graph_startup(MANIFEST)
        self.assertIs(self.Graph.initialize, installed)

    async def test_unknown_version_or_lifecycle_shape_fails_before_patch(self):
        for name, value in (("__version__", "1.5.8"),):
            setattr(self.modules["lightrag"], name, value)
            original = self.Graph.initialize
            with self.assertRaisesRegex(RuntimeError, "pinned LightRAG"):
                startup.install_postgres_graph_startup(MANIFEST)
            self.assertIs(self.Graph.initialize, original)
        self.modules["lightrag"].__version__ = "1.5.7"
        self.DB._run_with_retry = lambda operation: None
        with self.assertRaisesRegex(RuntimeError, "pinned LightRAG"):
            startup.install_postgres_graph_startup(MANIFEST)


if __name__ == "__main__":
    unittest.main()
