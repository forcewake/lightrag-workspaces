# Use the browser interface

Choose a workspace before uploading files or asking a question. Its identifier appears in the page URL, so a bookmark or a second tab can keep a different corpus selected.

## Two entry points

| Interface | Address | Purpose |
| --- | --- | --- |
| Admin | `/webui/` | Documents, Knowledge Graph, and Retrieval |
| Retrieval-focused | `/workspace/` | Query interface |
| Explicit corpus | `/workspaces/research/webui/` | Open the named corpus directly |

Unscoped UI URLs redirect to the default workspace. The selector is available in both interfaces. Selecting a different workspace reloads the page under its scoped URL; it does not mutate a server-wide “current workspace.” Two tabs can therefore work with different corpora at the same time.

## What follows the selection

- Documents, graph requests, queries, and uploads target the selected worker.
- Browser conversations, query parameters, additional prompts, and graph browsing state are stored under workspace-specific browser keys.
- Admin and retrieval-focused interfaces keep their own conversation histories.
- Returning to a workspace restores its browser state on that browser profile.

Browser storage is not a server backup or a synchronized account history. Another browser profile or cleared site storage starts a fresh local conversation.

## Opening a cold workspace

The selector and page shell can render before the corpus is loaded. The first API request starts a worker and waits for its health check. A large index can take time; the scoped browser initialization allows up to 240 seconds.

If a worker cannot start, a slot is busy, or memory admission refuses a load, the interface displays a retryable error while preserving the selector. A `401` or `403` remains an authentication problem and follows upstream authentication behavior.

## Protected legacy workspaces

A workspace with old `pending` or processing-stage documents is marked read-only for changes. Search and graph inspection remain available when its metadata and model configuration are compatible. Uploads, deletion, queue processing, and other mutations are rejected with `409`.

This state prevents old work from being resumed accidentally. It is not a recovery tool. Inspect a backup and plan any queue recovery separately; do not edit status files casually to remove the protection.

## Where is the API tab?

Use the workspace's Swagger endpoint directly:

```text
http://localhost:9621/workspaces/research/docs
```

The extension scopes Swagger assets and its OpenAPI URL to that workspace. The upstream interface layout is version-specific; a separate “API” tab is not required for the API to be available. Use the served OpenAPI schema for exact request and response fields.
