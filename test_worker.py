"""Focused isolation/preservation tests; no running LightRAG or provider needed."""

import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import worker


class WorkerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.storage = self.base / "storage"
        self.storage.mkdir()
        self.corpus = self.storage / "meeting-notes"
        self.corpus.mkdir()

    def test_legacy_hyphenated_directory_is_used_exactly(self):
        self.assertEqual(worker.resolve_workspace(self.storage, "meeting-notes"), self.corpus.resolve())
        self.assertFalse((self.storage / "meeting_notes").exists())

    def test_unknown_workspace_cannot_create_storage(self):
        with self.assertRaises(FileNotFoundError):
            worker.resolve_workspace(self.storage, "unknown")
        self.assertFalse((self.storage / "unknown").exists())

    def test_paths_and_aliases_cannot_escape_or_alias_corpus(self):
        for workspace_id in ("../outside", "/absolute", "a/b", "a\\b", "..", "."):
            with self.subTest(workspace_id=workspace_id), self.assertRaises(ValueError):
                worker.resolve_workspace(self.storage, workspace_id)
        alias = self.storage / "alias"
        alias.symlink_to(self.corpus, target_is_directory=True)
        with self.assertRaises(ValueError):
            worker.resolve_workspace(self.storage, "alias")

    def test_upload_symlink_cannot_select_another_workspace(self):
        inputs = self.base / "inputs"
        inputs.mkdir()
        (inputs / "other").mkdir()
        (inputs / "meeting-notes").symlink_to(inputs / "other", target_is_directory=True)
        with self.assertRaises(ValueError):
            worker.resolve_input_directory(inputs, "meeting-notes")

    def test_single_writer_lock_is_outside_corpus_and_reusable(self):
        runtime = self.base / "runtime"
        with worker.workspace_lock(runtime, self.storage, "meeting-notes"):
            self.assertEqual(list(self.corpus.iterdir()), [])
            with self.assertRaises(RuntimeError):
                with worker.workspace_lock(runtime, self.storage, "meeting-notes"):
                    self.fail("Concurrent writer was admitted")
        with worker.workspace_lock(runtime, self.storage, "meeting-notes"):
            pass
        self.assertTrue((runtime / "locks" / "meeting-notes.lock").exists())

    def test_runtime_files_cannot_be_placed_inside_storage(self):
        with self.assertRaises(ValueError):
            with worker.workspace_lock(self.corpus / "runtime", self.storage, "meeting-notes"):
                pass
        self.assertEqual(list(self.corpus.iterdir()), [])

    def test_legacy_cache_is_readable_without_rewriting_original(self):
        source = {"default": {"abc": {"return": "existing answer"}}}
        original = copy.deepcopy(source)
        cache_file = self.corpus / "kv_store_llm_response_cache.json"
        cache_file.write_text(json.dumps(source))
        before = cache_file.read_bytes()
        converted = worker.flatten_legacy_cache(source, lambda mode, kind, key: f"{mode}:{kind}:{key}")
        self.assertEqual(converted, {"default:extract:abc": {"return": "existing answer"}})
        self.assertEqual(source, original)
        self.assertEqual(cache_file.read_bytes(), before)

    def test_flattened_and_noncache_data_are_retained(self):
        current = {"default:extract:abc": {"return": "answer"}}
        self.assertIs(worker.flatten_legacy_cache(current, lambda *args: self.fail()), current)
        noncache = {"metadata": {"value": 1}}
        self.assertEqual(worker.flatten_legacy_cache(noncache, lambda *args: self.fail()), noncache)

    def test_worker_prefix_must_match_exact_selected_workspace(self):
        base = ["--workspace-id", "meeting-notes", "--storage-root", str(self.storage),
                "--input-root", str(self.base / "inputs"), "--port", "19621"]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            worker.parse_worker_args(base + ["--prefix", "/workspaces/another"])
        result = worker.parse_worker_args(base + ["--prefix", "/workspaces/meeting-notes"])
        self.assertEqual(result.workspace_id, "meeting-notes")


class StartupHookTests(unittest.TestCase):
    def test_provider_budgets_are_partitioned_without_changing_models_or_credentials(self):
        args = SimpleNamespace(max_async=30, embedding_func_max_async=8, rerank_max_async=30,
                               max_parallel_insert=10, query_llm_max_async=12,
                               extract_llm_max_async=None, llm_model="existing-deployment",
                               embedding_model="text-embedding-3-large", llm_binding_api_key="secret",
                               llm_binding_host="https://existing.example")
        models = (args.llm_model, args.embedding_model, args.llm_binding_api_key, args.llm_binding_host)
        worker.apply_provider_budgets(args, 3, [SimpleNamespace(name="query"), SimpleNamespace(name="extract")])
        self.assertEqual((args.max_async, args.embedding_func_max_async, args.rerank_max_async,
                          args.query_llm_max_async, args.max_parallel_insert), (10, 2, 10, 4, 3))
        self.assertIsNone(args.extract_llm_max_async)
        self.assertEqual(models, (args.llm_model, args.embedding_model, args.llm_binding_api_key, args.llm_binding_host))

    def test_provider_budget_too_small_for_pool_is_rejected_atomically(self):
        args = SimpleNamespace(max_async=30, embedding_func_max_async=8, rerank_max_async=30,
                               max_parallel_insert=10, query_llm_max_async=1)
        with self.assertRaisesRegex(ValueError, "active-workspace limit"):
            worker.apply_provider_budgets(args, 3, [SimpleNamespace(name="query")])
        self.assertEqual(args.max_async, 30)
        self.assertEqual(args.query_llm_max_async, 1)

    def fake_modules(self):
        modules = {name: ModuleType(name) for name in (
            "lightrag", "lightrag.kg", "lightrag.kg.json_doc_status_impl",
            "lightrag.kg.json_kv_impl", "lightrag.kg.nano_vector_db_impl",
            "lightrag.kg.networkx_impl", "lightrag.storage_migrations", "lightrag.utils")}
        modules["lightrag"].__version__ = "1.5.7"

        class Migrations:
            async def check_and_migrate_data(self):
                raise AssertionError("Startup tried to backfill files")

            async def explicit_upload(self):
                return "normal upload"

        class JsonKV:
            async def _migrate_legacy_cache_structure(self, data):
                raise AssertionError("Startup tried to rewrite the cache")

            async def index_done_callback(self):
                return "normal commit"

        modules["lightrag.storage_migrations"]._StorageMigrationMixin = Migrations
        modules["lightrag.kg.json_kv_impl"].JsonKVStorage = JsonKV
        modules["lightrag.utils"].generate_cache_key = lambda mode, kind, key: f"{mode}:{kind}:{key}"
        modules["lightrag.utils"].logger = SimpleNamespace(info=lambda *args: None)
        for name in ("json_doc_status_impl", "json_kv_impl", "nano_vector_db_impl", "networkx_impl"):
            module = modules[f"lightrag.kg.{name}"]
            module.reap_orphan_tmp_files = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("Startup tried to remove a corpus file"))
            setattr(modules["lightrag.kg"], name, module)
        return modules, Migrations, JsonKV

    def test_hooks_preserve_startup_files_without_disabling_future_api_writes(self):
        modules, migrations, kv = self.fake_modules()
        with patch.dict(sys.modules, modules):
            worker.install_startup_preservation()
            asyncio.run(migrations().check_and_migrate_data())
            result = asyncio.run(kv()._migrate_legacy_cache_structure(
                {"default": {"abc": {"return": "answer"}}}))
            self.assertEqual(result, {"default:extract:abc": {"return": "answer"}})
            self.assertEqual(asyncio.run(kv().index_done_callback()), "normal commit")
            self.assertEqual(asyncio.run(migrations().explicit_upload()), "normal upload")
            modules["lightrag.kg.json_kv_impl"].reap_orphan_tmp_files("unchanged.tmp")

    def test_unsupported_version_fails_closed(self):
        modules, _, _ = self.fake_modules()
        modules["lightrag"].__version__ = "1.6.0"
        with patch.dict(sys.modules, modules), self.assertRaises(RuntimeError):
            worker.install_startup_preservation()

    def test_postgres_startup_skips_backfill_without_patching_file_storages(self):
        modules, migrations, kv = self.fake_modules()
        original = kv._migrate_legacy_cache_structure
        with patch.dict(sys.modules, modules):
            worker.install_startup_preservation(file_backends=False)
            asyncio.run(migrations().check_and_migrate_data())
            self.assertIs(kv._migrate_legacy_cache_structure, original)
            with self.assertRaisesRegex(AssertionError, "remove a corpus file"):
                modules["lightrag.kg.json_kv_impl"].reap_orphan_tmp_files("retained.tmp")

    def test_identity_preserves_storage_namespace(self):
        async def health(request=None):
            return {"status": "healthy", "configuration": {"workspace": ""}}

        route = SimpleNamespace(path="/health", dependant=SimpleNamespace(call=health))
        app = SimpleNamespace(state=SimpleNamespace(), routes=[route], add_middleware=lambda *args, **kwargs: None)
        worker.identify_app(app, "meeting-notes")
        result = asyncio.run(route.dependant.call())
        self.assertEqual(result["selected_workspace_id"], "meeting-notes")
        self.assertEqual(result["configuration"]["workspace"], "")
        self.assertIs(result["startup_compatibility_writes"], False)

    def test_workspace_header_is_attached_to_streaming_responses(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"chunk", "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        messages = []

        async def send(message):
            messages.append(message)

        asyncio.run(worker.WorkspaceIdentityMiddleware(app, "meeting-notes")(
            {"type": "http"}, None, send))
        self.assertIn((b"x-lightrag-workspace", b"meeting-notes"), messages[0]["headers"])
        self.assertEqual(messages[1]["body"], b"chunk")
        self.assertTrue(messages[1]["more_body"])


if __name__ == "__main__":
    unittest.main()
