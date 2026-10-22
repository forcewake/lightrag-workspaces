# Changelog

## 0.2.1 — 2026-10-22

- Configurable `LIGHTRAG_LEGACY_STORAGE_WORKSPACE` keeps browser conversations and settings attached to their original workspace when the UI/API default changes.
- Existing installations retain their current browser keys when the new setting is omitted; explicit URL and header selection remain unchanged.
- English and Russian instructions for changing the default without moving existing data or browser history.

## 0.2.0 — 2026-10-22

- Optional PostgreSQL storage for documents, chunks, status, cache, graph data, and recovery anchors, with float32 VCHORDRQ vector indexes.
- Stable database namespaces and separate vector tables for each public workspace; existing UI, API header, and URL selectors keep their exact identifiers.
- Explicit, resumable migration into an owned database, read-back verification, original-record archives, and readiness receipts that prevent accidental empty-corpus startup.
- Reconstruction of missing recovery anchors from retained graph/chunk provenance without model calls; existing unfinished imports stay protected.
- PostgreSQL installation, migration, backup, and rollback documentation in English and Russian. File-backed installations remain supported.

## 0.1.0 — 2026-10-22

Initial public source release of the independent LightRAG Workspaces extension.

- Workspace selection in the browser interface, with workspace-specific conversations and settings.
- API selection through `/workspaces/<id>/…` and the `LIGHTRAG-WORKSPACE` header, including scoped Swagger and OpenAPI paths.
- Bounded, on-demand worker processes, idle retirement, and request/stream leases.
- Exact directory identities, guarded startup compatibility writes, and protection against automatically resuming unfinished legacy imports.
- Bounded NanoVectorDB loading for the pinned native format and configurable memory admission.
- A source-only Docker Compose installation, English documentation, Russian quick start, and guides for Unraid, existing data, backups, and rollback.

The supported baseline is LightRAG 1.5.7 with the exact source and image pins in the repository, using JSON, NanoVectorDB, and NetworkX storage. Workspaces share authentication and provider configuration. They do not provide tenant authorization. Existing installations are not upgraded by publishing this release.
