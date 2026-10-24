#!/usr/bin/env python3
"""Local-only behavioral tests for deterministic retrieval regression checks."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from test_lightrag_client import EVIDENCE, local_service


CHECK = Path(__file__).resolve().parents[1] / "scripts" / "lightrag_check.py"
ROUTE = ("POST", "/workspaces/research/query/data")
IDENTITY = {"LIGHTRAG-WORKSPACE": "research", "X-LightRAG-Workspace": "research"}


class RetrievalCheckTests(unittest.TestCase):
    def setUp(self):
        self.server_context = local_service()
        self.service = self.server_context.__enter__()
        self.addCleanup(self.server_context.__exit__, None, None, None)
        self.directory_context = tempfile.TemporaryDirectory()
        self.directory = Path(self.directory_context.__enter__())
        self.addCleanup(self.directory_context.__exit__, None, None, None)
        self.suite_file = self.directory / "suite.json"
        self.report_file = self.directory / "report.json"
        self.suite = {
            "schema_version": 1, "origin": self.service.url, "workspace": "research",
            "cases": [{"id": "recall", "question": "What recall did Study A report?",
                       "expected_sources": [{"basename": "study-a.pdf", "required_terms": ["73%", "stated test conditions"]}]}],
        }

    def invoke(self, *, env=None, timeout="2", raw_suite=None, extra=()):
        self.suite_file.write_text(raw_suite if raw_suite is not None else json.dumps(self.suite, ensure_ascii=False), encoding="utf-8")
        clean_env = {key: value for key, value in os.environ.items() if not key.startswith("LIGHTRAG_") and not key.lower().endswith("proxy")}
        clean_env.update(env or {})
        return subprocess.run([sys.executable, str(CHECK), "--suite", str(self.suite_file), "--report", str(self.report_file), "--timeout", timeout, *extra],
                              text=True, capture_output=True, env=clean_env, timeout=8)

    def test_changed_validated_suite_is_rejected_before_network_or_report(self):
        for option, value in (("--expected-suite-sha256", "0" * 64),
                              ("--expected-origin", "http://127.0.0.1:1"),
                              ("--expected-workspace", "another-workspace")):
            with self.subTest(option=option):
                result = self.invoke(extra=(option, value))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("changed after validation", result.stderr)
                self.assertEqual(self.service.requests, [])
                self.assertFalse(self.report_file.exists())

    def result(self, completed, expected_code=0):
        self.assertEqual(completed.returncode, expected_code, completed.stderr or completed.stdout)
        self.assertEqual(completed.stderr, "")
        summary = json.loads(completed.stdout)
        report = json.loads(self.report_file.read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], report["status"])
        self.assertEqual(summary["summary"], report["summary"])
        self.assertNotIn("evidence", summary)
        return report

    def override(self, body=None, **extras):
        self.service.responses[ROUTE] = {"headers": IDENTITY, "json": copy.deepcopy(body or EVIDENCE), **extras}

    def test_pass_retains_per_case_citations_and_never_generates_answer(self):
        report = self.result(self.invoke(env={"LIGHTRAG_URL": "http://127.0.0.1:1"}))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"], {"total": 1, "completed": 1, "passed": 1, "failed": 0, "not_run": 0})
        self.assertEqual([item["path"] for item in self.service.requests], ["/workspaces", "/workspaces/research/query/data"])
        source = report["cases"][0]["sources"][0]
        self.assertEqual(source["terms"][0]["citations"], [{"chunk_id": "chunk-r1", "reference_id": "1", "file_path": "papers/study-a.pdf"}])
        self.assertEqual(report["cases"][0]["evidence"]["chunks"][0]["content"], EVIDENCE["data"]["chunks"][0]["content"])
        self.assertGreaterEqual(report["elapsed_seconds"], report["cases"][0]["elapsed_seconds"])
        self.assertEqual(stat.S_IMODE(self.report_file.stat().st_mode), 0o600)
        self.assertEqual(len(report["suite_sha256"]), 64)
        self.assertIn("does not establish", report["limitation"])
        self.assertNotIn("entities", report["cases"][0]["evidence"])

    def test_other_source_or_answer_cannot_supply_expected_term(self):
        body = copy.deepcopy(EVIDENCE)
        body["data"]["chunks"][0]["content"] = "Study A did not give a numerical result."
        body["data"]["chunks"][1]["content"] = "73% under stated test conditions."
        body["response"] = "Study A reports 73% under stated test conditions."
        body["data"]["entities"][0]["description"] = body["response"]
        self.override(body)
        report = self.result(self.invoke(), 1)
        self.assertEqual([item["type"] for item in report["cases"][0]["failures"]], ["missing_term", "missing_term"])

    def test_required_terms_may_match_separate_chunks_of_same_cited_source(self):
        body = copy.deepcopy(EVIDENCE)
        body["data"]["chunks"][0]["content"] = "The recall was 73%."
        body["data"]["chunks"].append({"content": "Stated test conditions are documented here.", "file_path": "papers/study-a.pdf", "reference_id": "1", "chunk_id": "chunk-r3"})
        self.override(body)
        report = self.result(self.invoke())
        self.assertEqual(report["cases"][0]["sources"][0]["terms"][1]["citations"][0]["chunk_id"], "chunk-r3")

    def test_facts_only_in_known_generated_descriptions_cannot_pass(self):
        for kind in ("drawing", "table", "equation"):
            with self.subTest(kind=kind):
                if self.report_file.exists():
                    self.report_file.unlink()
                body = copy.deepcopy(EVIDENCE)
                cid = f"doc-synthetic-mm-{kind}-000"
                body["data"]["chunks"][0]["chunk_id"] = cid
                self.override(body)
                report = self.result(self.invoke(), 1)
                case = report["cases"][0]
                self.assertTrue(case["evidence"]["chunks"][0]["derived"])
                self.assertEqual(case["evidence"]["chunks"][0]["content"], EVIDENCE["data"]["chunks"][0]["content"])
                self.assertEqual(case["sources"][0]["excluded_derived_chunk_ids"], [cid])
                self.assertEqual(case["sources"][0]["chunk_ids"], [])
                self.assertFalse(any(term["found"] for term in case["sources"][0]["terms"]))

    def test_raw_and_generated_chunks_pass_using_only_raw_chunk_citations(self):
        body = copy.deepcopy(EVIDENCE)
        body["data"]["chunks"][0]["chunk_id"] = "doc-synthetic-chunk-000"
        generated = copy.deepcopy(body["data"]["chunks"][0])
        generated["chunk_id"] = "doc-synthetic-mm-table-000"
        body["data"]["chunks"].append(generated)
        self.override(body)
        case = self.result(self.invoke())["cases"][0]
        self.assertFalse(case["evidence"]["chunks"][0]["derived"])
        self.assertTrue(case["evidence"]["chunks"][-1]["derived"])
        source = case["sources"][0]
        self.assertEqual(source["chunk_ids"], ["doc-synthetic-chunk-000"])
        self.assertEqual(source["excluded_derived_chunk_ids"], ["doc-synthetic-mm-table-000"])
        for term in source["terms"]:
            self.assertEqual([citation["chunk_id"] for citation in term["citations"]], ["doc-synthetic-chunk-000"])

    def test_generated_description_cannot_complete_missing_fact_in_raw_chunk(self):
        body = copy.deepcopy(EVIDENCE)
        generated = copy.deepcopy(body["data"]["chunks"][0])
        generated["chunk_id"] = "doc-synthetic-mm-table-001"
        body["data"]["chunks"][0]["content"] = "Study A describes its stated test conditions."
        body["data"]["chunks"].append(generated)
        self.override(body)
        case = self.result(self.invoke(), 1)["cases"][0]
        self.assertEqual([failure["term"] for failure in case["failures"]], ["73%"])

    def test_unicode_normalization_casefold_and_question_survive(self):
        question = 'Что означает "Северный проект"? 引用; $(echo unsafe) `id`'
        self.suite["cases"][0]["question"] = question
        self.suite["cases"][0]["expected_sources"] = [{"basename": "café-данные.md", "required_terms": ["СЕВЕРНЫЙ ПРОЕКТ", "CAFÉ", "STRASSE"]}]
        body = {"status": "success", "data": {"chunks": [{"content": "Северный проект · cafe\u0301 · Straße", "chunk_id": "unicode-1", "reference_id": 1}],
                                                       "references": [{"reference_id": 1, "file_path": "资料/cafe\u0301-данные.md"}]}}
        self.override(body)
        self.result(self.invoke())
        self.assertEqual(self.service.requests[-1]["payload"]["query"], question)

    def test_patterns_are_only_literal_text_never_regex(self):
        self.suite["cases"][0]["expected_sources"][0]["required_terms"] = ["(a+)+$", ".*", "73%"]
        report = self.result(self.invoke(), 1)
        self.assertEqual([f["term"] for f in report["cases"][0]["failures"]], ["(a+)+$", ".*"])

    def test_expected_source_missing_despite_facts_elsewhere(self):
        self.suite["cases"][0]["expected_sources"][0]["basename"] = "absent.pdf"
        report = self.result(self.invoke(), 1)
        self.assertEqual(report["cases"][0]["failures"][0]["type"], "missing_source")

    def test_reference_mapping_authoritative_and_chunk_path_cannot_forge_source(self):
        body = copy.deepcopy(EVIDENCE)
        body["data"]["references"][0]["file_path"] = "private.md"
        self.override(body)
        report = self.result(self.invoke(), 1)
        self.assertEqual(report["cases"][0]["failures"][0]["type"], "evidence_contract")
        self.assertEqual(report["cases"][0]["evidence"], {"chunks": [], "references": []})

    def test_unresolved_reference_and_missing_chunk_id_are_not_usable_evidence(self):
        for field, value in (("reference_id", "missing"), ("chunk_id", None)):
            with self.subTest(field=field):
                if self.report_file.exists():
                    self.report_file.unlink()
                body = copy.deepcopy(EVIDENCE)
                body["data"]["chunks"][0][field] = value
                self.override(body)
                report = self.result(self.invoke(), 1)
                self.assertEqual(report["cases"][0]["failures"][0]["type"], "evidence_contract")

    def test_duplicate_reference_id_fails_even_if_terms_match(self):
        body = copy.deepcopy(EVIDENCE)
        body["data"]["references"].append({"reference_id": "1", "file_path": "other-study.pdf"})
        self.override(body)
        self.assertEqual(self.result(self.invoke(), 1)["cases"][0]["failures"][0]["type"], "evidence_contract")

    def test_malformed_source_url_is_recorded_as_failed_evidence(self):
        body = copy.deepcopy(EVIDENCE)
        body["data"]["references"][0]["file_path"] = "http://[broken"
        body["data"]["chunks"][0]["file_path"] = "http://[broken"
        self.override(body)
        report = self.result(self.invoke(), 1)
        self.assertEqual(report["cases"][0]["failures"][0]["type"], "evidence_contract")

    def test_same_basename_at_distinct_paths_is_ambiguous(self):
        body = copy.deepcopy(EVIDENCE)
        body["data"]["chunks"].append({"content": "73% and stated test conditions", "file_path": "elsewhere/study-a.pdf", "chunk_id": "chunk-r3", "reference_id": "3"})
        body["data"]["references"].append({"reference_id": "3", "file_path": "elsewhere/study-a.pdf"})
        self.override(body)
        self.assertIn("ambiguous_source", [f["type"] for f in self.result(self.invoke(), 1)["cases"][0]["failures"]])

    def test_http_200_failure_and_empty_evidence_do_not_pass(self):
        for body, failure_type in (({"status": "failure", "data": EVIDENCE["data"]}, "api_status"),
                                   ({"status": "success", "data": {"chunks": [], "references": []}}, "empty_chunks"),
                                   ({"status": "success", "data": {"references": []}}, "evidence_contract")):
            with self.subTest(failure_type=failure_type):
                if self.report_file.exists():
                    self.report_file.unlink()
                self.override(body)
                self.assertEqual(self.result(self.invoke(), 1)["cases"][0]["failures"][0]["type"], failure_type)

    def test_workspace_identity_failure_discards_content_and_stops_remaining_cases(self):
        second = copy.deepcopy(self.suite["cases"][0])
        second["id"] = "second"
        self.suite["cases"].append(second)
        self.override(headers={"LIGHTRAG-WORKSPACE": "private-notes", "X-LightRAG-Workspace": "private-notes"})
        report = self.result(self.invoke(), 1)
        self.assertEqual(report["not_run"], ["second"])
        self.assertEqual(report["cases"][0]["evidence"]["chunks"], [])
        self.assertIn("identity", report["cases"][0]["failures"][0]["detail"])
        self.assertEqual(len([r for r in self.service.requests if r["method"] == "POST"]), 1)

    def test_http_errors_do_not_retry_or_echo_server_body(self):
        self.override(status=500, json={"detail": "SECRET SENTINEL"})
        completed = self.invoke()
        report = self.result(completed, 1)
        self.assertNotIn("SECRET SENTINEL", self.report_file.read_text())
        self.assertEqual(len([r for r in self.service.requests if r["method"] == "POST"]), 1)
        self.assertEqual(report["cases"][0]["failures"][0]["type"], "request_error")

    def test_discovery_unavailable_saves_failure_without_queries(self):
        self.suite["workspace"] = "offline"
        report = self.result(self.invoke(), 1)
        self.assertEqual(report["summary"]["not_run"], 1)
        self.assertIn("unavailable", report["run_error"])
        self.assertEqual([r["path"] for r in self.service.requests], ["/workspaces"])

    def test_each_case_has_its_own_evidence_and_failed_oracle_does_not_stop_suite(self):
        self.suite["cases"][0]["expected_sources"][0]["required_terms"] = ["absent term"]
        self.suite["cases"].append({"id": "second", "question": "What recall did Study A report?", "expected_sources": [{"basename": "study-a.pdf", "required_terms": ["73%"]}]})
        report = self.result(self.invoke(), 1)
        self.assertEqual(report["summary"], {"total": 2, "completed": 2, "passed": 1, "failed": 1, "not_run": 0})
        self.assertFalse(report["cases"][0]["passed"])
        self.assertTrue(report["cases"][1]["passed"])

    def test_retrieval_options_forwarded_without_answer_flags(self):
        self.suite["retrieval"] = {"mode": "naive", "top_k": 3, "chunk_top_k": 5, "max_total_tokens": 4000, "enable_rerank": False}
        self.result(self.invoke())
        payload = self.service.requests[-1]["payload"]
        for key, value in self.suite["retrieval"].items():
            self.assertEqual(payload[key], value)
        self.assertTrue(payload["include_references"])
        self.assertNotIn("stream", payload)
        self.assertNotIn("conversation_history", payload)

    def test_existing_report_and_symlink_are_preserved_before_any_request(self):
        target = self.directory / "keep.json"
        target.write_text("preserve")
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                if self.report_file.exists() or self.report_file.is_symlink():
                    self.report_file.unlink()
                if symlink:
                    self.report_file.symlink_to(target)
                else:
                    self.report_file.write_text("preserve")
                completed = self.invoke()
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(self.report_file.read_text(), "preserve")
                self.assertEqual(target.read_text(), "preserve")
                self.assertEqual(self.service.requests, [])

    def test_invalid_suite_variants_stop_before_any_requests(self):
        baseline = copy.deepcopy(self.suite)
        variants = [dict(baseline, schema_version=True), dict(baseline, workspace="../research"), dict(baseline, cases=[]),
                    dict(baseline, origin=self.service.url + "/path"), dict(baseline, retrieval={"enable_rerank": "false"}),
                    dict(baseline, retrieval={"top_k": True}), dict(baseline, retrieval={"mode": "bypass"}),
                    dict(baseline, unexpected="field")]
        case = baseline["cases"][0]
        for modified in (dict(case, question=" "), dict(case, question="x" * 65537), dict(case, expected_sources=[]),
                         dict(case, expected_sources=[{"basename": "x.md", "required_terms": ["value"], "regex": True}]),
                         dict(case, expected_sources=[{"basename": "dir/x.md", "required_terms": ["value"]}]),
                         dict(case, expected_sources=[{"basename": "x.md", "required_terms": ["value", "VALUE"]}])):
            variants.append(dict(baseline, cases=[modified]))
        for index, suite in enumerate(variants):
            with self.subTest(index=index):
                self.suite = suite
                completed = self.invoke()
                self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)
                self.assertFalse(self.report_file.exists())
                self.assertEqual(self.service.requests, [])

    def test_duplicate_json_keys_cannot_silently_change_workspace(self):
        completed = self.invoke(raw_suite='{"workspace":"research","workspace":"private-notes"}')
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Duplicate JSON keys", completed.stderr)
        self.assertEqual(self.service.requests, [])


if __name__ == "__main__":
    unittest.main()
