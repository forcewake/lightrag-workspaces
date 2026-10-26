# Select a workspace through the API

Every routed request selects one existing corpus. Use a URL prefix or a request header; the default is used only when neither is present.

## Discover existing workspaces

```sh
curl http://localhost:9621/workspaces
```

The response contains `defaultWorkspace` and a `workspaces` array with identifiers, document counts, availability, and read-only metadata. Entries also contain `displayName`, `description`, `tags`, lifecycle `state`, `startupElapsedSeconds`, `errorCode`, `retryable`, and `retryAfterSeconds`. The response includes a `capabilities` map. Discovery does not initialize an index. An empty or misspelled identifier does not create one.

## Select by URL

```sh
curl http://localhost:9621/workspaces/research/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"What does the handbook say?","mode":"mix","include_references":true}'
```

## Select by header

```sh
curl http://localhost:9621/query \
  -H 'Content-Type: application/json' \
  -H 'LIGHTRAG-WORKSPACE: research' \
  -d '{"query":"What does the handbook say?","mode":"mix","include_references":true}'
```

Names are exact and case-sensitive. If both selectors are present, they must agree. A workspace is a request-routing attribute, not a server-wide switch. Do not put it in the JSON body and expect it to change routing.

Without either selector, `LIGHTRAG_DEFAULT_WORKSPACE` applies. Successful routed responses identify the workspace in both `LIGHTRAG-WORKSPACE` and `X-LightRAG-Workspace` response headers.

## Authentication

Upstream authentication is forwarded to the selected worker. For an API-key configuration, include:

```sh
curl http://localhost:9621/workspaces/research/health \
  -H "X-API-Key: ${LIGHTRAG_API_KEY}"
```

Set `LIGHTRAG_API_KEY` in your local shell to the same private value configured on the server. Do not paste a real key into documentation or an issue. For account authentication, use upstream `/login` and bearer-token behavior. The gateway's discovery list and UI bootstrap expose workspace identifiers, counts, and state before upstream authentication, and an API request may start a worker before upstream rejects its credentials. Put authentication in front of the whole origin for untrusted exposure; see the repository security policy.

### Credentials for the Python helpers

The client, batch, and check scripts can read `~/.config/lightrag/credentials.json` automatically. Store a JSON object with exactly two string fields: `origin` (your HTTP(S) origin, such as `http://localhost:9621`) and `api_key` (your private LightRAG key). The directory must have mode `0700`; the regular file must have mode `0600`. Both must be owned by the current user. Symlinks anywhere in the path and hard-linked files are rejected.

The stored key is sent as `X-API-Key` only when the requested scheme, hostname, and effective port match its origin. A different `--base-url` receives no stored key; redirects are refused. Missing files leave unauthenticated behavior unchanged; malformed or unsafe files stop the helper before it sends a request. Errors never include credential contents.

An explicit `LIGHTRAG_API_KEY` or `LIGHTRAG_BEARER_TOKEN` takes precedence over the file and retains its existing behavior for the selected destination. Keep the credential file private and outside the repository; never pass its contents as command arguments or print them in diagnostics.

## Upload and wait for indexing

```sh
curl http://localhost:9621/workspaces/research/documents/upload \
  -F 'file=@./notes.md'
```

This accepts a file for asynchronous processing. Wait for successful indexing before asking content-dependent questions. Consult the selected workspace's document status endpoints in Swagger; the extension forwards the upstream contract rather than replacing it with a second document API.

File uploads go to the selected workspace's input directory. Supported formats and parsing behavior depend on the pinned upstream parser and any configured external parser service. See [file processing](file-processing.md).

## Stream the answer

```sh
curl --no-buffer http://localhost:9621/workspaces/research/query/stream \
  -H 'Content-Type: application/json' \
  -d '{"query":"Summarize the review process.","mode":"mix","include_references":true,"stream":true}'
```

The upstream endpoint emits **newline-delimited JSON (NDJSON)**, not Server-Sent Events. Parse each complete line as JSON; network chunks can split or combine lines. With references enabled, the default stream begins with a reference record, followed by response chunks. Handle an error record or an interrupted connection as an incomplete response.

The gateway holds the selected worker lease until the response is closed, including cancellation cleanup. A client disconnect does not make it safe to evict a worker while upstream work is still active. Reverse proxies must permit the cold-start wait and pass streaming responses without buffering them into a single final body.

## Swagger and the exact upstream schema

```text
/workspaces/research/docs
/workspaces/research/openapi.json
```

The OpenAPI schema belongs to the pinned worker. Use it for exact upload, document deletion, graph, Ollama-compatible, and query fields. The gateway rewrites the Swagger page's schema and asset links so “Try it out” remains within the selected workspace.

A scoped `/health` request opens the corpus when cold. `/workspaces` checks gateway discovery without paying that load cost.

## Errors and recovery

| Status | Meaning | Next action |
| --- | --- | --- |
| `400` | Invalid or repeated selector, invalid path, or URL/header disagreement | Send one exact identifier; make both selectors agree |
| `401` / `403` | Upstream authentication rejected the request | Check key, token, and account configuration |
| `404` | Unknown workspace or missing route | Discover `/workspaces`; explicitly initialize a new corpus if intended |
| `409` | Incompatible metadata/dimensions, or protected old queue | Inspect metadata and original model configuration; do not bypass protection by editing statuses |
| `503` | Cold start failed/timed out, insufficient memory, or all worker slots busy | Inspect worker logs; finish current work and retry; reduce active workers or size memory appropriately |

A non-mutating query may write upstream response caches. Retrying an upload or deletion is not necessarily harmless: check its status before repeating it.

## Explicit creation and catalog metadata

Since 0.3, `POST /workspaces` creates an empty workspace. It uses the same shared API key or authenticated account token as the worker. A guest login token cannot replace an API key. Querying an unknown ID still returns `404` and creates nothing.

```sh
curl http://localhost:9621/workspaces \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: research-create-2026-0001' \
  -d '{"id":"research","displayName":"Research","description":"Reviewed source material","tags":["research"]}'
```

The response is a durable operation receipt (`202` while pending/running; `200` for an already completed same-key request). Poll `Location`, for example `/workspace-operations/<operation-id>`. States are `pending`, `running`, `succeeded`, or `failed`; failures include a stable `errorCode` and `retryable`. After a lost response or restart, repeat **the same key and the same body** to reconcile the existing operation. A changed body with the same key returns `409`. Another key cannot adopt or overwrite an occupied ID. Creation never deletes source data.

File mode creates new owned directories. PostgreSQL mode requires an already provisioned dedicated database and creates only an empty registered namespace and its three vector tables/indexes. Shared graph tables are validated without startup DDL. An interrupted creation remains absent from discovery until its database registration and manifest are validated and published together. Initial database provisioning/migration remains an explicit offline procedure.

```sh
curl -X PATCH http://localhost:9621/workspaces/research/metadata \
  -H 'Content-Type: application/json' \
  -d '{"displayName":"Research library","description":"Reviewed notes and papers","tags":["papers","notes"]}'
curl http://localhost:9621/workspaces/research/status
```

Only display metadata is editable. IDs, storage namespaces, model configuration, and browser history ownership remain fixed. Favorites are stored in the browser, separately from server metadata. Account/API-key headers are required when configured.

## Readiness, lifecycle, and maintenance

- `GET /livez`: the gateway process is alive; no database or worker startup. Used by the container healthcheck.
- `GET /readyz`: database identity/registrations and the default workspace are available, with no cold worker start. Returns `503` with a stable reason during maintenance or a database outage.
- `GET /workspaces/<id>/status`: exact workspace lifecycle (`stopped`, `starting`, `ready`, `busy`, `failed`) and counts. It does not start that worker.
- `POST /workspaces/<id>/prewarm`: explicitly open an existing workspace; no corpus creation or model query.

Gateway errors contain `code`, `detail`, and `retryable`; transient rejections also include `Retry-After`. These describe when a new attempt might succeed, not permission to blindly resend an uncertain upload/query. Creation is recoverable specifically through its idempotency key. Upstream request errors retain the upstream contract.

The [operations tool](operations.md) owns maintenance via an authenticated random token. Maintenance persists across process restarts, rejects new corpus traffic (including cache-writing queries), and waits for requests, pipelines and creation operations to drain. The catalog and UI shell remain available. Recovery uses the retained private owner receipt; it does not expire into unsafe concurrent writes automatically.

For source-grounded acceptance after an import or upgrade, use the [quality gate](quality.md).
