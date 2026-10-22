"""Storage selection, migration binding, and namespace safety without providers."""

import asyncio
from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

import postgres_backend as pg


class PostgresIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace_id = "meeting-notes"
        self.manifest = {
            "schema_version": 1, "workspace_id": self.workspace_id,
            "postgres_workspace": pg.workspace_namespace(self.workspace_id), "status": "ready",
            "embedding_dim": 3072, "embedding_model": "original-model",
            "database": "migration_destination", "db_instance_id": "77a749ae-35ca-4523-8f9b-234dd40ed2c4",
            "preserve_backlog": False, "read_only": False,
            "source_manifest_sha256": "a" * 64, "counts": {"documents": 0},
        }
        self.path = self.root / pg.MANIFEST_FILENAME
        self.write_manifest()
        self.env = {
            "POSTGRES_HOST": "postgres.internal", "POSTGRES_USER": "workspace_user",
            "POSTGRES_PASSWORD": "retained-secret", "POSTGRES_DATABASE": "migration_destination",
            "EMBEDDING_MODEL": "original-model", "EMBEDDING_DIM": "3072",
            "LLM_BINDING_API_KEY": "original-provider-key", "POSTGRES_WORKSPACE": "unsafe-shared-override",
        }

    def write_manifest(self):
        self.path.write_text(json.dumps(self.manifest))

    def test_file_backend_is_default_and_unknown_values_fail_closed(self):
        self.assertEqual(pg.selected_backend({}), "file")
        self.assertEqual(pg.selected_backend({"LIGHTRAG_STORAGE_BACKEND": "POSTGRES"}), "postgres")
        for value in ("postgresql", "", "fallback"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pg.selected_backend({"LIGHTRAG_STORAGE_BACKEND": value})

    def test_hyphens_case_and_underscores_remain_distinct_stable_namespaces(self):
        identifiers = ["meeting-notes", "meeting_notes", "Meeting-Notes", "meeting.notes"]
        namespaces = [pg.workspace_namespace(name) for name in identifiers]
        self.assertEqual(len(set(namespaces)), 4)
        self.assertEqual(namespaces[0], pg.workspace_namespace("meeting-notes"))
        for value in ("../bad", "a/b", "", "é"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pg.workspace_namespace(value)

    def test_existing_empty_workspace_is_ready_without_manufacturing_rows(self):
        self.assertEqual(pg.load_workspace_manifest(self.path, self.workspace_id), self.manifest)
        result = pg.configure_postgres_workspace(self.root, self.workspace_id, self.env)
        self.assertEqual(result["counts"]["documents"], 0)
        self.assertEqual(self.env["POSTGRES_WORKSPACE"], self.manifest["postgres_workspace"])
        self.assertEqual(self.env["WORKSPACE"], "")
        self.assertEqual(self.env["POSTGRES_MAX_CONNECTIONS"], "2")
        self.assertEqual(self.env["POSTGRES_VCHORDRQ_EPSILON"], "2.3")
        self.assertEqual(self.env["LLM_BINDING_API_KEY"], "original-provider-key")
        self.assertEqual(self.env["POSTGRES_PASSWORD"], "retained-secret")
        self.assertEqual(self.env["LIGHTRAG_GRAPH_STORAGE"], "PGTableGraphStorage")

    def test_identity_incomplete_receipts_and_half_precision_configuration_are_rejected(self):
        changes = {"schema_version": True, "workspace_id": "other", "postgres_workspace": "meeting_notes",
                   "status": "importing", "embedding_dim": 0, "embedding_model": "", "database": "",
                   "db_instance_id": "not-a-uuid", "source_manifest_sha256": "partial", "counts": [],
                   "preserve_backlog": "false", "read_only": None}
        for key, value in changes.items():
            with self.subTest(key=key):
                changed = dict(self.manifest, **{key: value})
                self.path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    pg.load_workspace_manifest(self.path, self.workspace_id)
        self.write_manifest()
        self.env["POSTGRES_VECTOR_INDEX_TYPE"] = "HNSW_HALFVEC"
        before = copy.deepcopy(self.env)
        with self.assertRaises(ValueError):
            pg.configure_postgres_workspace(self.root, self.workspace_id, self.env)
        self.assertEqual(self.env, before)

    def test_mismatched_database_model_dimension_and_unbounded_pool_refuse_atomically(self):
        for key, value in {"POSTGRES_DATABASE": "existing_other_db", "EMBEDDING_MODEL": "new-model",
                           "EMBEDDING_DIM": "1536", "POSTGRES_MAX_CONNECTIONS": "50",
                           "POSTGRES_VCHORDRQ_EPSILON": "nan", "POSTGRES_VCHORDRQ_PROBES": "1; SELECT 1"}.items():
            with self.subTest(key=key):
                configured = dict(self.env, **{key: value})
                before = dict(configured)
                with self.assertRaises(ValueError):
                    pg.configure_postgres_workspace(self.root, self.workspace_id, configured)
                self.assertEqual(configured, before)

    def test_manifest_symlinks_and_oversized_receipts_are_rejected(self):
        other = self.root / "other.json"
        self.path.rename(other)
        self.path.symlink_to(other)
        with self.assertRaises(ValueError):
            pg.load_workspace_manifest(self.path, self.workspace_id)
        self.path.unlink()
        self.path.write_bytes(b" " * (64 * 1024 + 1))
        with self.assertRaises(ValueError):
            pg.load_workspace_manifest(self.path, self.workspace_id)

    def test_explicit_database_credentials_and_validated_ssl_are_required(self):
        for name in ("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_DATABASE"):
            value = dict(self.env)
            del value[name]
            with self.subTest(name=name), self.assertRaises(ValueError):
                pg.postgres_connection_kwargs(value)
        for value in ("0", "65536", "bad"):
            with self.subTest(port=value), self.assertRaises(ValueError):
                pg.postgres_connection_kwargs(dict(self.env, POSTGRES_PORT=value))
        kwargs = pg.postgres_connection_kwargs(dict(self.env, POSTGRES_SSL_MODE="verify-full"))
        self.assertTrue(kwargs["ssl"].check_hostname)
        self.assertEqual(kwargs["database"], "migration_destination")
        with self.assertRaises(ValueError):
            pg.postgres_connection_kwargs(dict(self.env, POSTGRES_SSL_MODE="typo"))

    def test_vector_indexes_are_separate_for_each_workspace_model_and_dimension(self):
        original = pg.vector_collection_suffix("original-model", 3072, pg.workspace_namespace("meeting-notes"))
        changed = [pg.vector_collection_suffix("original-model", 3072, pg.workspace_namespace("meeting_notes")),
                   pg.vector_collection_suffix("other-model", 3072, pg.workspace_namespace("meeting-notes")),
                   pg.vector_collection_suffix("original-model", 1536, pg.workspace_namespace("meeting-notes"))]
        self.assertNotIn(original, changed)
        names = pg.vector_table_names(self.manifest)
        self.assertEqual(len(names), 3)
        self.assertTrue(all(len(table) <= 63 and table.endswith(original) for table in names.values()))

    def test_vector_namespace_hook_leaves_provider_model_untouched(self):
        lightrag = ModuleType("lightrag")
        lightrag.__version__ = "1.5.7"
        implementation = ModuleType("lightrag.kg.postgres_impl")
        class Storage:
            def _generate_collection_suffix(self):
                return "upstream"
        implementation.PGVectorStorage = Storage
        with patch.dict("sys.modules", {"lightrag": lightrag, "lightrag.kg.postgres_impl": implementation}), \
                patch.dict("os.environ", {"POSTGRES_WORKSPACE": self.manifest["postgres_workspace"]}):
            pg.install_postgres_vector_namespace()
            storage = Storage()
            storage.embedding_func = SimpleNamespace(model_name="original-model", embedding_dim=3072)
            self.assertEqual(storage._generate_collection_suffix(), pg.vector_collection_suffix(
                "original-model", 3072, self.manifest["postgres_workspace"]))
            self.assertEqual(storage.embedding_func.model_name, "original-model")


class PostgresReadinessTests(unittest.TestCase):
    setUp = PostgresIdentityTests.setUp
    write_manifest = PostgresIdentityTests.write_manifest

    class Connection:
        def __init__(self, manifest, *, change_marker=None, change_registry=None, missing_table=False,
                     indexed=True, vector_type="vector(3072)", extensions=("vector", "vchord")):
            self.manifest = manifest
            self.marker = {"database": manifest["database"], "db_instance_id": UUID(manifest["db_instance_id"]),
                           "schema_version": 1}
            self.record = {name: manifest[name] for name in (
                "postgres_workspace", "db_instance_id", "status", "embedding_model", "embedding_dim")}
            self.marker.update(change_marker or {})
            self.record.update(change_registry or {})
            self.missing_table, self.indexed, self.vector_type = missing_table, indexed, vector_type
            self.extensions, self.closed, self.readonly, self.queries = extensions, False, False, []

        @asynccontextmanager
        async def transaction(self, *, readonly):
            self.readonly = readonly
            yield

        async def execute(self, query):
            self.queries.append(query)

        async def fetchrow(self, query, *args):
            self.queries.append(query)
            if "lightrag_workspace_database" in query:
                return self.marker
            if "lightrag_workspace_registry" in query:
                return self.record
            return {"vector_type": self.vector_type, "indexed": self.indexed}

        async def fetch(self, query):
            return [{"extname": name} for name in self.extensions]

        async def fetchval(self, query, name):
            return not self.missing_table

        async def close(self):
            self.closed = True

    def check(self, **kwargs):
        connection = self.Connection(self.manifest, **kwargs)
        async def connect(**options):
            self.assertEqual(options["database"], self.manifest["database"])
            return connection
        try:
            asyncio.run(pg.validate_postgres_readiness(self.manifest, env=self.env, connect=connect))
        finally:
            self.assertTrue(connection.closed)
            self.assertTrue(connection.readonly)
            self.assertFalse(any("CREATE " in query or "INSERT " in query for query in connection.queries))
        return connection

    def test_completed_migration_is_checked_read_only_and_connection_closed(self):
        self.check()

    def test_wrong_database_unfinished_rows_missing_tables_or_invalid_indexes_fail_closed(self):
        cases = [{"change_marker": {"db_instance_id": "different"}},
                 {"change_marker": {"database": "another_database"}},
                 {"change_registry": {"status": "importing"}},
                 {"change_registry": {"postgres_workspace": pg.workspace_namespace("other")}},
                 {"missing_table": True}, {"indexed": False}, {"vector_type": "halfvec(3072)"},
                 {"extensions": ("vector",)}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(RuntimeError):
                self.check(**case)


if __name__ == "__main__":
    unittest.main()
