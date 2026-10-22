# PostgreSQL + VectorChord

Keep the same browser workspace selector and API selectors while moving corpus storage to PostgreSQL. This backend uses `PGKVStorage`, `PGDocStatusStorage`, `PGVectorStorage` with `VCHORDRQ`, and `PGTableGraphStorage`. The graph uses ordinary PostgreSQL tables; Apache AGE is not required.

[Русская инструкция](postgresql.ru.md)

## What moves

| Data | Destination |
| --- | --- |
| Documents, text chunks, document statuses and tracking metadata | PostgreSQL |
| Saved chunk, entity, and relation embeddings | PostgreSQL `vector` columns and VCHORDRQ indexes |
| Entities, relations, and graph properties | PostgreSQL graph tables |
| Supported LLM cache records, provenance anchors, and source metadata | PostgreSQL |
| Original JSON record values and graph/vector metadata | PostgreSQL migration archive, separate from native query tables |
| Original uploads and parser artifacts | Existing per-workspace input directories |
| Control manifests, process locks, and logs | Private filesystem directories |
| Original JSON/NanoVectorDB/GraphML corpus | Retained separately as the migration source and rollback snapshot |

The importer transfers existing embeddings without calling an embedding provider. VCHORDRQ keeps float32 vectors in the table; its approximate search still needs a recall check against exact search. Matching dimensions alone do not establish embedding-model compatibility. Keep the original model, dimension, provider configuration, and parser settings.

Native tables provide the LightRAG API's working records. The separate `lightrag_workspace_archive` retains original JSON values as canonical bytes, including values not representable as JSONB. Supported legacy cache shapes are adapted for the native cache, and missing graph/document anchors are reconstructed from existing provenance where possible, without calling a model. The archive and source snapshot retain the original records independently of these projections. An API delete or reprocessing operation does **not** erase historical migration archives or backups; apply your own retention and disposal policy to those copies.

Migration is explicit. Discovery, the health check, and ordinary startup never import a directory or create an unknown workspace. Failed documents are not automatically retried. Preserved pending or processing queues remain protected from mutation after migration.

Unknown store files matching `kv_store_*.json`, `vdb_*.json`, or `graph_*.graphml`, and unsupported GraphML structures, stop migration before a ready registration can be published. Adapt those formats explicitly rather than treating them as imported. Other files, such as source archives or sidecar artifacts, remain in the read-only source and cold backup: the reports list their paths under `retained_source_only_files` per workspace and `retained_source_only_root_files` at the source root. These reported files are not native PostgreSQL records; retain and back them up separately.

## Prepare PostgreSQL

Use a **new dedicated empty database**. Do not point the importer at a database already used by another LightRAG installation or another application. The importer records a database identity token and refuses a different target on resume.

Install compatible PostgreSQL, pgvector, and VectorChord packages. VectorChord's library must already be in `shared_preload_libraries`; arrange any database-service restart separately before migration. The application does not change this server-wide setting or restart PostgreSQL. In the new target database, enable:

```sql
CREATE EXTENSION vector;
CREATE EXTENSION vchord;
```

The application role needs connection and schema/table/index creation rights in this dedicated database. Preserve its credentials privately. This release's reference deployment was tested with PostgreSQL 18.3, pgvector 0.8.2, VectorChord 1.1.1, and 3072-dimensional embeddings. Use the [VectorChord installation documentation](https://docs.vectorchord.ai/vectorchord/getting-started/installation.html) for extension installation and the [pinned LightRAG adapter](https://github.com/HKUDS/LightRAG/blob/28ff1b05f2ac3f3e6fa14dd2cd33656579bd0c9c/lightrag/kg/postgres_impl.py) for upstream storage behavior.

Add database settings to the private `.env`; keep file mode selected until cutover:

```dotenv
LIGHTRAG_STORAGE_BACKEND=file
POSTGRES_HOST=postgresql
POSTGRES_PORT=5432
POSTGRES_USER=lightrag
POSTGRES_PASSWORD=your-private-database-password
POSTGRES_DATABASE=lightrag_workspaces
POSTGRES_SSL_MODE=disable
POSTGRES_MAX_CONNECTIONS=4
POSTGRES_VECTOR_INDEX_TYPE=VCHORDRQ
POSTGRES_VCHORDRQ_EPSILON=2.3
POSTGRES_VCHORDRQ_PROBES=
POSTGRES_VCHORDRQ_BUILD_OPTIONS=
```

`POSTGRES_HOST` must resolve **inside the LightRAG container**. For another Docker container, connect both containers to an existing shared network or use a reachable host address and its published PostgreSQL port. `localhost` inside LightRAG refers to LightRAG itself. The supplied Compose file does not create or manage PostgreSQL.

Use `disable` only for the intended trusted local database connection; supported TLS modes and certificate variables follow the pinned upstream configuration. Connection settings are validated before a worker starts. `POSTGRES_MAX_CONNECTIONS` is the maximum per worker, with an additional gateway pool capped at two connections. Size the PostgreSQL connection limit for the active worker count and your other applications.

`epsilon=2.3` and no explicit partition/probe override are a starting configuration, not a universal tuning result. Choose partitioning and search settings using representative data and measured recall. A 3072-dimensional `vector` does not fit pgvector's ordinary HNSW vector index; VCHORDRQ is the supported full-precision option here. See [pgvector's index limits](https://github.com/pgvector/pgvector#hnsw).

## Take and verify a cold source copy

1. Finish active ingestion and stop the old writer cleanly.
2. Preserve the complete old corpus and inputs, empty directories, configuration, exact image, and container settings. Verify a cold backup before doing anything to the live deployment.
3. Keep a separate, read-only migration source. Create a **new empty control root**, outside both the source and input roots. Never reuse the original file-corpus root as the PostgreSQL control root.
4. Generate a fresh UUID for the target database and retain it in the private deployment record. Reuse that same token only when resuming or verifying this same migration.

The source must contain direct child workspace directories, including empty ones. Public IDs keep their original spelling, case, dots, hyphens, and underscores. Internally each maps to `ws_` plus the first 32 hexadecimal characters of its SHA-256 digest. For example, `meeting-notes` and `meeting_notes` remain distinct.

## Plan, import, and verify

The migration CLI is part of the built image. It accepts `plan`, `apply`, and `verify`; `plan` reads source metadata without connecting to PostgreSQL. `apply` imports into the dedicated target, verifies imported data, and publishes ready control manifests only after verification. `verify` checks the same source and database without re-importing it. Verification includes source hashes before/after, original and native record readback, float32 vector bytes, counts, and valid VCHORDRQ indexes. Source mounts must be read-only.

The following example uses a host cold-copy path and a separate control path. Replace both absolute paths, the model, dimension, database name, and token. The UUID shown below is an example, not a token to reuse across installations.

```sh
docker compose build
docker compose run --rm --no-deps --entrypoint python \
  -v /absolute/verified-cold-copy/rag_storage:/migration-source:ro \
  -v /absolute/postgres-control:/postgres-control \
  lightrag /opt/lightrag-multiworkspace/migrate_postgres.py plan \
  --source-root /migration-source --control-root /postgres-control \
  --target-database lightrag_workspaces \
  --target-token 975c4bf0-d2fb-43d5-8498-9454926fb57c \
  --embedding-model original-embedding-model --embedding-dim 3072 \
  --report /app/data/workspace-runtime/postgres-plan.json
```

Review the plan and capacity before proceeding. Run the **same command** with `apply` instead of `plan` and a distinct report path, then run it with `verify`. Use repeated `--workspace exact-id` options to select specific source workspaces, or omit them to discover all direct child directories. `--batch-size 128` controls transfer batches; memory, disk, index-build cost, and database timeouts still need monitoring.

Do not regenerate successfully imported content or change embedding models to work around a failure. Inspect a failed verification first; preserve its source and report. If a record cannot be represented by the selected backend, keep its original material and define a separate recovery/reprocessing step. Ordinary migration does not silently enqueue provider calls or resume old failed jobs.

## Cut over while preserving selection

Stop all file-mode workers before changing the deployment. Retain the old image, its original mounts and environment, and the verified cold source. In Compose, change the host source of the `/app/data/rag_storage` mount to the **new PostgreSQL control root**. Keep `/app/data/inputs` mapped to the preserved input tree and runtime mapped separately. In `.env`, set:

```dotenv
LIGHTRAG_STORAGE_BACKEND=postgres
LIGHTRAG_DEFAULT_WORKSPACE=your-existing-default-id
```

Start one gateway and verify:

```sh
docker compose up -d
curl http://localhost:9621/workspaces
curl http://localhost:9621/workspaces/your-existing-default-id/health
curl http://localhost:9621/health -H 'LIGHTRAG-WORKSPACE: your-existing-default-id'
```

Check all original workspace IDs and document counts, the graph and retrieval of representative corpora, both browser interfaces, URL/header/default routing, and rejection of unknown or conflicting selectors. Add configured authentication to API calls. A synthetic workspace can verify upload, indexing, streaming, and deletion without changing a migrated client corpus.

PostgreSQL discovery reads the manifests and live database counts without loading the corpus or creating database objects. It fails closed if the database identity, registered namespace, model, dimension, or readiness disagrees. If PostgreSQL is unavailable, workspaces are shown as unavailable and requests do not fall back to old JSON files.

## Create another PostgreSQL workspace

Creation remains explicit. Use the existing initializer against a **separate empty source tree**, then run the same migration plan/apply/verify procedure with `--workspace exact-id`, the existing database identity token, and the existing PostgreSQL control root. An empty source workspace creates an empty registered database namespace. Refresh the UI to see it. Do not create a control manifest by hand or use an API request to initialize a name.

## Back up and roll back

After cutover, a filesystem-only backup no longer contains the current corpus. Back up PostgreSQL with a supported consistent backup method, retain extension versions and installation instructions, and keep the control manifests, input tree, private environment, and application revision together. Test restoration into a separate database and deployment. See [backup and rollback](backup-and-rollback.md).

To return to the original file snapshot, stop the PostgreSQL deployment, preserve the current database and inputs for recovery, and restore the old image, environment, and matching file/input snapshot. Changes made in PostgreSQL after cutover are **not copied back into JSON** by rollback. Reconcile those changes before choosing an older recovery point. Never run the old and new writers against the same inputs simultaneously.
