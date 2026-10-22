# Configure file processing

The extension routes files to the selected workspace. Parsing and indexing use the pinned LightRAG 1.5.7 server. A small compatibility hook preserves native Markdown table headers in the text passed to the upstream indexer.

## Search table column labels

Since extension **0.2.2**, native Markdown pipe-table headers are included before the sidecar writer computes document/block hashes and before the paragraph chunker counts tokens. Column labels therefore enter ordinary searchable chunks as literal source text. They also reach table analysis when it is enabled; search does not rely on an LLM repeating the labels in its description.

The structured `table_header` metadata is retained. Small tables and tables with no data rows keep their headers. When an oversized table is split by rows, the upstream chunker repeats the header in each slice within its token budget. A header or single row larger than that budget uses the upstream text-splitting fallback; its complete text can span several chunks. HTML tables retain their existing `<thead>`, `rowspan` and `colspan` representation.

The hook supports the exact pinned Markdown adapter only and refuses source/version drift. It changes future native parsing for both file and PostgreSQL workspaces. It does not alter the legacy parser or other document adapters.

### Existing indexed documents

Updating the application does **not** rewrite existing documents, chunks or embeddings. Existing table headers remain in their native sidecars until an explicit reindex operation includes them in searchable content.

Before repairing an existing corpus, back up its inputs, sidecars and affected stored rows, including vectors. Test on a disposable copy. A repair must update both searchable chunk text and its embedding, retain source/provenance and workspace ownership, and verify literal labels after retrieval. Changing metadata alone or appending text without recalculating the embedding does not repair vector search. This release does not run an automatic backfill at worker startup.

## Choose the parser deliberately

The example configuration uses the upstream parser declaration:

```dotenv
LIGHTRAG_PARSER=*:native-teP,*:legacy-R
CHUNK_P_SIZE=2000
CHUNK_P_OVERLAP_SIZE=100
```

These are upstream options. The native `P` path uses its paragraph-oriented chunk settings; other chunking paths have their own settings. Treat a parser or chunking change as a change to **future ingestion**, not a migration of documents already indexed.

Use the pinned [upstream example configuration](https://github.com/HKUDS/LightRAG/blob/28ff1b05f2ac3f3e6fa14dd2cd33656579bd0c9c/env.example) for the full parser grammar, supported file formats, and external parser configuration. Some document types or richer extraction require additional services or dependencies; an accepted filename does not prove extraction quality.

## Validate on a small representative sample

Create a test workspace. Import a document containing the content you need preserved: paragraphs, a table, headings, and any relevant non-Latin text. Check the document status, extracted chunks, reference filename, and an answer grounded in a known fact.

Upload acceptance only proves queue receipt. A processed status proves completion of that pipeline step, not correctness of extraction or answer quality. Inspect representative results before importing a large corpus.

## Keep inputs and indexes together

Each workspace has a separate input directory and saved corpus directory. Include both in backups. Explicit document deletion may remove source files or caches depending on the upstream request options. Review those options in the selected workspace's Swagger page.

Old documents may lack the recovery anchors expected by newer deletion or reprocessing paths. The startup compatibility layer does not manufacture missing anchors on disk. Test such operations on a copy before relying on them for a legacy corpus.

## Provider load

Indexing uses shared LLM and embedding configuration. The extension divides each configured provider concurrency budget across the maximum active worker count. An otherwise idle worker does not borrow another worker's share. This keeps the aggregate per-role concurrency bounded; it does not impose a provider-wide cost, token, or rate-limit policy across all roles.
