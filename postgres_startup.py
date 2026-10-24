"""Open migrated PostgreSQL graphs without running shared-table schema DDL.

The pinned upstream graph initializer repeats ALTER TABLE on every worker start.
Even an ADD COLUMN IF NOT EXISTS takes an exclusive lock, so a long-running
reader in another workspace can make an otherwise healthy cold worker fail.
Schema creation and legacy repairs belong to the explicit offline importer.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import re
from typing import Any, Mapping


GRAPH_NAMESPACE = "chunk_entity_relation"
GRAPH_INITIALIZE_SHA256 = "fc56b3532865b82e1c02148dd4f6b6943f42d9de33978fb532643d4d730bc21f"
_ORIGINAL_ATTRIBUTE = "_workspaces_graph_startup_original"
_IDENTITY_ATTRIBUTE = "_workspaces_graph_startup_identity"
GRAPH_COLUMNS = {
    "lightrag_graph_nodes": {
        "workspace": "text", "namespace": "text", "id": "text",
        "properties": "jsonb", "updated_at": "timestamp with time zone",
    },
    "lightrag_graph_edges": {
        "workspace": "text", "namespace": "text", "src_id": "text", "tgt_id": "text",
        "properties": "jsonb", "updated_at": "timestamp with time zone",
    },
}
GRAPH_PRIMARY_KEYS = {
    "lightrag_graph_nodes": ("workspace", "namespace", "id"),
    "lightrag_graph_edges": ("workspace", "namespace", "src_id", "tgt_id"),
}


async def validate_postgres_graph_schema(
    connection: Any, workspace: str, namespace: str = GRAPH_NAMESPACE,
) -> None:
    """Read catalog/data invariants; never create, normalize, or repair a graph.

    The caller supplies a read-only transaction. Checks use schema-qualified
    relations and also reject a search_path that resolves native unqualified
    graph queries elsewhere. Extra columns and renamed equivalent constraints
    or indexes are harmless; required keys and endpoint integrity are not.
    """
    if not re.fullmatch(r"ws_[0-9a-f]{32}", workspace) or namespace != GRAPH_NAMESPACE:
        raise RuntimeError("PostgreSQL graph startup requires the registered workspace and graph namespace")

    def incompatible(detail: str) -> None:
        raise RuntimeError(
            f"PostgreSQL graph schema is not ready: {detail}; "
            "run an explicit offline migration or repair before starting this workspace"
        )

    columns = await connection.fetch("""
        SELECT c.relname AS table_name, c.relkind::text AS relkind,
               c.oid = to_regclass(c.relname) AS resolves_to_public,
               a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS data_type,
               a.attnotnull AS not_null
        FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE c.oid IN (to_regclass('public.lightrag_graph_nodes'),
                        to_regclass('public.lightrag_graph_edges'))
          AND a.attnum > 0 AND NOT a.attisdropped
    """)
    actual = {(row["table_name"], row["column_name"]): row for row in columns}
    for table, required in GRAPH_COLUMNS.items():
        for name, data_type in required.items():
            row = actual.get((table, name))
            if (not row or row["relkind"] != "r" or not row["resolves_to_public"]
                    or row["data_type"] != data_type or not row["not_null"]):
                incompatible(f"missing or incompatible {table}.{name}")

    constraints = await connection.fetch("""
        SELECT t.relname AS table_name, c.contype::text AS kind,
               c.convalidated AS validated, c.condeferrable AS deferrable,
               c.confdeltype::text AS delete_action,
               ARRAY(SELECT a.attname::text
                     FROM unnest(c.conkey) WITH ORDINALITY k(attnum, position)
                     JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                     ORDER BY k.position) AS columns,
               c.confrelid = to_regclass('public.lightrag_graph_nodes') AS references_nodes,
               ARRAY(SELECT a.attname::text
                     FROM unnest(c.confkey) WITH ORDINALITY k(attnum, position)
                     JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum
                     ORDER BY k.position) AS referenced_columns,
               COALESCE(i.indisvalid AND i.indisready AND i.indisunique, false) AS index_ready
        FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
        LEFT JOIN pg_index i ON i.indexrelid = c.conindid
        WHERE c.conrelid IN (to_regclass('public.lightrag_graph_nodes'),
                            to_regclass('public.lightrag_graph_edges'))
          AND c.contype IN ('p', 'f')
    """)
    for table, keys in GRAPH_PRIMARY_KEYS.items():
        if not any(row["table_name"] == table and row["kind"] == "p"
                   and tuple(row["columns"]) == keys and row["validated"]
                   and not row["deferrable"] and row["index_ready"] for row in constraints):
            incompatible(f"{table} needs its namespace-scoped primary key")
    for endpoint in ("src_id", "tgt_id"):
        if not any(row["table_name"] == "lightrag_graph_edges" and row["kind"] == "f"
                   and tuple(row["columns"]) == ("workspace", "namespace", endpoint)
                   and row["references_nodes"]
                   and tuple(row["referenced_columns"]) == GRAPH_PRIMARY_KEYS["lightrag_graph_nodes"]
                   and row["validated"] and not row["deferrable"] and row["delete_action"] == "c"
                   for row in constraints):
            incompatible(f"graph {endpoint} needs a validated namespace-scoped cascading foreign key")

    indexes = await connection.fetch("""
        SELECT i.indisvalid AS valid, i.indisready AS ready, am.amname AS method,
               i.indpred IS NULL AS unconditional, i.indnkeyatts AS key_count,
               ARRAY(SELECT a.attname::text
                     FROM unnest(i.indkey) WITH ORDINALITY k(attnum, position)
                     JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                     WHERE k.position <= i.indnkeyatts ORDER BY k.position) AS columns
        FROM pg_index i JOIN pg_class ix ON ix.oid = i.indexrelid
        JOIN pg_am am ON am.oid = ix.relam
        WHERE i.indrelid = to_regclass('public.lightrag_graph_edges')
    """)
    if not any(row["valid"] and row["ready"] and row["method"] == "btree"
               and row["unconditional"] and row["key_count"] == 3
               and tuple(row["columns"]) == ("workspace", "namespace", "tgt_id") for row in indexes):
        incompatible("graph edges need the ready reverse-endpoint index")

    # Match upstream's C-collation predicate so its partial index can serve this
    # guard. Reading it does not take the schema advisory or exclusive lock.
    if await connection.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM public.lightrag_graph_edges
            WHERE workspace = $1 AND namespace = $2
              AND src_id COLLATE "C" > tgt_id COLLATE "C" LIMIT 1
        )
    """, workspace, namespace):
        incompatible("this workspace contains noncanonical legacy edges")


def install_postgres_graph_startup(manifest: Mapping[str, Any]) -> bool:
    """Replace only the pinned graph initializer in a dedicated worker process.

    Keep native ClientManager pool acquisition, vector settings, shared init
    locking, and finalize. The importer does not install this hook and retains
    the native schema bootstrap. All explicit graph read/write methods remain
    unchanged.
    """
    from lightrag import __version__
    from lightrag.kg.pgtable_impl import PGTableGraphStorage
    from lightrag.kg.postgres_impl import ClientManager, PostgreSQLDB
    from lightrag.kg.shared_storage import get_data_init_lock
    from lightrag.utils import logger

    if (__version__ != "1.5.7"
            or not inspect.iscoroutinefunction(PGTableGraphStorage.initialize)
            or not inspect.iscoroutinefunction(PGTableGraphStorage.finalize)
            or not inspect.iscoroutinefunction(PostgreSQLDB._run_with_retry)):
        raise RuntimeError("PostgreSQL graph startup hook requires pinned LightRAG 1.5.7")
    current = PGTableGraphStorage.initialize
    original = getattr(current, _ORIGINAL_ATTRIBUTE, current)
    try:
        digest = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    except (OSError, TypeError) as error:
        raise RuntimeError("Cannot verify the pinned PostgreSQL graph initializer") from error
    if digest != GRAPH_INITIALIZE_SHA256:
        raise RuntimeError("Unsupported PostgreSQL graph initializer; source SHA256 differs")
    workspace = manifest["postgres_workspace"]
    database = manifest["database"]
    if not re.fullmatch(r"ws_[0-9a-f]{32}", workspace) or not isinstance(database, str) or not database:
        raise ValueError("PostgreSQL graph startup requires a completed workspace manifest")
    if original is not current:
        if getattr(current, _IDENTITY_ATTRIBUTE, None) != (database, workspace):
            raise RuntimeError("PostgreSQL graph startup hook is already bound to another workspace")
        return False

    @functools.wraps(original)
    async def initialize(storage: Any) -> None:
        if storage.namespace != GRAPH_NAMESPACE:
            raise RuntimeError("PostgreSQL graph namespace differs from the completed migration")
        async with get_data_init_lock():
            if storage.db is not None:
                if (storage.workspace != workspace or storage.db.workspace != workspace
                        or storage.db.database != database):
                    raise RuntimeError("PostgreSQL graph is already bound to another workspace")
                return
            db = await ClientManager.get_client(
                vector_storage=storage.global_config.get("vector_storage")
            )
            try:
                if db.workspace != workspace or db.database != database:
                    raise RuntimeError("PostgreSQL graph client differs from the completed migration")

                async def validate(connection: Any) -> None:
                    async with connection.transaction(readonly=True):
                        await connection.execute("SET LOCAL statement_timeout = '10s'")
                        await validate_postgres_graph_schema(connection, workspace, storage.namespace)

                await db._run_with_retry(validate)
            except BaseException:
                # Do not retain a client reference when initialize never
                # completed, including cancellation while waiting for a query.
                await ClientManager.release_client(db)
                raise
            storage.db = db
            storage.workspace = workspace
        logger.info(f"[{workspace}] PGTableGraphStorage initialized without schema writes")

    setattr(initialize, _ORIGINAL_ATTRIBUTE, original)
    setattr(initialize, _IDENTITY_ATTRIBUTE, (database, workspace))
    PGTableGraphStorage.initialize = initialize
    return True
