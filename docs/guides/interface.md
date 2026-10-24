# Use the browser interface

Choose a workspace before uploading files or asking a question. Its identifier appears in the page URL, so a bookmark or a second tab can keep a different corpus selected.

## Two entry points

| Interface | Address | Purpose |
| --- | --- | --- |
| Admin | `/webui/` | Documents, Knowledge Graph, and Retrieval |
| Retrieval-focused | `/workspace/` | Query interface |
| Explicit corpus | `/workspaces/research/webui/` | Open the named corpus directly |

Unscoped UI URLs redirect to the default workspace. The selector is available in both interfaces. Selecting a different workspace reloads the page under its scoped URL; it does not mutate a server-wide “current workspace.” Two tabs can therefore work with different corpora at the same time.

## Find a workspace in the selector

Open the workspace control in the header and start typing. Search matches display names, identifiers and tags without changing the selected workspace. With no search, the current workspace and browser favorites appear first; the remaining list scrolls within the dropdown. Counts help distinguish corpora, and a custom display name keeps its exact ID visible.

Use **Arrow Up/Down** to move through results, **Home/End** to reach the first or last result, and **Enter** to open the highlighted workspace. **Escape** closes the dropdown and returns focus to the workspace control. Clear an unmatched search to show the list again. Read-only corpora remain selectable for search.

The catalog button beside the selector opens **Your workspaces** for favoriting, creation and metadata editing. Those controls continue to use the same exact workspace IDs.

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

## Search and organize the catalog

Open the catalog next to the selector to search identifiers, display names, descriptions and tags. Favorite frequently used workspaces; favorites remain in this browser. The list refreshes from the server and shows whether a workspace is stopped, starting, ready, busy or failed. A stopped workspace is available to open and is not an error.

Edit a workspace's display name, description or tags without renaming its ID. The scoped URL, database namespace and existing browser conversation/settings keys stay unchanged. The exact ID remains visible for API use.

Use **New workspace** to create an empty corpus. Choose its permanent ID once, then provide optional display metadata. The interface tracks the durable creation operation and reconciles a lost response with the same idempotency key. Failed initialization never presents a half-created corpus as ready. Creating a PostgreSQL workspace requires a previously provisioned database; it does not run a migration or reconfigure existing corpora.

The management controls are capability-driven. When connected to an older gateway without these endpoints, selection remains available and unsupported creation/editing controls are disabled. Authentication remains shared across the installation.
