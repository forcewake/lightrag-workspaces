"""Offline contract tests; the separate container smoke uses real LightRAG."""
import copy
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import table_headers as headers


@dataclass
class Table:
    rows: list | None
    num_rows: int
    num_cols: int
    table_header: list | str | None
    html: str | None = None
    body_override: str | None = None
    caption: str = "keep caption"


def adapter(placeholder, spec):
    if spec.get("kind") == "html":
        return Table(None, 1, 1, "<thead>kept</thead>", html=spec["html"], body_override="kept")
    rows = [[str(c) for c in row] for row in spec.get("rows", [])]
    return Table(rows, len(rows), max(map(len, rows), default=0), spec.get("header"))


class TableHeadersTests(unittest.TestCase):
    def test_header_enters_body_once_and_metadata_survives_without_input_mutation(self):
        spec = {"kind": "pipe", "header": [["Metric", "значение"]], "rows": [["value", 17]]}
        before = copy.deepcopy(spec)
        result = headers._build_header_complete_table(adapter, "tb1", spec)
        self.assertEqual(result.rows, [["Metric", "значение"], ["value", "17"]])
        self.assertEqual(result.table_header, spec["header"])
        self.assertEqual((result.num_rows, result.num_cols, result.caption), (2, 2, "keep caption"))
        result.table_header[0][0] = "changed"
        result.rows[1][0] = "changed"
        self.assertEqual(spec, before)

    def test_data_row_equal_to_header_is_not_deduplicated(self):
        spec = {"kind": "pipe", "header": [["Name"]], "rows": [["Name"], ["Other"]]}
        result = headers._build_header_complete_table(adapter, "tb1", spec)
        self.assertEqual(result.rows, [["Name"], ["Name"], ["Other"]])

    def test_header_only_and_multirow_headers_remain_content(self):
        spec = {"kind": "pipe", "header": [["Group", ""], ["Name", "Value"]], "rows": []}
        result = headers._build_header_complete_table(adapter, "tb1", spec)
        self.assertEqual(result.rows, spec["header"])
        self.assertEqual((result.num_rows, result.num_cols), (2, 2))

    def test_html_and_headerless_tables_are_passed_through_exactly(self):
        for spec in ({"kind": "html", "html": "<table>unchanged</table>", "header": "ignored"},
                     {"kind": "pipe", "rows": [["one"]]}, {"kind": "pipe", "rows": [], "header": []}):
            expected = adapter("tb1", spec)
            original = Mock(return_value=expected)
            self.assertIs(headers._build_header_complete_table(original, "tb1", spec), expected)
            original.assert_called_once_with("tb1", spec)

    def test_invalid_header_is_rejected_before_adapter_or_input_changes(self):
        for header in ("<thead>foreign</thead>", ["row"], [[1]], [[]], {"wrong": "shape"}):
            spec = {"kind": "pipe", "rows": [["body"]], "header": header}
            before = copy.deepcopy(spec)
            original = Mock()
            with self.subTest(header=header), self.assertRaises(ValueError):
                headers._build_header_complete_table(original, "tb1", spec)
            original.assert_not_called()
            self.assertEqual(spec, before)

    def installation(self, version="1.5.7", source="verified source\n"):
        builder = SimpleNamespace(_build_table=adapter)
        modules = {"lightrag": SimpleNamespace(__version__=version),
                   "lightrag.parser.markdown.ir_builder": builder}
        return builder, patch.object(headers.importlib, "import_module", side_effect=modules.__getitem__), \
            patch.object(headers.inspect, "getsource", return_value=source), \
            patch.object(headers, "BUILD_TABLE_SHA256", hashlib.sha256(source.encode()).hexdigest())

    def test_installer_validates_and_is_idempotent(self):
        builder, modules, source, digest = self.installation()
        with modules, source as getsource, digest:
            self.assertTrue(headers.install_table_header_preservation())
            installed = builder._build_table
            self.assertFalse(headers.install_table_header_preservation())
            self.assertIs(builder._build_table, installed)
            self.assertIs(installed.__wrapped__, adapter)
            self.assertEqual(installed("tb1", {"header": [["H"]], "rows": [["D"]]}).rows, [["H"], ["D"]])
            self.assertEqual(getsource.call_count, 2)

    def test_version_and_source_drift_do_not_install(self):
        for version, source in (("1.5.8", "verified source\n"), ("1.5.7", "changed source\n")):
            builder, modules, getsource, _ = self.installation(version, source)
            with modules, getsource, self.assertRaises(RuntimeError):
                headers.install_table_header_preservation()
            self.assertIs(builder._build_table, adapter)

    def test_source_unavailable_does_not_install(self):
        builder, modules, _, _ = self.installation()
        with modules, patch.object(headers.inspect, "getsource", side_effect=OSError), self.assertRaises(RuntimeError):
            headers.install_table_header_preservation()
        self.assertIs(builder._build_table, adapter)

    def test_repeated_install_rechecks_version_and_original_source(self):
        builder, modules, source, digest = self.installation()
        with modules, source, digest:
            headers.install_table_header_preservation()
            installed = builder._build_table
            with patch.object(headers.inspect, "getsource", return_value="drift"), self.assertRaises(RuntimeError):
                headers.install_table_header_preservation()
            self.assertIs(builder._build_table, installed)


if __name__ == "__main__":
    unittest.main()
