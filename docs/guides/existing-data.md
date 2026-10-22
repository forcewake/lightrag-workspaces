# Adopt an existing installation

Preserve the old deployment, take a cold backup, and verify the extension against an independent copy before changing the original writer. There is no automatic migration step. This guide adopts the existing **file backend**. To transfer it into SQL, use [PostgreSQL + VectorChord](postgresql.md).

## Confirm the supported layout

The extension expects one direct child directory per corpus:

```text
rag_storage/
  research/
    kv_store_doc_status.json
    vdb_chunks.json
    ...
  handbook/
    ...
```

Exact filenames vary with the pinned engine. Empty corpus directories are valid and must be retained in copies. Symlink aliases and nested workspace paths are rejected.

A stock single-corpus installation may keep files directly in its storage root. Make an independent copy of that whole corpus **inside a named child directory** in the new layout; do not point the gateway at a root full of loose corpus files and expect them to become a workspace. Inspect and preserve the source layout first, especially if the upstream `WORKSPACE` option previously created another directory level.

The supported storage combination is `JsonKVStorage`, `JsonDocStatusStorage`, `NanoVectorDBStorage`, and `NetworkXStorage`. Do not adopt an SQL or external vector/graph deployment through this filesystem recipe.

## Record the original configuration

Privately retain:

- The exact old image or package revision, container settings, and restart policy.
- Storage and input mount paths, including their authoritative host locations.
- LLM, embedding, and reranking bindings, model names, endpoints, dimensions, and relevant provider options.
- The private environment file and authentication configuration.
- Per-corpus document counts and unfinished queue states.

Do not publish the environment, full container inspection, source documents, or logs containing secrets.

## Stop and back up

1. Wait for intended ingestion and writes to finish. Stop the old service cleanly.
2. Back up the entire authoritative storage and input trees, preserving empty directories and metadata. Include the private configuration separately.
3. Record a file manifest or checksums and verify the backup. Keep the original service and image available for rollback.
4. Make a separate staging copy from the backup. A verified copy-on-write reflink is suitable where the filesystem supports it; a hard link is not an independent write copy. Never assume a copy tool provided the semantics you intended.

Do not allow the old and new deployment to write the same corpus at the same time. The extension's worker lock coordinates its own shared runtime directory; it cannot police an unrelated stock container.

## Test a staging copy

Set the new Compose bind mounts to the staging storage and input roots, retain the exact model configuration, and use a different localhost port. Set `LIGHTRAG_DEFAULT_WORKSPACE` to an existing child directory. Leave the old deployment stopped while taking the baseline, or run it only against its separate original data after the staging copy is complete.

Before any write test, compare directory names, empty workspaces, file counts/checksums, document counts, queue states, and vector dimensions. Then check:

- Discovery contains every expected workspace.
- Scoped health, document reads, and graph reads select the correct corpus.
- Header and URL selectors agree; malformed, conflicting, and unknown identifiers are rejected.
- A new synthetic workspace can upload, index, query, stream, and delete a synthetic file.
- A second synthetic copy of a legacy corpus supports the operations you actually need.
- Switching in both browser interfaces retains local history and settings for the chosen workspace.
- Representative large corpora fit the container memory limit without OOM or restart.

Startup preservation can be checked before model queries: compare the staging files with the baseline after loading and clean shutdown. Subsequent explicit operations may legitimately write caches or indexes; separate those phases when comparing bytes.

## Understand the compatibility guardrails

The pinned worker disables known startup migrations and compatibility persistence. It may adapt known old cache structures in memory. It neither re-embeds data nor changes its directory names.

A saved embedding dimension mismatch blocks the workspace. Matching dimensions are necessary but cannot prove that the same embedding model created the vectors.

If old metadata contains `pending` or processing-stage documents, the workspace is protected from mutations with `409`; compatible search remains available. Old `failed` documents are retained and not automatically retried. This is a preservation state, not a completed queue repair.

Some old document records lack the anchors needed for modern deletion or reprocessing. Test those actions on a disposable copy and design a separate, reviewed recovery or migration if needed. Do not claim legacy write compatibility merely because reads work.

## Cut over

After staging verification, stop its service. Stop the old writer again and take a **fresh, verified cold backup** if the original changed during testing. Retain the original container/configuration and keep its automatic restart disabled while the extension owns the data.

Point the extension at the intended authoritative trees, start it, and repeat discovery, counts, identity routing, queue protection, model-configuration, and health checks. Keep the old image and the verified backup until the new deployment has been accepted for your workload.

If a check fails, follow [rollback](backup-and-rollback.md). Do not overwrite current data while a writer is still running.
