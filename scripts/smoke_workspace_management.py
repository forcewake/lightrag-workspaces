#!/usr/bin/env python3
"""Create/reconcile managed PostgreSQL workspaces in an EMPTY disposable DB.

Only synthetic documents and the local synthetic embedding provider are used.
The caller must provision vector/vchord and explicitly name the fresh database.
The migration's empty-database/token guards protect an existing installation.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from smoke_postgres import DIMENSION, MODEL, WORKSPACES, check, fake_provider, hashes, seed
from postgres_backend import REQUIRED_TABLES, load_workspace_manifest, postgres_connection_kwargs, vector_table_names


async def fingerprint(connection, tables, *, details=False):
    contents = {}
    for table in sorted(tables):
        name = '"' + table.replace('"', '""') + '"'
        # Registry receives new rows legitimately; preserve all original ones.
        where = " WHERE workspace=ANY($1::text[])" if table == "lightrag_workspace_registry" else ""
        arguments = [list(WORKSPACES)] if where else []
        contents[table] = await connection.fetchval(
            "SELECT coalesce(jsonb_agg(value ORDER BY value::text),'[]'::jsonb)::text FROM "
            f"(SELECT to_jsonb(t) value FROM {name} t{where}) rows", *arguments)
    columns = await connection.fetch("SELECT table_name,column_name,data_type,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name=ANY($1::text[]) ORDER BY table_name,ordinal_position", list(tables))
    indexes = await connection.fetch("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=ANY($1::text[]) ORDER BY 1,2", list(tables))
    payload = {"contents": contents, "columns": [dict(row) for row in columns], "indexes": [dict(row) for row in indexes]}
    result = {"sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest(), "payload": payload}
    return result if details else result["sha256"]


def explain_difference(before, after):
    old, new = before["payload"], after["payload"]
    changed = {}
    for table in old["contents"]:
        if old["contents"][table] != new["contents"].get(table):
            changed[table] = {"before_sha256": hashlib.sha256(old["contents"][table].encode()).hexdigest(),
                              "after_sha256": hashlib.sha256(new["contents"].get(table, "").encode()).hexdigest(),
                              "before_bytes": len(old["contents"][table]), "after_bytes": len(new["contents"].get(table, ""))}
    result = {"changed_table_contents": changed}
    for key in ("columns", "indexes"):
        result[key + "_added"] = [row for row in new[key] if row not in old[key]]
        result[key + "_removed"] = [row for row in old[key] if row not in new[key]]
    return result


async def run_checks(base, control, receipt):
    import asyncpg
    import httpx
    from gateway import Config, WorkerManager, create_app
    from workspace_management import atomic_json, WorkspaceManagement

    config = Config(control, base / "inputs", base / "runtime", Path(os.environ.get("LIGHTRAG_MULTIWORKSPACE_STATIC", "/opt/lightrag-multiworkspace/webui")),
                    default_workspace=WORKSPACES[0], max_workers=2, storage_backend="postgres",
                    embedding_model=MODEL, postgres_database=os.environ["POSTGRES_DATABASE"])
    observer = await asyncpg.connect(**postgres_connection_kwargs())
    reader = await asyncpg.connect(**postgres_connection_kwargs())
    original_tables = [row["tablename"] for row in await observer.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    raw_migration = await fingerprint(observer, original_tables, details=True)
    original_manifests = hashes(control)
    # Model an already-running 0.2.3 installation, not an imported database
    # that has never been opened by the native runtime. The pinned upstream
    # PostgreSQLDB.check_tables() adds conditional shared secondary indexes on
    # first-ever startup. Actual diagnostic runs identified exactly 24 added
    # indexes, with no changed rows/columns or removed indexes. Bootstrap that
    # existing runtime explicitly before establishing the preservation oracle.
    bootstrap = WorkerManager(config)
    try:
        await bootstrap.start()
        async with bootstrap.lease(WORKSPACES[0]):
            pass
    finally:
        await bootstrap.stop()
    check(receipt, "pinned pre-existing runtime fixture completely stopped", not bootstrap.workers)
    original_snapshot = await fingerprint(observer, original_tables, details=True)
    bootstrap_difference = explain_difference(raw_migration, original_snapshot)
    receipt["existing_runtime_fixture_bootstrap"] = bootstrap_difference
    check(receipt, "existing-runtime fixture bootstrap preserves every original row and column", not bootstrap_difference["changed_table_contents"] and not bootstrap_difference["columns_added"] and not bootstrap_difference["columns_removed"])
    check(receipt, "existing-runtime fixture bootstrap only adds shared secondary indexes", not bootstrap_difference["indexes_removed"] and all(row["tablename"] in REQUIRED_TABLES for row in bootstrap_difference["indexes_added"]))
    check(receipt, "existing-runtime fixture bootstrap preserves original registration files", all(hashes(control).get(name) == value for name, value in original_manifests.items()))
    original_fingerprint = original_snapshot["sha256"]
    manager = WorkerManager(config)
    app = create_app(config, manager)
    try:
        async with reader.transaction(readonly=True):
            for table in REQUIRED_TABLES:
                await reader.fetchval(f'SELECT count(*) FROM public."{table}"')
            async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.synthetic", timeout=180) as client:
                async def done(operation):
                    task = manager.management.tasks.get(operation["id"])
                    if task:
                        await asyncio.wait_for(asyncio.shield(task), 90)
                    await asyncio.sleep(0)
                    result = await client.get("/workspace-operations/" + operation["id"])
                    check(receipt, "operation " + operation["workspaceId"] + " completed", result.status_code == 200 and result.json()["state"] == "succeeded")
                    return result.json()

                check(receipt, "database-aware readiness does not wake workers", (await client.get("/readyz")).status_code == 200 and not manager.workers)
                for index, name in enumerate(("Management-new", "Management_new")):
                    body = {"id": name, "displayName": "Synthetic new workspace", "description": "No real source content", "tags": ["smoke", "isolated"]}
                    headers = {"Idempotency-Key": "management-smoke-create-" + str(index)}
                    response = await client.post("/workspaces", json=body, headers=headers)
                    check(receipt, name + " creation accepted under held shared readers", response.status_code == 202)
                    operation = await done(response.json())
                    manifest = load_workspace_manifest(control / name / ".postgres-workspace.json", name)
                    check(receipt, name + " has three valid empty vector tables", all([await observer.fetchval(f'SELECT count(*) FROM public."{table}"') == 0 for table in vector_table_names(manifest).values()]))
                    repeated = await client.post("/workspaces", json=body, headers=headers)
                    check(receipt, name + " same key returns same completed operation", repeated.status_code == 200 and repeated.json()["id"] == operation["id"])
                    check(receipt, name + " changed body with same key conflicts", (await client.post("/workspaces", json={**body, "description": "different"}, headers=headers)).status_code == 409)
                    # Simulate process loss after database commit but before
                    # filesystem publication: remove ONLY our empty fixture's
                    # manifest and move its owned control directory to staging.
                    if index == 0:
                        record_path = manager.management._path(operation["id"])
                        record = json.loads(record_path.read_text())
                        (control / name / ".postgres-workspace.json").unlink()
                        (control / name).rename(manager.management.root / "staging" / operation["id"])
                        record.update(state="running", retryable=True)
                        atomic_json(record_path, record)
                        lost = await client.get("/workspace-operations/" + operation["id"])
                        check(receipt, "interrupted committed creation is reported explicitly", lost.json()["errorCode"] == "operation_interrupted")
                        resumed = await client.post("/workspaces", json=body, headers=headers)
                        await done(resumed.json())
                        check(receipt, "resumed registration preserves exact namespace", load_workspace_manifest(control / name / ".postgres-workspace.json", name) == manifest)
                check(receipt, "all original table contents and schema preserved by creation", await fingerprint(observer, original_tables) == original_fingerprint)
                check(receipt, "all original registration files preserved", all(hashes(control).get(name) == value for name, value in original_manifests.items()))
                check(receipt, "shared reader remains alive after creation", await reader.fetchval("SELECT 1") == 1)
                inventory = (await client.get("/workspaces")).json()
                check(receipt, "new and old exact public IDs remain independently discoverable", {item["id"] for item in inventory["workspaces"]} == {*WORKSPACES, "Management-new", "Management_new"})
                for name in ("Management-new", "Management_new", WORKSPACES[0]):
                    health = await client.get(f"/workspaces/{name}/health")
                    check(receipt, name + " native worker starts through path", health.status_code == 200 and health.json().get("selected_workspace_id") == name)
                    selected = await client.get("/health", headers={"LIGHTRAG-WORKSPACE": name})
                    check(receipt, name + " native worker starts through header", selected.status_code == 200 and selected.headers.get("X-LightRAG-Workspace") == name)
                    docs = await client.get(f"/workspaces/{name}/docs")
                    check(receipt, name + " scoped API docs remain correct", docs.status_code == 200 and f"/workspaces/{name}/openapi.json" in docs.text)
                after_startup = await fingerprint(observer, original_tables, details=True)
                if after_startup["sha256"] != original_fingerprint:
                    receipt["cold_startup_difference"] = explain_difference(original_snapshot, after_startup)
                    print(json.dumps({"cold_startup_difference": receipt["cold_startup_difference"]}, default=str), flush=True)
                check(receipt, "cold startup leaves original data unchanged", after_startup["sha256"] == original_fingerprint)
                created = await client.post("/workspaces/Management-new/graph/entity/create", json={"entity_name": "MANAGEMENT_SMOKE_NEW", "entity_data": {"description": "Synthetic new corpus", "entity_type": "CONCEPT"}})
                check(receipt, "new workspace accepts native graph/vector write", created.status_code == 200)
                for name in ("Management_new", WORKSPACES[0]):
                    isolated = await client.get(f"/workspaces/{name}/graph/entity/exists", params={"name": "MANAGEMENT_SMOKE_NEW"})
                    check(receipt, name + " isolated from new workspace write", isolated.status_code == 200 and isolated.json().get("exists") is False)
                patch = await client.patch("/workspaces/Management-new/metadata", json={"displayName": "Updated synthetic catalog", "tags": ["changed"]})
                check(receipt, "catalog editing changes labels without changing ID", patch.status_code == 200 and patch.json()["id"] == "Management-new")
                token = "management-smoke-maintenance-token-0123456789"
                maintenance = await client.post("/maintenance", json={"ownerToken": token})
                check(receipt, "maintenance entered with verified drained workers", maintenance.status_code == 200 and maintenance.json()["drained"])
                blocked = await client.post("/workspaces/Management-new/query", json={"query": "must not reach model"})
                check(receipt, "maintenance blocks queries before caches/providers", blocked.status_code == 503 and blocked.json()["code"] == "maintenance_active")
                check(receipt, "maintenance survives new management instance", WorkspaceManagement(config).maintenance(token)["ownerMatches"])
                check(receipt, "maintenance release succeeds for exact owner", (await client.post("/maintenance/release", json={"ownerToken": token})).status_code == 200)
                os.environ["LIGHTRAG_API_KEY"] = "synthetic-management-key"
                try:
                    check(receipt, "API key protects management operation receipts", (await client.get("/workspace-operations/" + operation["id"])).status_code == 401)
                    check(receipt, "guest JWT cannot bypass API key", (await client.patch("/workspaces/Management-new/metadata", json={"displayName": "Forbidden"}, headers={"Authorization": "Bearer forged-guest"})).status_code == 401)
                    check(receipt, "correct API key grants metadata operation", (await client.patch("/workspaces/Management-new/metadata", json={"description": "Authorized synthetic metadata"}, headers={"X-API-Key": "synthetic-management-key"})).status_code == 200)
                finally:
                    os.environ["LIGHTRAG_API_KEY"] = ""
        # A real gateway restart restores catalog and receipts, not just an
        # in-memory state check. Worker identity remains the same public ID.
        restarted = WorkerManager(config)
        restarted_app = create_app(config, restarted)
        async with restarted_app.router.lifespan_context(restarted_app), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted_app), base_url="http://gateway.synthetic", timeout=180) as client:
            rows = (await client.get("/workspaces")).json()["workspaces"]
            check(receipt, "catalog metadata survives gateway restart", next(item for item in rows if item["id"] == "Management-new")["displayName"] == "Updated synthetic catalog")
            exists = await client.get("/workspaces/Management-new/graph/entity/exists", params={"name": "MANAGEMENT_SMOKE_NEW"})
            check(receipt, "new PostgreSQL corpus survives gateway restart", exists.status_code == 200 and exists.json().get("exists") is True)
    finally:
        await reader.close()
        await observer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if os.environ.get("POSTGRES_DATABASE") != args.target_database:
        parser.error("POSTGRES_DATABASE must exactly match the disposable target database")
    receipt = {"passed": False, "checks": [], "real_provider_calls": 0}
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="lightrag-management-smoke-") as directory, fake_provider() as (provider, calls):
            base = Path(directory)
            source, control = base / "source", base / "control"
            seed(source)
            originals = hashes(source)
            os.environ.update(EMBEDDING_MODEL=MODEL, EMBEDDING_DIM=str(DIMENSION), LIGHTRAG_STORAGE_BACKEND="postgres",
                              POSTGRES_VECTOR_INDEX_TYPE="VCHORDRQ", POSTGRES_MAX_CONNECTIONS="2", POSTGRES_VCHORDRQ_EPSILON="2.3",
                              MAX_ASYNC_LLM="4", EMBEDDING_FUNC_MAX_ASYNC="4", MAX_ASYNC_RERANK="4", MAX_PARALLEL_INSERT="2", RERANK_BINDING="null",
                              LLM_BINDING="openai", LLM_BINDING_API_KEY="synthetic", LLM_MODEL=MODEL, LLM_BINDING_HOST=provider,
                              EMBEDDING_BINDING="openai", EMBEDDING_BINDING_API_KEY="synthetic", EMBEDDING_BINDING_HOST=provider,
                              EMBEDDING_TOKEN_LIMIT="8192", LIGHTRAG_API_KEY="", AUTH_ACCOUNTS="",
                              POSTGRES_SERVER_SETTINGS="lock_timeout=500ms&statement_timeout=30s")
            os.environ.pop("POSTGRES_WORKSPACE", None)
            from lightrag import ROLES
            for role in ROLES:
                os.environ.update({f"{role.env_prefix}_LLM_BINDING": "openai", f"{role.env_prefix}_LLM_BINDING_HOST": provider,
                                   f"{role.env_prefix}_LLM_BINDING_API_KEY": "synthetic", f"{role.env_prefix}_LLM_MODEL": MODEL})
            from migrate_postgres import Migration
            migration = argparse.Namespace(action="apply", source_root=source, control_root=control,
                target_database=args.target_database, target_token=str(uuid.uuid4()), embedding_model=MODEL,
                embedding_dim=DIMENSION, workspace=None, batch_size=16, statement_timeout=120, report=None)
            report = asyncio.run(Migration(migration).run())
            check(receipt, "fresh synthetic PostgreSQL fixture migrated", report["status"] == "verified")
            asyncio.run(run_checks(base, control, receipt))
            check(receipt, "source bytes preserved through complete management lifecycle", hashes(source) == originals)
            receipt.update(passed=True, synthetic_embedding_requests=len(calls))
    except Exception as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
    receipt["seconds"] = round(time.monotonic() - started, 2)
    if args.output:
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        args.output.chmod(0o600)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    raise SystemExit(0 if receipt["passed"] else 1)


if __name__ == "__main__":
    main()
