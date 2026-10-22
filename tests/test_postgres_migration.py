"""Offline migration contracts independent of a PostgreSQL service/provider."""
import argparse
import base64
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

import numpy as np

from migrate_postgres import (
    Migration, archive_payload, batches, cache_records, classify_source_files, file_manifest,
    graph_records, iter_object, preserve_backlog, project, timestamp, vector_tables,
    validate_control_root, validate_report_path, workspace_namespace,
)
from worker import load_nano_storage_bounded


class MigrationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path

    def test_workspace_identity_cannot_merge_punctuation(self):
        values = [workspace_namespace(name) for name in ("a-b", "a_b", "a.b", "A-b")]
        self.assertEqual(len(set(values)), 4)
        self.assertEqual(values[0], "ws_" + hashlib.sha256(b"a-b").hexdigest()[:32])

    def test_vector_tables_isolate_every_workspace_and_model(self):
        a = vector_tables("text-embedding-3-large", 3072, workspace_namespace("a-b"))
        b = vector_tables("text-embedding-3-large", 3072, workspace_namespace("a_b"))
        c = vector_tables("another model", 3072, workspace_namespace("a-b"))
        self.assertFalse(set(a.values()) & set(b.values()))
        self.assertFalse(set(a.values()) & set(c.values()))
        self.assertTrue(all(len(name) <= 63 for name in a.values()))

    def test_stream_json_preserves_unicode_and_nested_metadata(self):
        value = {"ид": {"text": "ñ\x00🐈", "nested": [True, None, 7, {"arbitrary": 0.125}]}}
        self.assertEqual(dict(iter_object(self.write("kv.json", value))), value)
        payload = archive_payload(value)
        self.assertEqual(json.loads(payload), value)

    def test_stream_json_rejects_duplicate_record_ids(self):
        path = self.root / "duplicate.json"
        path.write_text('{"same":{},"same":{"changed":true}}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            list(iter_object(path))

    def test_legacy_cache_flattens_without_losing_original_metadata(self):
        value = {"hybrid": {"hash": {"return": "ответ", "original_prompt": "query", "cache_type": "query", "unknown": [1, 2]}}}
        result = list(cache_records(self.write("cache.json", value)))
        self.assertEqual(result, [("hybrid:query:hash", value["hybrid"]["hash"], "llm_response_cache/hybrid", "hash")])

    def test_modern_and_legacy_cache_can_coexist(self):
        record = {"return": "yes", "original_prompt": "q"}
        result = list(cache_records(self.write("cache.json", {"hybrid": {"old": record}, "mix:query:new": record})))
        self.assertEqual([row[0] for row in result], ["hybrid:extract:old", "mix:query:new"])

    def test_cache_conversion_refuses_colliding_ids(self):
        record = {"return": "yes"}
        path = self.write("cache.json", {"hybrid": {"hash": record}, "hybrid:extract:hash": record})
        with self.assertRaises(ValueError):
            list(cache_records(path))

    def test_source_manifest_stable_and_detects_content_changes(self):
        self.write("kv.json", {"a": 1})
        before = file_manifest(self.root)
        self.assertEqual(before, file_manifest(self.root))
        self.write("kv.json", {"a": 2})
        self.assertNotEqual(before["sha256"], file_manifest(self.root)["sha256"])

    def test_source_manifest_rejects_symlink(self):
        self.write("kv.json", {})
        (self.root / "alias").symlink_to(self.root / "kv.json")
        with self.assertRaisesRegex(ValueError, "symbolic"):
            file_manifest(self.root)

    def test_native_nano_float32_bits_and_original_metadata(self):
        vectors = np.array([[0.0, -0.0, 1.125], [-3.0, 0.125, 20.0]], dtype=np.float32)
        records = [{"__id__": "a", "extra": {"metadata": "🙂"}}, {"__id__": "b", "__created_at__": 1234567890.25}]
        path = self.write("vdb.json", {"embedding_dim": 3, "data": records, "matrix": base64.b64encode(vectors.tobytes()).decode()})
        loaded = load_nano_storage_bounded(str(path), chunk_size=3)
        self.assertEqual(loaded["data"], records)
        self.assertEqual(loaded["matrix"].tobytes(), vectors.tobytes())

    def test_relationship_vector_maps_entity_endpoints_and_chunk_provenance(self):
        original = {"src_id": "A", "tgt_id": "Ω", "source_id": "chunk-1<SEP>chunk-2", "content": "relation"}
        row = project("relationships", "rel-id", original, "ws_test")
        self.assertEqual(row["source_id"], "A")
        self.assertEqual(row["target_id"], "Ω")
        self.assertEqual(row["chunk_ids"], ["chunk-1", "chunk-2"])
        self.assertEqual(original["source_id"], "chunk-1<SEP>chunk-2")

    def test_entity_vector_retains_source_chunks(self):
        row = project("entities", "ent-id", {"entity_name": "name", "source_id": "a<SEP>b"}, "ws_test")
        self.assertEqual(row["chunk_ids"], ["a", "b"])

    def test_status_timestamps_use_utc_without_overwriting_original(self):
        value = {"created_at": "2026-10-22T10:22:33.123456+02:00", "updated_at": "2026-10-22T09:00:00Z", "status": "processing", "private_extra": {"kept": True}}
        row = project("doc_status", "doc", value, "ws_test")
        self.assertEqual(row["created_at"], dt.datetime(2026, 10, 22, 8, 22, 33, 123456))
        self.assertEqual(row["status"], "processing")
        self.assertEqual(json.loads(archive_payload(value)), value)

    def test_upstream_second_precision_rounds_consistently(self):
        self.assertEqual(timestamp(0.8, seconds=True), dt.datetime(1970, 1, 1, 0, 0, 1))
        self.assertEqual(timestamp(0.2, seconds=True), dt.datetime(1970, 1, 1))

    def test_graphml_scalar_types_unicode_and_python_canonical_edge_order(self):
        path = self.root / "graph.graphml"
        path.write_text('''<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
          <key id="f" for="all" attr.name="weight" attr.type="double"/>
          <key id="s" for="node" attr.name="description" attr.type="string"/>
          <key id="b" for="node" attr.name="flag" attr.type="boolean"/>
          <graph edgedefault="undirected"><node id="Ω"><data key="s">Привет</data><data key="f">1.25</data><data key="b">true</data></node>
          <node id="a"/><edge source="Ω" target="a"><data key="f">2.5</data></edge></graph></graphml>''')
        rows = list(graph_records(path))
        self.assertEqual(rows[0], ("graph_nodes", "Ω", {"description": "Привет", "weight": 1.25, "flag": True}))
        self.assertEqual(rows[-1], ("graph_edges", '["a","Ω"]', {"weight": 2.5}))

    def test_directed_graph_fails_instead_of_losing_edges(self):
        path = self.root / "directed.graphml"
        path.write_text('<graphml><graph edgedefault="directed"><node id="a"/></graph></graphml>')
        with self.assertRaisesRegex(ValueError, "Directed"):
            list(graph_records(path))

    def test_unsupported_graphml_content_fails_instead_of_skipping(self):
        bodies = [
            '<graph edgedefault="undirected"><data key="x">metadata</data></graph>',
            '<graph edgedefault="undirected"><hyperedge id="h"/></graph>',
            '<graph edgedefault="undirected"><node id="a"><port name="p"/></node></graph>',
            '<graph edgedefault="undirected"><node id="a"><data key="x"><custom>nested</custom></data></node></graph>',
            '<graph edgedefault="undirected"/><graph edgedefault="undirected"/>',
            '<graph edgedefault="undirected"><node id="a"><graph edgedefault="undirected"/></node></graph>',
        ]
        for body in bodies:
            with self.subTest(body=body):
                path = self.root / "unsupported.graphml"
                path.write_text('<graphml>' + body + '</graphml>')
                with self.assertRaises(ValueError):
                    list(graph_records(path))

    def test_source_selection_includes_empty_explicit_workspaces(self):
        (self.root / "a-b").mkdir()
        args = argparse.Namespace(source_root=self.root, workspace=["a-b"], embedding_model="model", embedding_dim=3072, target_database="new", target_token=str(uuid.uuid4()))
        self.assertEqual(Migration(args).sources(), [("a-b", (self.root / "a-b").resolve())])

    def test_batches_bound_record_payloads(self):
        self.assertEqual(list(batches(range(7), 3)), [[0, 1, 2], [3, 4, 5], [6]])

    def test_all_upstream_inflight_statuses_preserve_read_only_backlog(self):
        for status in ("pending", "processing", "parsing", "analyzing", "preprocessed"):
            with self.subTest(status=status):
                self.assertTrue(preserve_backlog([{"status": status, "count": 1}]))
                self.assertFalse(preserve_backlog([{"status": status, "count": 0}]))
        self.assertFalse(preserve_backlog([{"status": "processed", "count": 10}, {"status": "failed", "count": 2}]))

    def test_unknown_store_files_fail_closed(self):
        for name in ("kv_store_unknown.json", "vdb_custom.json", "graph_other.graphml", "nested/kv_store_full_docs.json"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Unsupported source store"):
                classify_source_files({"files": [{"path": name}]})

    def test_nonstore_artifacts_are_explicitly_retained_on_source_filesystem(self):
        manifest = {"files": [{"path": "kv_store_full_docs.json"}, {"path": "parser/diagram.png"}, {"path": "import.log"}]}
        self.assertEqual(classify_source_files(manifest), ["parser/diagram.png", "import.log"])

    def test_control_root_rejects_symlink_in_existing_ancestry(self):
        actual = self.root / "actual"
        actual.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ancestors"):
            validate_control_root(linked / "control", ["workspace"], "db", "token")

    def test_control_root_rejects_workspace_symlink(self):
        root = self.root.resolve() / "control"
        root.mkdir()
        actual = self.root / "actual"
        actual.mkdir()
        (root / "workspace").symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            validate_control_root(root, ["workspace"], "db", "token")

    def test_control_root_refuses_foreign_manifest(self):
        root = self.root.resolve() / "control"
        directory = root / "workspace"
        directory.mkdir(parents=True)
        path = directory / ".postgres-workspace.json"
        path.write_text(json.dumps({"workspace_id": "workspace", "database": "other", "db_instance_id": "other-token"}))
        with self.assertRaisesRegex(ValueError, "another database"):
            validate_control_root(root, ["workspace"], "db", "token")
        path.write_text(json.dumps({"workspace_id": "workspace", "database": "db", "db_instance_id": "token"}))
        validate_control_root(root, ["workspace"], "db", "token")

    def test_control_root_refuses_unowned_existing_content(self):
        root = self.root.resolve() / "control"
        directory = root / "workspace"
        directory.mkdir(parents=True)
        (directory / "existing.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "ownership"):
            validate_control_root(root, ["workspace"], "db", "token")

    def test_report_cannot_modify_source_after_final_verification(self):
        source = self.root / "source"
        source.mkdir()
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_report_path(source, source / "report.json")
        alias = self.root / "source-alias"
        alias.symlink_to(source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_report_path(source, alias / "report.json")
        validate_report_path(source, self.root / "outside.json")
        validate_report_path(source, None)


if __name__ == "__main__":
    unittest.main()
