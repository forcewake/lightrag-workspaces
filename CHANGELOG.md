# Changelog

## 0.3.3 — 2026-10-26

- Both interfaces remember one API key per server origin when switching workspaces. Existing histories and query settings remain workspace-scoped; clearing or rotating a key updates other open tabs.
- The ingestion and retrieval client can load a private local credential file, restricted to its configured server origin. Explicit environment credentials retain precedence.
- Offline image checks cover missing, incorrect and valid API keys, guest-token rejection, header/URL workspace routing and unchanged synthetic data.
- Authentication documentation explains the upstream endpoint whitelist and the public discovery/UI boundary.

## 0.3.2 — 2026-10-25

- The workspace selector now searches names, IDs and tags directly in its dropdown. Current and favorite workspaces are easy to find, with document counts, bounded scrolling and keyboard navigation in both interfaces.
- Workspace IDs, scoped URLs, API selection and browser histories remain unchanged. The separate catalog still handles creation and metadata editing.
- Application update checks compare complete Docker mount records independently of their returned order, while rejecting changed fields and duplicate destinations.

## 0.3.1 — 2026-10-24

- Backup verification sorts fixed-size SHA-256 row digests instead of complete document/vector rows, reducing PostgreSQL temporary-file use without changing server limits. Counts and duplicate rows remain part of verification.
- New fingerprints pin timestamp, date, interval and bytea serialization settings. Restore drills select the recorded algorithm and retain support for existing v1 backup bundles.
- Release fixtures explicitly pull the pinned PostgreSQL image and wait for the final TCP listener before running checks.

## 0.3.0 — 2026-10-24

- Searchable workspace catalog with display names, descriptions, tags and browser-local favorites in both interfaces. Exact IDs, URL/header routing and existing browser history keys remain unchanged.
- Explicit idempotent workspace creation, durable operation receipts and crash reconciliation for file and PostgreSQL backends; new vector namespaces are provisioned without changing shared graph tables.
- Worker lifecycle diagnostics, separate liveness/readiness and authenticated persistent maintenance with request/pipeline draining.
- Host-run operations toolkit for consistent PostgreSQL/file snapshots, full archive validation, isolated restoration checks, application-only updates/rollback, protected retention and optional checksum-verified second copies.
- Portable version-preserving ingestion client and cited-source retrieval gates after imports and upgrades.
- Release workflow for versioned amd64 container images, pre-publication tests and checks of the published immutable digest; source builds remain available.

## 0.2.3 — 2026-10-24

- PostgreSQL graph workers validate the existing schema on startup instead of repeating schema DDL and legacy edge normalization. A reader holding a graph-table lock no longer causes a cold workspace to fail on the redundant `ALTER TABLE`.
- Missing or incompatible graph schemas fail closed with an offline repair instruction; migration remains an explicit operation. Existing workspace identifiers, UI/API selectors, documents, vectors and graph records are retained.
- Regression checks cover lock contention, concurrent initialization, pool cleanup, schema rejection and unchanged synthetic graph records.

## 0.2.2 — 2026-10-22

- Native Markdown pipe-table headers are included in the table body before sidecar hashing, chunking, table analysis and embedding. Literal column labels remain available even when table analysis is disabled or its generated description omits them.
- Small and header-only tables retain their headers; oversized table slices reuse the pinned chunker's header handling and token limits. HTML tables keep their original header/span representation.
- The compatibility hook verifies the exact upstream adapter source and LightRAG version, performs no startup corpus migration, and retains structured header metadata.
- Synthetic regression checks exercise the actual pinned parser, sidecar writer and paragraph chunker without provider calls. Existing documents require a separately backed-up reindex operation to gain the new searchable text.

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
