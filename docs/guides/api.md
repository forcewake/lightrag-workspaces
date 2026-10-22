# Select a workspace through the API

Every routed request selects one existing corpus. Use a URL prefix or a request header; the default is used only when neither is present.

## Discover existing workspaces

```sh
curl http://localhost:9621/workspaces
```

The response contains `defaultWorkspace` and a `workspaces` array with identifiers, document counts, availability, and read-only metadata. Discovery does not initialize an index. An empty or misspelled identifier does not create one.

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
