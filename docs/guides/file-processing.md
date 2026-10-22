# Configure file processing

The extension routes files to the selected workspace. Parsing and indexing are provided by the pinned LightRAG 1.5.7 server; this project does not add a separate parser implementation.

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
