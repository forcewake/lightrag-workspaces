"""Operations invariants and optional real PostgreSQL snapshot/restore regression.

WORKSPACE_OPS_INTEGRATION=1 enables a disposable Docker-only test. It creates
owned network-none PostgreSQL containers, never a live deployment connection.
"""
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workspace_ops as ops


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        # Runtime is Linux/GNU. macOS unit tests exercise the portable manifest
        # invariants with empty xattrs; Linux CI uses real xattr functions.
        if not hasattr(os, "listxattr"):
            xattrs = patch.object(os, "listxattr", return_value=[], create=True)
            xattrs.start()
            self.addCleanup(xattrs.stop)
        self.config = {"format": 1, "application": "example", "postgres_container": "example-pg",
                       "database": "example_db", "backup_root": str(self.root)}
        self.operations = ops.Operations(self.config)

    def test_ids_preserve_existing_case_dots_and_underscore(self):
        for name in ("LEGACY_TEAM", "example.v2", "legacy_workspace", "A" * 128):
            self.assertIsNotNone(ops.WORKSPACE.fullmatch(name))
        for name in ("../escape", "_invalid", "name/child", "A" * 129):
            self.assertIsNone(ops.WORKSPACE.fullmatch(name))

    def test_private_output_modes_and_no_symlink_receipt(self):
        target = self.root / "proof.json"
        ops.save(target, {"secret": "not printed"})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        link = self.root / "link.json"
        link.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            ops.save(link, {})

    def test_manifest_detects_bytes_empty_directories_modes_and_hardlink_breakage(self):
        source = self.root / "source"
        source.mkdir()
        (source / "empty").mkdir()
        (source / "a").write_bytes(b"original")
        os.link(source / "a", source / "b")
        (source / "external-link").symlink_to("../not-followed")
        before = ops.tree_manifest(source)
        self.assertEqual(before["hardlinks"], [["a", "b"]])
        (source / "b").unlink()
        (source / "b").write_bytes(b"original")
        self.assertNotEqual(before, ops.tree_manifest(source))
        self.assertIn("empty", [row["path"] for row in before["entries"]])
        self.assertEqual(next(row for row in before["entries"] if row["kind"] == "symlink")["target"], "../not-followed")

    def test_special_file_fails_before_copy(self):
        os.mkfifo(self.root / "fifo")
        with self.assertRaisesRegex(RuntimeError, "special file"):
            ops.tree_manifest(self.root)

    def test_nested_backup_or_existing_copy_target_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "overlaps"):
            ops.clone_tree(self.root, self.root / "nested")
        self.assertFalse(ops.disjoint(self.root, self.root / "nested"))

    def test_symlink_parent_directory_rejected(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            ops.private_directory(alias)

    @staticmethod
    def container():
        return {"Id": "a" * 64, "Name": "/example", "Image": "sha256:old",
                "Config": {"Env": ["AUTH_TOKEN=private", "DEFAULT_WORKSPACE=Legacy.V2"], "Image": "old:version",
                           "Labels": {"local": "preserve"}, "Cmd": ["old"], "Entrypoint": ["python"]},
                "HostConfig": {"NetworkMode": "private-net", "RestartPolicy": {"Name": "unless-stopped"},
                               "Binds": ["/data:/app/data"], "Memory": 123456, "PortBindings": {"9621/tcp": []}},
                "Mounts": [{"Type": "bind", "Source": "/data", "Destination": "/app/data", "RW": True}],
                "NetworkSettings": {"Networks": {"private-net": {"Aliases": ["example", "stable-alias", "a" * 12],
                                    "IPAMConfig": None, "Links": None, "DriverOpts": None}}}}

    def test_app_update_preserves_auth_settings_mounts_limits_and_aliases(self):
        old = self.container()
        original = copy.deepcopy(old)
        image = {"Config": {"Entrypoint": ["new-entry"], "Cmd": [], "Healthcheck": {"Test": ["CMD", "true"]},
                             "Labels": {"org.opencontainers.image.revision": "rev", "local": "do not overwrite"}}}
        payload = ops.image_config(old, image, image="sha256:new")
        self.assertEqual(old, original)
        self.assertEqual(payload["Env"], old["Config"]["Env"])
        self.assertEqual(payload["HostConfig"], old["HostConfig"])
        self.assertEqual(payload["NetworkingConfig"]["EndpointsConfig"], {"private-net": {"Aliases": ["stable-alias"]}})
        self.assertEqual(payload["Labels"]["local"], "preserve")
        self.assertEqual(payload["Image"], "sha256:new")

    def test_unsupported_container_layouts_fail_closed(self):
        for mutation in (lambda value: value["NetworkSettings"]["Networks"]["private-net"].update(IPAMConfig={"IPv4Address": "1.2.3.4"}),
                         lambda value: value["HostConfig"].update(AutoRemove=True),
                         lambda value: value["Mounts"][0].update(Type="volume")):
            old = self.container()
            mutation(old)
            with self.assertRaises(RuntimeError):
                ops.image_config(old, {"Config": {}}, image="sha256:new")

    def test_custom_entrypoint_cannot_be_silently_discarded(self):
        original = self.container()
        with self.assertRaisesRegex(RuntimeError, "Custom application"):
            ops.require_default_command(original, {"Config": {"Entrypoint": ["other"], "Cmd": []}})
        ops.require_default_command(original, {"Config": {"Entrypoint": ["python"], "Cmd": ["old"]}})

    def test_retained_container_checks_every_setting_except_intentional_disabled_restart(self):
        original = self.container()
        retained = copy.deepcopy(original)
        retained["HostConfig"]["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
        ops.validate_retained(original, retained)
        retained["Config"]["Env"].append("OTHER_DB=different")
        with self.assertRaisesRegex(RuntimeError, "configuration/mounts"):
            ops.validate_retained(original, retained)
        retained = copy.deepcopy(original)
        retained["HostConfig"]["Memory"] = 1
        with self.assertRaisesRegex(RuntimeError, "host settings"):
            ops.validate_retained(original, retained)

    def test_next_backup_template_and_container_share_same_immutable_image_reference(self):
        image = {"Id": "sha256:built", "RepoDigests": ["ghcr.io/example/image@sha256:published"], "Config": {}}
        for requested in ("example:latest", "ghcr.io/example/image@sha256:published"):
            reference = ops.immutable_reference(requested, image)
            payload = ops.image_config(self.container(), image, image=reference)
            template_repository = reference
            self.assertEqual(template_repository, payload["Image"])
            self.assertNotEqual(reference, "example:latest")

    def test_rollback_template_preflight_handles_interrupted_candidate_preparation(self):
        template = self.root / "container.xml"
        template.write_bytes(b"original")
        self.assertIsNone(ops.rollback_template_bytes(self.root, template))
        (self.root / "template-before.xml").write_bytes(b"original")
        self.assertEqual(ops.rollback_template_bytes(self.root, template), b"original")
        template.write_bytes(b"unrelated change")
        with self.assertRaisesRegex(RuntimeError, "outside this operation"):
            ops.rollback_template_bytes(self.root, template)
        (self.root / "template-after.xml").write_bytes(b"candidate")
        template.write_bytes(b"candidate")
        self.assertEqual(ops.rollback_template_bytes(self.root, template), b"original")
        (self.root / "template-after.xml").unlink()
        (self.root / "template-after.xml").symlink_to(template)
        with self.assertRaisesRegex(RuntimeError, "candidate receipt"):
            ops.rollback_template_bytes(self.root, template)

    def test_sql_snapshot_injection_rejected_and_readonly_required(self):
        with self.assertRaisesRegex(RuntimeError, "snapshot"):
            self.operations.transaction("1'; DROP DATABASE live; --")
        transaction = self.operations.transaction("00000003-0000001B-1")
        self.assertIn("REPEATABLE READ READ ONLY", transaction)
        self.assertIn("SET TRANSACTION SNAPSHOT", transaction)

    def test_restore_allows_only_absent_invalid_source_index_and_records_definition(self):
        value = {"algorithm": "sorted-jsonb-copy-sha256-v1", "tables": [{"table": "example", "count": 2, "sha256": "rows"}],
                 "schema": {"tables": ["example"], "columns": [{"column_name": "id"}], "constraints": [],
                            "extensions": [{"name": "vector", "version": "fixture"}], "indexes": [
                                {"tablename": "example", "indexname": "example_valid", "indexdef": "valid definition",
                                 "indisvalid": True, "indisready": True},
                                {"tablename": "example", "indexname": "example_invalid", "indexdef": "invalid definition",
                                 "indisvalid": False, "indisready": True}]}}

        def fingerprint(payload):
            return copy.deepcopy(payload) | {"sha256": ops.digest(payload)}

        restored = copy.deepcopy(value)
        omitted = restored["schema"]["indexes"].pop()
        result = ops.compare_restored_fingerprint(fingerprint(value), fingerprint(restored))
        self.assertEqual(result["omitted_invalid_source_indexes"], [omitted])
        self.assertTrue(result["remaining_schema_rows_extensions_matched"])
        self.assertEqual(len(value["schema"]["indexes"]), 2, "Oracle must not mutate source receipt")
        self.assertEqual(ops.compare_restored_fingerprint(fingerprint(value), fingerprint(value))["omitted_invalid_source_indexes"], [])
        for mutation in (
            lambda row: row["schema"].update(indexes=None),
            lambda row: row["schema"]["indexes"][0].update(indexdef="changed valid definition"),
            lambda row: row["tables"][0].update(count=1),
            lambda row: row["schema"]["extensions"][0].update(version="changed"),
            lambda row: row["schema"]["columns"][0].update(column_name="changed"),
        ):
            changed = copy.deepcopy(restored)
            mutation(changed)
            with self.assertRaisesRegex(RuntimeError, "differ"):
                ops.compare_restored_fingerprint(fingerprint(value), fingerprint(changed))
        changed = copy.deepcopy(value)
        changed["schema"]["indexes"][-1]["indisvalid"] = True
        with self.assertRaisesRegex(RuntimeError, "differ"):
            ops.compare_restored_fingerprint(fingerprint(value), fingerprint(changed))
        with self.assertRaisesRegex(RuntimeError, "Invalid database fingerprint"):
            ops.compare_restored_fingerprint(fingerprint(value), fingerprint(restored) | {"sha256": "wrong"})

    def test_mutations_require_exact_reviewed_source_container_ids(self):
        app, pg = {"Id": "a" * 64}, {"Id": "b" * 64}
        with self.assertRaisesRegex(RuntimeError, "expected_application_id"):
            self.operations.require_source_pins(app, pg)
        self.operations.config.update(expected_application_id="a" * 64, expected_postgres_id="b" * 64)
        self.operations.require_source_pins(app, pg)
        with self.assertRaisesRegex(RuntimeError, "expected_postgres_id"):
            self.operations.require_source_pins(app, {"Id": "c" * 64})

    def test_other_container_writable_parent_mount_blocks_backup(self):
        app = self.container()
        outside = {"Id": "other", "Mounts": [{"RW": True, "Source": str(self.root)}]}
        with patch.object(self.operations, "docker", side_effect=[b"other\n", json.dumps([outside]).encode()]):
            with self.assertRaisesRegex(RuntimeError, "Another running container"):
                self.operations.exclusive_writers(app, {"control": self.root / "source"})

    def test_binding_rejects_different_server_marker_and_workspace_inventory(self):
        class Child:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(json.dumps({"pid": 123, "database": "example_db", "marker": "fixture-marker",
                                                     "manifests": {"Legacy.V2": "ws_fixture"}}).encode() + b"\n")
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def kill(self):
                self.returncode = -9

        for answers, inventory, message in ((["0"], {}, "not the application's database"),
                (["1", "wrong-marker"], {}, "marker differs"),
                (["1", "fixture-marker"], {"workspaces": [{"id": "different"}]}, "workspace IDs")):
            with self.subTest(message=message), patch.object(ops.subprocess, "Popen", return_value=Child()), \
                    patch.object(self.operations, "sql", side_effect=answers), \
                    patch.object(self.operations, "json_api", return_value=inventory):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.operations.source_binding({"Id": "a" * 64}, {"Id": "b" * 64})

    def test_copy_cannot_write_backup_secrets_under_live_mount(self):
        with patch.object(self.operations, "verify_bundle", return_value={"id": "owned"}), \
                patch.object(self.operations, "inspect", return_value={"Mounts": [{"Source": "/srv/live-postgres"}]}), \
                patch.object(ops, "clone_tree") as clone:
            with self.assertRaisesRegex(RuntimeError, "overlaps a live"):
                self.operations.copy_bundle(self.root, Path("/srv/live-postgres/copied-backup"))
            clone.assert_not_called()

    def test_retention_keeps_newest_failed_and_all_restore_update_references(self):
        for number, state in ((1, "verified"), (2, "verified"), (3, "verified"), (4, "failed")):
            directory = self.root / ("backup-" + str(number))
            directory.mkdir(mode=0o700)
            ops.save(directory / "operation.json", {"owner": ops.OWNER, "kind": "backup", "id": str(number),
                                                   "created_at": str(number), "status": state})
        drill = self.root / "drill-2"
        drill.mkdir(mode=0o700)
        ops.save(drill / "operation.json", {"owner": ops.OWNER, "kind": "drill", "backup_id": "2"})
        result = self.operations.retention(1)
        self.assertEqual(result["candidates"], [str(self.root / "backup-1")])
        self.assertTrue((self.root / "backup-1").exists())
        with self.assertRaisesRegex(RuntimeError, "at least one"):
            self.operations.retention(0, apply=True)

    def test_unowned_bundle_and_checksum_traversal_rejected(self):
        directory = self.root / "bundle"
        directory.mkdir(mode=0o700)
        ops.save(directory / "operation.json", {"owner": "someone-else", "kind": "backup"})
        with self.assertRaisesRegex(RuntimeError, "verified owned"):
            self.operations.verify_bundle(directory)
        ops.save(directory / "operation.json", {"owner": ops.OWNER, "format": 1, "kind": "backup", "status": "verified",
                    "full_archive_read_verified": True, "checksums": {"../private": {"sha256": "x", "bytes": 0}}})
        with self.assertRaisesRegex(RuntimeError, "Unsafe checksum"):
            self.operations.verify_bundle(directory)

    def test_scope_inventory_ignores_loaded_state_but_preserves_identity_and_counts(self):
        before = {"defaultWorkspace": "Legacy.V2", "workspaces": [{"id": "Legacy.V2", "documents": 3, "loaded": False}]}
        after = copy.deepcopy(before)
        after["workspaces"][0]["loaded"] = True
        self.assertEqual(ops.stable_inventory(before), ops.stable_inventory(after))
        after["workspaces"][0]["documents"] = 2
        self.assertNotEqual(ops.stable_inventory(before), ops.stable_inventory(after))

    def test_quality_origin_mismatch_fails_before_network_or_mutation(self):
        suite = self.root / "suite.json"
        ops.save(suite, {"schema_version": 1, "origin": "http://unrelated.example:9621", "workspace": "Legacy.V2", "cases": [
            {"id": "one", "question": "What is the value?", "expected_sources": [{"basename": "source.md", "required_terms": ["42"]}]}]})
        with self.assertRaisesRegex(RuntimeError, "another origin"):
            ops.validate_quality([suite], "http://localhost:9621", self.root)

    def test_no_quality_suite_does_not_claim_retrieval_or_require_origin(self):
        self.assertEqual(ops.validate_quality([], None, None), [])

    def test_drill_removes_auth_passwords_and_uses_dummy_provider_keys(self):
        environment = ops.drill_environment({"AUTH_ACCOUNTS": "admin:real-password", "LLM_BINDING_API_KEY": "real-provider-key",
            "POSTGRES_PASSWORD": "real-db-password", "JWT_SECRET": "real-jwt", "EMBEDDING_MODEL": "model-name",
            "DEFAULT_WORKSPACE": "Legacy.V2"}, "fresh-fixture-password", "fixture")
        self.assertNotIn("AUTH_ACCOUNTS", environment)
        self.assertNotIn("JWT_SECRET", environment)
        self.assertEqual(environment["LLM_BINDING_API_KEY"], "synthetic-isolated-drill-only")
        self.assertEqual(environment["POSTGRES_PASSWORD"], "fresh-fixture-password")
        self.assertEqual(environment["EMBEDDING_MODEL"], "model-name")

    def test_nonroot_fixture_user_resolved_from_exact_image_and_only_empty_dir_chowned(self):
        directory = self.root / "pgdata"
        directory.mkdir(mode=0o700)
        with patch.object(self.operations, "docker", side_effect=[b"999\n", b"998\n"]) as docker, patch.object(os, "chown") as chown:
            owner = self.operations.prepare_fixture_directory(directory, "sha256:exact", "postgres")
            self.assertEqual(owner["uid"], 999)
            chown.assert_called_once_with(directory, 999, 998)
            self.assertEqual(docker.call_args_list[0].args[-2:], ("sha256:exact", "-u"))
            self.assertIn("none", docker.call_args_list[0].args)
        (directory / "existing").write_text("preserve")
        with patch.object(os, "chown") as chown:
            with self.assertRaisesRegex(RuntimeError, "new empty"):
                self.operations.prepare_fixture_directory(directory, "sha256:exact", "postgres")
            chown.assert_not_called()

    def test_root_postgres_entrypoint_parent_is_owned_by_resolved_postgres_account(self):
        directory = self.root / "pgdata"
        directory.mkdir(mode=0o700)
        with patch.object(self.operations, "docker", side_effect=[b"0\n", b"0\n", b"999\n", b"999\n"]) as docker, patch.object(os, "chown") as chown:
            owner = self.operations.prepare_fixture_directory(directory, "sha256:exact", "root", postgres=True)
            self.assertEqual(owner["uid"], 999)
            chown.assert_called_once_with(directory, 999, 999)
            self.assertIn("postgres", docker.call_args_list[-1].args)
        with patch.object(self.operations, "docker", side_effect=[b"999\n", b"999\n"]) as docker, patch.object(os, "chown"):
            self.operations.prepare_fixture_directory(directory, "sha256:exact", "", postgres=True)
            self.assertIn("postgres", docker.call_args_list[0].args)


@unittest.skipUnless(os.environ.get("WORKSPACE_OPS_INTEGRATION") == "1", "Explicit disposable Docker integration only")
class SnapshotIntegration(unittest.TestCase):
    def test_snapshot_dump_preserves_frozen_view_while_new_writes_continue_and_restore_matches(self):
        prefix = "lr-ops-ci-" + uuid.uuid4().hex[:12]
        image = os.environ.get("WORKSPACE_OPS_POSTGRES_IMAGE",
            "tensorchord/vchord-postgres:pg18-v1.1.1@sha256:2cfaf4ea4cf744dd53c750b82901fb1bc118a4e3b96ab5b499ce690b4f124cd8")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            config = {"format": 1, "application": prefix + "-unused", "postgres_container": prefix + "-source",
                      "database": "ops_synthetic", "backup_root": str(root)}
            operation = ops.Operations(config)
            created = []
            try:
                for name in (prefix + "-source", prefix + "-restore"):
                    operation.docker("run", "-d", "--name", name, "--network", "none", "--memory", "1g",
                        "--label", ops.OWNER + "=" + prefix, "--tmpfs", "/var/lib/postgresql:rw,size=512m",
                        "-e", "PGDATA=/var/lib/postgresql/test", "-e", "POSTGRES_DB=ops_synthetic",
                        "-e", "POSTGRES_PASSWORD=synthetic-only", image)
                    created.append(name)
                    deadline = time.monotonic() + 90
                    while True:
                        try:
                            if operation.sql("SELECT 1;", container=name) == "1":
                                break
                        except RuntimeError:
                            pass
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(1)
                operation.sql("CREATE EXTENSION vector; CREATE EXTENSION vchord;"
                    "CREATE TABLE lightrag_workspace_database(singleton boolean PRIMARY KEY, db_instance_id uuid NOT NULL);"
                    "INSERT INTO lightrag_workspace_database VALUES(true,'00000000-0000-0000-0000-000000000001');"
                    "CREATE TABLE lightrag_synthetic(id integer PRIMARY KEY, payload jsonb, embedding vector(3));"
                    "INSERT INTO lightrag_synthetic VALUES(1,'{\"source\":\"before Δ\"}','[1,2,3]');"
                    "CREATE TABLE synthetic_index_fixture(id integer, duplicate_value integer);"
                    "INSERT INTO synthetic_index_fixture VALUES(1,7),(2,7);")
                # A failed concurrent unique build leaves a real invalid index;
                # pg_dump omits it. No system catalog changes or live data used.
                with self.assertRaises(RuntimeError):
                    operation.sql("CREATE UNIQUE INDEX CONCURRENTLY synthetic_invalid_index ON synthetic_index_fixture(duplicate_value);")
                baseline = operation.fingerprint()
                invalid = next(index for index in baseline["schema"]["indexes"] if index["indexname"] == "synthetic_invalid_index")
                self.assertIs(invalid["indisvalid"], False)
                with operation.snapshot() as snapshot:
                    operation.sql("INSERT INTO lightrag_synthetic VALUES(2,'{\"source\":\"after\"}','[4,5,6]');"
                                  "UPDATE lightrag_synthetic SET payload='{\"source\":\"changed\"}' WHERE id=1;")
                    self.assertNotEqual(operation.fingerprint(), baseline)
                    self.assertEqual(operation.fingerprint(snapshot), baseline)
                    dump = root / "database.dump"
                    with dump.open("wb") as stream:
                        operation.docker("exec", operation.pg, "pg_dump", "-Fc", "-Z1", "--snapshot=" + snapshot,
                                         "-U", "postgres", "-d", operation.database, output=stream)
                    operation.archive_read(dump, operation.pg, root)
                with dump.open("rb") as stream:
                    result = subprocess.run(["docker", "exec", "-i", prefix + "-restore", "pg_restore", "--exit-on-error",
                                             "--no-owner", "--no-privileges", "-U", "postgres", "-d", operation.database],
                                            stdin=stream, capture_output=True, timeout=120)
                self.assertEqual(result.returncode, 0, "Synthetic restore failed")
                restored = operation.fingerprint(container=prefix + "-restore")
                comparison = ops.compare_restored_fingerprint(baseline, restored)
                self.assertEqual(comparison["omitted_invalid_source_indexes"], [invalid])
                corrupt = root / "corrupt.dump"
                corrupt.write_bytes(dump.read_bytes()[:-64])
                with self.assertRaises(RuntimeError):
                    operation.archive_read(corrupt, operation.pg, root)
            finally:
                for name in reversed(created):
                    inspected = operation.inspect(name)
                    self.assertEqual(inspected["Config"]["Labels"][ops.OWNER], prefix)
                    operation.docker("rm", "-f", name)


if __name__ == "__main__":
    unittest.main()
