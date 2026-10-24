#!/usr/bin/env python3
"""Plan, resume and inspect version-preserving LightRAG imports (stdlib only).

Plan snapshots a reviewed preparation directory. Run uploads new/changed snapshots;
status makes one bounded tracking pass, and report reads saved local evidence.
Accepted or uncertain submissions are never automatically sent again.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from types import SimpleNamespace
import urllib.parse
import uuid

from lightrag_client import Client, ClientError, DEFAULT_URL, ID_PATTERN, STATUSES, sync_directory, upload_file

SCHEMA = 1
MAX_JSON = 16 * 1024 * 1024
MAX_FILES = 10000
PHASES = {"pending", "submitting", "preflight_failed", "accepted", "processing", "processed", "failed", "duplicate", "unknown", "inherited_pending"}
TRACK = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def regular_file(path):
    """Refuse symlinks and non-regular files, including a symlink parent."""
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ClientError("Symlink paths are not accepted for import evidence.")
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ClientError("Expected an existing regular evidence file.")
    return path


def read_json(path):
    path = regular_file(path)
    with path.open("rb") as handle:
        raw = handle.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ClientError("Import evidence exceeds the 16 MiB limit.")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ClientError("Invalid JSON import evidence.") from None
    if not isinstance(value, dict):
        raise ClientError("Import evidence must be a JSON object.")
    return value, raw


def atomic_json(path, value, *, new=False):
    path = Path(path)
    if path.is_symlink() or (new and path.exists()):
        raise ClientError("Refusing to replace existing or symlink evidence.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".batch-", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(encoded(value))
            handle.flush()
            os.fsync(handle.fileno())
        if new:
            # A hard link publishes the complete file without overwriting a racer.
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def locked(root):
    root = Path(root)
    if root.is_symlink() or root.resolve() != root.absolute():
        raise ClientError("Import directory must have no symlink components.")
    descriptor = os.open(root / ".batch.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ClientError("This import is already running; wait for its existing process.") from None
        yield
    finally:
        os.close(descriptor)


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ClientError("Invalid relative source path in manifest.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in path.parts) or str(path) != value:
        raise ClientError("Source paths must be canonical and stay inside their preparation directory.")
    return path


def file_id(relative):
    return digest_bytes(relative.encode("utf-8"))


def upload_name(relative, sha256):
    source = PurePosixPath(relative)
    suffix = source.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
        raise ClientError("Each prepared file needs a simple supported extension.")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", source.stem).strip("-")[:48] or "document"
    return f"{stem}--p{file_id(relative)[:16]}--v{sha256}{suffix}"


def load_bundle(manifest_path):
    path = regular_file(manifest_path)
    if path.name != "manifest.json":
        raise ClientError("Use the generated manifest.json without renaming it.")
    root = path.parent
    manifest, raw = read_json(path)
    state, _ = read_json(root / "state.json")
    if manifest.get("schema_version") != SCHEMA or state.get("schema_version") != SCHEMA:
        raise ClientError("Unsupported import manifest/state schema.")
    if state.get("manifest_sha256") != digest_bytes(raw) or state.get("run_id") != manifest.get("run_id"):
        raise ClientError("Manifest/state binding changed; no upload is safe.")
    if not isinstance(manifest.get("workspace"), str) or not ID_PATTERN.fullmatch(manifest["workspace"]):
        raise ClientError("Invalid manifest workspace.")
    if not isinstance(manifest.get("origin"), str) or Client(manifest["origin"]).base_url != manifest["origin"]:
        raise ClientError("Invalid manifest origin.")
    files = manifest.get("files")
    if not isinstance(files, list) or not 0 <= len(files) <= MAX_FILES:
        raise ClientError("Invalid or oversized manifest file inventory.")
    identifiers = set()
    for item in files:
        if not isinstance(item, dict):
            raise ClientError("Invalid manifest file entry.")
        relative_path(item.get("relative_path"))
        fid, sha = item.get("id"), item.get("sha256")
        if fid != file_id(item["relative_path"]) or fid in identifiers or not isinstance(sha, str) or not DIGEST.fullmatch(sha):
            raise ClientError("Invalid or duplicate manifest identity/digest.")
        identifiers.add(fid)
        if type(item.get("bytes")) is not int or item["bytes"] <= 0:
            raise ClientError("Invalid manifest file size.")
        expected_name = upload_name(item["relative_path"], sha)
        if item.get("upload_name") != expected_name or item.get("staged_path") != "staging/" + expected_name:
            raise ClientError("Staging path or immutable version filename changed.")
        if item.get("change") not in ("new", "changed", "unchanged"):
            raise ClientError("Invalid incremental classification.")
    if not isinstance(state.get("files"), dict) or set(state["files"]) != identifiers:
        raise ClientError("State does not match the manifest inventory.")
    for fid, item in state["files"].items():
        if not isinstance(item, dict) or item.get("phase") not in PHASES or type(item.get("indexed")) is not bool:
            raise ClientError("Invalid per-file import state.")
        if item.get("track_id") is not None and (not isinstance(item["track_id"], str) or not TRACK.fullmatch(item["track_id"])):
            raise ClientError("Invalid saved track ID.")
        if item.get("receipt") not in (None, f"receipts/{fid}.json"):
            raise ClientError("Receipt path escapes this run or has changed.")
    return root, manifest, state


def save_state(root, state):
    state["updated_at"] = now()
    atomic_json(root / "state.json", state)


def snapshot(source, destination):
    source = regular_file(source)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as handle, destination.open("xb") as target:
        os.chmod(destination, 0o600)
        before = os.fstat(handle.fileno())
        while block := handle.read(1024 * 1024):
            digest.update(block)
            size += len(block)
            target.write(block)
        after = os.fstat(handle.fileno())
        target.flush()
        os.fsync(target.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
        raise ClientError("Source changed while preparing its snapshot; make a new plan.")
    if size == 0:
        raise ClientError("Empty source files are not ingestible; remove them from the reviewed preparation directory.")
    return digest.hexdigest(), size


def plan(args):
    origin = Client(args.base_url or os.environ.get("LIGHTRAG_URL", DEFAULT_URL), args.timeout).base_url
    if not ID_PATTERN.fullmatch(args.workspace):
        raise ClientError("Invalid workspace ID.")
    source = Path(os.path.abspath(args.source_dir))
    output = Path(os.path.abspath(args.output_dir))
    if not source.is_dir() or source.resolve() != source or output.parent.resolve() != output.parent:
        raise ClientError("Source and output directories must be real local paths without symlinks.")
    if output == source or source in output.parents:
        raise ClientError("Keep the import output outside the source directory.")
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ClientError("Output must be a new directory with an existing parent.")
    previous = previous_state = None
    previous_pointer = None
    if args.previous:
        prior_path = regular_file(args.previous)
        with locked(prior_path.parent):
            _, previous, previous_state = load_bundle(prior_path)
            if (previous["origin"], previous["workspace"], previous.get("source_dir")) != (origin, args.workspace, str(source)):
                raise ClientError("Previous manifest must have the same origin, workspace and source directory.")
            previous_pointer = {"manifest": str(prior_path), "sha256": previous_state["manifest_sha256"], "run_id": previous["run_id"]}
    paths = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ClientError("Preparation directory contains a symlink; review a regular-file-only snapshot.")
        if path.is_file():
            relative_path(path.relative_to(source).as_posix())
            paths.append(path)
            if len(paths) > MAX_FILES:
                raise ClientError("A batch supports at most 10000 files; split the reviewed corpus.")
        elif not path.is_dir():
            raise ClientError("Preparation directory contains a non-regular entry.")
    prior_files = {item["relative_path"]: item for item in previous["files"]} if previous else {}
    output.mkdir(mode=0o700)
    (output / "staging").mkdir(mode=0o700)
    (output / "receipts").mkdir(mode=0o700)
    files, states = [], {}
    for index, path in enumerate(paths):
        relative = path.relative_to(source).as_posix()
        temporary = output / "staging" / f".snapshot-{index}"
        sha, size = snapshot(path, temporary)
        name = upload_name(relative, sha)
        destination = output / "staging" / name
        os.link(temporary, destination)
        temporary.unlink()
        os.chmod(destination, 0o400)
        prior = prior_files.get(relative)
        change = "new" if prior is None else ("unchanged" if prior["sha256"] == sha else "changed")
        item = {"id": file_id(relative), "relative_path": relative, "sha256": sha, "bytes": size,
                "upload_name": name, "staged_path": "staging/" + name, "change": change}
        if prior:
            item["previous_version"] = {"sha256": prior["sha256"], "upload_name": prior["upload_name"]}
        files.append(item)
        if change == "unchanged":
            # Preserve evidence and unresolved outcomes, but never submit an unchanged
            # file through a second run even if the earlier run has not submitted it.
            prior_state = previous_state["files"][item["id"]]
            inherited = {key: prior_state[key] for key in ("phase", "indexed", "track_id", "documents", "checked_at", "error", "refresh_error") if key in prior_state}
            if inherited["phase"] in ("pending", "preflight_failed", "inherited_pending"):
                inherited["phase"] = "inherited_pending"
            if inherited["phase"] == "submitting":
                inherited["phase"] = "unknown"
            inherited["inherited_from"] = previous_pointer
            states[item["id"]] = inherited
        else:
            states[item["id"]] = {"phase": "pending", "indexed": False}
    current_paths = {item["relative_path"] for item in files}
    removed = [{"relative_path": item["relative_path"], "sha256": item["sha256"], "upload_name": item["upload_name"], "action": "retain_on_server"}
               for item in prior_files.values() if item["relative_path"] not in current_paths]
    manifest = {"schema_version": SCHEMA, "run_id": str(uuid.uuid4()), "created_at": now(),
                "origin": origin, "workspace": args.workspace, "source_dir": str(source),
                "previous": previous_pointer, "version_policy": "append_only_preserve_existing", "files": files, "removed": removed}
    raw = encoded(manifest)
    state = {"schema_version": SCHEMA, "run_id": manifest["run_id"], "manifest_sha256": digest_bytes(raw),
             "created_at": now(), "updated_at": now(), "files": states}
    sync_directory(output / "staging")
    atomic_json(output / "manifest.json", manifest, new=True)
    atomic_json(output / "state.json", state, new=True)
    sync_directory(output.parent)
    return report_data(output, manifest, state)


def check_snapshot(root, item):
    path = regular_file(root / item["staged_path"])
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    if (digest.hexdigest(), size) != (item["sha256"], item["bytes"]):
        raise ClientError("Immutable staged bytes changed; no upload was sent.")
    return path


def adopt_receipt(root, manifest, item, record):
    path = root / "receipts" / (item["id"] + ".json")
    if not path.exists() and not path.is_symlink():
        if record.get("receipt"):
            raise ClientError("A saved receipt is missing; refusing to resend its file.")
        if record["phase"] == "submitting":
            record.update(phase="unknown", indexed=False, error="Interrupted submission without a readable receipt; reconcile manually.")
        return False
    receipt, raw = read_json(path)
    if record.get("receipt_sha256") and record["receipt_sha256"] != digest_bytes(raw):
        raise ClientError("Saved upload receipt changed; no upload is safe.")
    expected = {"name": item["upload_name"], "sha256": item["sha256"], "bytes": item["bytes"]}
    if (receipt.get("origin"), receipt.get("workspace"), receipt.get("operation"), receipt.get("source")) != (manifest["origin"], manifest["workspace"], "upload", expected):
        raise ClientError("Receipt identity/digest does not match this import; refusing to reuse it.")
    record.update(receipt="receipts/" + path.name, receipt_sha256=digest_bytes(raw))
    response = receipt.get("result", {})
    track = response.get("track_id") if isinstance(response, dict) else None
    if receipt.get("outcome") == "accepted" and isinstance(track, str) and TRACK.fullmatch(track):
        if record["phase"] not in ("processed", "processing", "failed", "duplicate"):
            record.update(phase="accepted", indexed=False)
        record["track_id"] = track
        record.pop("error", None)
    elif record["phase"] not in ("processed", "processing", "failed", "duplicate"):
        record.update(phase="unknown", indexed=False,
                      error="Submission has an uncertain or unaccepted result; inspect receipt and reconcile without resending.")
    return True


def scoped_client(args, manifest):
    explicit = args.base_url or os.environ.get("LIGHTRAG_URL")
    if explicit is not None and Client(explicit, args.timeout).base_url != manifest["origin"]:
        raise ClientError("Configured origin differs from this manifest; refusing to redirect the import.")
    return Client(manifest["origin"], args.timeout)


def submit(args):
    manifest_path = regular_file(args.manifest)
    with locked(manifest_path.parent):
        root, manifest, state = load_bundle(manifest_path)
        client = scoped_client(args, manifest)
        for item in manifest["files"]:
            record = state["files"][item["id"]]
            if not record.get("inherited_from"):
                adopt_receipt(root, manifest, item, record)
        save_state(root, state)
        candidates = [item for item in manifest["files"] if item["change"] in ("new", "changed") and state["files"][item["id"]]["phase"] in ("pending", "preflight_failed")]
        candidates = candidates[:args.max_files] if args.max_files is not None else candidates
        # Validate every selected snapshot before contacting the server.
        for item in candidates:
            check_snapshot(root, item)
        if not candidates:
            return report_data(root, manifest, state)
        entry = client.select(manifest["workspace"])
        for item in candidates:
            record = state["files"][item["id"]]
            record.update(phase="submitting", indexed=False, attempted_at=now())
            record.pop("error", None)
            save_state(root, state)
            receipt = root / "receipts" / (item["id"] + ".json")
            arguments = SimpleNamespace(file=str(root / item["staged_path"]), receipt_file=str(receipt),
                                        workspace=manifest["workspace"], expected_sha256=item["sha256"])
            try:
                upload_file(client, entry, arguments)
            except (ClientError, KeyboardInterrupt) as exc:
                if not adopt_receipt(root, manifest, item, record):
                    # upload_file cannot POST before durably reserving its receipt.
                    record.update(phase="preflight_failed", indexed=False,
                                  error=str(exc) if isinstance(exc, ClientError) else "Interrupted before submission.")
                save_state(root, state)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                if record["phase"] == "unknown":
                    break
            else:
                adopt_receipt(root, manifest, item, record)
                save_state(root, state)
        return report_data(root, manifest, state)


def refresh_track(client, manifest, item, record):
    track = record.get("track_id")
    if not track:
        return
    response = client.request("/documents/track_status/" + urllib.parse.quote(track, safe=""), workspace=manifest["workspace"])
    documents, total = response.get("documents"), response.get("total_count")
    if not isinstance(documents, list) or type(total) is not int or total != len(documents) or response.get("track_id", track) != track:
        raise ClientError("Incomplete or mismatched track inventory; completion is unverified.")
    compact = []
    for doc in documents:
        if not isinstance(doc, dict) or doc.get("status") not in STATUSES or not isinstance(doc.get("file_path"), str):
            raise ClientError("Unexpected document tracking metadata.")
        if doc["file_path"].replace("\\", "/").rsplit("/", 1)[-1] != item["upload_name"]:
            raise ClientError("Tracking returned another filename; completion is unverified.")
        metadata = doc.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ClientError("Unexpected duplicate metadata.")
        selected = {key: doc[key] for key in ("id", "status", "file_path", "error_msg", "chunks_count") if key in doc}
        for key in ("is_duplicate", "original_doc_id", "original_track_id"):
            if key in metadata:
                selected[key] = metadata[key]
        compact.append(selected)
    record.update(documents=compact, checked_at=now())
    record.pop("refresh_error", None)
    if not compact:
        # No docs can mean indexing has not made the queued receipt visible yet.
        record.update(phase="accepted", indexed=False)
    elif all(doc["status"] == "processed" for doc in compact):
        record.update(phase="processed", indexed=True)
    elif any(doc["status"] == "failed" for doc in compact):
        duplicate = all(doc["status"] == "failed" and doc.get("is_duplicate") is True for doc in compact)
        record.update(phase="duplicate" if duplicate else "failed", indexed=False)
    else:
        record.update(phase="processing", indexed=False)


def refresh(args):
    manifest_path = regular_file(args.manifest)
    with locked(manifest_path.parent):
        root, manifest, state = load_bundle(manifest_path)
        client = scoped_client(args, manifest)
        # The same explicit identity is checked at discovery and every track reply.
        client.select(manifest["workspace"])
        for item in manifest["files"]:
            record = state["files"][item["id"]]
            if not record.get("inherited_from"):
                adopt_receipt(root, manifest, item, record)
            try:
                refresh_track(client, manifest, item, record)
            except ClientError as exc:
                record["refresh_error"] = str(exc)
            save_state(root, state)
        return report_data(root, manifest, state)


def report_data(root, manifest, state):
    files = []
    counts = Counter()
    changes = Counter()
    for item in manifest["files"]:
        record = state["files"][item["id"]]
        counts[record["phase"]] += 1
        changes[item["change"]] += 1
        files.append({"relative_path": item["relative_path"], "upload_name": item["upload_name"], "sha256": item["sha256"],
                      "change": item["change"], **record})
    changes["removed"] = len(manifest["removed"])
    return {"schema_version": SCHEMA, "run_id": manifest["run_id"], "manifest": str(root / "manifest.json"),
            "origin": manifest["origin"], "workspace": manifest["workspace"], "updated_at": state["updated_at"],
            "version_policy": manifest["version_policy"], "counts": dict(counts), "changes": dict(changes),
            "complete": all(item["phase"] == "processed" and not item.get("refresh_error") for item in files),
            "quality_checks": state.get("quality_checks", []),
            "files": files, "removed": manifest["removed"],
            "note": "Processed confirms server status only; run evidence checks separately. Removed and earlier versions remain on the server."}


def saved_report(args):
    path = regular_file(args.manifest)
    with locked(path.parent):
        root, manifest, state = load_bundle(path)
        scoped_client(args, manifest)  # Validate supplied origin even in read-only mode.
        return report_data(root, manifest, state)


def verify(args):
    """Refresh ingestion status, then run a source-grounded quality gate once."""
    from lightrag_check import load_suite, run as run_checks

    path = regular_file(args.manifest)
    suite, suite_digest = load_suite(args.suite)
    with locked(path.parent):
        root, manifest, state = load_bundle(path)
        scoped_client(args, manifest)
        if (Client(suite["origin"]).base_url, suite["workspace"]) != (manifest["origin"], manifest["workspace"]):
            raise ClientError("Quality suite must target this import's sealed origin and workspace; no requests sent.")
        names = {item["upload_name"] for item in manifest["files"]}
        tested = {source["basename"] for case in suite["cases"] for source in case["expected_sources"]}
        if not names or not names.intersection(tested):
            raise ClientError("Quality suite must check at least one exact versioned upload basename from this import.")
        # Validate the output before any server request, without reserving it twice.
        report_path = Path(args.report).absolute()
        if report_path.exists() or report_path.is_symlink() or not report_path.parent.is_dir():
            raise ClientError("Quality report must be a new path in an existing directory.")
    # Refresh never uploads. Accepted, ambiguous and failed files are never retried.
    current = refresh(args)
    if not current["complete"]:
        raise ClientError("Import is not fully processed or tracking failed; no retrieval checks sent. Inspect status first.")
    with locked(path.parent):
        root, manifest, state = load_bundle(path)
        if not report_data(root, manifest, state)["complete"]:
            raise ClientError("Import status changed; inspect before running the quality gate.")
        summary, code = run_checks(args, expected_suite_sha256=suite_digest,
                                   expected_origin=suite["origin"], expected_workspace=suite["workspace"])
        report_raw = regular_file(args.report).read_bytes()
        receipt = {"checked_at": now(), "suite_sha256": suite_digest,
                   "report_sha256": digest_bytes(report_raw), "report_file": str(report_path),
                   "status": summary["status"], "summary": summary["summary"]}
        state["quality_checks"] = [*state.get("quality_checks", []), receipt][-100:]
        save_state(root, state)
        return {"import": report_data(root, manifest, state), "quality": summary}, code


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base-url", help="Origin only; plan defaults to LIGHTRAG_URL/default, existing runs use their sealed origin.")
    result.add_argument("--timeout", type=float, default=300, help="Per-request socket timeout; no implicit polling or retries (default 300 seconds).")
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("plan", help="Locally snapshot a reviewed source directory into a new private run.")
    create.add_argument("--workspace", required=True)
    create.add_argument("--source-dir", required=True)
    create.add_argument("--output-dir", required=True)
    create.add_argument("--previous", help="Prior manifest from the SAME origin, workspace and source directory.")
    for name, help_text in (("run", "Upload pending new/changed files once; rerun the same manifest to resume."),
                            ("status", "Refresh each known track once; never submits files."),
                            ("report", "Read the saved report without network requests.")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--manifest", required=True)
        if name == "run":
            command.add_argument("--max-files", type=int, help="Submit at most this many files, then checkpoint and return.")
    checks = commands.add_parser("verify", help="Refresh processing status, then check cited source facts; no uploads or automatic retries.")
    checks.add_argument("--manifest", required=True)
    checks.add_argument("--suite", required=True)
    checks.add_argument("--report", required=True, help="New private quality report path.")
    return result


def main():
    try:
        args = parser().parse_args()
        if getattr(args, "max_files", None) is not None and args.max_files <= 0:
            raise ClientError("max-files must be positive.")
        if args.command == "verify":
            value, code = verify(args)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return code
        value = {"plan": plan, "run": submit, "status": refresh, "report": saved_report}[args.command](args)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        # Status and report are observations. A run with unresolved failed submissions
        # still prints its full durable report, while signalling attention to callers.
        return 2 if args.command == "run" and any(value["counts"].get(key) for key in ("unknown", "preflight_failed", "failed", "duplicate")) else 0
    except (ClientError, OSError, ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, ClientError) else "Import file/configuration I/O failed; inspect existing state before resuming."
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('{"error":"Interrupted. Resume the same manifest; never start a replacement run to retry a submission."}', file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
