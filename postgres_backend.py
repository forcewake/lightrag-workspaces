"""PostgreSQL workspace identity and migration-readiness checks.

Public workspace IDs remain directory and API identities. PostgreSQL receives a
separate stable namespace, so upstream identifier normalization cannot merge two
public IDs. A worker may only open an explicitly completed migration in the
configured database; startup never imports or repairs a corpus.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import ssl
from typing import Any, Mapping, MutableMapping
from uuid import UUID

from postgres_startup import validate_postgres_graph_schema


MANIFEST_FILENAME = ".postgres-workspace.json"
MANIFEST_VERSION = 1
WORKSPACE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
POSTGRES_STORAGES = {
    "kv_storage": "PGKVStorage",
    "doc_status_storage": "PGDocStatusStorage",
    "vector_storage": "PGVectorStorage",
    "graph_storage": "PGTableGraphStorage",
}
STORAGE_ENVIRONMENT = {
    "LIGHTRAG_KV_STORAGE": "PGKVStorage",
    "LIGHTRAG_DOC_STATUS_STORAGE": "PGDocStatusStorage",
    "LIGHTRAG_VECTOR_STORAGE": "PGVectorStorage",
    "LIGHTRAG_GRAPH_STORAGE": "PGTableGraphStorage",
}
REQUIRED_TABLES = (
    "lightrag_doc_full", "lightrag_doc_chunks", "lightrag_doc_status",
    "lightrag_llm_cache", "lightrag_full_entities", "lightrag_full_relations",
    "lightrag_entity_chunks", "lightrag_relation_chunks",
    "lightrag_graph_nodes", "lightrag_graph_edges",
)


def selected_backend(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    backend = source.get("LIGHTRAG_STORAGE_BACKEND", "file").strip().lower()
    if backend not in {"file", "postgres"}:
        raise ValueError("LIGHTRAG_STORAGE_BACKEND must be file or postgres")
    return backend


def workspace_namespace(workspace_id: str) -> str:
    if not WORKSPACE_PATTERN.fullmatch(workspace_id):
        raise ValueError("Invalid workspace ID")
    return "ws_" + hashlib.sha256(workspace_id.encode("ascii")).hexdigest()[:32]


def load_workspace_manifest(path: Path, workspace_id: str) -> dict[str, Any]:
    """Read a bounded, regular-file completion receipt from the control root."""
    expected_namespace = workspace_namespace(workspace_id)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError("PostgreSQL workspace needs a regular migration manifest")
    with path.open("rb") as stream:
        raw = stream.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("PostgreSQL workspace manifest exceeds its size limit")
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("Invalid PostgreSQL workspace manifest") from error
    if not isinstance(manifest, dict):
        raise ValueError("Invalid PostgreSQL workspace manifest")
    if (type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != MANIFEST_VERSION
            or manifest.get("workspace_id") != workspace_id
            or manifest.get("postgres_workspace") != expected_namespace
            or manifest.get("status") != "ready"):
        raise ValueError("PostgreSQL workspace migration is incomplete or has another identity")
    if (type(manifest.get("embedding_dim")) is not int
            or not 1 <= manifest["embedding_dim"] <= 16000
            or not isinstance(manifest.get("embedding_model"), str)
            or not manifest["embedding_model"].strip()):
        raise ValueError("PostgreSQL workspace embedding identity is required")
    if not isinstance(manifest.get("database"), str) or not manifest["database"].strip():
        raise ValueError("PostgreSQL workspace database identity is required")
    try:
        UUID(manifest.get("db_instance_id", ""))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("PostgreSQL workspace database marker is required") from error
    if (not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("source_manifest_sha256", "")))
            or not isinstance(manifest.get("counts"), dict)
            or any(type(manifest.get(flag)) is not bool for flag in ("preserve_backlog", "read_only"))):
        raise ValueError("PostgreSQL workspace preservation receipt is incomplete")
    return manifest


def postgres_connection_kwargs(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Use explicit PostgreSQL credentials without falling back to another DB."""
    source = os.environ if env is None else env
    for name in ("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_DATABASE"):
        if not source.get(name, "").strip():
            raise ValueError(f"{name} is required for PostgreSQL workspaces")
    try:
        port = int(source.get("POSTGRES_PORT", "5432"))
    except ValueError as error:
        raise ValueError("POSTGRES_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("POSTGRES_PORT is outside its valid range")
    kwargs: dict[str, Any] = {
        "host": source["POSTGRES_HOST"], "port": port,
        "user": source["POSTGRES_USER"], "password": source.get("POSTGRES_PASSWORD"),
        "database": source["POSTGRES_DATABASE"], "timeout": 10, "command_timeout": 15,
    }
    mode = source.get("POSTGRES_SSL_MODE", "").lower()
    if mode not in {"", "disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise ValueError("Unsupported POSTGRES_SSL_MODE")
    if mode == "disable":
        kwargs["ssl"] = False
    elif mode in {"prefer", "require"}:
        kwargs["ssl"] = True  # Matches the pinned upstream connection pool.
    elif mode in {"verify-ca", "verify-full"}:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.check_hostname = mode == "verify-full"
        if source.get("POSTGRES_SSL_ROOT_CERT"):
            context.load_verify_locations(cafile=source["POSTGRES_SSL_ROOT_CERT"])
        cert, key = source.get("POSTGRES_SSL_CERT"), source.get("POSTGRES_SSL_KEY")
        if bool(cert) != bool(key):
            raise ValueError("PostgreSQL client certificate and key must be provided together")
        if cert and key:
            context.load_cert_chain(cert, key)
        if source.get("POSTGRES_SSL_CRL"):
            context.load_verify_locations(cafile=source["POSTGRES_SSL_CRL"])
            context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
        kwargs["ssl"] = context
    return kwargs


def configure_postgres_workspace(
    selected: Path, workspace_id: str, env: MutableMapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate the receipt, then atomically select all four PG backends."""
    target = os.environ if env is None else env
    manifest = load_workspace_manifest(selected / MANIFEST_FILENAME, workspace_id)
    connection = postgres_connection_kwargs(target)
    if connection["database"] != manifest["database"]:
        raise ValueError("POSTGRES_DATABASE does not match the completed workspace migration")
    if (target.get("EMBEDDING_MODEL") != manifest["embedding_model"]
            or target.get("EMBEDDING_DIM") != str(manifest["embedding_dim"])):
        raise ValueError("Embedding model/dimension do not match the migrated vectors")
    if target.get("POSTGRES_VECTOR_INDEX_TYPE", "VCHORDRQ").upper() != "VCHORDRQ":
        raise ValueError("PostgreSQL workspaces require POSTGRES_VECTOR_INDEX_TYPE=VCHORDRQ")
    try:
        pool_size = int(target.get("POSTGRES_MAX_CONNECTIONS", "2"))
        epsilon = float(target.get("POSTGRES_VCHORDRQ_EPSILON", "2.3"))
    except ValueError as error:
        raise ValueError("Invalid PostgreSQL pool or VCHORDRQ query settings") from error
    if not 1 <= pool_size <= 20 or not math.isfinite(epsilon) or epsilon <= 0:
        raise ValueError("PostgreSQL pool must be 1..20 and VCHORDRQ epsilon must be positive")
    probes = target.get("POSTGRES_VCHORDRQ_PROBES", "")
    if probes and not re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", probes):
        raise ValueError("POSTGRES_VCHORDRQ_PROBES must contain positive comma-separated integers")
    updates = {
        **STORAGE_ENVIRONMENT,
        # Public upload paths remain INPUT_DIR/<public ID>. PG storages take
        # their identity from POSTGRES_WORKSPACE on initialize(), independently
        # of the per-process shared-state and input-directory workspace.
        "WORKSPACE": "", "POSTGRES_WORKSPACE": manifest["postgres_workspace"],
        "POSTGRES_VECTOR_INDEX_TYPE": "VCHORDRQ",
        "POSTGRES_MAX_CONNECTIONS": str(pool_size),
        "POSTGRES_VCHORDRQ_EPSILON": str(epsilon),
    }
    target.update(updates)
    return manifest


def vector_collection_suffix(model: str, dimension: int, postgres_workspace: str) -> str:
    """Give each workspace/model its own ANN index and candidate population."""
    if (not isinstance(model, str) or not model.strip()
            or type(dimension) is not int or not 1 <= dimension <= 16000
            or not re.fullmatch(r"ws_[0-9a-f]{32}", postgres_workspace)):
        raise ValueError("Vector collections require a model, dimension, and mapped workspace")
    identity = f"{postgres_workspace}\0{model}\0{dimension}".encode("utf-8")
    return f"ws_{hashlib.sha256(identity).hexdigest()[:24]}_{dimension}d"


def install_postgres_vector_namespace() -> None:
    """Adapt only the pinned collection-name hook, retaining native PG methods."""
    from lightrag import __version__
    from lightrag.kg.postgres_impl import PGVectorStorage

    if __version__ != "1.5.7" or not callable(getattr(PGVectorStorage, "_generate_collection_suffix", None)):
        raise RuntimeError("PostgreSQL vector namespace hook requires pinned LightRAG 1.5.7")

    def per_workspace_suffix(storage: Any) -> str:
        return vector_collection_suffix(
            storage.embedding_func.model_name, storage.embedding_func.embedding_dim,
            os.environ.get("POSTGRES_WORKSPACE", ""),
        )

    PGVectorStorage._generate_collection_suffix = per_workspace_suffix


def vector_table_names(manifest: Mapping[str, Any]) -> dict[str, str]:
    """Compute the exact tables created by the pinned collection-name hook."""
    suffix = vector_collection_suffix(
        manifest["embedding_model"], manifest["embedding_dim"], manifest["postgres_workspace"],
    )
    result = {name: f"{base}_{suffix}" for name, base in (
        ("chunks", "lightrag_vdb_chunks"), ("entities", "lightrag_vdb_entity"),
        ("relationships", "lightrag_vdb_relation"),
    )}
    if any(len(name) > 63 for name in result.values()):
        raise ValueError("Embedding model produces an unsupported PostgreSQL table name")
    return result


async def validate_postgres_readiness(
    manifest: Mapping[str, Any], *, env: Mapping[str, str] | None = None, connect: Any = None,
) -> None:
    """Fail closed before upstream's schema bootstrap can create empty tables."""
    if connect is None:
        import asyncpg
        connect = asyncpg.connect
    connection = await connect(**postgres_connection_kwargs(env))
    try:
        async with connection.transaction(readonly=True):
            await connection.execute("SET LOCAL statement_timeout = '10s'")
            marker = await connection.fetchrow(
                "SELECT current_database() AS database, db_instance_id, schema_version "
                "FROM lightrag_workspace_database WHERE singleton = true")
            if (not marker or marker["database"] != manifest["database"]
                    or str(marker["db_instance_id"]) != manifest["db_instance_id"]
                    or marker["schema_version"] != MANIFEST_VERSION):
                raise RuntimeError("PostgreSQL migration database marker does not match the workspace")
            record = await connection.fetchrow(
                "SELECT postgres_workspace, db_instance_id, status, embedding_model, embedding_dim "
                "FROM lightrag_workspace_registry WHERE workspace = $1", manifest["workspace_id"])
            if not record or any(str(record[name]) != str(manifest[name]) for name in (
                "postgres_workspace", "db_instance_id", "status", "embedding_model", "embedding_dim",
            )):
                raise RuntimeError("PostgreSQL workspace is not a verified completed migration")
            extensions = await connection.fetch(
                "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'vchord')")
            if {row["extname"] for row in extensions} != {"vector", "vchord"}:
                raise RuntimeError("PostgreSQL workspace requires installed vector and vchord extensions")
            for table in REQUIRED_TABLES:
                exists = await connection.fetchval("SELECT to_regclass($1) IS NOT NULL", "public." + table)
                if not exists:
                    raise RuntimeError(f"PostgreSQL migration is missing table {table}")
            await validate_postgres_graph_schema(connection, manifest["postgres_workspace"])
            for table in vector_table_names(manifest).values():
                index = await connection.fetchrow(
                    "SELECT format_type(a.atttypid, a.atttypmod) AS vector_type, "
                    "EXISTS (SELECT 1 FROM pg_index i JOIN pg_class ix ON ix.oid = i.indexrelid "
                    "JOIN pg_am am ON am.oid = ix.relam WHERE i.indrelid = c.oid "
                    "AND i.indisvalid AND i.indisready AND am.amname = 'vchordrq') AS indexed "
                    "FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid "
                    "WHERE c.oid = to_regclass($1) AND a.attname = 'content_vector' AND NOT a.attisdropped",
                    "public." + table)
                if (not index or index["vector_type"] != f"vector({manifest['embedding_dim']})"
                        or not index["indexed"]):
                    raise RuntimeError(f"PostgreSQL migration lacks a valid float32 VCHORDRQ index on {table}")
    finally:
        await connection.close()
