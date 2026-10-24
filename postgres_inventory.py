"""Read-only PostgreSQL workspace discovery without starting corpus workers.

The filesystem manifest and database registry must agree before a workspace is
advertised as available. This module never creates tables or repairs metadata.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from postgres_backend import postgres_connection_kwargs


class PostgresInventory:
    """A small separate pool for live status counts, not corpus contents."""

    def __init__(self) -> None:
        self.pool = None
        self.lock = asyncio.Lock()

    async def _pool(self):
        if self.pool is None:
            async with self.lock:
                if self.pool is None:
                    import asyncpg

                    connection = postgres_connection_kwargs()
                    connection.update(timeout=5, command_timeout=5)
                    self.pool = await asyncpg.create_pool(
                        min_size=1, max_size=2, max_inactive_connection_lifetime=60,
                        **connection,
                    )
        return self.pool

    async def close(self) -> None:
        if self.pool is not None:
            pool, self.pool = self.pool, None
            try:
                await asyncio.wait_for(pool.close(), timeout=5)
            except Exception:
                pool.terminate()

    async def probe(self, expected_database: str) -> bool:
        """Read database identity and registry presence without waking a worker."""
        try:
            pool = await self._pool()
            async with pool.acquire(timeout=5) as connection:
                async with connection.transaction(readonly=True):
                    await connection.execute("SET LOCAL statement_timeout = '3s'")
                    marker = await connection.fetchrow(
                        "SELECT current_database() AS database, db_instance_id::text AS db_instance_id, schema_version "
                        "FROM lightrag_workspace_database WHERE singleton = TRUE")
                    registry = await connection.fetchval("SELECT to_regclass('public.lightrag_workspace_registry') IS NOT NULL")
                    return bool(marker and marker["database"] == expected_database and marker["schema_version"] == 1 and registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def refresh(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        registered = [item for item in items if item.get("available") and item.get("_postgresManifest")]
        if not registered:
            for item in items:
                item.pop("_postgresManifest", None)
                item.pop("postgresWorkspace", None)
            return items
        try:
            pool = await self._pool()
            async with pool.acquire(timeout=5) as connection:
                async with connection.transaction(readonly=True):
                    await connection.execute("SET LOCAL statement_timeout = '3s'")
                    await connection.execute("SET LOCAL lock_timeout = '1s'")
                    marker = await connection.fetchrow(
                        "SELECT current_database() AS database, db_instance_id::text AS db_instance_id, schema_version "
                        "FROM lightrag_workspace_database WHERE singleton = TRUE"
                    )
                    rows = await connection.fetch(
                        "SELECT workspace, postgres_workspace, db_instance_id::text AS db_instance_id, "
                        "status, embedding_model, embedding_dim FROM lightrag_workspace_registry "
                        "WHERE workspace = ANY($1::text[])", [item["id"] for item in registered],
                    )
                    counts = await connection.fetch(
                        "SELECT workspace, status, COUNT(*)::bigint AS count FROM lightrag_doc_status "
                        "WHERE workspace = ANY($1::text[]) GROUP BY workspace, status",
                        [item["_postgresManifest"]["postgres_workspace"] for item in registered],
                    )
            registry = {row["workspace"]: row for row in rows}
            status_counts: dict[str, dict[str, int]] = {}
            for row in counts:
                status_counts.setdefault(row["workspace"], {})[row["status"]] = int(row["count"])
            for item in registered:
                manifest = item["_postgresManifest"]
                row = registry.get(item["id"])
                if (not marker or marker["schema_version"] != 1
                        or marker["database"] != manifest["database"]
                        or marker["db_instance_id"] != manifest["db_instance_id"]
                        or not row or row["status"] != "ready"
                        or row["postgres_workspace"] != manifest["postgres_workspace"]
                        or row["db_instance_id"] != manifest["db_instance_id"]
                        or row["embedding_model"] != manifest["embedding_model"]
                        or row["embedding_dim"] != manifest["embedding_dim"]):
                    item.update(available=False, readOnly=True, errorCode="workspace_registration_invalid", readOnlyReason=(
                        "PostgreSQL workspace registration does not match the verified database."
                    ))
                    continue
                counts_for_workspace = status_counts.get(manifest["postgres_workspace"], {})
                item.update(documents=sum(counts_for_workspace.values()),
                            processed=counts_for_workspace.get("processed", 0),
                            failed=counts_for_workspace.get("failed", 0),
                            pending=counts_for_workspace.get("pending", 0),
                            processing=sum(counts_for_workspace.get(status, 0) for status in
                                           ("processing", "parsing", "analyzing", "preprocessed")))
                if item.get("readOnly"):
                    item["readOnlyReason"] = (
                        "Earlier unfinished imports are preserved. Uploads and changes are paused; search remains available."
                        if manifest["preserve_backlog"] else "This workspace is registered as read-only."
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Connection errors can contain connection strings. Expose and log
            # only a generic state; no credentials or document data are needed.
            logging.getLogger(__name__).warning("PostgreSQL workspace discovery is unavailable")
            for item in registered:
                item.update(available=False, readOnly=True, errorCode="database_unavailable", readOnlyReason=(
                    "PostgreSQL workspace status is unavailable; retry after the database is reachable."
                ))
        finally:
            for item in items:
                item.pop("_postgresManifest", None)
                item.pop("postgresWorkspace", None)
        return items
