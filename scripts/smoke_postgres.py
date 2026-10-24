#!/usr/bin/env python3
"""Exercise native-file migration and PostgreSQL workspace routing in the image.

Run only against a newly created, dedicated database with vector and vchord
already enabled. The migration refuses a nonempty, unowned database. This client
creates synthetic corpora in a temporary directory and never needs real model
credentials. It leaves the owned database for its caller to inspect and remove.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from postgres_backend import (  # noqa: E402
    configure_postgres_workspace, install_postgres_vector_namespace,
    load_workspace_manifest, postgres_connection_kwargs, validate_postgres_readiness,
    vector_table_names, workspace_namespace,
)

DIMENSION = 3072
MODEL = "synthetic-postgres-smoke"
WORKSPACES = ("meeting-notes", "meeting_notes")
ENTITY = "ENTITY_Ä"
TARGET_ENTITY = "Узел"


def check(receipt: dict, name: str, condition: Any) -> None:
    if not condition:
        raise AssertionError(name)
    receipt["checks"].append(name)
    print("PASS " + name, flush=True)


def hashes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def seed(source: Path) -> dict:
    import networkx as nx
    import numpy as np

    fixtures = {}
    for number, name in enumerate(WORKSPACES):
        directory = source / name
        directory.mkdir(parents=True)
        chunks = {f"chunk-shared-{i}": {
            "tokens": 7, "chunk_order_index": i, "full_doc_id": "doc-shared",
            "content": f"{name} synthetic passage {i}", "file_path": "source.txt",
            "llm_cache_list": ["default:extract:cache-shared"], "heading": {"title": "Заголовок"},
            "sidecar": {"preserved": True}, "extra_original": {"workspace": name, "position": i},
        } for i in range(32)}
        full = {"doc-shared": {"content": f"Original document for {name}", "file_path": "source.txt",
                               "extra_original": {"unicode": "данные", "nested": [1, False, None]}}}
        status = {"doc-shared": {
            "content_summary": name, "content_length": 40, "status": "processed" if number == 0 else "pending",
            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-02T00:00:00+00:00",
            "file_path": "source.txt", "chunks_count": len(chunks), "chunks_list": list(chunks),
            "metadata": {"source": "synthetic", "workspace": name}, "extra_original": "retain status metadata",
        }}
        cache = {"default": {"cache-shared": {"original_prompt": "synthetic", "return": name,
            "cache_type": "extract", "chunk_id": "chunk-shared-0", "extra_original": ["retained", name]}}}
        for namespace, payload in (("full_docs", full), ("text_chunks", chunks), ("doc_status", status),
                                   ("llm_response_cache", cache)):
            (directory / f"kv_store_{namespace}.json").write_text(json.dumps(payload, ensure_ascii=False))
        graph = nx.Graph()
        graph.add_node(ENTITY, entity_id=ENTITY, entity_type="ORGANIZATION", description=name,
                       source_id="chunk-shared-0", file_path="source.txt", extra_number=7, extra_bool=True)
        graph.add_node(TARGET_ENTITY, entity_id=TARGET_ENTITY, entity_type="CONCEPT", description="Unicode target",
                       source_id="chunk-shared-1", file_path="source.txt")
        graph.add_edge(ENTITY, TARGET_ENTITY, description=name, keywords="synthetic", weight=1.0,
                       source_id="chunk-shared-0", file_path="source.txt", extra_property="сохранено")
        nx.write_graphml(graph, directory / "graph_chunk_entity_relation.graphml")
        vectors = {}
        for namespace, size in (("chunks", len(chunks)), ("entities", 2), ("relationships", 1)):
            matrix = np.zeros((size, DIMENSION), dtype=np.float32)
            for i in range(size):
                matrix[i, i] = 1
            records = []
            for i in range(size):
                value = {"__id__": f"{namespace}-shared-{i}", "__created_at__": 1735689600,
                         "content": f"{name} {namespace} {i}", "file_path": "source.txt",
                         "extra_original": {"workspace": name}}
                if namespace == "chunks":
                    value.update(chunks[f"chunk-shared-{i}"], __id__=f"chunk-shared-{i}")
                elif namespace == "entities":
                    value.update(entity_name=(ENTITY, TARGET_ENTITY)[i], source_id=f"chunk-shared-{i}")
                else:
                    value.update(src_id=ENTITY, tgt_id=TARGET_ENTITY, source_id="chunk-shared-0")
                records.append(value)
            payload = {"embedding_dim": DIMENSION, "data": records,
                       "matrix": base64.b64encode(matrix.tobytes()).decode("ascii")}
            (directory / f"vdb_{namespace}.json").write_text(json.dumps(payload, ensure_ascii=False))
            vectors[namespace] = (records, matrix)
        fixtures[name] = {"full": full, "chunks": chunks, "status": status, "cache": cache,
                          "vectors": vectors, "graph": graph}
    return fixtures


async def storage_checks(control: Path, fixtures: dict, receipt: dict) -> None:
    import asyncpg
    import numpy as np
    from pgvector.asyncpg import register_vector
    from lightrag.kg.shared_storage import initialize_share_data, finalize_share_data
    from lightrag.kg.postgres_impl import PGDocStatusStorage, PGKVStorage, PGVectorStorage
    from lightrag.kg.pgtable_impl import PGTableGraphStorage
    from lightrag.utils import EmbeddingFunc

    initialize_share_data()
    connection = await asyncpg.connect(**postgres_connection_kwargs())
    await register_vector(connection)
    try:
        for name in WORKSPACES:
            os.environ.pop("POSTGRES_WORKSPACE", None)
            manifest = configure_postgres_workspace(control / name, name)
            await validate_postgres_readiness(manifest)
            install_postgres_vector_namespace()
            fake_calls = []
            async def fake_embedding(texts: list[str]):
                fake_calls.append(len(texts))
                matrix = np.zeros((len(texts), DIMENSION), dtype=np.float32)
                matrix[:, 9] = 1
                return matrix
            embedding = EmbeddingFunc(embedding_dim=DIMENSION, max_token_size=8192,
                                      model_name=MODEL, func=fake_embedding)
            config = {"vector_storage": "PGVectorStorage", "embedding_batch_num": 16,
                      "vector_db_storage_cls_kwargs": {"cosine_better_than_threshold": 0.1}}
            stores = []
            def add(cls, namespace):
                store = cls(namespace=namespace, workspace="", global_config=config, embedding_func=embedding)
                stores.append(store)
                return store
            vectors = {namespace: add(PGVectorStorage, namespace) for namespace in fixtures[name]["vectors"]}
            full, cache = add(PGKVStorage, "full_docs"), add(PGKVStorage, "llm_response_cache")
            status = add(PGDocStatusStorage, "doc_status")
            graph = PGTableGraphStorage(namespace="chunk_entity_relation", workspace="", global_config=config,
                                        embedding_func=embedding)
            stores.append(graph)
            try:
                for store in stores:
                    await store.initialize()
                for namespace, (records, matrix) in fixtures[name]["vectors"].items():
                    table = vector_table_names(manifest)[namespace]
                    rows = await connection.fetch(f'SELECT id,content_vector FROM "{table}" WHERE workspace=$1', manifest["postgres_workspace"])
                    readback = {row["id"]: row["content_vector"].astype(np.float32).tobytes() for row in rows}
                    check(receipt, f"{name}: {namespace} every float32 vector is bit-exact",
                          readback == {row["__id__"]: vector.tobytes() for row, vector in zip(records, matrix, strict=True)})
                    results = await vectors[namespace].query("precomputed-only", top_k=1, query_embedding=matrix[0].tolist())
                    expected = ({"id": records[0]["__id__"]} if namespace == "chunks" else
                                {"entity_name": records[0]["entity_name"]} if namespace == "entities" else
                                {"src_id": records[0]["src_id"], "tgt_id": records[0]["tgt_id"]})
                    check(receipt, f"{name}: {namespace} native VCHORDRQ lookup", len(results) == 1 and
                          all(results[0].get(field) == value for field, value in expected.items()))
                check(receipt, f"{name}: native full document lookup", (await full.get_by_id("doc-shared"))["content"] == fixtures[name]["full"]["doc-shared"]["content"])
                check(receipt, f"{name}: legacy cache remains readable", (await cache.get_by_id("default:extract:cache-shared"))["return"] == name)
                counts = await status.get_status_counts()
                check(receipt, f"{name}: native document status count", counts.get("processed" if name == WORKSPACES[0] else "pending") == 1)
                node, edge = await graph.get_node(ENTITY), await graph.get_edge(ENTITY, TARGET_ENTITY)
                check(receipt, f"{name}: graph Unicode properties retained", node["description"] == name and node["extra_number"] == 7 and node["extra_bool"] is True and edge["extra_property"] == "сохранено")
                original = await connection.fetchval("SELECT original_json FROM lightrag_workspace_archive WHERE workspace=$1 AND namespace='full_docs' AND id='doc-shared'", manifest["postgres_workspace"])
                check(receipt, f"{name}: unsupported original metadata remains archived", json.loads(original) == fixtures[name]["full"]["doc-shared"])
                check(receipt, f"{name}: migration reads use no embedding calls", not fake_calls)
                vector = vectors["chunks"]
                # Upstream stores an uppercase SQL identifier and uses it
                # unquoted, so PostgreSQL folds the actual relation to lower.
                vector_table = vector.table_name.lower()
                await vector.upsert({"smoke-new-vector": {"content": "new synthetic content", "tokens": 3,
                    "chunk_order_index": 0, "full_doc_id": "smoke-doc", "file_path": "synthetic.txt"}})
                await vector.index_done_callback()
                check(receipt, f"{name}: explicit vector write commits fake embedding", len(fake_calls) == 1 and
                      await connection.fetchval(f'SELECT count(*) FROM "{vector_table}" WHERE workspace=$1 AND id=$2', manifest["postgres_workspace"], "smoke-new-vector") == 1)
                await vector.delete(["smoke-new-vector"])
                await vector.index_done_callback()
                check(receipt, f"{name}: explicit vector deletion commits", await connection.fetchval(
                    f'SELECT count(*) FROM "{vector_table}" WHERE workspace=$1 AND id=$2', manifest["postgres_workspace"], "smoke-new-vector") == 0)
            finally:
                for store in reversed(stores):
                    await store.finalize()
    finally:
        await connection.close()
        finalize_share_data()
        os.environ.pop("POSTGRES_WORKSPACE", None)


@contextmanager
def fake_provider():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/v1/embeddings":
                self.send_error(400, "Only synthetic embeddings are allowed")
                return
            inputs = payload["input"]
            if isinstance(inputs, str):
                inputs = [inputs]
            calls.append(len(inputs))
            vector = [1.0] + [0.0] * (DIMENSION - 1)
            encoded = {"object": "list", "model": MODEL, "data": [
                {"object": "embedding", "index": i, "embedding": vector} for i in range(len(inputs))],
                "usage": {"prompt_tokens": 1, "total_tokens": 1}}
            body = json.dumps(encoded).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def graph_reader_lock(receipt: dict):
    """Keep real shared graph reader locks across gateway cold starts/restart."""
    import asyncpg

    ready, release = threading.Event(), threading.Event()
    failures = []

    async def hold():
        connection = await asyncpg.connect(**postgres_connection_kwargs())
        try:
            async with connection.transaction(readonly=True):
                await connection.fetch("SELECT * FROM lightrag_graph_nodes LIMIT 1")
                await connection.fetch("SELECT * FROM lightrag_graph_edges LIMIT 1")
                count = await connection.fetchval("""SELECT count(*) FROM pg_locks
                    WHERE pid=pg_backend_pid() AND granted AND mode='AccessShareLock'
                      AND relation IN ('lightrag_graph_nodes'::regclass,'lightrag_graph_edges'::regclass)""")
                if count != 2:
                    raise AssertionError("Synthetic reader did not hold both graph table locks")
                ready.set()
                while not release.is_set():
                    # A heartbeat detects lost connections, so a vacuous pass
                    # after silently dropping the reader transaction is impossible.
                    await connection.fetchval("SELECT 1")
                    await asyncio.sleep(.25)
        finally:
            await connection.close()

    def run():
        try:
            asyncio.run(hold())
        except BaseException as error:
            failures.append(error)
        finally:
            ready.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        if not ready.wait(20):
            raise RuntimeError("Synthetic graph reader did not become ready")
        if failures:
            raise RuntimeError("Synthetic graph reader failed") from failures[0]
        check(receipt, "shared graph reader locks held before real worker startup", True)
        yield
        if failures:
            raise RuntimeError("Synthetic graph reader lost its transaction") from failures[0]
        check(receipt, "shared graph reader remained alive through gateway restart and API writes", True)
    finally:
        release.set()
        thread.join(timeout=20)
        if thread.is_alive():
            raise RuntimeError("Synthetic graph reader did not stop")
        if failures:
            raise RuntimeError("Synthetic graph reader failed") from failures[0]


def gateway_checks(base: Path, control: Path, receipt: dict) -> None:
    import httpx
    from lightrag import ROLES

    port = free_port()
    env = os.environ.copy()
    env.update(WORKING_DIR=str(control), INPUT_DIR=str(base / "inputs"),
               LIGHTRAG_MULTIWORKSPACE_RUNTIME=str(base / "runtime"), LIGHTRAG_DEFAULT_WORKSPACE=WORKSPACES[0],
               LIGHTRAG_MAX_ACTIVE_WORKSPACES="2", LIGHTRAG_STORAGE_BACKEND="postgres", MAX_ASYNC_LLM="4",
               EMBEDDING_FUNC_MAX_ASYNC="4", MAX_ASYNC_RERANK="4", MAX_PARALLEL_INSERT="2", RERANK_BINDING="null",
               LLM_BINDING="openai", LLM_BINDING_API_KEY="synthetic", LLM_MODEL=MODEL,
               EMBEDDING_BINDING="openai", EMBEDDING_BINDING_API_KEY="synthetic", EMBEDDING_MODEL=MODEL,
               EMBEDDING_DIM=str(DIMENSION), EMBEDDING_TOKEN_LIMIT="8192", LIGHTRAG_API_KEY="", AUTH_ACCOUNTS="",
               POSTGRES_SERVER_SETTINGS="lock_timeout=500ms&statement_timeout=30s")
    env.pop("POSTGRES_WORKSPACE", None)
    log = base / "gateway.log"
    with fake_provider() as (provider, calls), log.open("wb") as log_stream, graph_reader_lock(receipt):
        env.update(LLM_BINDING_HOST=provider, EMBEDDING_BINDING_HOST=provider)
        for role in ROLES:
            env.update({f"{role.env_prefix}_LLM_BINDING": "openai", f"{role.env_prefix}_LLM_BINDING_HOST": provider,
                        f"{role.env_prefix}_LLM_BINDING_API_KEY": "synthetic", f"{role.env_prefix}_LLM_MODEL": MODEL})
        def start():
            return subprocess.Popen([sys.executable, "-m", "uvicorn", "gateway:app", "--host", "127.0.0.1",
                "--port", str(port)], cwd=ROOT, env=env, stdout=log_stream, stderr=subprocess.STDOUT, start_new_session=True)
        process = start()
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180)
        def stop():
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
        def ready():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    response = client.get("/workspaces")
                    if response.status_code == 200:
                        return response.json()
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    break
                time.sleep(.2)
            raise RuntimeError("Synthetic PostgreSQL gateway did not start")
        try:
            inventory = ready()
            check(receipt, "gateway discovers both exact punctuation-distinct identities", {item["id"] for item in inventory["workspaces"]} == set(WORKSPACES))
            check(receipt, "gateway inventory starts no workers", all(not item["loaded"] for item in inventory["workspaces"]))
            for name in WORKSPACES:
                for path, headers in ((f"/workspaces/{name}/health", {}), ("/health", {"LIGHTRAG-WORKSPACE": name})):
                    response = client.get(path, headers=headers)
                    check(receipt, f"{name}: actual PostgreSQL worker selected through {path}", response.status_code == 200 and response.json().get("selected_workspace_id") == name and response.headers.get("X-LightRAG-Workspace") == name)
                labels = client.get(f"/workspaces/{name}/graph/label/list")
                check(receipt, f"{name}: graph API reads migrated Unicode labels", labels.status_code == 200 and ENTITY in labels.json())
                docs = client.get(f"/workspaces/{name}/docs")
                check(receipt, f"{name}: API documentation remains workspace-scoped", docs.status_code == 200 and f"/workspaces/{name}/openapi.json" in docs.text)
                ui = client.get(f"/workspaces/{name}/webui/")
                check(receipt, f"{name}: UI retains canonical public selection", ui.status_code == 200 and f'"workspaceId": "{name}"' in ui.text)
            (control / "unregistered-empty").mkdir()
            check(receipt, "unregistered empty workspace fails closed", client.get("/workspaces/unregistered-empty/health").status_code == 404)
            check(receipt, "unknown workspace fails closed", client.get("/workspaces/unknown/health").status_code == 404)
            check(receipt, "conflicting API selectors fail closed", client.get(f"/workspaces/{WORKSPACES[0]}/health", headers={"LIGHTRAG-WORKSPACE": WORKSPACES[1]}).status_code == 400)
            protected = client.post(f"/workspaces/{WORKSPACES[1]}/graph/entity/create", json={"entity_name": "must-not-write", "entity_data": {"description": "blocked"}})
            check(receipt, "migrated pending backlog stays protected from new ingestion", protected.status_code == 409)
            created = client.post(f"/workspaces/{WORKSPACES[0]}/graph/entity/create", json={"entity_name": "SMOKE_NEW_ENTITY", "entity_data": {"description": "created through API", "entity_type": "CONCEPT"}})
            check(receipt, "graph API creates entity with synthetic embedding", created.status_code == 200 and bool(calls))
            isolated = client.get(f"/workspaces/{WORKSPACES[1]}/graph/entity/exists", params={"name": "SMOKE_NEW_ENTITY"})
            check(receipt, "API writes remain isolated from adjacent workspace", isolated.status_code == 200 and isolated.json().get("exists") is False)
            stop()
            process = start()
            ready()
            exists = client.get(f"/workspaces/{WORKSPACES[0]}/graph/entity/exists", params={"name": "SMOKE_NEW_ENTITY"})
            check(receipt, "PostgreSQL API write survives worker and gateway restart", exists.status_code == 200 and exists.json().get("exists") is True)
            deleted = client.request("DELETE", f"/workspaces/{WORKSPACES[0]}/graph/entity/delete", json={"entity_name": "SMOKE_NEW_ENTITY"})
            check(receipt, "graph API entity deletion persists", deleted.status_code == 200 and client.get(f"/workspaces/{WORKSPACES[0]}/graph/entity/exists", params={"name": "SMOKE_NEW_ENTITY"}).json().get("exists") is False)
            receipt["synthetic_http_embedding_requests"] = len(calls)
        finally:
            stop()
            client.close()
            if not receipt.get("passed"):
                # Only this synthetic fixture's logs; no real provider secrets.
                receipt["gateway_log_tail"] = log.read_text(errors="replace")[-8000:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--target-token", default=str(uuid.uuid4()))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if os.environ.get("POSTGRES_DATABASE") != args.target_database:
        parser.error("POSTGRES_DATABASE must exactly match the dedicated --target-database")
    args.target_token = str(uuid.UUID(args.target_token))
    receipt = {"passed": False, "checks": [], "real_provider_calls": 0, "dimension": DIMENSION,
               "workspaces": list(WORKSPACES), "database": args.target_database, "db_instance_id": args.target_token}
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="lightrag-pg-smoke-") as temporary:
            base = Path(temporary)
            source, control = base / "source", base / "control"
            fixtures = seed(source)
            before = hashes(source)
            os.environ.update(EMBEDDING_MODEL=MODEL, EMBEDDING_DIM=str(DIMENSION),
                LIGHTRAG_STORAGE_BACKEND="postgres", POSTGRES_VECTOR_INDEX_TYPE="VCHORDRQ", POSTGRES_MAX_CONNECTIONS="2", POSTGRES_VCHORDRQ_EPSILON="2.3")
            os.environ.pop("POSTGRES_WORKSPACE", None)
            from migrate_postgres import Migration
            for action in ("plan", "apply", "verify"):
                migration = argparse.Namespace(action=action, source_root=source, control_root=control,
                    target_database=args.target_database, target_token=args.target_token, embedding_model=MODEL,
                    embedding_dim=DIMENSION, workspace=None, batch_size=16, statement_timeout=120, report=None)
                report = asyncio.run(Migration(migration).run())
                check(receipt, "native migration " + action, report["status"] == ("planned" if action == "plan" else "verified"))
            check(receipt, "original file fixtures unchanged after full migration", hashes(source) == before)
            manifests = [load_workspace_manifest(control / name / ".postgres-workspace.json", name) for name in WORKSPACES]
            check(receipt, "separate PostgreSQL vector tables for punctuation-distinct identities", set(vector_table_names(manifests[0]).values()).isdisjoint(vector_table_names(manifests[1]).values()))
            asyncio.run(storage_checks(control, fixtures, receipt))
            gateway_checks(base, control, receipt)
            check(receipt, "source bytes unchanged after native reads, API writes, restart and deletion", hashes(source) == before)
            receipt.pop("gateway_log_tail", None)
            receipt["passed"] = True
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
