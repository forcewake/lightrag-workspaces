# Architecture and limits

One gateway routes each request to an individual LightRAG worker process selected by corpus ID. The ID resolves to a file-corpus directory or a registered PostgreSQL namespace; selection never renames it or mutates a global active-workspace variable.

![Illustrative request and process routing](../assets/workspaces.svg)

## Request lifecycle

1. Discover valid direct child directories; in PostgreSQL mode, accept only verified control registrations and check live database metadata.
2. Resolve the URL prefix or the exact `LIGHTRAG-WORKSPACE` header; reject conflicts and unknown names.
3. Check compatibility and legacy-queue protection before forwarding a mutation.
4. Acquire or start the selected worker, subject to pool and memory admission.
5. Hold a worker lease through the complete request or streaming response.
6. Release the lease and retire the worker later only when idle checks permit it.

Each worker uses one upstream process, a workspace-specific input directory, and private runtime logs. Its internal bind is loopback inside the container. Keep only one gateway process: multiple gateway replicas sharing the same corpus are outside the supported deployment contract.

## What is isolated

| Separate per workspace | Shared across the instance |
| --- | --- |
| File corpus or PostgreSQL namespace, vectors, graph, metadata, caches | Provider endpoints, API keys, model choices, and embedding dimension |
| Uploaded source/input directory | Authentication configuration and user trust domain |
| Worker process and its upstream pipeline state | Gateway, host, container memory limit, and worker-pool capacity |
| Browser history/settings keys | Browser origin and its access boundary |

This is data routing and process separation, not hostile-tenant isolation. The gateway discovery metadata is instance-visible. A user with access to the shared service is not given per-workspace authorization by the selector.

## Loading and memory

Workers load on first use. The default active limit is two; a ten-minute idle threshold makes a worker eligible for retirement, while requests and upstream pipeline state remain part of the safety check. A busy worker is not evicted merely because another corpus was selected.

Cold-load admission estimates memory from vector JSON, graph files, and text/cache metadata, reserves in-progress cold loads, and considers Linux host/cgroup headroom. It can refuse a request with `503` even when fewer than the maximum workers are resident. The estimator is not a measured peak or an OOM guarantee. Set a hard container limit and test your largest representative corpus.

PostgreSQL mode uses a fixed process startup allowance rather than adding the size of archived JSON matrices. PostgreSQL manages stored vectors and graph tables separately; SQL queries, index builds, batches, and provider work still consume memory. The application and database need separate resource monitoring.

The pinned NanoVectorDB loader decodes the saved matrix into one writable float32 buffer and normalizes bounded row blocks. This reduces large transient allocations compared with loading and normalizing the complete representation repeatedly. It does not make a large corpus small or bound the entire upstream engine's memory use. The loader is specific to the pinned NanoVectorDB format and is covered by equivalence tests for metadata, vectors, and query results.

## Provider budgets

Maximum provider concurrency is statically partitioned among the permitted workers. Increasing the worker limit divides each worker's share further; changing it can make a previously valid small budget fail startup. The partition is per configured role, not a global cross-role rate limiter or cost ceiling.

A provider's model context window, rate limits, timeouts, and account capacity still apply. Multiple workers do not create extra provider capacity.

## Preserving legacy data

The extension is pinned to LightRAG 1.5.7 and intercepts known startup compatibility hooks. It keeps exact directory names, avoids automatic ingestion resume, and adapts known legacy cache records in memory without persisting that adaptation at startup.

This is not a general migration engine. New explicit uploads, queries, and deletions can write indexes, source artifacts, or caches according to upstream behavior. Old deletion and reprocessing paths may require metadata anchors absent from legacy documents. Review and test those operations separately on a copy.

Old pending or processing-stage imports trigger protection from mutation; compatible reads remain available. Failed records remain recorded rather than being silently retried. Saved vector dimensions are checked, but the storage format alone cannot establish the historical embedding model identity.

## PostgreSQL storage and migration

PostgreSQL mode combines upstream `PGKVStorage`, `PGDocStatusStorage`, `PGVectorStorage` with `VCHORDRQ`, and `PGTableGraphStorage`. It uses ordinary relational graph tables and does not require AGE. Each public ID maps deterministically to `ws_` plus 32 hexadecimal SHA-256 characters. The complete public name is retained in the registry, so punctuation normalization cannot merge `meeting-notes` with `meeting_notes`.

The control root is separate from the original file corpus and contains explicit workspace registrations and `.workspace-management/` catalog/operation state. A registration is usable only when its ready state, namespace, embedding identity, and database identity match the live database. Discovery uses a small read-only pool and current document-status counts without starting a worker. It never creates a schema. A database outage or mismatch fails closed rather than reading stale JSON.

Migration is an offline operation into a new dedicated database, not part of startup. The importer plans, transfers, verifies, and only then publishes ready manifests. Original source files, uploads, configuration, and snapshots remain available for audit and rollback. A legacy queue's preservation flag is carried into the registration, while fresh uploads in a writable workspace do not freeze it merely because they are pending.

Full-precision vector storage and approximate-index recall are separate properties. VCHORDRQ stores original float32 vector columns and builds its search index from them; validate stored values, index presence/use, recall against exact search, and performance on representative data. Do not infer a completed migration from a healthy gateway or a successfully started worker.

## Supported and unsupported deployment changes

Supported here: the pinned upstream version, the four file-backed storage adapters or the documented PostgreSQL + VCHORDRQ combination, one gateway process, and its managed local worker pool.

Outside the current contract: other PostgreSQL adapter combinations, Neo4j/Qdrant adapters, per-workspace credentials or user permissions, distributed gateway replicas, automatic migration at startup, automatic queue recovery, and unreviewed upstream image upgrades. These require separate contracts and tests.
