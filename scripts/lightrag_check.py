#!/usr/bin/env python3
"""Run source-grounded retrieval fixtures against one explicitly pinned workspace."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata
import urllib.parse

from lightrag_client import Client, ClientError, ID_PATTERN, MODES, sync_directory

MAX_SUITE_BYTES = 2 * 1024 * 1024
DERIVED_CHUNK_ID = re.compile(r"-mm-(drawing|table|equation)-[0-9]+\Z")
LIMITATION = ("Checks retrieval of the supplied literal facts from the expected sources. "
              "A pass does not establish exhaustive coverage, factual correctness of the sources, "
              "or correctness of a generated answer. Known multimodal description chunk IDs are excluded; "
              "other chunks are not thereby proven verbatim. /query/data can still invoke model services and caches.")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ClientError("Duplicate JSON keys in suite; no requests sent.")
        result[key] = value
    return result


def exact_keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ClientError("Invalid suite structure or unknown field; consult docs/guides/quality.md.")


def text_value(value, maximum, label):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ClientError(f"{label} must be a nonempty string of at most {maximum} characters.")
    return value


def normalized(value):
    return unicodedata.normalize("NFC", value).casefold()


def load_suite(path):
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_SUITE_BYTES + 1)
        if len(raw) > MAX_SUITE_BYTES:
            raise ClientError("Suite exceeds 2 MiB; no requests sent.")
        suite = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, ValueError):
        raise ClientError("Could not read a valid UTF-8 JSON suite.") from None
    exact_keys(suite, ("schema_version", "origin", "workspace", "cases"), ("retrieval",))
    if type(suite["schema_version"]) is not int or suite["schema_version"] != 1:
        raise ClientError("Unsupported suite schema_version; expected 1.")
    text_value(suite["origin"], 2048, "Origin")
    text_value(suite["workspace"], 128, "Workspace")
    if not ID_PATTERN.fullmatch(suite["workspace"]):
        raise ClientError("Invalid workspace ID in suite.")
    cases = suite["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ClientError("Suite must contain 1..100 cases.")
    case_ids = set()
    for case in cases:
        exact_keys(case, ("id", "question", "expected_sources"))
        text_value(case["id"], 128, "Case ID")
        if case["id"] in case_ids:
            raise ClientError("Case IDs must be unique.")
        case_ids.add(case["id"])
        text_value(case["question"], 65536, "Question")
        sources = case["expected_sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 100:
            raise ClientError("Each case needs 1..100 expected sources.")
        basenames = set()
        for source in sources:
            exact_keys(source, ("basename", "required_terms"))
            basename = text_value(source["basename"], 1024, "Source basename")
            if basename in (".", "..") or basename != basename.strip() or any(c in basename for c in ("/", "\\", "\x00", "\n", "\r")):
                raise ClientError("Expected sources must use literal basenames, without directories or URLs.")
            # Filename matching stays case sensitive, like the workspace filesystem.
            key = unicodedata.normalize("NFC", basename)
            if key in basenames:
                raise ClientError("Expected source basenames must be unique within a case.")
            basenames.add(key)
            terms = source["required_terms"]
            if not isinstance(terms, list) or not 1 <= len(terms) <= 100:
                raise ClientError("Each expected source needs 1..100 required literal terms.")
            for term in terms:
                text_value(term, 4096, "Required term")
            if len({normalized(term) for term in terms}) != len(terms):
                raise ClientError("Required terms must be distinct after Unicode normalization and case folding.")
    options = suite.get("retrieval", {})
    exact_keys(options, (), ("mode", "top_k", "chunk_top_k", "max_total_tokens", "enable_rerank"))
    if "mode" in options and options["mode"] not in MODES:
        raise ClientError("Invalid retrieval mode in suite.")
    for name, limit in (("top_k", 1000), ("chunk_top_k", 1000), ("max_total_tokens", 1000000)):
        if name in options and (type(options[name]) is not int or not 1 <= options[name] <= limit):
            raise ClientError(f"{name} must be an integer in 1..{limit}.")
    if "enable_rerank" in options and type(options["enable_rerank"]) is not bool:
        raise ClientError("enable_rerank must be a JSON boolean.")
    return suite, hashlib.sha256(raw).hexdigest()


def reference_id(value):
    if type(value) not in (str, int) or str(value) == "":
        raise ClientError("Retrieval evidence contains a missing or invalid reference_id.")
    return str(value)


def source_basename(path):
    # Keep source references as data: never fetch URLs or open returned paths.
    if path.startswith(("http://", "https://")):
        try:
            path = urllib.parse.unquote(urllib.parse.urlsplit(path).path)
        except ValueError:
            raise ClientError("Malformed source URL in retrieval evidence.") from None
    return unicodedata.normalize("NFC", path.replace("\\", "/").rsplit("/", 1)[-1])


def evaluate(case, result):
    """Check referenced chunks, excluding known generated multimodal descriptions."""
    checked = {"id": case["id"], "question": case["question"], "passed": False,
               "failures": [], "sources": [], "evidence": {"chunks": [], "references": []}}
    failures = checked["failures"]
    if result.get("status") != "success":
        failures.append({"type": "api_status", "detail": "Retrieval did not return status=success."})
        return checked
    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("chunks"), list) or not isinstance(data.get("references"), list):
        failures.append({"type": "evidence_contract", "detail": "Missing chunks/references lists."})
        return checked
    try:
        refs = {}
        for ref in data["references"]:
            if not isinstance(ref, dict):
                raise ClientError("Malformed reference object.")
            rid = reference_id(ref.get("reference_id"))
            path = ref.get("file_path")
            if not isinstance(path, str) or not path:
                raise ClientError("Reference has no file_path.")
            source_basename(path)
            if rid in refs:
                raise ClientError("Duplicate reference_id makes citations ambiguous.")
            refs[rid] = path
            checked["evidence"]["references"].append({"reference_id": rid, "file_path": path})
        for chunk in data["chunks"]:
            if not isinstance(chunk, dict) or not isinstance(chunk.get("content"), str):
                raise ClientError("Malformed source chunk or missing content.")
            rid = reference_id(chunk.get("reference_id"))
            if rid not in refs:
                raise ClientError("Chunk reference_id does not resolve to a returned reference.")
            path = refs[rid]
            if chunk.get("file_path") is not None and chunk["file_path"] != path:
                raise ClientError("Chunk file_path disagrees with its reference.")
            cid = chunk.get("chunk_id")
            if not isinstance(cid, str) or not cid:
                raise ClientError("Source chunk has no chunk_id.")
            checked["evidence"]["chunks"].append({"chunk_id": cid, "reference_id": rid,
                                                   "file_path": path, "content": chunk["content"],
                                                   "derived": DERIVED_CHUNK_ID.search(cid) is not None})
    except ClientError as exc:
        # Discard ambiguous evidence instead of using a partial mapping to pass.
        checked["evidence"] = {"chunks": [], "references": []}
        failures.append({"type": "evidence_contract", "detail": str(exc)})
        return checked
    chunks = checked["evidence"]["chunks"]
    if not chunks:
        failures.append({"type": "empty_chunks", "detail": "No source chunks returned."})
    for expected in case["expected_sources"]:
        basename = unicodedata.normalize("NFC", expected["basename"])
        source_chunks = [chunk for chunk in chunks if source_basename(chunk["file_path"]) == basename]
        candidates = [chunk for chunk in source_chunks if not chunk["derived"]]
        paths = sorted({chunk["file_path"] for chunk in source_chunks})
        source = {"basename": expected["basename"], "paths": paths, "passed": False,
                  "terms": [], "chunk_ids": [chunk["chunk_id"] for chunk in candidates],
                  "excluded_derived_chunk_ids": [chunk["chunk_id"] for chunk in source_chunks if chunk["derived"]]}
        checked["sources"].append(source)
        if not candidates:
            failures.append({"type": "missing_source", "source": expected["basename"], "detail": "No eligible cited chunks from expected source; known generated descriptions are excluded."})
        if len(paths) > 1:
            failures.append({"type": "ambiguous_source", "source": expected["basename"], "detail": "Several returned paths share this basename; use uniquely named fixtures."})
        for term in expected["required_terms"]:
            matches = [chunk for chunk in candidates if normalized(term) in normalized(chunk["content"])]
            source["terms"].append({"term": term, "found": bool(matches),
                                    "citations": [{"chunk_id": chunk["chunk_id"], "reference_id": chunk["reference_id"],
                                                   "file_path": chunk["file_path"]} for chunk in matches]})
            if not matches:
                failures.append({"type": "missing_term", "source": expected["basename"], "term": term,
                                 "detail": "Literal term absent from this source's eligible retrieved chunks."})
        source["passed"] = len(paths) == 1 and all(term["found"] for term in source["terms"])
    checked["passed"] = not failures
    return checked


def write_report(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".lightrag-check-", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run(args, *, expected_suite_sha256=None, expected_origin=None, expected_workspace=None):
    suite, digest = load_suite(args.suite)
    expected_suite_sha256 = expected_suite_sha256 or getattr(args, "expected_suite_sha256", None)
    expected_origin = expected_origin or getattr(args, "expected_origin", None)
    expected_workspace = expected_workspace or getattr(args, "expected_workspace", None)
    if ((expected_suite_sha256 is not None and digest != expected_suite_sha256)
            or (expected_origin is not None and suite["origin"] != expected_origin)
            or (expected_workspace is not None and suite["workspace"] != expected_workspace)):
        raise ClientError("Quality suite changed after validation; no requests sent.")
    # The suite pins the origin. LIGHTRAG_URL cannot silently redirect a regression run.
    client = Client(suite["origin"], args.timeout)
    path = Path(args.report).absolute()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    except OSError:
        raise ClientError("Report must be a new path in an existing writable directory; no requests sent.") from None
    options = {"mode": "mix", "top_k": 40, "chunk_top_k": 20, "max_total_tokens": 16000,
               "include_references": True, **suite.get("retrieval", {})}
    report = {"schema_version": 1, "operation": "retrieval_check", "origin": client.base_url,
              "workspace": suite["workspace"], "suite_sha256": digest,
              "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
              "retrieval": options, "limitation": LIMITATION, "cases": []}
    start = time.monotonic()
    interrupted = False
    try:
        write_report(path, report)
        client.select(suite["workspace"])
        for case in suite["cases"]:
            case_start = time.monotonic()
            try:
                result = client.request("/query/data", workspace=suite["workspace"], payload={**options, "query": case["question"]})
                checked = evaluate(case, result)
            except ClientError as exc:
                checked = {"id": case["id"], "question": case["question"], "passed": False,
                           "failures": [{"type": "request_error", "detail": str(exc)}],
                           "sources": [], "evidence": {"chunks": [], "references": []}}
                report["stopped_reason"] = "Request failed; remaining cases were not sent and no request was retried."
            checked["elapsed_seconds"] = round(time.monotonic() - case_start, 6)
            report["cases"].append(checked)
            write_report(path, report)
            if "stopped_reason" in report:
                break
    except ClientError as exc:
        report["run_error"] = str(exc)
    except KeyboardInterrupt:
        interrupted = True
        report["run_error"] = "Interrupted; an in-flight retrieval may still finish on the server."
    completed = {case["id"] for case in report["cases"]}
    report["not_run"] = [case["id"] for case in suite["cases"] if case["id"] not in completed]
    passed = sum(case["passed"] for case in report["cases"])
    failed = len(report["cases"]) - passed
    report["summary"] = {"total": len(suite["cases"]), "completed": len(report["cases"]),
                         "passed": passed, "failed": failed, "not_run": len(report["not_run"])}
    report["status"] = "interrupted" if interrupted else ("passed" if not failed and not report["not_run"] and "run_error" not in report else "failed")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["elapsed_seconds"] = round(time.monotonic() - start, 6)
    write_report(path, report)
    summary = {key: report[key] for key in ("status", "origin", "workspace", "summary", "elapsed_seconds", "limitation")}
    summary["report_file"] = str(path)
    return summary, (130 if interrupted else (0 if report["status"] == "passed" else 1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, help="UTF-8 JSON suite with pinned origin/workspace and source expectations.")
    parser.add_argument("--report", required=True, help="New private JSON report; parent directory must already exist.")
    parser.add_argument("--timeout", type=float, default=300, help="Socket timeout in seconds (default: 300).")
    parser.add_argument("--expected-suite-sha256", help="Reject a suite changed since the caller validated it.")
    parser.add_argument("--expected-origin", help="Require this exact validated suite origin before any request.")
    parser.add_argument("--expected-workspace", help="Require this exact validated workspace before any request.")
    try:
        result, exit_code = run(parser.parse_args())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code
    except (ClientError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ClientError) else "Could not persist the private report or read the suite; inspect local paths."
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
