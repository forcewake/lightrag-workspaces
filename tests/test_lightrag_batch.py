#!/usr/bin/env python3
"""End-to-end batch CLI tests using only a synthetic localhost HTTP service."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from test_lightrag_upload import local_service, IDENTITIES, PREFIX, UPLOAD, WORKSPACE

BATCH = Path(__file__).resolve().parents[1] / "scripts" / "lightrag_batch.py"


@contextmanager
def batch_service():
    with local_service() as service:
        url = service.url
        original_reply = service.reply
        service.accepted_files = []
        service.tracks = {}
        service.upload_disconnect = False
        service.bad_identity = False
        service.track_empty = False
        service.track_state = "processed"
        service.track_duplicate = False
        service.track_wrong_file = False
        service.track_bad_total = False
        def reply(request):
            path = request["path"]
            if path == UPLOAD:
                name = request["parts"][0]["filename"]
                track = f"batch-track-{len(service.accepted_files) + 1}"
                service.accepted_files.append(request["parts"][0])
                service.tracks[track] = name
                service.pages[1]["documents"].append({"id": track, "file_path": name, "status": "processing"})
                if service.upload_disconnect:
                    return {"disconnect": True}
                return {"headers": IDENTITIES, "json": {"status": "success", "track_id": track, "message": "Queued"}}
            if path.startswith(PREFIX + "/documents/track_status/"):
                track = path.rsplit("/", 1)[-1]
                name = service.tracks[track]
                documents = [] if service.track_empty else [{"id": track, "file_path": "wrong.md" if service.track_wrong_file else name,
                                                            "status": service.track_state, "chunks_count": 3,
                                                            "error_msg": "Synthetic parser failed" if service.track_state == "failed" else None,
                                                            "metadata": {"is_duplicate": True, "original_doc_id": "original"} if service.track_duplicate else {}}]
                return {"headers": {} if service.bad_identity else IDENTITIES,
                        "json": {"track_id": track, "total_count": 999 if service.track_bad_total else len(documents), "documents": documents}}
            return original_reply(request)
        service.reply = reply
        yield service, url


class BatchCLI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.sources = self.root / "sources"
        self.sources.mkdir()
        (self.sources / "one.md").write_text("# One\n\nSynthetic fact: copper lantern.\n", encoding="utf-8")
        (self.sources / "two.md").write_text("# Two\n\nSynthetic fact: 73 kites.\n", encoding="utf-8")
        self.environment = {k: v for k, v in os.environ.items() if not k.startswith("LIGHTRAG_")}

    def tearDown(self):
        self.directory.cleanup()

    def cli(self, *args, code=0, env=None):
        result = subprocess.run([sys.executable, str(BATCH), *map(str, args)], capture_output=True, text=True,
                                timeout=10, env=env or self.environment)
        self.assertEqual(result.returncode, code, result.stderr + result.stdout)
        return json.loads(result.stdout if result.stdout else result.stderr)

    def plan(self, url, name="run1", previous=None):
        arguments = ["--base-url", url, "plan", "--workspace", WORKSPACE, "--source-dir", self.sources, "--output-dir", self.root / name]
        if previous:
            arguments += ["--previous", previous]
        return self.cli(*arguments)

    @staticmethod
    def manifest(report):
        return Path(report["manifest"])

    def test_pause_resume_and_track_are_distinct_from_acceptance(self):
        with batch_service() as (service, url):
            planned = self.plan(url)
            self.assertEqual(planned["changes"], {"new": 2, "removed": 0})
            self.assertEqual(service.requests, [])
            manifest = self.manifest(planned)
            first = self.cli("run", "--manifest", manifest, "--max-files", 1)
            self.assertEqual(first["counts"], {"accepted": 1, "pending": 1})
            self.assertFalse(first["complete"])
            second = self.cli("run", "--manifest", manifest)
            self.assertEqual(second["counts"], {"accepted": 2})
            self.assertEqual(len(service.accepted_files), 2)
            refreshed = self.cli("status", "--manifest", manifest)
            self.assertEqual(refreshed["counts"], {"processed": 2})
            self.assertTrue(refreshed["complete"])
            self.cli("run", "--manifest", manifest)
            self.assertEqual(len(service.accepted_files), 2)
            requests = len(service.requests)
            self.cli("report", "--manifest", manifest)
            self.assertEqual(len(service.requests), requests)
            self.assertFalse(any("/query" in req["path"] for req in service.requests))

    def quality_suite(self, planned, *, workspace=WORKSPACE, term="copper lantern"):
        suite = {"schema_version": 1, "origin": planned["origin"], "workspace": workspace,
                 "retrieval": {"mode": "naive", "enable_rerank": False},
                 "cases": [{"id": "source-fact", "question": "What is the synthetic fact?",
                            "expected_sources": [{"basename": planned["files"][0]["upload_name"],
                                                  "required_terms": [term]}]}]}
        path = self.root / "suite.json"
        path.write_text(json.dumps(suite))
        return path

    def test_quality_suite_changed_during_status_refresh_cannot_redirect_queries(self):
        with batch_service() as (service, url):
            planned = self.plan(url)
            self.cli("run", "--manifest", planned["manifest"])
            suite_path = self.quality_suite(planned)
            original_reply = service.reply
            def replace_suite(request):
                if "/documents/track_status/" in request["path"]:
                    changed = json.loads(suite_path.read_text())
                    changed["workspace"] = "another-workspace"
                    suite_path.write_text(json.dumps(changed))
                return original_reply(request)
            service.reply = replace_suite
            failed = self.cli("verify", "--manifest", planned["manifest"], "--suite", suite_path,
                              "--report", self.root / "changed-quality.json", code=1)
            self.assertIn("changed after validation", failed["error"])
            self.assertFalse(any("/query" in request["path"] for request in service.requests))
            self.assertFalse((self.root / "changed-quality.json").exists())

    def test_post_ingestion_gate_refreshes_status_and_records_cited_source_check(self):
        with batch_service() as (service, url):
            planned = self.plan(url)
            self.cli("run", "--manifest", planned["manifest"])
            name = planned["files"][0]["upload_name"]
            service.responses[("POST", PREFIX + "/query/data")] = {
                "headers": IDENTITIES, "json": {"status": "success", "data": {
                    "references": [{"reference_id": "1", "file_path": name}],
                    "chunks": [{"chunk_id": "synthetic-c1", "reference_id": "1",
                                "content": "Synthetic fact: copper lantern."}]}}}
            result = self.cli("verify", "--manifest", planned["manifest"], "--suite", self.quality_suite(planned),
                              "--report", self.root / "quality.json")
            self.assertTrue(result["import"]["complete"])
            self.assertEqual(result["quality"]["status"], "passed")
            self.assertEqual(len(service.accepted_files), 2)
            receipts = self.cli("report", "--manifest", planned["manifest"])["quality_checks"]
            self.assertEqual(receipts[0]["status"], "passed")
            self.assertEqual(receipts[0]["report_sha256"], hashlib.sha256((self.root / "quality.json").read_bytes()).hexdigest())
            # A regression remains a failure; no ingestion retry and no rewritten old report.
            failed = self.cli("verify", "--manifest", planned["manifest"], "--suite", self.quality_suite(planned, term="silver lantern"),
                              "--report", self.root / "regression.json", code=1)
            self.assertEqual(failed["quality"]["status"], "failed")
            self.assertEqual(len(service.accepted_files), 2)
            self.assertEqual(json.loads((self.root / "quality.json").read_text())["status"], "passed")

    def test_quality_gate_refuses_wrong_workspace_before_network_and_unfinished_before_query(self):
        with batch_service() as (service, url):
            planned = self.plan(url)
            wrong = self.cli("verify", "--manifest", planned["manifest"], "--suite", self.quality_suite(planned, workspace="elsewhere"),
                             "--report", self.root / "wrong.json", code=1)
            self.assertIn("sealed origin", wrong["error"])
            self.assertEqual(service.requests, [])
            self.cli("run", "--manifest", planned["manifest"])
            service.track_state = "processing"
            incomplete = self.cli("verify", "--manifest", planned["manifest"], "--suite", self.quality_suite(planned),
                                  "--report", self.root / "unfinished.json", code=1)
            self.assertIn("not fully processed", incomplete["error"])
            self.assertFalse(any("/query" in req["path"] for req in service.requests))
            self.assertFalse((self.root / "unfinished.json").exists())

    def test_incremental_new_changed_unchanged_removed_preserves_server(self):
        with batch_service() as (service, url):
            first = self.plan(url)
            self.cli("run", "--manifest", first["manifest"])
            self.cli("status", "--manifest", first["manifest"])
            old_names = {file["relative_path"]: file["upload_name"] for file in first["files"]}
            (self.sources / "one.md").write_text("# One\n\nNew fact: silver lantern.\n", encoding="utf-8")
            (self.sources / "three.md").write_text("# Three\n\nAnother unique fact.\n", encoding="utf-8")
            second = self.plan(url, "run2", first["manifest"])
            self.assertEqual(second["changes"], {"changed": 1, "new": 1, "unchanged": 1, "removed": 0})
            self.assertEqual(second["counts"], {"pending": 2, "processed": 1})
            names = {file["relative_path"]: file["upload_name"] for file in second["files"]}
            self.assertNotEqual(names["one.md"], old_names["one.md"])
            self.assertEqual(names["two.md"], old_names["two.md"])
            self.cli("run", "--manifest", second["manifest"])
            self.assertEqual(len(service.accepted_files), 4)
            (self.sources / "two.md").unlink()
            third = self.plan(url, "run3", second["manifest"])
            self.assertEqual(third["removed"][0]["relative_path"], "two.md")
            self.assertEqual(third["removed"][0]["action"], "retain_on_server")
            self.cli("run", "--manifest", third["manifest"])
            self.assertEqual(len(service.accepted_files), 4)
            self.assertTrue(all(req["method"] in ("GET", "POST") and "delete" not in req["path"] for req in service.requests))

    def test_unsubmitted_unchanged_is_unresolved_and_never_resent(self):
        with batch_service() as (service, url):
            prior = self.plan(url)
            new = self.plan(url, "run2", prior["manifest"])
            self.assertEqual(new["counts"], {"inherited_pending": 2})
            self.assertFalse(new["complete"])
            self.cli("run", "--manifest", new["manifest"])
            self.assertEqual(service.requests, [])

    def test_unknown_submission_is_never_replayed_on_resume_or_incremental_plan(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            service.upload_disconnect = True
            failed = self.cli("run", "--manifest", plan["manifest"], code=2)
            self.assertEqual(failed["counts"], {"unknown": 1, "pending": 1})
            self.assertEqual(len(service.accepted_files), 1)
            service.upload_disconnect = False
            resumed = self.cli("run", "--manifest", plan["manifest"], code=2)
            self.assertEqual(resumed["counts"], {"unknown": 1, "accepted": 1})
            self.assertEqual(len(service.accepted_files), 2)
            self.cli("run", "--manifest", plan["manifest"], code=2)
            incremental = self.plan(url, "run2", plan["manifest"])
            self.assertEqual(incremental["counts"], {"unknown": 1, "accepted": 1})
            self.cli("run", "--manifest", incremental["manifest"], code=2)
            self.assertEqual(len(service.accepted_files), 2)

    def test_preflight_failure_can_resume_without_a_receipt(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            service.formats["supported_extensions"] = [".pdf"]
            result = self.cli("run", "--manifest", plan["manifest"], code=2)
            self.assertEqual(result["counts"], {"preflight_failed": 2})
            self.assertEqual(list(Path(plan["manifest"]).parent.joinpath("receipts").iterdir()), [])
            service.formats["supported_extensions"] = [".md"]
            resumed = self.cli("run", "--manifest", plan["manifest"])
            self.assertEqual(resumed["counts"], {"accepted": 2})
            self.assertEqual(len(service.accepted_files), 2)

    def test_receipt_recovers_crash_before_batch_state_checkpoint(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            self.cli("run", "--manifest", plan["manifest"], "--max-files", 1)
            state_path = self.manifest(plan).parent / "state.json"
            state = json.loads(state_path.read_text())
            record = next(item for item in state["files"].values() if item["phase"] == "accepted")
            record.clear()
            record.update(phase="submitting", indexed=False)
            state_path.write_text(json.dumps(state))
            result = self.cli("run", "--manifest", plan["manifest"])
            self.assertEqual(result["counts"], {"accepted": 2})
            self.assertEqual(len(service.accepted_files), 2)

    def test_interrupted_submitting_state_without_receipt_stays_unknown(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            state_path = self.manifest(plan).parent / "state.json"
            state = json.loads(state_path.read_text())
            for record in state["files"].values():
                record["phase"] = "submitting"
            state_path.write_text(json.dumps(state))
            result = self.cli("run", "--manifest", plan["manifest"], code=2)
            self.assertEqual(result["counts"], {"unknown": 2})
            self.assertEqual(service.requests, [])

    def test_original_source_edit_does_not_change_planned_upload(self):
        with batch_service() as (service, url):
            originals = {path.name: path.read_bytes() for path in self.sources.iterdir()}
            plan = self.plan(url)
            (self.sources / "one.md").write_text("CHANGED AFTER PLAN")
            self.cli("run", "--manifest", plan["manifest"])
            for item, uploaded in zip(plan["files"], service.accepted_files):
                self.assertEqual(uploaded["bytes"], originals[item["relative_path"]])
                self.assertEqual(hashlib.sha256(uploaded["bytes"]).hexdigest(), item["sha256"])

    def test_empty_tracking_does_not_mean_completed(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            self.cli("run", "--manifest", plan["manifest"])
            service.track_empty = True
            result = self.cli("status", "--manifest", plan["manifest"])
            self.assertEqual(result["counts"], {"accepted": 2})
            self.assertFalse(result["complete"])

    def test_failed_and_duplicate_processing_are_not_indexed(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            self.cli("run", "--manifest", plan["manifest"])
            service.track_state = "failed"
            result = self.cli("status", "--manifest", plan["manifest"])
            self.assertEqual(result["counts"], {"failed": 2})
            for item in result["files"]:
                self.assertEqual(item["documents"][0]["error_msg"], "Synthetic parser failed")
                self.assertEqual(item["documents"][0]["chunks_count"], 3)
            service.track_duplicate = True
            duplicate = self.cli("status", "--manifest", plan["manifest"])
            self.assertEqual(duplicate["counts"], {"duplicate": 2})
            self.assertFalse(duplicate["complete"])
            self.assertTrue(all(not item["indexed"] for item in duplicate["files"]))
            self.cli("run", "--manifest", plan["manifest"], code=2)
            self.assertEqual(len(service.accepted_files), 2)

    def test_tracking_identity_filename_and_count_errors_do_not_claim_success(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            self.cli("run", "--manifest", plan["manifest"])
            for flag in ("bad_identity", "track_wrong_file", "track_bad_total"):
                setattr(service, flag, True)
                result = self.cli("status", "--manifest", plan["manifest"])
                self.assertFalse(result["complete"])
                self.assertTrue(all("refresh_error" in item for item in result["files"]))
                setattr(service, flag, False)
            success = self.cli("status", "--manifest", plan["manifest"])
            self.assertTrue(success["complete"])

    def test_incremental_plan_preserves_failed_refresh_until_reverified(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            self.cli("run", "--manifest", plan["manifest"])
            self.assertTrue(self.cli("status", "--manifest", plan["manifest"])["complete"])
            service.bad_identity = True
            failed_refresh = self.cli("status", "--manifest", plan["manifest"])
            self.assertEqual(failed_refresh["counts"], {"processed": 2})
            self.assertFalse(failed_refresh["complete"])
            next_plan = self.plan(url, "run2", plan["manifest"])
            self.assertEqual(next_plan["counts"], {"processed": 2})
            self.assertFalse(next_plan["complete"])
            self.assertTrue(all("refresh_error" in item for item in next_plan["files"]))
            service.bad_identity = False
            self.assertTrue(self.cli("status", "--manifest", next_plan["manifest"])["complete"])
            self.assertEqual(len(service.accepted_files), 2)

    def test_processing_is_pending_and_can_finish_later(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            self.cli("run", "--manifest", plan["manifest"])
            service.track_state = "parsing"
            result = self.cli("status", "--manifest", plan["manifest"])
            self.assertEqual(result["counts"], {"processing": 2})
            service.track_state = "processed"
            result = self.cli("status", "--manifest", plan["manifest"])
            self.assertTrue(result["complete"])

    def test_configured_origin_cannot_redirect_manifest(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            for args in (("--base-url", "http://127.0.0.1:1", "run"), ("--base-url", "http://127.0.0.1:1", "status")):
                self.cli(*args, "--manifest", plan["manifest"], code=1)
            self.cli("run", "--manifest", plan["manifest"], code=1,
                     env={**self.environment, "LIGHTRAG_URL": "http://127.0.0.1:1"})
            self.assertEqual(service.requests, [])

    def test_changed_manifest_is_refused_before_network(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            manifest = self.manifest(plan)
            body = json.loads(manifest.read_text())
            body["workspace"] = "other-workspace"
            manifest.write_text(json.dumps(body))
            self.cli("run", "--manifest", manifest, code=1)
            self.assertEqual(service.requests, [])

    def test_changed_staged_snapshot_is_refused_before_network(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            staged = self.manifest(plan).parent / "staging" / plan["files"][0]["upload_name"]
            staged.chmod(0o600)
            staged.write_bytes(b"tampered staged bytes")
            self.cli("run", "--manifest", plan["manifest"], code=1)
            self.assertEqual(service.requests, [])

    def test_changed_or_missing_receipt_blocks_replay(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            run = self.cli("run", "--manifest", plan["manifest"], "--max-files", 1)
            receipt = self.manifest(plan).parent / run["files"][0]["receipt"]
            original = receipt.read_bytes()
            receipt.write_bytes(original + b"\n")
            self.cli("run", "--manifest", plan["manifest"], code=1)
            receipt.unlink()
            self.cli("run", "--manifest", plan["manifest"], code=1)
            self.assertEqual(len(service.accepted_files), 1)

    def test_lock_prevents_concurrent_runner(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            lock_path = self.manifest(plan).parent / ".batch.lock"
            with lock_path.open("a") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.cli("run", "--manifest", plan["manifest"], code=1)
                self.assertIn("already running", result["error"])
            self.assertEqual(service.requests, [])

    def test_previous_manifest_cannot_cross_workspace_origin_or_source_root(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            for change in (("--workspace", "different"), ("--base-url", "http://127.0.0.1:1"), ("--source-dir", self.root)):
                arguments = {"--base-url": url, "--workspace": WORKSPACE, "--source-dir": str(self.sources)}
                arguments[change[0]] = str(change[1])
                self.cli("--base-url", arguments["--base-url"], "plan", "--workspace", arguments["--workspace"],
                         "--source-dir", arguments["--source-dir"], "--output-dir", self.root / "run2", "--previous", plan["manifest"], code=1)
            self.assertEqual(service.requests, [])

    def test_symlink_source_and_evidence_paths_are_rejected(self):
        with batch_service() as (service, url):
            (self.sources / "linked.md").symlink_to(self.sources / "one.md")
            self.cli("--base-url", url, "plan", "--workspace", WORKSPACE, "--source-dir", self.sources, "--output-dir", self.root / "bad", code=1)
            (self.sources / "linked.md").unlink()
            plan = self.plan(url)
            path = self.manifest(plan)
            linked = self.root / "manifest.json"
            linked.symlink_to(path)
            self.cli("run", "--manifest", linked, code=1)
            self.assertEqual(service.requests, [])

    def test_local_evidence_is_private_and_output_not_overwritten(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            root = self.manifest(plan).parent
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            for name in ("manifest.json", "state.json"):
                self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o600)
            for staged in (root / "staging").iterdir():
                self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o400)
            self.cli("--base-url", url, "plan", "--workspace", WORKSPACE, "--source-dir", self.sources,
                     "--output-dir", root, code=1)
            self.assertEqual(service.requests, [])

    def test_same_basename_in_different_directories_has_distinct_version_names(self):
        with batch_service() as (service, url):
            nested = self.sources / "nested"
            nested.mkdir()
            (nested / "one.md").write_bytes((self.sources / "one.md").read_bytes())
            plan = self.plan(url)
            names = [item["upload_name"] for item in plan["files"]]
            self.assertEqual(len(set(names)), 3)
            self.assertEqual(service.requests, [])

    def test_nonpositive_max_files_rejected_without_network(self):
        with batch_service() as (service, url):
            plan = self.plan(url)
            self.cli("run", "--manifest", plan["manifest"], "--max-files", 0, code=1)
            self.assertEqual(service.requests, [])


if __name__ == "__main__":
    unittest.main()
