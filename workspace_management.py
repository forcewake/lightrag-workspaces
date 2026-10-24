"""Explicit, durable workspace management; query routing never creates a corpus.

Receipts and catalog metadata live beneath the backed-up control root. PostgreSQL
creation owns only a new namespace and three new vector tables. Shared schema
bootstrap and repair remain offline operations.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
import uuid

from fastapi import HTTPException, Request

from postgres_backend import (
    MANIFEST_FILENAME, REQUIRED_TABLES, WORKSPACE_PATTERN, load_workspace_manifest,
    postgres_connection_kwargs, validate_postgres_readiness, vector_table_names,
    workspace_namespace,
)
from postgres_startup import validate_postgres_graph_schema

CAPABILITIES = {"management": True, "metadata": True, "create": True, "prewarm": True,
                "maintenance": True}
TOKEN_PATTERN = re.compile(r"[!-~]{16,128}\Z")
OP_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def failure(status: int, code: str, detail: str, *, retryable: bool = False,
            retry_after: int | None = None) -> HTTPException:
    return HTTPException(status, {"code": code, "message": detail, "retryable": retryable},
                         headers={"Retry-After": str(retry_after)} if retry_after else None)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists() and not path.is_symlink():
        return default
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("Management state must be a bounded regular file")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise RuntimeError("Management state must not be a symbolic link")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def metadata_body(value: Any, *, create: bool = False) -> dict[str, Any]:
    allowed = {"displayName", "description", "tags"} | ({"id"} if create else set())
    if not isinstance(value, dict) or set(value) - allowed:
        raise failure(422, "invalid_metadata", "Supply only the supported workspace fields.")
    result = dict(value)
    if create:
        if not isinstance(result.get("id"), str) or not WORKSPACE_PATTERN.fullmatch(result["id"]):
            raise failure(422, "invalid_workspace_id", "Use one exact 1-128 character workspace identifier.")
        result.setdefault("displayName", result["id"])
        result.setdefault("description", "")
        result.setdefault("tags", [])
    for key, maximum in (("displayName", 160), ("description", 2000)):
        if key in result:
            text = result[key]
            if (not isinstance(text, str) or len(text) > maximum
                    or (key == "displayName" and not text.strip())
                    or any(ord(char) < 32 and char not in "\n\t" for char in text)):
                raise failure(422, "invalid_metadata", f"Invalid {key}.")
            result[key] = text.strip()
    if "tags" in result:
        tags = result["tags"]
        if (not isinstance(tags, list) or len(tags) > 20 or any(
                not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 48
                or any(ord(char) < 32 for char in tag) for tag in tags)):
            raise failure(422, "invalid_metadata", "Use at most 20 nonempty tags of up to 48 characters.")
        result["tags"] = list(dict.fromkeys(tag.strip() for tag in tags))
    return result


async def body_json(request: Request) -> Any:
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise failure(415, "json_required", "Management requests require application/json.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 16384:
            raise failure(413, "request_too_large", "Management request exceeds 16 KiB.")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except (ValueError, UnicodeDecodeError):
        raise failure(422, "invalid_json", "Supply a valid JSON object.") from None


def _upstream_auth_handler():
    # Match the worker's existing argv isolation. Import is synchronous; no
    # global argument mutation spans an await or another event-loop task.
    arguments = sys.argv
    try:
        sys.argv = [arguments[0]]
        from lightrag import __version__
        if __version__ != "1.5.7":
            raise RuntimeError("Unsupported authentication adapter")
        from lightrag.api.auth import auth_handler
        return auth_handler
    finally:
        sys.argv = arguments


def internal_auth_headers() -> dict[str, str]:
    """Authenticate only loopback supervisor health checks in password mode."""
    if os.environ.get("LIGHTRAG_API_KEY"):
        return {"X-API-Key": os.environ["LIGHTRAG_API_KEY"]}
    if os.environ.get("AUTH_ACCOUNTS"):
        handler = _upstream_auth_handler()
        if not handler.accounts:
            raise RuntimeError("Configured supervisor authentication is unavailable")
        token = handler.create_token(next(iter(handler.accounts)), role="user", custom_expire_hours=1 / 12)
        return {"Authorization": "Bearer " + token}
    return {}


def authenticate(request: Request) -> None:
    """Use the same API-key/non-guest JWT trust as the pinned upstream server.

    Whitelisted public health paths never grant management access. In key-only
    mode a guest JWT cannot replace the key. No separate signing secret exists.
    """
    for name in (b"authorization", b"x-api-key"):
        if sum(key.lower() == name for key, _ in request.scope["headers"]) > 1:
            raise failure(400, "ambiguous_credentials", "Supply one credential header of each type.")
    key = os.environ.get("LIGHTRAG_API_KEY", "")
    accounts = os.environ.get("AUTH_ACCOUNTS", "")
    supplied = request.headers.get("X-API-Key", "")
    if key and supplied and hmac.compare_digest(key, supplied):
        return
    if not key and not accounts:
        return
    authorization = request.headers.get("Authorization", "")
    if accounts and authorization.lower().startswith("bearer "):
        try:
            info = _upstream_auth_handler().validate_token(authorization[7:])
            if info.get("role") != "guest":
                return
        except HTTPException:
            pass
        except Exception:
            raise failure(503, "authentication_unavailable", "Authentication configuration is unavailable.") from None
    raise failure(401, "authentication_required", "Supply the configured API key or sign in.")


class WorkspaceManagement:
    def __init__(self, config: Any):
        self.config = config
        self.root = config.storage_root / ".workspace-management"
        self.lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task] = {}
        self.owner_file = None

    def _ensure_root(self) -> None:
        for root in (self.config.storage_root, self.config.input_root):
            if root.is_symlink():
                raise RuntimeError("Workspace roots must not be symbolic links")
            root.mkdir(parents=True, exist_ok=True)
        if (self.config.storage_root.resolve().is_relative_to(self.config.input_root.resolve())
                or self.config.input_root.resolve().is_relative_to(self.config.storage_root.resolve())):
            raise RuntimeError("Workspace roots must be separate, non-nested directories")
        if self.root.is_symlink():
            raise RuntimeError("Management root must not be a symbolic link")
        self.root.mkdir(mode=0o700, exist_ok=True)
        for name in ("operations", "staging"):
            folder = self.root / name
            if folder.is_symlink():
                raise RuntimeError("Management directories must not be symbolic links")
            folder.mkdir(mode=0o700, exist_ok=True)

    def start(self) -> None:
        self._ensure_root()
        fd = os.open(self.root / "gateway.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise RuntimeError("Another gateway owns this control root") from None
        self.owner_file = fd

    async def cancel_operations(self) -> None:
        # Keep durable receipts for the next explicit same-key request. A
        # cancelled PG transaction rolls back; a committed one is reconciled.
        for task in self.tasks.values():
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def close(self) -> None:
        await self.cancel_operations()
        if self.owner_file is not None:
            os.close(self.owner_file)
            self.owner_file = None

    def catalog(self) -> dict[str, Any]:
        catalog = read_json(self.root / "catalog.json", {})
        if not isinstance(catalog, dict):
            raise RuntimeError("Invalid catalog metadata")
        return catalog

    def decorate(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        catalog = self.catalog()
        for item in items:
            item.update(displayName=item["id"], description="", tags=[])
            item.update(metadata_body(catalog.get(item["id"], {})))
        return items

    def maintenance(self, token: str | None = None) -> dict[str, Any]:
        state = read_json(self.root / "maintenance.json", {})
        if not isinstance(state, dict):
            raise RuntimeError("Invalid maintenance state")
        return {"active": bool(state.get("active")), "since": state.get("since"),
                "ownerMatches": bool(token and state.get("ownerHash") and
                    hmac.compare_digest(digest(token), state["ownerHash"]))}

    def assert_available(self) -> None:
        if self.maintenance()["active"]:
            raise failure(503, "maintenance_active", "Maintenance is in progress; no new work was accepted.",
                          retryable=True, retry_after=10)

    async def set_maintenance(self, body: Any, *, release: bool = False) -> None:
        if (not isinstance(body, dict) or set(body) != {"ownerToken"}
                or not isinstance(body["ownerToken"], str)
                or not TOKEN_PATTERN.fullmatch(body["ownerToken"]) or len(body["ownerToken"]) < 32):
            raise failure(422, "invalid_maintenance_token", "Supply a private random ownerToken of 32-128 characters.")
        async with self.lock:
            state = self.maintenance(body["ownerToken"])
            if state["active"] and not state["ownerMatches"]:
                raise failure(409, "maintenance_owned", "Another owner holds maintenance.")
            if release:
                if not state["active"]:
                    return
                payload = {"active": False, "releasedAt": utcnow()}
            else:
                payload = {"active": True, "ownerHash": digest(body["ownerToken"]),
                           "since": state["since"] if state["active"] else utcnow()}
            self._ensure_root()
            atomic_json(self.root / "maintenance.json", payload)

    async def update_metadata(self, workspace_id: str, body: Any) -> dict[str, Any]:
        patch = metadata_body(body)
        async with self.lock:
            self.assert_available()
            self._ensure_root()
            catalog = self.catalog()
            entry = {"displayName": workspace_id, "description": "", "tags": [],
                     **catalog.get(workspace_id, {}), **patch}
            catalog[workspace_id] = entry
            atomic_json(self.root / "catalog.json", catalog)
            return {"id": workspace_id, **entry}

    def _path(self, operation_id: str) -> Path:
        if not OP_PATTERN.fullmatch(operation_id):
            raise failure(404, "operation_not_found", "Unknown workspace operation.")
        return self.root / "operations" / (operation_id + ".json")

    def operation(self, operation_id: str) -> dict[str, Any]:
        operation = read_json(self._path(operation_id))
        if not isinstance(operation, dict):
            raise failure(404, "operation_not_found", "Unknown workspace operation.")
        public = {key: operation.get(key) for key in (
            "id", "state", "workspaceId", "createdAt", "updatedAt", "errorCode", "retryable")}
        if public["state"] in ("pending", "running") and operation_id not in self.tasks:
            public.update(state="failed", errorCode="operation_interrupted", retryable=True)
        if public["state"] == "succeeded":
            public["workspace"] = {"id": operation["workspaceId"], **operation["metadata"]}
        return public

    async def create(self, body: Any, key: str | None) -> dict[str, Any]:
        data = metadata_body(body, create=True)
        if not key or not TOKEN_PATTERN.fullmatch(key):
            raise failure(422, "idempotency_key_required", "Supply an Idempotency-Key of 16-128 visible ASCII characters.")
        operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "lightrag-workspaces:create:" + digest(key)))
        async with self.lock:
            self.assert_available()
            self._ensure_root()
            path = self._path(operation_id)
            record = read_json(path)
            if record is not None:
                if record.get("requestHash") != digest(data):
                    raise failure(409, "idempotency_conflict", "This key belongs to another creation request.")
                if record.get("state") == "succeeded" or operation_id in self.tasks:
                    return self.operation(operation_id)
                if not record.get("retryable", True):
                    return self.operation(operation_id)
            else:
                if (self.config.storage_root / data["id"]).exists() or (self.config.storage_root / data["id"]).is_symlink():
                    raise failure(409, "workspace_exists", "That exact workspace already exists; it was not changed.")
                for receipt in (self.root / "operations").glob("*.json"):
                    other = read_json(receipt)
                    if other.get("workspaceId") == data["id"]:
                        raise failure(409, "workspace_reserved", "Another creation operation owns this workspace ID; reuse its key.")
                record = {"id": operation_id, "workspaceId": data["id"],
                          "requestHash": digest(data), "metadata": {k: v for k, v in data.items() if k != "id"},
                          "state": "pending", "createdAt": utcnow(), "retryable": True,
                          "backend": self.config.storage_backend,
                          "embeddingModel": self.config.embedding_model,
                          "embeddingDim": self.config.embedding_dim,
                          "database": self.config.postgres_database}
            if any(record.get(key) != value for key, value in (
                    ("backend", self.config.storage_backend), ("embeddingModel", self.config.embedding_model),
                    ("embeddingDim", self.config.embedding_dim), ("database", self.config.postgres_database))):
                raise failure(409, "operation_configuration_changed", "Creation configuration changed; inspect its retained receipt.")
            record.update(state="pending", errorCode=None, updatedAt=utcnow())
            atomic_json(path, record)
            task = asyncio.create_task(self._run(record))
            self.tasks[operation_id] = task
            task.add_done_callback(lambda done, name=operation_id: self.tasks.pop(name, None))
            return self.operation(operation_id)

    def _owned_directory(self, path: Path, operation: dict[str, Any]) -> None:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise failure(409, "workspace_path_conflict", "A workspace path is already occupied.")
        marker = path / ".workspace-created"
        ownership = {"operationId": operation["id"], "workspaceId": operation["workspaceId"]}
        if not path.exists():
            # Publish a directory with its durable owner already inside. A
            # crash cannot leave an ownerless final input directory; the hidden
            # temporary name is never advertised or scanned as a workspace.
            temporary = Path(tempfile.mkdtemp(prefix=".workspace-create-", dir=path.parent))
            atomic_json(temporary / marker.name, ownership)
            if path.exists() or path.is_symlink():
                raise failure(409, "workspace_path_conflict", "A workspace path appeared during creation.")
            temporary.rename(path)
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        elif read_json(marker) != ownership:
            raise failure(409, "workspace_path_conflict", "An existing workspace path has another owner.")

    async def _run(self, record: dict[str, Any]) -> None:
        path = self._path(record["id"])
        try:
            record.update(state="running", updatedAt=utcnow())
            atomic_json(path, record)
            public_id = record["workspaceId"]
            stage = self.root / "staging" / record["id"]
            target = self.config.storage_root / public_id
            inputs = self.config.input_root / public_id
            # Both input and control paths must be new or owned by this exact
            # durable operation. A retry never adopts an existing corpus.
            self._owned_directory(inputs, record)
            self._owned_directory(target if target.exists() else stage, record)
            if self.config.storage_backend == "postgres":
                manifest = await create_postgres_namespace(self.config, record)
                await validate_postgres_readiness(manifest)
                published = target if target.exists() else stage
                current = read_json(published / MANIFEST_FILENAME)
                if current is not None and current != manifest:
                    raise failure(409, "workspace_manifest_conflict", "An existing registration has another identity.")
                atomic_json(published / MANIFEST_FILENAME, manifest)
            async with self.lock:
                catalog = self.catalog()
                if public_id not in catalog:
                    catalog[public_id] = record["metadata"]
                    atomic_json(self.root / "catalog.json", catalog)
                # The directory rename is the publication boundary for both
                # backends. Queries cannot observe partial storage creation.
                if target.is_symlink():
                    raise failure(409, "workspace_path_conflict", "Workspace publication encountered a symbolic link.")
                if not target.exists():
                    stage.rename(target)
                    fd = os.open(self.config.storage_root, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                record.update(state="succeeded", errorCode=None, retryable=False, updatedAt=utcnow())
                atomic_json(path, record)
        except asyncio.CancelledError:
            record.update(state="failed", errorCode="operation_interrupted", retryable=True, updatedAt=utcnow())
            atomic_json(path, record)
            raise
        except Exception as error:
            detail = error.detail if isinstance(error, HTTPException) and isinstance(error.detail, dict) else {}
            record.update(state="failed", errorCode=detail.get("code", "workspace_creation_failed"),
                          retryable=not isinstance(error, HTTPException) or bool(detail.get("retryable")),
                          updatedAt=utcnow())
            # Exception strings can contain credentials. Retain only stable
            # categories; the owned state remains available for explicit retry.
            atomic_json(path, record)


async def create_postgres_namespace(config: Any, operation: dict[str, Any], *, connect: Any = None) -> dict[str, Any]:
    """Create only three absent vector tables and one owned registry row.

    All writes are one transaction. An interrupted publication reuses the
    committed row only when its operation ID and complete configuration match.
    Existing shared tables are inspected, never bootstrapped or altered.
    """
    if connect is None:
        import asyncpg
        connect = asyncpg.connect
    connection = await connect(**postgres_connection_kwargs())
    public_id = operation["workspaceId"]
    namespace = workspace_namespace(public_id)
    source = {"kind": "empty-workspace", "operation_id": operation["id"], "files": []}
    source["sha256"] = digest(source)
    try:
        async with connection.transaction(readonly=True):
            await connection.execute("SET LOCAL statement_timeout = '10s'")
            marker = await connection.fetchrow("SELECT current_database() AS database,db_instance_id::text AS db_instance_id,schema_version FROM public.lightrag_workspace_database WHERE singleton=true")
            if not marker or marker["schema_version"] != 1 or marker["database"] != config.postgres_database:
                raise failure(409, "database_identity_mismatch", "The verified database marker is unavailable or incompatible.")
            for folder in config.storage_root.iterdir():
                if folder.is_dir() and not folder.is_symlink() and WORKSPACE_PATTERN.fullmatch(folder.name) and (folder / MANIFEST_FILENAME).exists():
                    existing = load_workspace_manifest(folder / MANIFEST_FILENAME, folder.name)
                    if existing["db_instance_id"] != marker["db_instance_id"] or existing["database"] != marker["database"]:
                        raise failure(409, "database_identity_mismatch", "Existing registrations identify another database.")
            for table in REQUIRED_TABLES:
                if not await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", "public." + table):
                    raise failure(409, "database_schema_not_ready", "Shared storage must be provisioned offline before creating a workspace.")
            await validate_postgres_graph_schema(connection, namespace)
            extensions = await connection.fetch("SELECT extname FROM pg_extension WHERE extname IN ('vector','vchord')")
            if {row["extname"] for row in extensions} != {"vector", "vchord"}:
                raise failure(409, "database_schema_not_ready", "The configured vector extensions are unavailable.")
        manifest = {"schema_version": 1, "workspace_id": public_id,
                    "postgres_workspace": namespace, "status": "ready",
                    "embedding_model": config.embedding_model, "embedding_dim": config.embedding_dim,
                    "database": marker["database"], "db_instance_id": marker["db_instance_id"],
                    "source_manifest_sha256": source["sha256"], "counts": {},
                    "preserve_backlog": False, "read_only": False}
        tables = vector_table_names(manifest)
        manifest["vector_tables"] = tables
        async with connection.transaction():
            await connection.execute("SET LOCAL search_path = public")
            await connection.execute("SET LOCAL lock_timeout = '2s'")
            await connection.execute("SET LOCAL statement_timeout = '30s'")
            if not await connection.fetchval("SELECT pg_try_advisory_xact_lock(hashtext('lightrag_workspace_migration'))"):
                raise failure(503, "database_migration_busy", "An offline database operation is active.", retryable=True, retry_after=5)
            locked_marker = await connection.fetchrow("SELECT current_database() AS database,db_instance_id::text AS db_instance_id,schema_version FROM public.lightrag_workspace_database WHERE singleton=true FOR SHARE")
            if not locked_marker or dict(locked_marker) != dict(marker):
                raise failure(409, "database_identity_mismatch", "Database identity changed before creation; no workspace was published.")
            row = await connection.fetchrow("SELECT * FROM public.lightrag_workspace_registry WHERE workspace=$1 OR postgres_workspace=$2", public_id, namespace)
            if row:
                expected = {"workspace": public_id, "postgres_workspace": namespace, "migration_id": operation["id"],
                            "db_instance_id": marker["db_instance_id"], "status": "ready",
                            "embedding_model": config.embedding_model, "embedding_dim": config.embedding_dim}
                if any(str(row[key]) != str(value) for key, value in expected.items()):
                    raise failure(409, "workspace_database_conflict", "An existing database namespace has another owner.")
                saved_source = json.loads(row["source_manifest"]) if isinstance(row["source_manifest"], str) else row["source_manifest"]
                if saved_source != source:
                    raise failure(409, "workspace_database_conflict", "The database operation receipt does not match.")
                return manifest
            for table in (*REQUIRED_TABLES, "lightrag_workspace_archive"):
                if await connection.fetchval(f'SELECT EXISTS (SELECT 1 FROM public."{table}" WHERE workspace=$1)', namespace):
                    raise failure(409, "workspace_database_conflict", "The requested namespace contains unowned data.")
            for table in tables.values():
                if await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", "public." + table):
                    raise failure(409, "workspace_database_conflict", "A requested vector table already exists.")
            from lightrag import __version__
            if __version__ != "1.5.7":
                raise failure(409, "unsupported_native_schema", "Creation requires the pinned native schema version.")
            from lightrag.kg.postgres_impl import TABLES, _safe_index_name
            bases = {"chunks": "LIGHTRAG_VDB_CHUNKS", "entities": "LIGHTRAG_VDB_ENTITY", "relationships": "LIGHTRAG_VDB_RELATION"}
            for kind, table in tables.items():
                ddl = TABLES[bases[kind]]["ddl"].replace(bases[kind], table).replace("VECTOR(dimension)", f"VECTOR({config.embedding_dim})")
                await connection.execute(ddl)
                # These are newly-created, operation-owned tables only.
                for column in ["id"] + {"entities": ["entity_name"], "relationships": ["source_id", "target_id"]}.get(kind, []):
                    await connection.execute(f'ALTER TABLE public."{table}" ALTER COLUMN "{column}" TYPE TEXT')
                name = _safe_index_name(table, "vchordrq_cosine")
                options = "build.pin = -1\nbuild.internal.lists = []\nbuild.internal.build_threads = 1\ndegree_of_parallelism = 1"
                await connection.execute(f'CREATE INDEX "{name}" ON public."{table}" USING vchordrq(content_vector vector_cosine_ops) WITH(options=$${options}$$)')
                index = await connection.fetchrow("SELECT i.indisvalid,i.indisready,am.amname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_am am ON am.oid=c.relam WHERE i.indexrelid=$1::regclass", "public." + name)
                if not index or not index["indisvalid"] or not index["indisready"] or index["amname"] != "vchordrq":
                    raise RuntimeError("New vector index verification failed")
            await connection.execute("INSERT INTO public.lightrag_workspace_registry(workspace,postgres_workspace,db_instance_id,migration_id,status,embedding_model,embedding_dim,source_manifest,counts,verification) VALUES($1,$2,$3,$4,'ready',$5,$6,$7::jsonb,'{}'::jsonb,$8::jsonb)",
                                     public_id, namespace, uuid.UUID(marker["db_instance_id"]), operation["id"], config.embedding_model, config.embedding_dim,
                                     json.dumps(source), json.dumps({"empty_creation": True, "operation_id": operation["id"]}))
        return manifest
    finally:
        await connection.close()
