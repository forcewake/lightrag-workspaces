"""Native-format equivalence using synthetic vectors only, never an AI service."""

import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import nano_vectordb.dbs as nano

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import install_bounded_nano_loader, load_nano_storage_bounded, normalize_nano_in_place


class BoundedNanoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "vectors.json"
        random = np.random.default_rng(42)
        self.matrix = random.normal(size=(17, 12)).astype(np.float32)
        self.data = [{"__id__": f"row-{index}", "content": "Text with \"quotes\", \\ slash /, 中文 😃",
                      "group": "odd" if index % 2 else "even", "nested": {"number": 12.5, "flag": True}}
                     for index in range(len(self.matrix))]

    def storage(self):
        return {"embedding_dim": self.matrix.shape[1], "data": self.data,
                "matrix": nano.array_to_buffer_string(self.matrix),
                "additional_data": {"kept": "\u2028test😃", "values": [None, False, 1, 2.5]}}

    def write(self, storage=None):
        self.path.write_text(json.dumps(self.storage() if storage is None else storage, ensure_ascii=False))

    def custom_database(self):
        with patch.object(nano, "load_storage", load_nano_storage_bounded), \
                patch.object(nano.NanoVectorDB, "pre_process", normalize_nano_in_place):
            return nano.NanoVectorDB(self.matrix.shape[1], storage_file=str(self.path))

    def test_pinned_installer_patches_only_loader_and_constructor_normalization(self):
        original_normalize = nano.normalize
        original_query = nano.NanoVectorDB.query
        original_save = nano.NanoVectorDB.save
        original_upsert = nano.NanoVectorDB.upsert
        with patch.object(nano, "load_storage", nano.load_storage), \
                patch.object(nano.NanoVectorDB, "pre_process", nano.NanoVectorDB.pre_process):
            install_bounded_nano_loader()
            self.assertIs(nano.load_storage, load_nano_storage_bounded)
            self.assertIs(nano.NanoVectorDB.pre_process, normalize_nano_in_place)
            self.assertIs(nano.normalize, original_normalize)
            self.assertIs(nano.NanoVectorDB.query, original_query)
            self.assertIs(nano.NanoVectorDB.upsert, original_upsert)
            self.assertIs(nano.NanoVectorDB.save, original_save)
        with patch("importlib.metadata.version", return_value="unexpected"), self.assertRaises(RuntimeError):
            install_bounded_nano_loader()

    def test_metadata_and_float32_bytes_match_stock_across_input_boundaries(self):
        self.write()
        original_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        expected = nano.load_storage(str(self.path))
        for chunk_size in (1, 2, 3, 7, 31, 64, 4096, 65536):
            with self.subTest(chunk_size=chunk_size):
                actual = load_nano_storage_bounded(str(self.path), chunk_size=chunk_size)
                self.assertEqual(actual["embedding_dim"], expected["embedding_dim"])
                self.assertEqual(actual["data"], expected["data"])
                self.assertEqual(actual["additional_data"], expected["additional_data"])
                self.assertEqual(actual["matrix"].dtype, np.float32)
                self.assertTrue(actual["matrix"].flags.writeable)
                self.assertEqual(actual["matrix"].tobytes(), expected["matrix"].tobytes())
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), original_hash)

    def test_escaped_keys_arbitrary_key_order_and_empty_matrix_are_supported(self):
        storage = self.storage()
        ordered = {"additional_data": storage["additional_data"], "matrix": storage["matrix"],
                   "data": storage["data"], "embedding_dim": storage["embedding_dim"]}
        raw = json.dumps(ordered, ensure_ascii=False, indent=2).replace('"data":', '"\\u0064ata":')
        self.path.write_text(raw)
        loaded = load_nano_storage_bounded(str(self.path), chunk_size=3)
        self.assertEqual(loaded["data"], self.data)
        self.assertEqual(loaded["matrix"].tobytes(), self.matrix.tobytes())
        self.write({"data": [], "matrix": "", "embedding_dim": 12, "extra": {"preserved": "yes"}})
        empty = load_nano_storage_bounded(str(self.path), chunk_size=1)
        self.assertEqual(empty["matrix"].shape, (0, 12))
        self.assertEqual(empty["extra"], {"preserved": "yes"})

    def test_normalization_query_ids_and_scores_are_exactly_equivalent(self):
        self.write()
        before = self.path.read_bytes()
        stock = nano.NanoVectorDB(12, storage_file=str(self.path))
        actual = self.custom_database()
        self.assertEqual(stock._NanoVectorDB__storage["matrix"].tobytes(), actual._NanoVectorDB__storage["matrix"].tobytes())
        query = np.arange(1, 13, dtype=np.float32)
        for filter_lambda in (None, lambda record: record["group"] == "odd"):
            expected = stock.query(query, top_k=8, filter_lambda=filter_lambda)
            observed = actual.query(query, top_k=8, filter_lambda=filter_lambda)
            self.assertEqual([item["__id__"] for item in observed], [item["__id__"] for item in expected])
            np.testing.assert_array_equal([item["__metrics__"] for item in observed],
                                          [item["__metrics__"] for item in expected])
        self.assertEqual(self.path.read_bytes(), before)

    def test_escaped_matrix_slashes_and_ascii_unicode_survive_every_boundary(self):
        storage = self.storage()
        raw = json.dumps(storage, ensure_ascii=False)
        base64_value = storage["matrix"]
        escaped = base64_value.replace("/", "\\/").replace("A", "\\u0041")
        self.path.write_text(raw.replace('"' + base64_value + '"', '"' + escaped + '"'))
        for size in (1, 2, 5, 17, 65536):
            with self.subTest(size=size):
                loaded = load_nano_storage_bounded(str(self.path), chunk_size=size)
                self.assertEqual(loaded["matrix"].tobytes(), self.matrix.tobytes())

    def test_zero_and_nonfinite_rows_keep_upstream_normalization_behavior(self):
        matrix = np.array([[0, 0, 0], [np.nan, 1, 2], [np.inf, 1, 2], [3, 4, 0]], dtype=np.float32)
        with np.errstate(divide="ignore", invalid="ignore"):
            expected = nano.normalize(matrix)
        database = SimpleNamespace(metric="cosine", _NanoVectorDB__storage={"matrix": matrix.copy()})
        normalize_nano_in_place(database, block_bytes=12)
        np.testing.assert_array_equal(database._NanoVectorDB__storage["matrix"], expected)
        self.assertEqual(database._NanoVectorDB__storage["matrix"].tobytes(), expected.tobytes())

    def test_original_upsert_delete_and_save_still_work_in_the_same_format(self):
        self.write()
        database = self.custom_database()
        database.upsert([{"__id__": "new", "__vector__": np.ones(12, dtype=np.float32), "content": "new"}])
        database.delete(["row-0"])
        database.save()
        standard = nano.NanoVectorDB(12, storage_file=str(self.path))
        self.assertEqual(len(standard), 17)
        self.assertEqual(standard.get(["new"])[0]["content"], "new")
        self.assertEqual(standard.get(["row-0"]), [])
        self.assertEqual(standard.get_additional_data(), self.storage()["additional_data"])

    def test_reads_are_bounded_and_do_not_require_whole_file_text(self):
        self.write()
        native_open = Path.open
        reads = []

        class BoundedStream:
            def __init__(self, stream):
                self.stream = stream

            def read(self, size=-1):
                self.assert_bounded(size)
                return self.stream.read(size)

            def assert_bounded(self, size):
                if not 0 < size <= 23:
                    raise AssertionError("Whole-file read attempted")
                reads.append(size)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

        def instrumented(path, *args, **kwargs):
            return BoundedStream(native_open(path, *args, **kwargs))

        with patch.object(Path, "open", instrumented):
            loaded = load_nano_storage_bounded(str(self.path), chunk_size=23)
        self.assertTrue(reads)
        self.assertEqual(loaded["matrix"].tobytes(), self.matrix.tobytes())

    def test_malformed_and_truncated_inputs_fail_without_modifying_bytes(self):
        valid = json.dumps(self.storage(), ensure_ascii=False)
        bad_inputs = [valid[:-1], valid[:-8], valid + " trailing", '{"data":[{},],"matrix":"","embedding_dim":12}',
                      '{"data":[],"matrix":"!!!!","embedding_dim":12}',
                      '{"data":[],"matrix":"AA=","embedding_dim":12}',
                      '{"data":[],"matrix":"AAAA","embedding_dim":12}',
                      '{"data":[],"matrix":"AAAAAA==","embedding_dim":12}',
                      '{"data":[],"matrix":"","embedding_dim":12,"embedding_dim":12}',
                      '{"data":[],"matrix":"\\u0000","embedding_dim":12}']
        for raw in bad_inputs:
            with self.subTest(raw_sha256=hashlib.sha256(raw.encode()).hexdigest()):
                self.path.write_text(raw)
                before = self.path.read_bytes()
                with self.assertRaises((ValueError, TypeError)):
                    load_nano_storage_bounded(str(self.path), chunk_size=7)
                self.assertEqual(self.path.read_bytes(), before)

    def test_missing_file_preserves_upstream_empty_initialization(self):
        self.assertIsNone(load_nano_storage_bounded(str(self.path)))
        empty = self.custom_database()
        self.assertEqual(len(empty), 0)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
