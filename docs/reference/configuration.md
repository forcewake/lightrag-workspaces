# Configuration reference

Copy `.env.example` to `.env` and keep it private. Compose reads host options from this file and passes the provider and extension settings to the container. Recreate the container after changing environment values.

## Extension and deployment settings

| Variable | Example/default | Meaning |
| --- | --- | --- |
| `BIND_ADDRESS` | `127.0.0.1` | Compose host bind address; use network controls before changing it |
| `LIGHTRAG_PORT` | `9621` | Published host port; the gateway listens on container port 9621 |
| `LIGHTRAG_MEMORY_LIMIT` | `8g` | Compose container hard memory ceiling; size for your corpora |
| `LIGHTRAG_STORAGE_BACKEND` | `file` | `file` or `postgres`; use the explicit migration procedure before changing |
| `LIGHTRAG_DEFAULT_WORKSPACE` | `default` | Existing corpus used when no selector is supplied |
| `LIGHTRAG_MAX_ACTIVE_WORKSPACES` | `2` | Maximum resident workers; memory admission can allow fewer |
| `LIGHTRAG_WORKSPACE_STARTUP_TIMEOUT` | `240` | Maximum seconds to wait for a cold worker to become healthy |
| `LIGHTRAG_WORKSPACE_IDLE_SECONDS` | `600` | Idle seconds before a worker becomes eligible for retirement |
| `LIGHTRAG_WORKSPACE_MEMORY_HEADROOM_BYTES` | `2147483648` | Reserve subtracted from available memory before admitting a cold worker |
| `WORKING_DIR` | `/app/data/rag_storage` | Parent of file corpora, or a separate PostgreSQL control root containing registered workspace folders |
| `INPUT_DIR` | `/app/data/inputs` | Container parent directory for per-workspace uploads |
| `LIGHTRAG_MULTIWORKSPACE_RUNTIME` | `/app/data/workspace-runtime` | Locks and logs; must remain outside the corpus tree |
| `LIGHTRAG_MULTIWORKSPACE_STATIC` | `/opt/lightrag-multiworkspace/webui` | Built UI assets; normally leave at image default |

The startup deadline defaults to **240 seconds**, and idle retirement defaults to **600 seconds**. A worker must also pass the idle check before retirement; elapsed time alone does not override active work.

`LIGHTRAG_WORKER_BUDGET_DIVISOR` is internal: the gateway sets it from the worker-pool limit. Worker-level `WORKSPACE`, `POSTGRES_WORKSPACE`, `WORKING_DIR`, `INPUT_DIR`, `HOST`, `PORT`, `WORKERS`, `LOG_DIR`, and `LIGHTRAG_API_PREFIX` are set internally for each selected corpus. Do not use upstream `WORKSPACE` or `POSTGRES_WORKSPACE` as an alternative per-request selector.

## Provider identity

| Variable | Purpose |
| --- | --- |
| `LLM_BINDING`, `LLM_BINDING_HOST`, `LLM_BINDING_API_KEY`, `LLM_MODEL` | Upstream LLM provider and model |
| `EMBEDDING_BINDING`, `EMBEDDING_BINDING_HOST`, `EMBEDDING_BINDING_API_KEY`, `EMBEDDING_MODEL` | Upstream embedding provider and model |
| `EMBEDDING_DIM` | Expected saved and generated vector dimension; set explicitly |
| `EMBEDDING_SEND_DIM` | Upstream option to send an explicit dimension to supported providers |
| `RERANK_BINDING`, `RERANK_MODEL`, `RERANK_BINDING_HOST`, `RERANK_BINDING_API_KEY` | Optional upstream reranker; `null` disables the binding in the example |
| `LLM_TIMEOUT`, `EMBEDDING_TIMEOUT`, `RERANK_TIMEOUT` | Upstream provider timeouts; distinct from worker startup time |

All workspaces share these settings. The extension has no per-workspace provider-secret or embedding-model configuration. An equal dimension does not make two different embedding models compatible.

The sample model identifiers are configuration examples, not a promise of provider availability or a recommendation to replace an existing model. Use a reachable model supported by your selected binding.

## Concurrency budgets

| Variable | Scope |
| --- | --- |
| `MAX_ASYNC_LLM` | Base LLM concurrency |
| `EMBEDDING_FUNC_MAX_ASYNC` | Embedding concurrency |
| `MAX_ASYNC_RERANK` | Reranking concurrency |
| `<ROLE>_MAX_ASYNC_LLM` | Explicit upstream role-level LLM overrides, when configured |
| `MAX_PARALLEL_INSERT` | Ingestion pipeline parallelism |

The worker divides configured LLM, embedding, rerank, and role budgets by the maximum active workspace count using integer division. Each configured provider budget must be at least that count; otherwise startup is refused. For example, a budget of 8 with two workers gives each worker 4. An idle worker does not lend its share to another.

`MAX_PARALLEL_INSERT` is divided with a floor of one. These per-role bounds do not constitute a single global provider token, cost, or request-rate limit across distinct roles.

## Storage contract

The extension pins one coherent storage combination for the selected backend. In `LIGHTRAG_STORAGE_BACKEND=file`, it uses:

```dotenv
LIGHTRAG_KV_STORAGE=JsonKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=JsonDocStatusStorage
LIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage
LIGHTRAG_GRAPH_STORAGE=NetworkXStorage
```

In `LIGHTRAG_STORAGE_BACKEND=postgres`, it uses:

```dotenv
LIGHTRAG_KV_STORAGE=PGKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=PGDocStatusStorage
LIGHTRAG_VECTOR_STORAGE=PGVectorStorage
LIGHTRAG_GRAPH_STORAGE=PGTableGraphStorage
POSTGRES_VECTOR_INDEX_TYPE=VCHORDRQ
```

The worker validates the registered database, exact workspace mapping, embedding model, and dimension before starting. It overrides upstream workspace variables with the registered internal namespace. PostgreSQL is selected for the entire deployment; mixing file and database storage choices in one worker is unsupported. Other backend combinations are rejected.

| PostgreSQL variable | Meaning |
| --- | --- |
| `POSTGRES_HOST`, `POSTGRES_PORT` | Required host reachable from the container; port defaults to `5432` |
| `POSTGRES_USER`, `POSTGRES_PASSWORD` | Dedicated database role and private credential |
| `POSTGRES_DATABASE` | Required dedicated target database; no implicit fallback to `postgres` |
| `POSTGRES_SSL_MODE` | Upstream TLS mode: `disable`, `prefer`, `require`, `verify-ca`, or `verify-full` |
| `POSTGRES_SSL_CERT`, `POSTGRES_SSL_KEY`, `POSTGRES_SSL_ROOT_CERT`, `POSTGRES_SSL_CRL` | Optional upstream client/trust certificate paths, available inside the container |
| `POSTGRES_MAX_CONNECTIONS` | Per-worker pool maximum; configure explicitly, for example `4` |
| `POSTGRES_VECTOR_INDEX_TYPE` | Must be `VCHORDRQ` in this extension's PostgreSQL mode |
| `POSTGRES_VCHORDRQ_EPSILON` | Search/recall setting; `2.3` is the reference starting configuration |
| `POSTGRES_VCHORDRQ_PROBES` | Optional probe override; leave empty unless it matches your index partition hierarchy |
| `POSTGRES_VCHORDRQ_BUILD_OPTIONS` | Optional VectorChord index options; leave empty for the initial unpartitioned setup |

The gateway has an additional read-only metadata pool capped at two connections. It checks the target database identity and reads live document counts; discovery never creates tables or loads original JSON. The control root contains a `.postgres-workspace.json` registration under each exact public workspace ID. The offline importer publishes this file only after verification. Do not edit registrations or copy them between unrelated databases.

See [PostgreSQL migration](../guides/postgresql.md) for extension installation, connection networking, an explicit import, and backup requirements. A configuration change alone does not migrate a corpus.

## File processing

The example uses:

```dotenv
LIGHTRAG_PARSER=*:native-teP,*:legacy-R
CHUNK_P_SIZE=2000
CHUNK_P_OVERLAP_SIZE=100
```

Read [file processing](../guides/file-processing.md) before changing ingestion behavior. Upstream exposes additional chunk, parser, embedding, and role options; see its [pinned example configuration](https://github.com/HKUDS/LightRAG/blob/28ff1b05f2ac3f3e6fa14dd2cd33656579bd0c9c/env.example) rather than copying options from a moving branch.

## Authentication

`LIGHTRAG_API_KEY` configures upstream API-key authentication. `AUTH_ACCOUNTS`, `TOKEN_SECRET`, and related upstream JWT settings configure account authentication. Use fresh secrets and preserve them privately with the deployment.

The gateway does not implement per-workspace permissions or independently authenticate discovery and UI-shell responses. A worker can be cold-started before upstream rejects an unauthenticated API request. A shared authenticated instance is still one trust domain. For untrusted exposure, put authentication in front of the **whole origin**, including discovery and static UI, and retain network controls. Keep the localhost bind unless you have deliberately designed this access boundary.

## Apply changes

```sh
docker compose up -d --force-recreate
```

Finish or stop active ingestion before recreation. Changing providers, embeddings, or parser settings can affect newly written data; a container restart does not migrate existing indexes. Back up before changes that alter the data contract.
