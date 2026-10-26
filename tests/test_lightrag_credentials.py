#!/usr/bin/env python3
"""Private credential-file behavior; synthetic keys and localhost HTTP only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.lightrag_client import Client, origin_identity
import test_lightrag_client as behavior

local_service = behavior.local_service


SYNTHETIC_KEY = "test-only-key-never-a-real-credential"


class CredentialFileTests(unittest.TestCase):
    success = behavior.ClientBehaviorTests.success
    failure = behavior.ClientBehaviorTests.failure

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.home = Path(self.scratch.name).resolve()
        self.path = self.home / ".config" / "lightrag" / "credentials.json"
        self.path.parent.mkdir(parents=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.server_context = local_service()
        self.service = self.server_context.__enter__()
        self.addCleanup(self.server_context.__exit__, None, None, None)
        self.addCleanup(patch.stopall)
        patch("pathlib.Path.home", return_value=self.home).start()
        patch.dict(os.environ, {}, clear=True).start()

    def save(self, value=None, *, raw=None):
        value = value or {"origin": self.service.url, "api_key": SYNTHETIC_KEY}
        self.path.write_text(raw if raw is not None else json.dumps(value))
        self.path.chmod(0o600)

    def cli(self, *arguments, env=None, **kwargs):
        # Isolate the subprocess from the user's real credential file as well.
        return behavior.ClientBehaviorTests.cli(self, *arguments, env={"HOME": str(self.home), **(env or {})}, **kwargs)

    def test_absent_file_preserves_unauthenticated_requests(self):
        self.success(self.cli("workspaces"))
        self.assertNotIn("X-Api-Key", self.service.requests[0]["headers"])

    def test_matching_origin_authenticates_discovery_and_scoped_retrieval(self):
        self.save()
        completed = self.cli("retrieve", "--workspace", "research", "--query", "Find retrieval evidence")
        self.success(completed)
        self.assertEqual([request["path"] for request in self.service.requests],
                         ["/workspaces", "/workspaces/research/query/data"])
        self.assertTrue(all(request["headers"].get("X-Api-Key") == SYNTHETIC_KEY
                            for request in self.service.requests))
        self.assertNotIn(SYNTHETIC_KEY, completed.stdout + completed.stderr)

    def test_foreign_port_receives_no_stored_key(self):
        self.save()
        with local_service() as other:
            self.success(self.cli("workspaces", origin=other.url))
            self.assertNotIn("X-Api-Key", other.requests[0]["headers"])
        self.assertEqual(self.service.requests, [])

    def test_origin_matching_includes_scheme_hostname_and_effective_port(self):
        self.save({"origin": "https://Example.test:443/", "api_key": SYNTHETIC_KEY})
        self.assertEqual(Client("https://example.test").headers.get("X-API-Key"), SYNTHETIC_KEY)
        for origin in ("http://example.test:443", "https://other.test", "https://example.test:444",
                       "https://example.test:0"):
            with self.subTest(origin=origin):
                self.assertNotIn("X-API-Key", Client(origin).headers)
        self.assertNotEqual(origin_identity("http://example.test:0"), origin_identity("http://example.test"))

    def test_explicit_environment_key_bypasses_invalid_stored_file(self):
        self.save(raw="not json")
        self.success(self.cli("workspaces", env={"LIGHTRAG_API_KEY": "explicit-test-key"}))
        self.assertEqual(self.service.requests[0]["headers"].get("X-Api-Key"), "explicit-test-key")

    def test_explicit_bearer_bypasses_stored_key(self):
        self.save()
        self.success(self.cli("workspaces", env={"LIGHTRAG_BEARER_TOKEN": "explicit-test-bearer"}))
        headers = self.service.requests[0]["headers"]
        self.assertEqual(headers.get("Authorization"), "Bearer explicit-test-bearer")
        self.assertNotIn("X-Api-Key", headers)

    def test_explicit_environment_credentials_keep_foreign_origin_behavior(self):
        self.save({"origin": "http://other.test", "api_key": SYNTHETIC_KEY})
        self.success(self.cli("workspaces", env={"LIGHTRAG_API_KEY": "explicit-test-key",
                                                "LIGHTRAG_BEARER_TOKEN": "explicit-test-bearer"}))
        headers = self.service.requests[0]["headers"]
        self.assertEqual(headers.get("X-Api-Key"), "explicit-test-key")
        self.assertEqual(headers.get("Authorization"), "Bearer explicit-test-bearer")

    def test_malformed_credentials_fail_before_http_without_exposing_contents(self):
        invalid = [
            "not JSON " + SYNTHETIC_KEY,
            "[]", "null",
            json.dumps({"origin": self.service.url, "api_key": SYNTHETIC_KEY, "extra": True}),
            '{"origin":"http://a.test","origin":"http://b.test","api_key":"' + SYNTHETIC_KEY + '"}',
            json.dumps({"origin": self.service.url, "api_key": ""}),
            json.dumps({"origin": self.service.url, "api_key": 7}),
            json.dumps({"origin": self.service.url, "api_key": SYNTHETIC_KEY + "\r\nX-Other: value"}),
            json.dumps({"origin": self.service.url, "api_key": "\u2603"}),
            json.dumps({"origin": self.service.url, "api_key": "a" * 4097}),
            json.dumps({"origin": "http://user:password@example.test", "api_key": SYNTHETIC_KEY}),
            json.dumps({"origin": "http://example.test/path", "api_key": SYNTHETIC_KEY}),
            json.dumps({"origin": "http://example.test?query=1", "api_key": SYNTHETIC_KEY}),
            json.dumps({"origin": "http://example.test:bad", "api_key": SYNTHETIC_KEY}),
            json.dumps({"origin": None, "api_key": SYNTHETIC_KEY}),
            " " * (16 * 1024 + 1),
        ]
        for raw in invalid:
            with self.subTest(case=invalid.index(raw)):
                self.save(raw=raw)
                completed = self.cli("workspaces")
                self.failure(completed, "Invalid local LightRAG credentials")
                self.assertNotIn(SYNTHETIC_KEY, completed.stdout + completed.stderr)
        self.assertEqual(self.service.requests, [])

    def test_permissions_must_be_exactly_private(self):
        self.save()
        for file_mode, parent_mode in ((0o644, 0o700), (0o400, 0o700), (0o600, 0o755), (0o600, 0o770)):
            with self.subTest(file_mode=file_mode, parent_mode=parent_mode):
                self.path.chmod(file_mode)
                self.path.parent.chmod(parent_mode)
                self.failure(self.cli("workspaces"), "private file permissions")
        self.assertEqual(self.service.requests, [])

    def test_symlink_file_is_rejected(self):
        self.save()
        real = self.path.with_name("real.json")
        self.path.rename(real)
        self.path.symlink_to(real)
        self.failure(self.cli("workspaces"), "Invalid local LightRAG credentials")
        self.assertEqual(self.service.requests, [])

    def test_symlink_directory_and_ancestor_are_rejected(self):
        self.save()
        for directory in (self.path.parent, self.path.parent.parent):
            with self.subTest(directory=directory.name):
                real = directory.with_name(directory.name + "-real")
                directory.rename(real)
                directory.symlink_to(real, target_is_directory=True)
                self.failure(self.cli("workspaces"), "Invalid local LightRAG credentials")
                directory.unlink()
                real.rename(directory)
        self.assertEqual(self.service.requests, [])

    def test_hardlink_and_nonregular_file_are_rejected(self):
        self.save()
        linked = self.path.with_name("copy.json")
        os.link(self.path, linked)
        self.failure(self.cli("workspaces"), "Invalid local LightRAG credentials")
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        self.failure(self.cli("workspaces"), "Invalid local LightRAG credentials")
        self.assertEqual(self.service.requests, [])

    def test_redirect_does_not_forward_stored_key(self):
        self.save()
        with local_service() as other:
            self.service.responses[("GET", "/workspaces")] = {
                "status": 302, "headers": {"Location": other.url + "/workspaces"}}
            completed = self.cli("workspaces")
            self.failure(completed, "Redirect refused")
            self.assertEqual(other.requests, [])
        self.assertEqual(self.service.requests[0]["headers"].get("X-Api-Key"), SYNTHETIC_KEY)
        self.assertNotIn(SYNTHETIC_KEY, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
