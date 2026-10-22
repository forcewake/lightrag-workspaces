"""Portable configuration and explicit empty-workspace creation, without providers."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gateway import Config, workspace_metadata
from scripts.init_workspace import initialize_workspace


class PortableConfigTests(unittest.TestCase):
    def test_clean_environment_uses_generic_default_without_creating_directories(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_env()
        self.assertEqual(config.default_workspace, "default")
        self.assertEqual(config.max_workers, 2)
        self.assertEqual(config.startup_timeout, 240)
        self.assertEqual(config.idle_seconds, 600)

    def test_all_routing_defaults_are_configurable_and_legacy_workspace_env_is_supported(self):
        with patch.dict(os.environ, {"WORKSPACE": "legacy.notes", "LIGHTRAG_MAX_ACTIVE_WORKSPACES": "3",
                                    "LIGHTRAG_WORKSPACE_STARTUP_TIMEOUT": "360",
                                    "LIGHTRAG_WORKSPACE_IDLE_SECONDS": "900", "EMBEDDING_DIM": "768"}, clear=True):
            config = Config.from_env()
            self.assertEqual(config.default_workspace, "legacy.notes")
            self.assertEqual((config.max_workers, config.startup_timeout, config.idle_seconds, config.embedding_dim),
                             (3, 360, 900, 768))
            os.environ["LIGHTRAG_DEFAULT_WORKSPACE"] = "custom-default"
            self.assertEqual(Config.from_env().default_workspace, "custom-default")

    def test_legacy_browser_storage_owner_is_optional_and_independent_of_routing_default(self):
        with patch.dict(os.environ, {"LIGHTRAG_DEFAULT_WORKSPACE": "meeting-notes"}, clear=True):
            self.assertIsNone(Config.from_env().legacy_storage_workspace)
            os.environ["LIGHTRAG_LEGACY_STORAGE_WORKSPACE"] = "original-root"
            config = Config.from_env()
            self.assertEqual(config.default_workspace, "meeting-notes")
            self.assertEqual(config.legacy_storage_workspace, "original-root")

    def test_bad_config_is_rejected_before_gateway_startup(self):
        for key, value in (("LIGHTRAG_DEFAULT_WORKSPACE", "../escape"),
                           ("LIGHTRAG_LEGACY_STORAGE_WORKSPACE", "../escape"),
                           ("LIGHTRAG_MAX_ACTIVE_WORKSPACES", "0"),
                           ("LIGHTRAG_MAX_ACTIVE_WORKSPACES", "101"),
                           ("LIGHTRAG_WORKSPACE_STARTUP_TIMEOUT", "nan"),
                           ("LIGHTRAG_WORKSPACE_IDLE_SECONDS", "inf"),
                           ("EMBEDDING_DIM", "0")):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}, clear=True):
                with self.assertRaises(ValueError):
                    Config.from_env()


class InitializeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = self.root / "storage"
        self.inputs = self.root / "inputs"

    def test_creation_is_explicit_exact_and_visible_to_metadata_only_discovery(self):
        self.assertTrue(initialize_workspace(self.storage, self.inputs, "meeting-notes.v2"))
        self.assertEqual(list((self.storage / "meeting-notes.v2").iterdir()), [])
        self.assertTrue((self.inputs / "meeting-notes.v2").is_dir())
        config = Config(self.storage, self.inputs, self.root / "runtime", self.root / "static")
        inventory = workspace_metadata(config)
        self.assertEqual([item["id"] for item in inventory], ["meeting-notes.v2"])
        self.assertEqual(inventory[0]["documents"], 0)
        self.assertFalse((self.root / "runtime").exists())

    def test_existing_corpus_bytes_and_pending_imports_are_untouched(self):
        initialize_workspace(self.storage, self.inputs, "default")
        corpus = self.storage / "default"
        status = corpus / "kv_store_doc_status.json"
        status.write_text(json.dumps({"old": {"status": "pending"}}))
        before = status.read_bytes()
        self.assertFalse(initialize_workspace(self.storage, self.inputs, "default"))
        self.assertEqual(status.read_bytes(), before)
        self.assertEqual([file.name for file in corpus.iterdir()], [status.name])

    def test_invalid_identifiers_do_not_create_even_roots(self):
        for name in ("", "../escape", "a/b", "a\\b", ".", "..", " leading", "a" * 129):
            with self.subTest(name=name), self.assertRaises(ValueError):
                initialize_workspace(self.storage, self.inputs, name)
        self.assertFalse(self.storage.exists())
        self.assertFalse(self.inputs.exists())

    def test_symlinked_roots_or_workspace_paths_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.storage.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            initialize_workspace(self.storage, self.inputs, "new")
        self.storage.unlink()
        self.storage.mkdir()
        (self.storage / "alias").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            initialize_workspace(self.storage, self.inputs, "alias")
        self.assertEqual(list(outside.iterdir()), [])

    def test_overlapping_roots_or_invalid_input_destination_do_not_create_a_corpus(self):
        with self.assertRaises(ValueError):
            initialize_workspace(self.storage, self.storage / "nested", "new")
        self.inputs.mkdir()
        (self.inputs / "new").write_text("keep")
        with self.assertRaises(ValueError):
            initialize_workspace(self.storage, self.inputs, "new")
        self.assertFalse(self.storage.exists())
        self.assertEqual((self.inputs / "new").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
