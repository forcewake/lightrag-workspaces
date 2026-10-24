#!/usr/bin/env python3
"""Black-box ingestion tests; every request goes to a local HTTP fixture.

Run: python3 -m unittest discover -s scripts -p 'test_lightrag*.py' -v
The fixture parses the actual multipart request using Python's MIME parser.
It never imports the client or calls LightRAG, provider models, or a live corpus.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from email import policy
from email.parser import BytesParser
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest


CLIENT = Path(__file__).resolve().parents[1] / "scripts" / "lightrag_client.py"
WORKSPACE = "research"
PREFIX = "/workspaces/" + WORKSPACE
UPLOAD = PREFIX + "/documents/upload"
FORMATS = PREFIX + "/documents/supported_file_types"
DOCUMENTS = PREFIX + "/documents/paginated"
IDENTITIES = {"LIGHTRAG-WORKSPACE": WORKSPACE, "X-LightRAG-Workspace": WORKSPACE}
ACCEPTED = {"status": "success", "message": "Queued for parsing", "track_id": "track-upload-123"}
SOURCE = ('# Workshop 2026-10-23\r\n\r\n'
          '| Decision | Value |\r\n| --- | --- |\r\n| Capacity | 73 MW |\r\n'
          'Сравни 引用. No approval was given.\n'
          'Literal $(echo unsafe) `id` ${HOME}.\n').encode("utf-8")


def multipart_parts(content_type, body):
    """Parse MIME boundaries/disposition/bytes independently of client code."""
    message = BytesParser(policy=policy.default).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii")
        + b"\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise ValueError("Expected a real multipart/form-data request")
    return [{"field": part.get_param("name", header="content-disposition"),
             "filename": part.get_filename(), "bytes": part.get_payload(decode=True),
             "content_type": part.get_content_type()}
            for part in message.iter_parts()]


class UploadService:
    def __init__(self):
        self.requests = []
        self.responses = {}
        self.receipt_path = None
        self.on_request = None
        self.on_upload = None
        self.pages = {1: {"documents": [], "pagination": {"page": 1, "has_next": False}}}
        self.formats = {"supported_extensions": [".md", ".pdf", ".txt"],
                        "engines": [{"id": "native", "extensions": [".md"]}]}
        self.inventory = {"defaultWorkspace": "private-notes", "workspaces": [
            {"id": WORKSPACE, "available": True, "readOnly": False},
            {"id": "private-notes", "available": True, "readOnly": True},
            {"id": "offline", "available": False, "readOnly": False},
        ]}
        self.track = {"track_id": "track-upload-123", "total_count": 1,
                      "documents": [{"id": "doc-upload-123", "file_path": "workshop.md",
                                     "status": "processed"}], "status_summary": {"processed": 1}}

    def reply(self, request):
        method, path = request["method"], request["path"]
        override = self.responses.get((method, path))
        if override is not None:
            return copy.deepcopy(override)
        if (method, path) == ("GET", "/workspaces"):
            return {"json": self.inventory}
        if (method, path) == ("GET", FORMATS):
            return {"headers": IDENTITIES, "json": self.formats}
        if (method, path) == ("POST", DOCUMENTS):
            return {"headers": IDENTITIES, "json": self.pages[request["payload"]["page"]]}
        if (method, path) == ("POST", UPLOAD):
            return {"headers": IDENTITIES, "json": ACCEPTED}
        if (method, path) == ("GET", PREFIX + "/documents/track_status/track-upload-123"):
            return {"headers": IDENTITIES, "json": self.track}
        if (method, path) == ("GET", PREFIX + "/documents/status_counts"):
            return {"headers": IDENTITIES, "json": {"status_counts": {"processed": 1}}}
        if (method, path) == ("GET", PREFIX + "/documents/pipeline_status"):
            return {"headers": IDENTITIES, "json": {"busy": False, "docs": 0}}
        return {"status": 404, "json": {"detail": "Unexpected request in ingestion test"}}


@contextmanager
def local_service():
    service = UploadService()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            content_type = self.headers.get("Content-Type", "")
            request = {"method": self.command, "path": self.path,
                       "headers": dict(self.headers), "raw": raw, "payload": None}
            if content_type.startswith("multipart/form-data"):
                request["parts"] = multipart_parts(content_type, raw)
            elif raw:
                request["payload"] = json.loads(raw)
            if self.path == UPLOAD and service.receipt_path is not None:
                try:
                    request["receipt_at_post"] = json.loads(service.receipt_path.read_text(encoding="utf-8"))
                    request["receipt_mode_at_post"] = stat.S_IMODE(service.receipt_path.stat().st_mode)
                except (OSError, ValueError) as exc:
                    request["receipt_error"] = str(exc)
            service.requests.append(request)
            if service.on_request is not None:
                service.on_request(request)
            if self.path == UPLOAD and service.on_upload is not None:
                service.on_upload(request)
            response = service.reply(request)
            if response.get("disconnect"):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if response.get("delay"):
                time.sleep(response["delay"])
            body = response.get("raw", json.dumps(response.get("json", {}), ensure_ascii=False).encode("utf-8"))
            try:
                self.send_response(response.get("status", 200))
                for name, value in response.get("headers", {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                if response.get("truncated"):
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if response.get("truncated"):
                    self.wfile.write(f"{len(body) + 100:x}\r\n".encode("ascii") + body)
                    self.close_connection = True
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    service.url = f"http://127.0.0.1:{server.server_port}"
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield service
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class UploadBehaviorTests(unittest.TestCase):
    def setUp(self):
        context = local_service()
        self.service = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "workshop.md"
        self.source.write_bytes(SOURCE)
        self.receipt = self.folder / "attempt-1.json"
        self.service.receipt_path = self.receipt

    def tearDown(self):
        # Ingestion must not spend model work generating a final answer, or
        # silently turn a processing check into any retrieval operation.
        self.assertFalse(any("/query" in request["path"] for request in self.service.requests))
        self.assertFalse(any("/workspaces/private-notes/" in request["path"]
                             for request in self.service.requests))

    def cli(self, *arguments, timeout="2", env=None):
        clean = {key: value for key, value in os.environ.items()
                 if not key.startswith("LIGHTRAG_") and not key.lower().endswith("proxy")}
        clean.update(env or {})
        return subprocess.run([sys.executable, str(CLIENT), "--base-url", self.service.url,
                               "--timeout", timeout, *arguments], env=clean, text=True,
                              capture_output=True, timeout=8)

    def upload(self, *extra, workspace=WORKSPACE, source=None, receipt=None, **kwargs):
        return self.cli("upload", "--workspace", workspace, "--file", str(source or self.source),
                        "--receipt-file", str(receipt or self.receipt), *extra, **kwargs)

    def success(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def failure(self, completed, contains=None):
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(completed.stdout, "")
        self.assertNotIn("Traceback", completed.stderr)
        message = json.loads(completed.stderr)["error"]
        if contains is not None:
            self.assertIn(contains, message)
        return message

    def posts(self):
        return [request for request in self.service.requests if request["path"] == UPLOAD]

    def saved_receipt(self):
        return json.loads(self.receipt.read_text(encoding="utf-8"))

    def assert_no_upload(self):
        self.assertEqual(self.posts(), [])
        self.assertFalse(self.receipt.exists())

    def test_upload_preserves_exact_source_bytes_and_hash_then_checks_processing(self):
        before = self.source.stat()
        expected = hashlib.sha256(SOURCE).hexdigest()
        result = self.success(self.upload("--expected-sha256", expected.upper()))
        self.assertEqual(result["workspace"], WORKSPACE)
        self.assertEqual(result["operation"], "upload")
        self.assertEqual(result["outcome"], "accepted")
        self.assertIs(result["indexed"], False)
        self.assertEqual(result["result"], ACCEPTED)
        self.assertEqual(result["source"], {"name": self.source.name, "bytes": len(SOURCE), "sha256": expected})
        self.assertEqual(result["receipt_file"], str(self.receipt.resolve()))
        self.assertIs(self.saved_receipt()["indexed"], False)
        self.assertEqual(self.saved_receipt(), {k: v for k, v in result.items() if k != "receipt_file"})
        upload = self.posts()[0]
        self.assertEqual(upload["parts"], [{"field": "file", "filename": self.source.name,
                                          "bytes": SOURCE, "content_type": "application/octet-stream"}])
        self.assertEqual(int(upload["headers"]["Content-Length"]), len(upload["raw"]))
        self.assertNotIn("Transfer-Encoding", upload["headers"])
        self.assertEqual(self.source.read_bytes(), SOURCE)
        after = self.source.stat()
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode),
                         (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode))
        self.assertEqual([(r["method"], r["path"]) for r in self.service.requests],
                         [("GET", "/workspaces"), ("GET", FORMATS), ("POST", DOCUMENTS), ("POST", UPLOAD)])
        track = self.success(self.cli("track", "--workspace", WORKSPACE, "--track-id", result["result"]["track_id"]))
        self.assertEqual(track["result"], self.service.track)
        self.assertEqual(track["result"]["documents"][0]["status"], "processed")
        status = self.success(self.cli("status", "--workspace", WORKSPACE))
        self.assertEqual(status["result"]["status_counts"]["status_counts"], {"processed": 1})
        self.assertEqual(len(self.posts()), 1)

    def test_private_receipt_is_present_before_server_receives_post(self):
        self.success(self.upload())
        request = self.posts()[0]
        self.assertNotIn("receipt_error", request)
        self.assertEqual(request["receipt_at_post"]["outcome"], "submitting")
        self.assertEqual(request["receipt_at_post"]["source"]["sha256"], hashlib.sha256(SOURCE).hexdigest())
        self.assertEqual(request["receipt_mode_at_post"], 0o600)
        self.assertEqual(stat.S_IMODE(self.receipt.stat().st_mode), 0o600)

    def test_unicode_and_quoted_basename_roundtrips_in_real_multipart(self):
        self.source = self.folder / 'Сводка 引用 "review".md'
        self.source.write_bytes(SOURCE)
        result = self.success(self.upload())
        self.assertEqual(self.posts()[0]["parts"][0]["filename"], self.source.name)
        self.assertEqual(result["source"]["name"], self.source.name)
        self.assertEqual(self.posts()[0]["parts"][0]["bytes"], SOURCE)

    def test_binary_bytes_are_not_decoded_reencoded_or_newline_normalized(self):
        self.source = self.folder / "binary.pdf"
        raw = b"%PDF-1.7\r\n\x00\xff\xfe\x80\r\n--boundary-like-data--\n\r\x00"
        self.source.write_bytes(raw)
        result = self.success(self.upload())
        self.assertEqual(self.posts()[0]["parts"][0]["bytes"], raw)
        self.assertEqual(result["source"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.source.read_bytes(), raw)

    def test_formats_uses_get_in_exact_workspace(self):
        result = self.success(self.cli("formats", "--workspace", WORKSPACE))
        self.assertEqual(result, {"workspace": WORKSPACE, "operation": "formats", "result": self.service.formats})
        self.assertEqual([(r["method"], r["path"]) for r in self.service.requests],
                         [("GET", "/workspaces"), ("GET", FORMATS)])

    def test_workspace_must_be_explicit_even_when_live_default_exists(self):
        completed = self.cli("upload", "--file", str(self.source), "--receipt-file", str(self.receipt))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--workspace", completed.stderr)
        self.assertEqual(self.service.requests, [])
        self.assert_no_upload()

    def test_unknown_unavailable_case_mismatch_and_invalid_workspace_stop(self):
        for workspace in ("missing", "offline", "Research", "../research"):
            with self.subTest(workspace=workspace):
                self.service.requests.clear()
                self.failure(self.upload(workspace=workspace))
                self.assertTrue(all(r["path"] == "/workspaces" for r in self.service.requests))
                self.assert_no_upload()

    def test_readonly_missing_protection_state_and_preserved_backlog_block_upload(self):
        for fields in ({"readOnly": True}, {"readOnly": None}, {}, {"readOnly": "false"},
                       {"readOnly": False, "preserveBacklog": True}):
            with self.subTest(fields=fields):
                self.service.requests.clear()
                self.service.inventory["workspaces"][0] = {"id": WORKSPACE, "available": True, **fields}
                self.failure(self.upload(), "does not allow ingestion")
                self.assertEqual([r["path"] for r in self.service.requests], ["/workspaces"])
                self.assert_no_upload()

    def test_unsupported_or_malformed_formats_block_upload(self):
        for formats in ({"supported_extensions": [".docx"]}, {}, {"supported_extensions": ".md"},
                        {"supported_extensions": [".md", None]}):
            with self.subTest(formats=formats):
                self.service.requests.clear()
                self.service.formats = formats
                self.failure(self.upload())
                self.assertEqual([r["path"] for r in self.service.requests], ["/workspaces", FORMATS])
                self.assert_no_upload()

    def test_duplicate_basename_on_later_inventory_page_blocks_upload(self):
        for file_path in ("workshop.md", "/archive/workshop.md", r"C:\archive\workshop.md"):
            with self.subTest(file_path=file_path):
                self.service.requests.clear()
                self.service.pages = {
                    1: {"documents": [{"id": "first", "file_path": "different.md"}], "pagination": {"has_next": True}},
                    2: {"documents": [{"id": "duplicate", "file_path": file_path, "status": "failed"}], "pagination": {"has_next": False}},
                }
                self.failure(self.upload(), "filename already exists")
                pages = [r["payload"]["page"] for r in self.service.requests if r["path"] == DOCUMENTS]
                self.assertEqual(pages, [1, 2])
                self.assert_no_upload()

    def test_full_multipage_inventory_allows_one_upload_after_last_page(self):
        self.service.pages = {
            1: {"documents": [{"file_path": "alpha.md"}], "pagination": {"has_next": True}},
            2: {"documents": [{"file_path": "beta.md"}], "pagination": {"has_next": False}},
        }
        self.success(self.upload())
        self.assertEqual([r["path"] for r in self.service.requests],
                         ["/workspaces", FORMATS, DOCUMENTS, DOCUMENTS, UPLOAD])
        for request in self.service.requests:
            if request["path"] == DOCUMENTS:
                self.assertEqual(request["payload"]["page_size"], 200)
                self.assertNotIn("status_filters", request["payload"])

    def test_incomplete_inventory_never_becomes_permission_to_upload(self):
        malformed = [{}, {"documents": [], "pagination": {"has_next": True}},
                     {"documents": [], "pagination": {"has_next": "false"}},
                     {"documents": [{"id": "missing-name"}], "pagination": {"has_next": False}},
                     {"documents": ["not-an-object"], "pagination": {"has_next": False}}]
        for listing in malformed:
            with self.subTest(listing=listing):
                self.service.pages = {1: listing}
                self.failure(self.upload(), "inventory")
                self.assert_no_upload()

    def test_preparation_digest_mismatch_prevents_upload_and_receipt(self):
        self.failure(self.upload("--expected-sha256", "0" * 64), "digest differs")
        self.assert_no_upload()
        self.assertEqual(self.source.read_bytes(), SOURCE)

    def test_malformed_expected_digest_is_rejected_before_preflight(self):
        for digest in ("not-a-hash", "f" * 63, "g" * 64):
            with self.subTest(digest=digest):
                self.service.requests.clear()
                self.failure(self.upload("--expected-sha256", digest), "64 hexadecimal")
                self.assertEqual([r["path"] for r in self.service.requests], ["/workspaces"])
                self.assert_no_upload()

    def test_existing_receipt_is_never_overwritten_or_used_for_another_post(self):
        original = b'{"outcome":"unknown","track_id":"prior-attempt"}\n'
        self.receipt.write_bytes(original)
        self.failure(self.upload(), "Receipt path must be new")
        self.assertEqual(self.receipt.read_bytes(), original)
        self.assertEqual(self.posts(), [])
        self.assertEqual([r["path"] for r in self.service.requests], ["/workspaces"])

    def test_receipt_symlink_and_missing_parent_block_upload(self):
        target = self.folder / "original-receipt.json"
        target.write_bytes(b"keep this receipt")
        self.receipt.symlink_to(target)
        self.failure(self.upload(), "Receipt path must be new")
        self.assertEqual(target.read_bytes(), b"keep this receipt")
        self.assertTrue(self.receipt.is_symlink())
        self.receipt.unlink()
        self.receipt.symlink_to(self.folder / "absent-target")
        self.failure(self.upload(), "Receipt path must be new")
        self.receipt.unlink()
        self.failure(self.upload(receipt=self.folder / "missing" / "receipt.json"), "Receipt path must be new")
        self.assert_no_upload()

    def test_receipt_created_during_preflight_is_not_overwritten(self):
        original = b'{"outcome":"unknown","source":"another attempt"}\n'

        def create_competing_receipt(request):
            if request["path"] == FORMATS:
                self.receipt.write_bytes(original)

        self.service.on_request = create_competing_receipt
        self.failure(self.upload(), "I/O failed")
        self.assertEqual(self.receipt.read_bytes(), original)
        self.assertEqual(self.posts(), [])

    def test_missing_directory_and_empty_source_never_upload(self):
        for source in (self.folder / "missing.md", self.folder):
            with self.subTest(source=source):
                self.failure(self.upload(source=source), "regular file")
                self.assert_no_upload()
        self.source.write_bytes(b"")
        self.failure(self.upload(), "Empty file")
        self.assert_no_upload()

    def test_unsafe_basename_rejected_without_altering_original(self):
        for name in (" leading.md", "trailing.md ", "native:workshop.md", "back\\slash.md", "new\nline.md"):
            with self.subTest(name=name):
                source = self.folder / name
                source.write_bytes(SOURCE)
                self.failure(self.upload(source=source), "Unsafe upload filename")
                self.assertEqual(source.read_bytes(), SOURCE)
                self.assert_no_upload()

    def test_preflight_identity_mismatch_stops_before_upload(self):
        for method, path in (("GET", FORMATS), ("POST", DOCUMENTS)):
            with self.subTest(path=path):
                self.service.responses.clear()
                self.service.responses[(method, path)] = {"headers": {"LIGHTRAG-WORKSPACE": "other"},
                    "json": self.service.formats if path == FORMATS else self.service.pages[1]}
                self.failure(self.upload(), "identity")
                self.assert_no_upload()

    def assert_unknown_once(self, response, expected_error=None, **kwargs):
        self.service.responses[("POST", UPLOAD)] = response
        completed = self.upload(**kwargs)
        self.failure(completed, "needs reconciliation")
        if expected_error:
            self.assertIn(expected_error, completed.stderr)
        receipt = self.saved_receipt()
        self.assertEqual(receipt["outcome"], "unknown")
        self.assertEqual(receipt["source"]["sha256"], hashlib.sha256(SOURCE).hexdigest())
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0]["parts"][0]["bytes"], SOURCE)
        self.assertNotIn("result", receipt)
        self.assertNotIn("PRIVATE SERVER SENTINEL", self.receipt.read_text() + completed.stderr + completed.stdout)
        return receipt

    def test_http_errors_after_submission_are_persisted_and_never_retried(self):
        for code in (400, 401, 403, 409, 413, 422, 429, 500, 503):
            with self.subTest(code=code):
                self.service.requests.clear()
                self.assert_unknown_once({"status": code, "json": {"detail": "PRIVATE SERVER SENTINEL"}}, f"HTTP {code}")
                self.receipt.unlink()

    def test_timeout_after_server_receives_file_is_unknown_and_not_retried(self):
        self.assert_unknown_once({"headers": IDENTITIES, "json": ACCEPTED, "delay": 0.2}, "timed out", timeout="0.05")

    def test_connection_closed_after_server_receives_file_is_unknown_and_not_retried(self):
        self.assert_unknown_once({"disconnect": True}, "Connection failed")

    def test_missing_or_wrong_upload_identity_discards_response_and_keeps_receipt(self):
        for headers in ({}, {"LIGHTRAG-WORKSPACE": WORKSPACE},
                        {"LIGHTRAG-WORKSPACE": WORKSPACE, "X-LightRAG-Workspace": "other"}):
            with self.subTest(headers=headers):
                self.service.requests.clear()
                self.assert_unknown_once({"headers": headers,
                                          "json": {**ACCEPTED, "message": "PRIVATE SERVER SENTINEL"}}, "identity")
                self.receipt.unlink()

    def test_invalid_and_truncated_upload_responses_are_unknown_and_not_retried(self):
        for response in ({"raw": b"<html>bad gateway</html>"}, {"raw": b"[]"},
                         {"raw": b"\xff"}, {"raw": b'{"status":"success",', "truncated": True}):
            with self.subTest(response=response):
                self.service.requests.clear()
                self.assert_unknown_once({"headers": IDENTITIES, **response})
                self.receipt.unlink()

    def test_redirect_after_upload_is_not_followed_or_retried(self):
        self.assert_unknown_once({"status": 307, "headers": {"Location": self.service.url + "/second-upload"},
                                  "json": {"detail": "PRIVATE SERVER SENTINEL"}}, "Redirect refused")
        self.assertNotIn("/second-upload", [r["path"] for r in self.service.requests])

    def test_reusing_unknown_receipt_cannot_blindly_replay_post(self):
        self.assert_unknown_once({"disconnect": True})
        prior = self.receipt.read_bytes()
        self.failure(self.upload(), "Receipt path must be new")
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.receipt.read_bytes(), prior)

    def test_http_success_without_track_id_is_not_reported_accepted_or_indexed(self):
        for response in ({"status": "success", "message": "No job ID"},
                         {"status": "failure", "message": "Parser rejected document"},
                         {"status": "unknown", "track_id": "unexpected-track"}):
            with self.subTest(response=response):
                self.service.responses[("POST", UPLOAD)] = {"headers": IDENTITIES, "json": response}
                result = self.success(self.upload())
                self.assertEqual(result["outcome"], "server_response")
                self.assertIs(result["indexed"], False)
                self.assertEqual(self.saved_receipt()["result"], response)
                self.receipt.unlink()

    def test_partial_acceptance_keeps_server_details_without_claiming_indexing(self):
        response = {"status": "partial_success", "track_id": "track-upload-123",
                    "message": "Accepted with extraction limitations",
                    "warnings": ["A source image has no extractable text"]}
        self.service.responses[("POST", UPLOAD)] = {"headers": IDENTITIES, "json": response}
        result = self.success(self.upload())
        self.assertEqual(result["outcome"], "accepted")
        self.assertIs(result["indexed"], False)
        self.assertEqual(result["result"], response)
        self.assertEqual(self.saved_receipt()["result"], response)

    def test_pending_empty_or_failed_track_does_not_upgrade_acceptance_receipt(self):
        result = self.success(self.upload())
        prior = self.receipt.read_bytes()
        for documents, summary in (([], {}),
                                   ([{"id": "doc-upload-123", "file_path": self.source.name,
                                      "status": "processing"}], {"processing": 1}),
                                   ([{"id": "doc-upload-123", "file_path": self.source.name,
                                      "status": "failed", "error_msg": "Extraction failed"}], {"failed": 1})):
            with self.subTest(summary=summary):
                self.service.track = {"track_id": result["result"]["track_id"], "documents": documents,
                                      "total_count": len(documents), "status_summary": summary}
                track = self.success(self.cli("track", "--workspace", WORKSPACE,
                                              "--track-id", result["result"]["track_id"]))
                self.assertEqual(track["result"], self.service.track)
                self.assertFalse(track.get("indexed", False))
                self.assertEqual(self.receipt.read_bytes(), prior)
                self.assertEqual(len(self.posts()), 1)

    def test_source_change_after_snapshot_cannot_change_wire_bytes_or_receipt_hash(self):
        # A real external writer changes the source when the local server sees
        # the request. The submitted immutable spool must retain reviewed bytes.
        changed = b"external writer changed the original after the snapshot\n"
        self.service.on_upload = lambda _request: self.source.write_bytes(changed)
        result = self.success(self.upload())
        self.assertEqual(self.posts()[0]["parts"][0]["bytes"], SOURCE)
        self.assertEqual(result["source"]["sha256"], hashlib.sha256(SOURCE).hexdigest())
        self.assertEqual(self.source.read_bytes(), changed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
