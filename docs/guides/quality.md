# Source-grounded retrieval checks

Use a small, reviewed set of questions and known source facts to catch retrieval regressions after an import or an authorized upgrade. Choose facts by reading the prepared source and, when needed, its original. Do not derive the expected facts from the answer being tested.

These checks call `/query/data` in one explicit workspace. They do not call `/query` or ask a model to grade itself. Structured retrieval can still use the keyword LLM, embeddings, reranker and query caches. Run cases sequentially; do not fan them out across all workspaces.

## Prepare a suite

Store the suite with the private import run. Use the exact upload basenames from its manifest, including any version suffixes. Each basename must identify one source unambiguously. Pin both the origin and workspace; the helper deliberately ignores `LIGHTRAG_URL` for this run. Review those two values before running a suite received from elsewhere.

```json
{
  "schema_version": 1,
  "origin": "http://127.0.0.1:9621",
  "workspace": "your-authorized-workspace",
  "retrieval": {
    "mode": "naive",
    "top_k": 5,
    "chunk_top_k": 5,
    "max_total_tokens": 6000,
    "enable_rerank": false
  },
  "cases": [
    {
      "id": "project-owner",
      "question": "Who owns the fictional Aurora project and what is its acceptance code?",
      "expected_sources": [
        {
          "basename": "aurora-project-v1.md",
          "required_terms": ["Owner: Mira Chen", "Acceptance code: AURORA-731"]
        }
      ]
    },
    {
      "id": "table-units",
      "question": "What is the planned processing capacity of the fictional Aurora project?",
      "expected_sources": [
        {
          "basename": "aurora-project-v1.md",
          "required_terms": ["Planned capacity", "requests per minute", "240"]
        }
      ]
    }
  ]
}
```

The example contains synthetic facts; replace them with reviewed expectations before checking a real corpus. `naive` tests chunk retrieval directly. Use `mix` or another supported mode in a separate suite when that is the actual agent workflow; passing `naive` does not establish the quality of graph-assisted retrieval.

`retrieval` is optional. Defaults are `mode: "mix"`, `top_k: 40`, `chunk_top_k: 20`, and `max_total_tokens: 16000`. Omit `enable_rerank` to use the server's default. Unknown fields are rejected to catch misspellings. A suite can contain 1–100 cases; each case needs a unique ID, a nonempty question, and at least one expected source with at least one literal term.

Choose full phrases with their meaning, units and qualifiers where possible. A check for `240` alone could pass on an unrelated number in that source. Include coverage for Cyrillic or other source languages, tables with headers and units, source locators, and explicit uncertainty where relevant. For versioned imports, include both preserved historical evidence and the newly added version. Do not quietly change the expected answers until a failing suite passes.

## Run and inspect

```bash
python3 scripts/lightrag_check.py \
  --suite /absolute/import-run/retrieval-suite.json \
  --report /absolute/import-run/quality-after-import.json
```

The report path must be new and its parent must exist. Reports use private file permissions, preserve source chunks, and are checkpointed after every completed question. Never put reports containing private corpus text in a public repository. Optional `--timeout` is a socket timeout, default 300 seconds; it is not a total suite deadline. Existing API-key/bearer environment variables work through the shared client, and no credentials are written to reports.

- Exit **0**: every case passed the supplied checks.
- Exit **1**: at least one check failed or the service could not complete the suite. Inspect `summary`, `not_run`, per-case `failures`, and any `run_error` or `stopped_reason`.
- Exit **2**: invalid suite, invalid report path, or local persistence error. Inspect the concise error; do not assume a report exists or a run completed.
- Exit **130**: interrupted run. Completed cases remain in the report; an in-flight retrieval may still finish on the server.

The helper discovers the workspace first and checks both workspace identity headers on every query response. A transport, HTTP or identity error stops further cases without retries. An ordinary failed source/term check is recorded and the remaining cases continue. Repeating a suite creates a new report and sends new queries; it does not resume a report or modify its prior evidence.

## What a pass establishes

Each case requires `status: "success"`, valid source chunks and references, and all expected sources and terms. Reference IDs are local to one response. Each chunk must resolve to a returned reference, and its optional `file_path` must agree with that reference. Missing IDs, duplicate reference IDs and ambiguous same-basename paths fail the case. Returned paths and URLs are treated as data and are never opened.

Native multimodal parsing can return both extracted source text and generated descriptions under the same source reference. The helper recognizes chunk IDs ending in `-mm-drawing-<digits>`, `-mm-table-<digits>`, or `-mm-equation-<digits>` as known generated descriptions. These remain in the report with `derived: true` but cannot satisfy terms. Each source lists their IDs in `excluded_derived_chunk_ids`; eligible chunk IDs are listed separately in `chunk_ids`. This is a heuristic based on the known parser ID convention. `derived: false` means that convention was not recognized, not that the text has been proved verbatim or faithful. Compare important quotations and numbers with the original source.

Filename matching is case sensitive with Unicode NFC normalization. Terms use NFC normalization and case folding, then literal substring matching; no user-supplied regular expressions or shell evaluation are supported. Different terms may occur in different eligible retrieved chunks of the same expected source. The report records which chunk and reference satisfied each term. A term in another file, a known generated multimodal description, a graph entity summary, a generated answer, or an unresolved chunk cannot satisfy the source check.

A pass establishes that these particular source facts were retrieved under the recorded settings. It does not establish that every document was indexed faithfully, that numerical statements are true, that adjacent phrases express the intended relationship, or that a generated answer is correct. Check processing status separately, inspect the original documents for extraction fidelity, and review full returned chunks for meaning. For answer correctness, use an independently reviewed answer rubric and source comparison; this helper does not grade generated answers.

Compare reports using the same suite hash, source versions, workspace and retrieval settings. Record changed corpus or runtime versions with the import report. After a failure, inspect the missing source, processing state, source text and retrieval limits before deciding whether to change a fixture or rerun. Never hide a regression by deleting a failing case or switching the suite to another workspace.

## Gate an import

The repository includes the same version-preserving batch client as the agent workflow. It snapshots prepared files, binds each upload to an origin/workspace, uses content-versioned names and records uncertain submissions without resending them. Original and earlier versions remain intact.

```sh
python3 scripts/lightrag_batch.py --base-url http://127.0.0.1:9621 plan \
  --workspace research --source-dir /private/prepared --output-dir /private/import-001
python3 scripts/lightrag_batch.py run --manifest /private/import-001/manifest.json
python3 scripts/lightrag_batch.py status --manifest /private/import-001/manifest.json
```

Read `upload_name` from that manifest when writing a reviewed suite. After all files are processed:

```sh
python3 scripts/lightrag_batch.py verify \
  --manifest /private/import-001/manifest.json \
  --suite /private/import-001/suite.json \
  --report /private/import-001/quality.json
```

`verify` refreshes every tracked upload, requires the complete import to be processed, checks the suite's exact origin/workspace and at least one current versioned source, then runs retrieval once. A wrong target fails before any network request. Pending, failed or ambiguous imports cannot pass this gate. Quality checks are recorded with suite/report hashes in the durable import state; a later failure retains the earlier evidence. These receipts describe the corpus at their recorded time, not a perpetual guarantee.

For a later source version, create a new plan with `--previous /private/import-001/manifest.json`. Unchanged processed files are inherited. Changed files receive new versioned names. Files removed from the preparation directory remain on the server. Resume the same manifest after interruption; a new plan is not a retry mechanism for uncertain uploads.

## Gate an upgrade

Supply reviewed private suites to the [operations CLI](operations.md). The upgrade must verify the suite origin before swapping containers, then run cases sequentially after readiness and routing checks. Keep the reports beside the private upgrade receipt. A failed gate leaves the old image available for explicit application rollback and reports the candidate as unverified. Never roll the database back merely because retrieval quality failed.

An isolated restore without provider access proves restoration, schema, counts and API routing. It cannot establish embedding/retrieval quality; the receipt records that boundary. The synthetic example in `examples/quality/` is suitable for an explicitly created test workspace. It contains no private documents or provider credentials.
