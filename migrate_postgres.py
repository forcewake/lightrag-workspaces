#!/usr/bin/env python3
"""Offline, resumable migration of pinned LightRAG file stores into a new database.

Run in the project image with the source mounted read-only. No model/provider is
imported or called. Each dataset is replaced transactionally inside an explicitly
owned database; a workspace becomes ready only after complete source/readback
verification. Original JSON values are retained independently of upstream's
normalized SQL projection, including unsupported metadata and legacy cache keys.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Iterator
import uuid
import xml.etree.ElementTree as ET

from worker import SUPPORTED_VERSION, _VectorJSONReader, load_nano_storage_bounded
from postgres_backend import postgres_connection_kwargs, vector_collection_suffix, workspace_namespace


SCHEMA_VERSION = 1
GRAPH_NAMESPACE = "chunk_entity_relation"
SEP = "<SEP>"
KV_TABLES = {
    "full_docs": "lightrag_doc_full",
    "text_chunks": "lightrag_doc_chunks",
    "doc_status": "lightrag_doc_status",
    "llm_response_cache": "lightrag_llm_cache",
    "full_entities": "lightrag_full_entities",
    "full_relations": "lightrag_full_relations",
    "entity_chunks": "lightrag_entity_chunks",
    "relation_chunks": "lightrag_relation_chunks",
}
VECTOR_BASES = {"chunks": "lightrag_vdb_chunks", "entities": "lightrag_vdb_entity",
                "relationships": "lightrag_vdb_relation"}
RECOGNIZED_STORE_FILES = ({f"kv_store_{name}.json" for name in KV_TABLES}
    | {f"vdb_{name}.json" for name in VECTOR_BASES}
    | {f"graph_{GRAPH_NAMESPACE}.graphml"})
JSON_COLUMNS = {"meta", "chunk_options", "llm_cache_list", "heading", "sidecar",
                "chunks_list", "metadata", "queryparam", "entity_names", "relation_pairs",
                "chunk_ids", "properties"}
EPOCH = dt.datetime(1970, 1, 1)
BACKLOG_STATUSES = frozenset({"pending", "processing", "parsing", "analyzing", "preprocessed"})


def preserve_backlog(status_counts: Iterable[dict]) -> bool:
    return any(row["status"] in BACKLOG_STATUSES and row["count"] > 0 for row in status_counts)


def chunk_document_memberships(source_chunks: Iterable[dict], status_rows: Iterable[dict]) -> dict[str, set[str]]:
    """Retain every document membership of a content-addressed chunk.

    A chunk KV row has one full_doc_id, while multiple document status records
    may legitimately reference that same chunk ID. Neither source overwrites
    the other when reconstructing the document-level recovery indexes.
    """
    memberships: dict[str, set[str]] = {}
    for row in source_chunks:
        if row["full_doc_id"]:
            memberships.setdefault(row["id"], set()).add(row["full_doc_id"])
    for row in status_rows:
        values = json.loads(row["chunks_list"]) if isinstance(row["chunks_list"], str) else row["chunks_list"]
        for chunk in values or []:
            memberships.setdefault(chunk, set()).add(row["id"])
    return memberships


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def vector_tables(model: str, dimension: int, postgres_workspace: str) -> dict[str, str]:
    suffix = vector_collection_suffix(model, dimension, postgres_workspace)
    if not model.strip() or dimension < 1:
        raise ValueError("An exact embedding model and positive dimension are required")
    result = {key: value + "_" + suffix for key, value in VECTOR_BASES.items()}
    if any(len(name.encode()) > 63 for name in result.values()):
        raise ValueError("Embedding model creates a PostgreSQL table name exceeding 63 bytes")
    return result


def timestamp(value: Any, *, seconds: bool = False) -> dt.datetime:
    """Keep original values in the archive; use upstream's UTC-naive SQL time."""
    if value is None or value == "":
        result = EPOCH
    elif isinstance(value, (int, float)):
        result = dt.datetime.fromtimestamp(value, dt.timezone.utc).replace(tzinfo=None)
    elif isinstance(value, str):
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is not None:
            result = result.astimezone(dt.timezone.utc).replace(tzinfo=None)
    else:
        raise ValueError("Unsupported source timestamp")
    # TIMESTAMP(0) rounds, rather than truncates, fractional seconds.
    if seconds:
        result = (result + dt.timedelta(microseconds=500000)).replace(microsecond=0)
    return result


def iter_object(path: Path) -> Iterator[tuple[str, Any]]:
    """Stream a JSON object one record at a time, reject duplicate source IDs."""
    seen: set[str] = set()
    with path.open(encoding="utf-8") as stream:
        reader = _VectorJSONReader(stream)
        reader.expect("{")
        while reader.peek() != "}":
            key = reader.value()
            if not isinstance(key, str) or key in seen:
                raise ValueError(f"Duplicate/invalid record ID in {path.name}")
            seen.add(key)
            reader.expect(":")
            yield key, reader.value()
            if reader.peek() == "}":
                break
            reader.expect(",")
            if reader.peek() == "}":
                raise ValueError("Trailing JSON comma")
        reader.expect("}")
        if reader.peek():
            raise ValueError("Trailing JSON input")


def cache_records(path: Path) -> Iterator[tuple[str, Any, str, str]]:
    """Flatten legacy mode/hash caches while retaining every original value.

    Legacy mode dictionaries are streamed too: a single mode can contain a
    multi-GB cache, so treating it as one top-level value is unsafe.
    """
    seen: set[str] = set()
    outer_seen: set[str] = set()
    with path.open(encoding="utf-8") as stream:
        reader = _VectorJSONReader(stream)
        reader.expect("{")
        while reader.peek() != "}":
            key = reader.value()
            if not isinstance(key, str) or key in outer_seen:
                raise ValueError("Invalid cache ID")
            outer_seen.add(key)
            reader.expect(":")
            if key.count(":") != 2 and reader.peek() == "{":
                reader.expect("{")
                ordinary: dict = {}
                legacy = None
                while reader.peek() != "}":
                    nested_key = reader.value()
                    reader.expect(":")
                    value = reader.value()
                    is_legacy = isinstance(value, dict) and "return" in value
                    if legacy is not None and legacy != is_legacy:
                        raise ValueError("Mixed legacy/non-cache mode dictionary; review source")
                    legacy = is_legacy
                    if is_legacy:
                        native_id = f"{key}:{value.get('cache_type', 'extract')}:{nested_key}"
                        if native_id in seen:
                            raise ValueError("Cache IDs collide during legacy conversion")
                        seen.add(native_id)
                        yield native_id, value, f"llm_response_cache/{key}", str(nested_key)
                    else:
                        ordinary[nested_key] = value
                    if reader.peek() == "}":
                        break
                    reader.expect(",")
                    if reader.peek() == "}":
                        raise ValueError("Trailing cache comma")
                reader.expect("}")
                if not legacy:
                    if key in seen:
                        raise ValueError("Duplicate cache ID")
                    seen.add(key)
                    yield key, ordinary, "llm_response_cache", key
            else:
                value = reader.value()
                if key in seen:
                    raise ValueError("Duplicate cache ID")
                seen.add(key)
                yield key, value, "llm_response_cache", key
            if reader.peek() == "}":
                break
            reader.expect(",")
            if reader.peek() == "}":
                raise ValueError("Trailing cache comma")
        reader.expect("}")
        if reader.peek():
            raise ValueError("Trailing cache data")


def graph_records(path: Path) -> Iterator[tuple[str, str, dict]]:
    """Stream GraphML, preserving scalar types and graph defaults like NetworkX."""
    keys: dict[str, tuple[str, str, Any]] = {}
    defaults: dict[str, dict] = {"node": {}, "edge": {}}
    stack: list = []
    graph_count = 0
    for event, element in ET.iterparse(path, events=("start", "end")):
        tag = element.tag.rsplit("}", 1)[-1]
        if event == "start":
            parent_tag = stack[-1].tag.rsplit("}", 1)[-1] if stack else None
            if any(parent.tag.rsplit("}", 1)[-1] == "data" for parent in stack):
                raise ValueError("Nested GraphML data XML is unsupported")
            if tag not in {"graphml", "key", "default", "graph", "node", "edge", "data"}:
                raise ValueError(f"Unsupported GraphML element: {tag}")
            if parent_tag is None and tag != "graphml":
                raise ValueError("Source graph is not a GraphML document")
            if tag == "graph":
                graph_count += 1
                if graph_count != 1 or parent_tag != "graphml":
                    raise ValueError("Multiple or nested GraphML graphs are unsupported")
                if element.attrib.get("edgedefault", "undirected") != "undirected":
                    raise ValueError("Directed GraphML cannot be migrated to undirected PGTableGraphStorage")
            elif tag == "data" and parent_tag not in {"node", "edge"}:
                raise ValueError("Graph-level GraphML data is unsupported; retain and adapt it explicitly")
            elif tag in {"node", "edge"} and parent_tag != "graph":
                raise ValueError("GraphML nodes/edges must be direct graph children")
            stack.append(element)
            continue
        if tag == "key":
            kind = element.attrib.get("attr.type", "string")
            if kind not in {"string", "boolean", "int", "long", "float", "double"}:
                raise ValueError("Unsupported GraphML scalar type")
            def convert(text: str, kind: str = kind) -> Any:
                if kind == "boolean":
                    return text.lower() in ("true", "1")
                if kind in ("int", "long"):
                    return int(text)
                if kind in ("float", "double"):
                    return float(text)
                return text
            scope = element.attrib.get("for", "all")
            if scope not in {"all", "node", "edge"}:
                raise ValueError("Unsupported graph-level GraphML key")
            name = element.attrib.get("attr.name", element.attrib["id"])
            keys[element.attrib["id"]] = (name, kind, convert)
            for child in element:
                if child.tag.rsplit("}", 1)[-1] == "default":
                    for target in (["node", "edge"] if scope == "all" else [scope]):
                        defaults.setdefault(target, {})[name] = convert(child.text or "")
            element.clear()
            stack[-2].remove(element)
        elif tag in ("node", "edge"):
            props = dict(defaults[tag])
            for child in element:
                if child.tag.rsplit("}", 1)[-1] == "data":
                    name, _, convert = keys[child.attrib["key"]]
                    props[name] = convert(child.text or "")
            if tag == "node":
                yield "graph_nodes", element.attrib["id"], props
            else:
                if "directed" in element.attrib and element.attrib["directed"] not in ("false", "0"):
                    raise ValueError("Directed edge cannot be migrated")
                source, target = sorted((element.attrib["source"], element.attrib["target"]))
                # Encode the pair without separator ambiguity or metadata loss.
                yield "graph_edges", dumps([source, target]), props
            element.clear()
            # Remove from the actual <graph> parent, not merely clear its
            # content: otherwise one empty Element accumulates per graph row.
            stack[-2].remove(element)
        stack.pop()
    if graph_count != 1:
        raise ValueError("Source GraphML must contain exactly one graph")


def file_manifest(source: Path) -> dict:
    files = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Migration source must not contain symbolic links")
        if not path.is_file():
            continue
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("Source changed during manifest; stop writers first")
        files.append({"path": str(path.relative_to(source)), "bytes": before.st_size, "sha256": digest.hexdigest()})
    return {"files": files, "sha256": hashlib.sha256(dumps(files).encode()).hexdigest()}


def classify_source_files(manifest: dict) -> list[str]:
    """Fail closed on unknown stores; report other retained filesystem artifacts."""
    retained = []
    for entry in manifest["files"]:
        name = entry["path"]
        if name in RECOGNIZED_STORE_FILES:
            continue
        basename = Path(name).name
        if ((basename.startswith(("kv_store_", "vdb_")) and basename.endswith(".json"))
                or (basename.startswith("graph_") and basename.endswith(".graphml"))):
            raise ValueError(f"Unsupported source store format: {name}; migration did not complete")
        retained.append(name)
    return retained


def validate_control_root(control_root: Path, workspace_ids: Iterable[str], database: str, token: str) -> None:
    """Never follow control-directory links or replace another installation."""
    root = Path(os.path.abspath(control_root))
    for directory in reversed((root, *root.parents)):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Control root and its existing ancestors must be real directories")
    for public_id in workspace_ids:
        directory = root / public_id
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Workspace control path must be a real directory")
        manifest_path = directory / ".postgres-workspace.json"
        partial_path = directory / ".postgres-workspace.json.partial"
        candidates = [path for path in (manifest_path, partial_path) if path.exists() or path.is_symlink()]
        if not candidates and any(directory.iterdir()):
            raise ValueError("Existing nonempty workspace control directory has no ownership manifest")
        for path in candidates:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
                raise ValueError("Workspace ownership manifest must be a bounded regular file")
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if (manifest.get("workspace_id") != public_id or manifest.get("database") != database
                    or manifest.get("db_instance_id") != token):
                raise ValueError("Existing control manifest belongs to another database or workspace")


def validate_report_path(source_root: Path, report: Path | None) -> None:
    if report is None:
        return
    source, target = source_root.resolve(), report.resolve()
    if target == source or source in target.parents:
        raise ValueError("Migration report must be outside the read-only source root")


EMPTY_DATABASE_OBJECTS_SQL = """
WITH RECURSIVE extension_owned(classid,objid,objsubid) AS (
    SELECT d.classid,d.objid,d.objsubid FROM pg_depend d
    WHERE d.refclassid='pg_extension'::regclass AND d.deptype='e'
    UNION
    SELECT d.classid,d.objid,d.objsubid
    FROM pg_depend d JOIN extension_owned parent
      ON d.refclassid=parent.classid AND d.refobjid=parent.objid
      AND (parent.objsubid=0 OR d.refobjsubid=parent.objsubid)
    WHERE d.deptype IN ('i','a')
), user_schemas AS (
    SELECT oid,nspname FROM pg_namespace
    WHERE nspname <> 'information_schema' AND left(nspname,3) <> 'pg_'
), objects AS (
    SELECT 'pg_class'::regclass AS classid,c.oid AS objid
    FROM pg_class c JOIN user_schemas n ON n.oid=c.relnamespace
    WHERE c.relkind IN ('r','p','v','m','S','f','c')
    UNION ALL
    SELECT 'pg_proc'::regclass,p.oid FROM pg_proc p JOIN user_schemas n ON n.oid=p.pronamespace
    UNION ALL
    SELECT 'pg_type'::regclass,t.oid FROM pg_type t JOIN user_schemas n ON n.oid=t.typnamespace
    UNION ALL
    SELECT 'pg_namespace'::regclass,n.oid FROM user_schemas n WHERE n.nspname <> 'public'
)
SELECT count(*) FROM objects o
WHERE NOT EXISTS (
    SELECT 1 FROM extension_owned e WHERE e.classid=o.classid AND e.objid=o.objid AND e.objsubid=0
)
"""


def project(namespace: str, key: str, value: dict, workspace: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"Native {namespace} record must be an object")
    base = {"workspace": workspace, "id": key}
    created = timestamp(value.get("create_time", value.get("__created_at__")), seconds=True)
    updated = timestamp(value.get("update_time", value.get("create_time", value.get("__created_at__"))), seconds=True)
    if namespace == "full_docs":
        return base | {"content": value.get("content", ""), "doc_name": value.get("file_path", ""),
            "meta": value.get("meta"), "sidecar_location": value.get("sidecar_location"),
            "parse_format": value.get("parse_format", "raw"), "content_hash": value.get("content_hash"),
            "process_options": value.get("process_options"), "chunk_options": value.get("chunk_options") or {},
            "parse_engine": value.get("parse_engine"), "create_time": created, "update_time": updated}
    if namespace in ("text_chunks", "chunks"):
        row = base | {name: value.get(name) for name in ("tokens", "chunk_order_index", "full_doc_id", "content", "file_path")}
        row |= {"create_time": created, "update_time": updated}
        if namespace == "text_chunks":
            row |= {"llm_cache_list": value.get("llm_cache_list") or [], "heading": value.get("heading") or {}, "sidecar": value.get("sidecar") or {}}
        return row
    if namespace == "doc_status":
        row = base | {name: value.get(name) for name in ("content_summary", "content_length", "chunks_count", "status", "file_path", "track_id", "error_msg", "content_hash")}
        return row | {"chunks_list": value.get("chunks_list") or [], "metadata": value.get("metadata") or {},
            "created_at": timestamp(value.get("created_at")), "updated_at": timestamp(value.get("updated_at", value.get("created_at")))}
    if namespace == "llm_response_cache":
        return base | {"original_prompt": value.get("original_prompt", ""), "return_value": value.get("return", ""),
            "chunk_id": value.get("chunk_id"), "cache_type": value.get("cache_type", "extract"),
            "queryparam": value.get("queryparam"), "create_time": timestamp(value.get("create_time")), "update_time": timestamp(value.get("update_time", value.get("create_time")))}
    if namespace in ("full_entities", "full_relations", "entity_chunks", "relation_chunks"):
        field = {"full_entities": "entity_names", "full_relations": "relation_pairs"}.get(namespace, "chunk_ids")
        return base | {field: value.get(field) or [], "count": value.get("count", len(value.get(field) or [])), "create_time": created, "update_time": updated}
    if namespace in ("entities", "relationships"):
        fields = ("entity_name",) if namespace == "entities" else ("source_id", "target_id")
        row = base | {name: value.get(name) for name in fields}
        if namespace == "relationships":
            row["source_id"] = value.get("src_id", value.get("source_id"))
            row["target_id"] = value.get("tgt_id", value.get("target_id"))
        chunk_ids = value.get("chunk_ids", value.get("source_id"))
        if isinstance(chunk_ids, str):
            chunk_ids = chunk_ids.split(SEP)
        return row | {"content": value.get("content", ""), "chunk_ids": chunk_ids,
            "file_path": value.get("file_path"), "create_time": created, "update_time": updated}
    raise ValueError(f"Unknown native namespace: {namespace}")


def archive_payload(value: Any) -> bytes:
    return dumps(value).encode("utf-8")


def estimated_bytes(value: Any) -> int:
    if isinstance(value, str):
        return len(value) * 4 + 64  # Conservative UTF-8 size without allocating.
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if hasattr(value, "nbytes"):
        return int(value.nbytes)
    if isinstance(value, dict):
        return 64 + sum(estimated_bytes(key) + estimated_bytes(item) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return 64 + sum(estimated_bytes(item) for item in value)
    return 32


def batches(rows: Iterable, size: int, max_bytes: int = 8 * 1024 * 1024) -> Iterator[list]:
    batch = []
    byte_size = 0
    for row in rows:
        row_size = estimated_bytes(row)
        if batch and byte_size + row_size > max_bytes:
            yield batch
            batch, byte_size = [], 0
        batch.append(row)
        byte_size += row_size
        if len(batch) >= size:
            yield batch
            batch, byte_size = [], 0
    if batch:
        yield batch


class Migration:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.tables: dict[str, str] = {}
        self.db = None
        self.report: dict = {"schema_version": SCHEMA_VERSION, "database": args.target_database,
            "db_instance_id": args.target_token, "embedding_model": args.embedding_model,
            "embedding_dim": args.embedding_dim, "provider_calls": 0, "workspaces": {}}

    async def connect(self) -> None:
        import asyncpg
        from pgvector.asyncpg import register_vector
        if os.environ.get("POSTGRES_DATABASE") != self.args.target_database:
            raise ValueError("POSTGRES_DATABASE must exactly match --target-database")
        if os.environ.get("POSTGRES_WORKSPACE"):
            raise ValueError("Unset POSTGRES_WORKSPACE; each source has a different namespace")
        connection_kwargs = postgres_connection_kwargs()
        connection_kwargs["command_timeout"] = self.args.statement_timeout
        self.db = await asyncpg.connect(**connection_kwargs, statement_cache_size=0,
            server_settings={"application_name": "lightrag_workspaces_migration", "timezone": "UTC",
                "statement_timeout": str(self.args.statement_timeout * 1000), "lock_timeout": "5000",
                "maintenance_work_mem": "64MB", "work_mem": "16MB", "max_parallel_maintenance_workers": "0"})
        if await self.db.fetchval("SELECT current_database()") != self.args.target_database:
            raise ValueError("Connected database identity mismatch")
        methods = await self.db.fetchval("SELECT EXISTS(SELECT 1 FROM pg_am WHERE amname='vchordrq')")
        if not methods:
            raise ValueError("Activate vector and vchord extensions in the NEW target database first")
        await register_vector(self.db)

    async def initialize(self, allow_create: bool) -> None:
        # Acquire before inspecting/creating ownership; separate invocations
        # must never race during the initial empty-database check.
        locked = await self.db.fetchval("SELECT pg_try_advisory_lock(hashtext('lightrag_workspace_migration'))")
        if not locked:
            raise ValueError("Another migration owns this target database")
        marker = await self.db.fetchval("SELECT to_regclass('lightrag_workspace_database')")
        if marker is None:
            existing = await self.db.fetchval(EMPTY_DATABASE_OBJECTS_SQL)
            if not allow_create or existing:
                raise ValueError("Refusing unowned or nonempty target database")
            # Include every initial DDL in one transaction: a failed bootstrap
            # leaves the target empty and can be safely retried.
            async with self.db.transaction():
                await self.db.execute("CREATE TABLE lightrag_workspace_database(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),db_instance_id uuid NOT NULL,schema_version int NOT NULL,created_at timestamptz NOT NULL DEFAULT now())")
                await self.db.execute("INSERT INTO lightrag_workspace_database(singleton,db_instance_id,schema_version) VALUES(true,$1,1)", uuid.UUID(self.args.target_token))
                await self.db.execute("""CREATE TABLE lightrag_workspace_registry(
                    workspace text PRIMARY KEY,postgres_workspace text UNIQUE NOT NULL,db_instance_id uuid NOT NULL,
                    migration_id text NOT NULL,status text NOT NULL,embedding_model text NOT NULL,embedding_dim int NOT NULL,
                    source_manifest jsonb NOT NULL,counts jsonb NOT NULL DEFAULT '{}',verification jsonb NOT NULL DEFAULT '{}')""")
                await self.db.execute("""CREATE TABLE lightrag_workspace_archive(
                    workspace text NOT NULL,namespace text NOT NULL,id text NOT NULL,original_json bytea NOT NULL,
                    sha256 text NOT NULL,PRIMARY KEY(workspace,namespace,id))""")
                from lightrag.kg.postgres_impl import TABLES
                from lightrag.kg.pgtable_impl import _DDL
                for name, spec in TABLES.items():
                    if name not in {base.upper() for base in VECTOR_BASES.values()}:
                        await self.db.execute(spec["ddl"])
                        await self.db.execute(f"ALTER TABLE {name} ALTER COLUMN id TYPE TEXT")
                await self.db.execute(_DDL)
                # The pinned adapter performs this widening itself. Do it up
                # front so no source path is silently truncated by import.
                await self.db.execute("ALTER TABLE lightrag_doc_full ALTER COLUMN doc_name TYPE TEXT")
        row = await self.db.fetchrow("SELECT db_instance_id,schema_version FROM lightrag_workspace_database WHERE singleton=true")
        if not row or str(row["db_instance_id"]) != self.args.target_token or row["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Target ownership token/schema does not match")

    async def ensure_vector_tables(self, *, verify: bool) -> None:
        from lightrag.kg.postgres_impl import TABLES
        for namespace, table in self.tables.items():
            exists = await self.db.fetchval("SELECT to_regclass($1)", table)
            if not exists:
                if verify:
                    raise ValueError("Missing workspace-specific vector table")
                ddl = TABLES[VECTOR_BASES[namespace].upper()]["ddl"]
                ddl = ddl.replace(VECTOR_BASES[namespace].upper(), table).replace("VECTOR(dimension)", f"VECTOR({self.args.embedding_dim})")
                await self.db.execute(ddl)
                await self.db.execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE TEXT")
                columns = {"entities": ["entity_name"], "relationships": ["source_id", "target_id"]}.get(namespace, [])
                for column in columns:
                    await self.db.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TEXT")

    async def copy(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        columns = list(rows[0])
        records = []
        for row in rows:
            values = []
            for key in columns:
                value = row[key]
                # Native vector chunk_ids is varchar[]; auxiliary chunk_ids is JSONB.
                is_json = key in JSON_COLUMNS and not (key == "chunk_ids" and table in self.tables.values())
                values.append(dumps(value) if is_json and value is not None else value)
            records.append(tuple(values))
        await self.db.copy_records_to_table(table, records=records, columns=columns)

    async def archive(self, workspace: str, rows: list[tuple[str, str, Any]], *, verify: bool) -> None:
        if not rows:
            return
        encoded = [(workspace, namespace, key, archive_payload(value)) for namespace, key, value in rows]
        if not verify:
            await self.db.copy_records_to_table("lightrag_workspace_archive", columns=["workspace", "namespace", "id", "original_json", "sha256"],
                records=[(*row, hashlib.sha256(row[3]).hexdigest()) for row in encoded])
        stored_rows = await self.db.fetch("SELECT namespace,id,original_json,sha256 FROM lightrag_workspace_archive WHERE workspace=$1 AND (namespace,id) IN (SELECT * FROM unnest($2::text[],$3::text[]))", workspace, [r[1] for r in encoded], [r[2] for r in encoded])
        stored_map = {(row["namespace"], row["id"]): row for row in stored_rows}
        for _, namespace, key, payload in encoded:
            stored = stored_map.get((namespace, key))
            if not stored or stored["original_json"] != payload or stored["sha256"] != hashlib.sha256(payload).hexdigest():
                raise ValueError("Original-record archive verification failed")

    async def verify_rows(self, table: str, rows: list[dict]) -> None:
        import numpy as np
        if not rows:
            return
        graph = table in ("lightrag_graph_nodes", "lightrag_graph_edges")
        workspace = rows[0]["workspace"]
        if table == "lightrag_graph_edges":
            actual_rows = await self.db.fetch(f"SELECT * FROM {table} WHERE workspace=$1 AND namespace=$2 AND (src_id,tgt_id) IN (SELECT * FROM unnest($3::text[],$4::text[]))", workspace, GRAPH_NAMESPACE, [r["src_id"] for r in rows], [r["tgt_id"] for r in rows])
            actual_map = {(r["src_id"], r["tgt_id"]): r for r in actual_rows}
        elif graph:
            actual_rows = await self.db.fetch(f"SELECT * FROM {table} WHERE workspace=$1 AND namespace=$2 AND id=ANY($3::text[])", workspace, GRAPH_NAMESPACE, [r["id"] for r in rows])
            actual_map = {r["id"]: r for r in actual_rows}
        else:
            actual_rows = await self.db.fetch(f"SELECT * FROM {table} WHERE workspace=$1 AND id=ANY($2::text[])", workspace, [r["id"] for r in rows])
            actual_map = {r["id"]: r for r in actual_rows}
        for expected in rows:
            identity = (expected["src_id"], expected["tgt_id"]) if table == "lightrag_graph_edges" else expected["id"]
            stored = actual_map.get(identity)
            if not stored:
                raise ValueError(f"Missing imported row in {table}")
            for key, value in expected.items():
                actual = stored[key]
                if key == "content_vector":
                    if actual.dtype != np.float32 or actual.tobytes() != np.asarray(value, dtype=np.float32).tobytes():
                        raise ValueError("float32 vector bits changed")
                elif key in JSON_COLUMNS and not (key == "chunk_ids" and table in self.tables.values()):
                    if isinstance(actual, str):
                        actual = json.loads(actual)
                    if dumps(actual) != dumps(value):
                        raise ValueError(f"Native JSON projection mismatch in {table}.{key}")
                elif actual != value:
                    raise ValueError(f"Native projection mismatch in {table}.{key}")

    async def dataset(self, public_id: str, namespace: str, records: Iterable[tuple], *, verify: bool) -> int:
        workspace = workspace_namespace(public_id)
        table = self.tables.get(namespace, KV_TABLES.get(namespace))
        count = 0
        async with self.db.transaction():
            if not verify:
                await self.db.execute(f"DELETE FROM {table} WHERE workspace=$1", workspace)
                await self.db.execute("DELETE FROM lightrag_workspace_archive WHERE workspace=$1 AND (namespace=$2 OR starts_with(namespace,$2||'/'))", workspace, namespace)
            for batch in batches(records, self.args.batch_size):
                rows, originals = [], []
                for record in batch:
                    key, value, archive_namespace, archive_key = record[:4]
                    row = project(namespace, key, value, workspace)
                    if namespace in self.tables:
                        row["content_vector"] = record[4]
                    rows.append(row)
                    originals.append((archive_namespace, archive_key, value))
                if namespace == "chunks" and rows:
                    # Nano keeps only selected metadata fields. Recover native
                    # PG's token/order columns from the already imported KV
                    # rows; existing vector bytes and metadata stay untouched.
                    chunks = await self.db.fetch("SELECT id,tokens,chunk_order_index,full_doc_id,content,file_path FROM lightrag_doc_chunks WHERE workspace=$1 AND id=ANY($2::text[])", workspace, [row["id"] for row in rows])
                    by_id = {chunk["id"]: chunk for chunk in chunks}
                    for row in rows:
                        chunk = by_id.get(row["id"])
                        if chunk:
                            for field in ("tokens", "chunk_order_index", "full_doc_id", "content", "file_path"):
                                if row[field] is None:
                                    row[field] = chunk[field]
                if not verify:
                    await self.copy(table, rows)
                await self.verify_rows(table, rows)
                await self.archive(workspace, originals, verify=verify)
                count += len(rows)
            actual = await self.db.fetchval(f"SELECT count(*) FROM {table} WHERE workspace=$1", workspace)
            if actual != count:
                raise ValueError(f"Native count mismatch in {namespace}: expected {count}, found {actual}")
        return count

    async def graph(self, public_id: str, path: Path | None, *, verify: bool) -> dict:
        workspace = workspace_namespace(public_id)
        counts = Counter()
        async with self.db.transaction():
            if not verify:
                await self.db.execute("DELETE FROM lightrag_graph_edges WHERE workspace=$1", workspace)
                await self.db.execute("DELETE FROM lightrag_graph_nodes WHERE workspace=$1", workspace)
                await self.db.execute("DELETE FROM lightrag_workspace_archive WHERE workspace=$1 AND namespace IN ('graph_nodes','graph_edges')", workspace)
            if path:
                for batch in batches(graph_records(path), self.args.batch_size):
                    # GraphML from NetworkX writes nodes before edges.
                    for kind in ("graph_nodes", "graph_edges"):
                        original = [(ns, key, value) for ns, key, value in batch if ns == kind]
                        rows = []
                        for _, key, props in original:
                            row = {"workspace": workspace, "namespace": GRAPH_NAMESPACE, "properties": props}
                            if kind == "graph_nodes":
                                row["id"] = key
                            else:
                                row["src_id"], row["tgt_id"] = json.loads(key)
                            rows.append(row)
                        if not verify:
                            await self.copy("lightrag_" + kind, rows)
                        await self.verify_rows("lightrag_" + kind, rows)
                        await self.archive(workspace, original, verify=verify)
                        counts[kind] += len(rows)
            for kind in ("graph_nodes", "graph_edges"):
                if await self.db.fetchval(f"SELECT count(*) FROM lightrag_{kind} WHERE workspace=$1", workspace) != counts[kind]:
                    raise ValueError("Graph count mismatch")
        return dict(counts)

    async def recovery_anchors(self, workspace: str, *, verify: bool) -> dict:
        """Derive only absent anchors from retained graph/chunk provenance.

        Uses the same source_id relationship as pinned upstream's startup
        migration, extended to chunk.full_doc_id when old status lacks chunks_list.
        Existing native anchor records remain unchanged. No model calls occur.
        """
        doc_entities: dict[str, set] = {}
        doc_relations: dict[str, set] = {}
        derived = Counter()
        warnings = Counter()
        orphan_source_chunk_ids: set[str] = set()
        async with self.db.transaction():
            source_chunks = await self.db.fetch("SELECT id,full_doc_id FROM lightrag_doc_chunks WHERE workspace=$1", workspace)
            source_chunk_ids = {row["id"] for row in source_chunks}
            full_doc_ids = {row["id"] for row in await self.db.fetch("SELECT id FROM lightrag_doc_full WHERE workspace=$1", workspace)}
            status_rows = await self.db.fetch("SELECT id,chunks_list FROM lightrag_doc_status WHERE workspace=$1", workspace)
            mapping = chunk_document_memberships(source_chunks, status_rows)
            warnings["source_chunks_without_doc"] = len(source_chunk_ids - mapping.keys())
            warnings["source_chunks_with_missing_full_doc"] = sum(any(doc not in full_doc_ids for doc in docs) for chunk, docs in mapping.items() if chunk in source_chunk_ids)
            warnings["source_chunks_shared_between_documents"] = sum(len(docs) > 1 for chunk, docs in mapping.items() if chunk in source_chunk_ids)
            for kind in ("nodes", "edges"):
                query = f"SELECT * FROM lightrag_graph_{kind} WHERE workspace=$1"
                pending = []
                async for row in self.db.cursor(query, workspace, prefetch=self.args.batch_size):
                    props = json.loads(row["properties"]) if isinstance(row["properties"], str) else row["properties"]
                    chunk_ids = sorted(set(str(props.get("source_id", "")).split(SEP)) - {""})
                    if not chunk_ids:
                        warnings["graph_" + kind + "_without_chunk_provenance"] += 1
                    unresolved = [chunk for chunk in chunk_ids if chunk not in mapping]
                    warnings["unresolved_graph_chunk_references"] += len(unresolved)
                    orphans = [chunk for chunk in chunk_ids if chunk not in source_chunk_ids]
                    warnings["orphan_source_chunk_refs"] += len(orphans)
                    orphan_source_chunk_ids.update(orphans)
                    if kind == "nodes":
                        namespace, key = "entity_chunks", row["id"]
                        for chunk in chunk_ids:
                            for doc in mapping.get(chunk, ()):
                                doc_entities.setdefault(doc, set()).add(key)
                    else:
                        namespace, key = "relation_chunks", SEP.join((row["src_id"], row["tgt_id"]))
                        for chunk in chunk_ids:
                            for doc in mapping.get(chunk, ()):
                                doc_relations.setdefault(doc, set()).add((row["src_id"], row["tgt_id"]))
                    if chunk_ids:
                        pending.append((key, {"chunk_ids": chunk_ids, "count": len(chunk_ids)}))
                    if len(pending) >= self.args.batch_size:
                        derived[namespace] += await self.derived_records(workspace, namespace, pending, verify=verify)
                        pending = []
                if pending:
                    derived[namespace] += await self.derived_records(workspace, namespace, pending, verify=verify)
            # Empty anchors make the absence of extracted entities explicit for
            # old processed documents, and avoid guessing from a sample of docs.
            docs = await self.db.fetch("SELECT id FROM lightrag_doc_status WHERE workspace=$1 AND status='processed'", workspace)
            for batch in batches(docs, self.args.batch_size):
                entities_batch, relations_batch = [], []
                for doc in batch:
                    key = doc["id"]
                    entities = sorted(doc_entities.get(key, set()))
                    relations = [list(pair) for pair in sorted(doc_relations.get(key, set()))]
                    entities_batch.append((key, {"entity_names": entities, "count": len(entities)}))
                    relations_batch.append((key, {"relation_pairs": relations, "count": len(relations)}))
                derived["full_entities"] += await self.derived_records(workspace, "full_entities", entities_batch, verify=verify)
                derived["full_relations"] += await self.derived_records(workspace, "full_relations", relations_batch, verify=verify)
        warnings["orphan_source_chunk_ids"] = len(orphan_source_chunk_ids)
        return dict(derived) | {"_warnings": dict(warnings)}

    async def derived_records(self, workspace: str, namespace: str, records: list[tuple[str, dict]], *, verify: bool) -> int:
        table = KV_TABLES[namespace]
        originals = await self.db.fetch("SELECT id FROM lightrag_workspace_archive WHERE workspace=$1 AND namespace=$2 AND id=ANY($3::text[])", workspace, namespace, [key for key, _ in records])
        original_ids = {row["id"] for row in originals}
        # The source's complete existing anchor is authoritative.
        expected = [project(namespace, key, value, workspace) for key, value in records if key not in original_ids]
        if not expected:
            return 0
        if not verify:
            await self.db.execute(f"DELETE FROM {table} WHERE workspace=$1 AND id=ANY($2::text[])", workspace, [row["id"] for row in expected])
            await self.copy(table, expected)
        await self.verify_rows(table, expected)
        return len(expected)

    async def indexes(self, *, verify: bool) -> dict:
        results = {}
        from lightrag.kg.postgres_impl import _safe_index_name
        tables = sorted({table for result in self.report["workspaces"].values() for table in result["vector_tables"].values()})
        for table in tables:
            name = _safe_index_name(table, "vchordrq_cosine")
            if not verify:
                exists = await self.db.fetchval("SELECT to_regclass($1)", name)
                if not exists:
                    options = "build.pin = -1\nbuild.internal.lists = []\nbuild.internal.build_threads = 1\ndegree_of_parallelism = 1"
                    await self.db.execute(f"CREATE INDEX {name} ON {table} USING vchordrq(content_vector vector_cosine_ops) WITH(options=$${options}$$)")
                await self.db.execute(f"ANALYZE {table}")
            row = await self.db.fetchrow("SELECT i.indisvalid,i.indisready,am.amname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_am am ON am.oid=c.relam WHERE i.indexrelid=$1::regclass", name)
            if not row or not row["indisvalid"] or not row["indisready"] or row["amname"] != "vchordrq":
                raise ValueError("Missing or invalid VCHORDRQ index")
            results[table] = {"name": name, **dict(row)}
        return results

    def sources(self) -> list[tuple[str, Path]]:
        root = self.args.source_root.resolve(strict=True)
        ids = self.args.workspace or sorted(p.name for p in root.iterdir() if p.is_dir())
        result = []
        for public_id in ids:
            workspace_namespace(public_id)
            path = root / public_id
            if path.is_symlink() or path.resolve(strict=True).parent != root:
                raise ValueError("Workspace source must be a direct, real directory")
            result.append((public_id, path))
        if not result:
            raise ValueError("No workspace source directories found")
        if len({public_id for public_id, _ in result}) != len(result):
            raise ValueError("Duplicate workspace selection")
        return result

    async def workspace(self, public_id: str, source: Path, *, verify: bool) -> None:
        import numpy as np
        before = file_manifest(source)
        retained_source_only = classify_source_files(before)
        graph = source / f"graph_{GRAPH_NAMESPACE}.graphml"
        if graph.exists():
            # Complete unsupported-format detection before the first workspace
            # import mutation; stream and discard this inexpensive preflight.
            for _ in graph_records(graph):
                pass
        workspace = workspace_namespace(public_id)
        self.tables = vector_tables(self.args.embedding_model, self.args.embedding_dim, workspace)
        existing = await self.db.fetchrow("SELECT * FROM lightrag_workspace_registry WHERE workspace=$1", public_id)
        if existing:
            stored = json.loads(existing["source_manifest"]) if isinstance(existing["source_manifest"], str) else existing["source_manifest"]
            if str(existing["db_instance_id"]) != self.args.target_token or existing["postgres_workspace"] != workspace or stored != before:
                raise ValueError("Existing workspace ownership/source differs; refusing overwrite")
            if existing["embedding_model"] != self.args.embedding_model or existing["embedding_dim"] != self.args.embedding_dim:
                raise ValueError("Embedding identity differs from existing migration")
            prior_proof = json.loads(existing["verification"]) if isinstance(existing["verification"], str) else existing["verification"]
            if not verify and (existing["status"] == "ready" or all(prior_proof.get(key) is True for key in ("source_unchanged", "native_rows_read_back", "archive_rows_read_back", "float32_bits_verified"))):
                # Resume publication after a crash without resetting a ready
                # target. Complete readback must still match the cold source;
                # any legitimate live write makes that verification fail.
                verify = True
        elif verify:
            raise ValueError("Workspace has not been imported")
        else:
            await self.db.execute("INSERT INTO lightrag_workspace_registry(workspace,postgres_workspace,db_instance_id,migration_id,status,embedding_model,embedding_dim,source_manifest) VALUES($1,$2,$3,$4,'importing',$5,$6,$7)", public_id, workspace, uuid.UUID(self.args.target_token), self.args.target_token, self.args.embedding_model, self.args.embedding_dim, dumps(before))
        await self.ensure_vector_tables(verify=verify)
        counts = {}
        # Auxiliary counts additionally include derivations, so verify native
        # source rows before deriving/verifying the missing anchors below.
        for namespace in KV_TABLES:
            path = source / f"kv_store_{namespace}.json"
            if namespace == "llm_response_cache" and path.exists():
                records = cache_records(path)
            else:
                records = ((key, value, namespace, key) for key, value in iter_object(path)) if path.exists() else iter(())
            if verify and namespace in ("full_entities", "full_relations", "entity_chunks", "relation_chunks"):
                # Derived rows have no source archive entry; verify only original
                # rows here, then verify every derived row deterministically.
                count = 0
                for batch in batches(records, self.args.batch_size):
                    await self.verify_rows(KV_TABLES[namespace], [project(namespace, key, value, workspace) for key, value, _, _ in batch])
                    await self.archive(workspace, [(ns, key, value) for _, value, ns, key in batch], verify=True)
                    count += len(batch)
                counts[namespace] = count
            else:
                counts[namespace] = await self.dataset(public_id, namespace, records, verify=verify)
        statuses = await self.db.fetch("SELECT status,count(*) AS count FROM lightrag_doc_status WHERE workspace=$1 GROUP BY status", workspace)
        backlog = preserve_backlog(statuses)
        for namespace in self.tables:
            path = source / f"vdb_{namespace}.json"
            if path.exists():
                native = load_nano_storage_bounded(str(path))
                if native["embedding_dim"] != self.args.embedding_dim:
                    raise ValueError("Source embedding dimension differs")
                metadata = {key: value for key, value in native.items() if key not in ("matrix", "data")}
                if not verify:
                    await self.db.execute("DELETE FROM lightrag_workspace_archive WHERE workspace=$1 AND namespace='vector_metadata' AND id=$2", workspace, namespace)
                await self.archive(workspace, [("vector_metadata", namespace, metadata)], verify=verify)
                counts["vector_metadata_" + namespace] = 1
                matrix = native["matrix"]
                def vectors():
                    for value, vector in zip(native["data"], matrix, strict=True):
                        if not np.isfinite(vector).all():
                            raise ValueError("PostgreSQL vector cannot represent non-finite source embeddings")
                        key = value.get("__id__")
                        if not isinstance(key, str):
                            raise ValueError("Native vector is missing its original ID")
                        yield key, value, namespace, key, vector
                counts["vectors_" + namespace] = await self.dataset(public_id, namespace, vectors(), verify=verify)
                del native, matrix
            else:
                counts["vectors_" + namespace] = await self.dataset(public_id, namespace, (), verify=verify)
        counts |= await self.graph(public_id, graph if graph.exists() else None, verify=verify)
        source_archive_count = sum(counts.values())
        derived = await self.recovery_anchors(workspace, verify=verify)
        for namespace in ("full_entities", "full_relations", "entity_chunks", "relation_chunks"):
            counts[namespace + "_source"] = counts[namespace]
            counts[namespace] = await self.db.fetchval(f"SELECT count(*) FROM {KV_TABLES[namespace]} WHERE workspace=$1", workspace)
            if counts[namespace] != counts[namespace + "_source"] + derived.get(namespace, 0):
                raise ValueError("Auxiliary anchor count differs from source plus deterministic recovery")
        after = file_manifest(source)
        if before != after:
            raise ValueError("Source changed during migration/verification")
        archive_count = await self.db.fetchval("SELECT count(*) FROM lightrag_workspace_archive WHERE workspace=$1", workspace)
        if archive_count != source_archive_count:
            raise ValueError("Archive contains missing or unexpected source records")
        verification = {"source_unchanged": True, "native_rows_read_back": True, "archive_rows_read_back": True,
            "float32_bits_verified": True, "archive_records": archive_count, "derived_anchors": derived,
            "recovery_membership_version": 2, "provider_calls": 0, "statuses": {row["status"]: row["count"] for row in statuses}}
        verification["retained_source_only_files"] = retained_source_only
        self.report["workspaces"][public_id] = {"postgres_workspace": workspace, "source_manifest_sha256": before["sha256"], "counts": counts, "preserve_backlog": bool(backlog), "verification": verification, "vector_tables": dict(self.tables)}
        if self.args.action == "apply":
            await self.db.execute("UPDATE lightrag_workspace_registry SET counts=$2,verification=$3 WHERE workspace=$1", public_id, dumps(counts), dumps(verification))
        print(dumps({"workspace": public_id, "phase": "verified", "counts": counts}), flush=True)

    async def publish(self) -> None:
        # All indexes and ALL workspaces must be verified before any manifest
        # is published. A partial run cannot masquerade as a ready installation.
        validate_control_root(self.args.control_root, self.report["workspaces"], self.args.target_database, self.args.target_token)
        for public_id, result in self.report["workspaces"].items():
            manifest = {"schema_version": SCHEMA_VERSION, "workspace_id": public_id,
                "postgres_workspace": result["postgres_workspace"], "status": "ready",
                "embedding_model": self.args.embedding_model, "embedding_dim": self.args.embedding_dim,
                "database": self.args.target_database, "db_instance_id": self.args.target_token,
                "source_manifest_sha256": result["source_manifest_sha256"], "counts": result["counts"],
                "preserve_backlog": result["preserve_backlog"], "read_only": result["preserve_backlog"],
                "vector_tables": result["vector_tables"]}
            await self.db.execute("UPDATE lightrag_workspace_registry SET status='ready' WHERE workspace=$1", public_id)
            directory = self.args.control_root / public_id
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / ".postgres-workspace.json.partial"
            temporary.write_text(dumps(manifest) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(directory / ".postgres-workspace.json")

    async def run(self) -> dict:
        sources = self.sources()
        validate_report_path(self.args.source_root, getattr(self.args, "report", None))
        validate_control_root(self.args.control_root, [public_id for public_id, _ in sources], self.args.target_database, self.args.target_token)
        root_entries = sorted(self.args.source_root.iterdir())
        if any(path.is_symlink() for path in root_entries):
            raise ValueError("Source root entries must not be symbolic links")
        self.report["retained_source_only_root_files"] = [{"path": path.name, "bytes": path.stat().st_size} for path in root_entries if path.is_file()]
        if self.args.action == "plan":
            for public_id, path in sources:
                manifest = file_manifest(path)
                retained = classify_source_files(manifest)
                self.report["workspaces"][public_id] = {"postgres_workspace": workspace_namespace(public_id), "source_manifest": manifest, "retained_source_only_files": retained}
            return self.report | {"status": "planned"}
        from lightrag import __version__
        if __version__ != SUPPORTED_VERSION:
            raise ValueError("Offline migration requires pinned LightRAG " + SUPPORTED_VERSION)
        await self.connect()
        try:
            await self.initialize(self.args.action == "apply")
            for public_id, source in sources:
                await self.workspace(public_id, source, verify=self.args.action == "verify")
            self.report["indexes"] = await self.indexes(verify=self.args.action == "verify")
            if self.args.action == "apply":
                await self.publish()
            return self.report | {"status": "verified"}
        finally:
            if self.db is not None:
                await self.db.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=("plan", "apply", "verify"))
    result.add_argument("--source-root", type=Path, required=True)
    result.add_argument("--control-root", type=Path, required=True)
    result.add_argument("--target-database", required=True)
    result.add_argument("--target-token", type=lambda value: str(uuid.UUID(value)), required=True)
    result.add_argument("--embedding-model", required=True)
    result.add_argument("--embedding-dim", type=int, required=True)
    result.add_argument("--workspace", action="append")
    result.add_argument("--batch-size", type=int, default=128)
    result.add_argument("--statement-timeout", type=int, default=1800)
    result.add_argument("--report", type=Path)
    return result


def main() -> None:
    args = parser().parse_args()
    if not 1 <= args.batch_size <= 1000 or args.statement_timeout < 1:
        raise SystemExit("Batch size must be 1..1000 and timeout positive")
    source, control = args.source_root.resolve(), args.control_root.resolve()
    if source == control or source in control.parents or control in source.parents:
        raise SystemExit("Control root must be separate from the read-only source root")
    validate_report_path(args.source_root, args.report)
    result = asyncio.run(Migration(args).run())
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_suffix(args.report.suffix + ".partial")
        temporary.write_text(dumps(result) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(args.report)
    print(dumps(result), flush=True)


if __name__ == "__main__":
    main()
