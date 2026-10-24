#!/usr/bin/env python3
"""Workspace-scoped LightRAG retrieval and explicit file upload; standard library only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_URL = "http://127.0.0.1:9621"
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
MODES = ("mix", "local", "global", "hybrid", "naive")
STATUSES = ("pending", "parsing", "analyzing", "processing", "preprocessed", "processed", "failed")
MAX_RESPONSE = 32 * 1024 * 1024


class ClientError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base_url: str, timeout: float = 300):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ClientError("Use an http(s) origin only, without credentials, path, query, or fragment.")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ClientError("Timeout must be a finite positive number.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "User-Agent": "lightrag-workspaces-client/0.3"}
        for variable, header, prefix in (
            ("LIGHTRAG_API_KEY", "X-API-Key", ""),
            ("LIGHTRAG_BEARER_TOKEN", "Authorization", "Bearer "),
        ):
            value = os.environ.get(variable)
            if value:
                if "\r" in value or "\n" in value:
                    raise ClientError(f"Invalid newline in {variable}.")
                self.headers[header] = prefix + value
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path: str, *, workspace: str | None = None, payload=None,
                raw_body=None, content_type=None, content_length=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ClientError("An API path must start with one slash.")
        headers = dict(self.headers)
        if workspace is not None:
            if not ID_PATTERN.fullmatch(workspace):
                raise ClientError("Invalid workspace ID; use an exact ID from workspaces.")
            path = "/workspaces/" + urllib.parse.quote(workspace, safe="") + path
        if raw_body is not None and (payload is not None or not content_type or content_length is None):
            raise ClientError("Raw uploads require a content type and length, without a JSON payload.")
        body = raw_body
        if raw_body is not None:
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(content_length)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers,
                                         method="GET" if body is None else "POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if workspace is not None:
                    identities = [response.headers.get(name) for name in
                                  ("LIGHTRAG-WORKSPACE", "X-LightRAG-Workspace")]
                    if identities != [workspace, workspace]:
                        raise ClientError("Workspace identity headers missing or mismatched; result discarded.")
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ClientError("Response exceeds 32 MiB; reduce page size or retrieval limits.")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ClientError("Expected a JSON object; inspect the running API schema.")
                return result
        except urllib.error.HTTPError as exc:
            hints = {
                400: "Check the workspace selector and request.",
                401: "Check the authorized API key or bearer token.",
                403: "Check the authorized API key or account permissions.",
                404: "Rediscover workspaces or inspect the scoped API schema.",
                409: "Workspace is protected or incompatible; inspect its status without changing it.",
                413: "Upload is too large for the server; inspect configured limits and prepare smaller logical documents.",
                422: "Request validation failed; inspect the scoped API schema.",
                429: "Rate limited; wait before considering a retry.",
                503: "Worker unavailable or busy; inspect discovery/status and allow cold-start time.",
            }
            hint = "Redirect refused; verify the configured origin." if 300 <= exc.code < 400 else hints.get(exc.code, "Inspect service health; no automatic retry was attempted.")
            exc.close()
            # Server error bodies can contain private provider details; do not echo them.
            raise ClientError(f"HTTP {exc.code}. {hint}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            raise ClientError("Connection failed or timed out; no automatic retry. Check LAN access and workspace status.") from None
        except (ValueError, UnicodeError):
            raise ClientError("Invalid JSON response; inspect the running API schema.") from None

    def workspaces(self):
        result = self.request("/workspaces")
        entries = result.get("workspaces")
        if not isinstance(entries, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in entries):
            raise ClientError("Unexpected workspace inventory format.")
        ids = [item["id"] for item in entries]
        if len(set(ids)) != len(ids):
            raise ClientError("Duplicate workspace identifiers; inventory is ambiguous.")
        return result

    def select(self, workspace: str):
        if not ID_PATTERN.fullmatch(workspace):
            raise ClientError("Invalid workspace ID; use an exact ID from workspaces.")
        entry = next((item for item in self.workspaces()["workspaces"] if item["id"] == workspace), None)
        if entry is None:
            raise ClientError("Unknown workspace; use an exact ID from workspaces. No query was sent.")
        if entry.get("available") is not True:
            raise ClientError("Workspace unavailable; no query was sent.")
        return entry


def bounded_integer(minimum, maximum):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("Expected an integer.") from None
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"Expected {minimum}..{maximum}.")
        return number
    return parse


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--base-url", default=os.environ.get("LIGHTRAG_URL", DEFAULT_URL), help="Origin only; also LIGHTRAG_URL.")
    root.add_argument("--timeout", type=float, default=300, help="Socket timeout in seconds (default: 300).")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("workspaces", help="Discover existing corpora without starting workers.")
    for name, help_text in (
        ("status", "Counts and concise pipeline state."),
        ("documents", "One page of document metadata."),
        ("track", "Status of an existing ingestion track ID."),
        ("schema", "Live OpenAPI schema, optionally filtered to a route."),
        ("formats", "Live file extensions and parser engines."),
        ("upload", "Upload ONE authorized file and persist its receipt; never retries."),
        ("retrieve", "Structured evidence without final-answer generation."),
        ("answer", "LightRAG-generated answer with supporting chunks."),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--workspace", required=True, help="Exact existing workspace ID; never implicit default.")
        if name == "documents":
            sub.add_argument("--page", type=bounded_integer(1, 1000000), default=1)
            sub.add_argument("--page-size", type=bounded_integer(10, 200), default=20)
            sub.add_argument("--status", choices=STATUSES, action="append", help="Repeat to select several statuses.")
        if name == "track":
            sub.add_argument("--track-id", required=True)
        if name == "schema":
            sub.add_argument("--route", help="Example: /query/data; includes referenced schemas.")
        if name == "upload":
            sub.add_argument("--file", required=True, help="Prepared file; original is read, never modified.")
            sub.add_argument("--receipt-file", required=True, help="New private JSON receipt path; parent must exist.")
            sub.add_argument("--expected-sha256", help="Optional prepared-file digest; a mismatch prevents upload.")
        if name in ("retrieve", "answer"):
            query = sub.add_mutually_exclusive_group(required=True)
            query.add_argument("--query")
            query.add_argument("--query-file", help="UTF-8 question file; - reads stdin.")
            sub.add_argument("--mode", choices=MODES, default="mix")
            sub.add_argument("--top-k", type=bounded_integer(1, 1000), default=40)
            sub.add_argument("--chunk-top-k", type=bounded_integer(1, 1000), default=20)
            sub.add_argument("--max-total-tokens", type=bounded_integer(1, 1000000), default=16000)
            sub.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=None, help="Omit to keep server default.")
    return root


def read_query(args):
    text = args.query
    if args.query_file is not None:
        try:
            if args.query_file == "-":
                text = sys.stdin.read(65537)
            else:
                with Path(args.query_file).open(encoding="utf-8") as handle:
                    text = handle.read(65537)
        except (OSError, UnicodeError):
            raise ClientError("Could not read a UTF-8 query file.") from None
    if len(text) > 65536:
        raise ClientError("Query input exceeds 65536 characters; it was not sent or truncated.")
    text = text.strip()
    if not text or len(text) > 65536:
        raise ClientError("Query must contain 1..65536 characters after trimming; RAG also enforces a minimum English-equivalent length of 3.")
    return text


def filtered_schema(schema, route):
    if not route:
        return schema
    paths = schema.get("paths", {})
    if route not in paths:
        raise ClientError("Route not found in the live schema.")
    selected = {"openapi": schema.get("openapi"), "info": schema.get("info"),
                "servers": schema.get("servers"), "paths": {route: paths[route]}, "components": {"schemas": {}}}
    if "securitySchemes" in schema.get("components", {}):
        selected["components"]["securitySchemes"] = schema["components"]["securitySchemes"]
    if "security" in schema:
        selected["security"] = schema["security"]
    known = schema.get("components", {}).get("schemas", {})

    def collect(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[-1]
                if name in known and name not in selected["components"]["schemas"]:
                    selected["components"]["schemas"][name] = known[name]
                    collect(known[name])
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(paths[route])
    return selected


def sync_directory(path):
    """Persist the receipt directory entry on the supported POSIX hosts."""
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def upload_file(client, entry, args):
    """Preflight, snapshot exact bytes, record submission, and send once."""
    if entry.get("readOnly") is not False or entry.get("preserveBacklog"):
        raise ClientError("Workspace does not allow ingestion; no file was uploaded.")
    source = Path(args.file)
    if not source.is_file():
        raise ClientError("Upload source must be an existing regular file.")
    name = source.name
    if name != name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name) or ":" in name or "\\" in name:
        raise ClientError("Unsafe upload filename; prepare a separate file with a stable, valid basename.")
    if args.expected_sha256 and not re.fullmatch(r"[A-Fa-f0-9]{64}", args.expected_sha256):
        raise ClientError("Expected SHA-256 must contain 64 hexadecimal characters.")
    receipt_path = Path(args.receipt_file)
    if receipt_path.exists() or receipt_path.is_symlink() or not receipt_path.parent.is_dir():
        raise ClientError("Receipt path must be new and its parent directory must exist.")
    scope = {"workspace": args.workspace}
    formats = client.request("/documents/supported_file_types", **scope)
    extensions = formats.get("supported_extensions")
    if not isinstance(extensions, list) or not all(isinstance(v, str) for v in extensions):
        raise ClientError("Unexpected supported-file-types response; no file was uploaded.")
    if source.suffix.lower() not in {v.lower() for v in extensions}:
        raise ClientError("Extension is not advertised by this workspace. Review extraction/conversion before upload.")
    # Advisory status-store check. The server also checks canonical parser-hint
    # names and pending input files atomically; a subsequent 409 must be respected.
    for page in range(1, 10001):
        listing = client.request("/documents/paginated", payload={"page": page, "page_size": 200,
                                 "sort_field": "file_path", "sort_direction": "asc"}, **scope)
        docs, pagination = listing.get("documents"), listing.get("pagination", {})
        if not isinstance(docs, list) or not isinstance(pagination, dict) or type(pagination.get("has_next")) is not bool:
            raise ClientError("Incomplete document inventory; no file was uploaded.")
        for doc in docs:
            if not isinstance(doc, dict) or not isinstance(doc.get("file_path"), str):
                raise ClientError("Invalid document inventory; no file was uploaded.")
            if doc["file_path"].replace("\\", "/").rsplit("/", 1)[-1] == name:
                raise ClientError("A document with this filename already exists; reconcile its status/content before uploading.")
        if not pagination["has_next"]:
            break
        if not docs or page == 10000:
            raise ClientError("Could not finish the document inventory; no file was uploaded.")

    boundary = "lightrag-skill-" + uuid.uuid4().hex
    escaped_name = name.replace('"', '\\"')
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{escaped_name}"\r\n'
              'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8")
    digest, size = hashlib.sha256(), 0
    try:
        with tempfile.TemporaryFile(mode="w+b") as spool:
            spool.write(prefix)
            with source.open("rb") as handle:
                before = os.fstat(handle.fileno())
                while block := handle.read(1024 * 1024):
                    digest.update(block)
                    size += len(block)
                    spool.write(block)
                after = os.fstat(handle.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ClientError("Source changed while preparing upload; no file was uploaded.")
            if not size:
                raise ClientError("Empty file; no file was uploaded.")
            sha256 = digest.hexdigest()
            if args.expected_sha256 and sha256 != args.expected_sha256.lower():
                raise ClientError("Source digest differs from preparation manifest; no file was uploaded.")
            spool.write(f"\r\n--{boundary}--\r\n".encode("ascii"))
            content_length = spool.tell()
            spool.seek(0)
            receipt = {"workspace": args.workspace, "origin": client.base_url, "operation": "upload",
                       "source": {"name": name, "sha256": sha256, "bytes": size},
                       "outcome": "prepared", "indexed": False,
                       "updated_at": datetime.now(timezone.utc).isoformat()}
            # Reserve an unused destination before the POST. Refuse existing receipts.
            descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(receipt, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(receipt_path.parent)

            def save_receipt():
                receipt["updated_at"] = datetime.now(timezone.utc).isoformat()
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=receipt_path.parent,
                                                     prefix=".lightrag-receipt-", delete=False) as handle:
                        temporary = Path(handle.name)
                        json.dump(receipt, handle, ensure_ascii=False, indent=2)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, receipt_path)
                    sync_directory(receipt_path.parent)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)

            receipt["outcome"] = "submitting"
            save_receipt()
            try:
                response = client.request("/documents/upload", raw_body=spool, content_length=content_length,
                                          content_type=f"multipart/form-data; boundary={boundary}", **scope)
            except (ClientError, KeyboardInterrupt) as exc:
                receipt.update(outcome="unknown", error=str(exc) if isinstance(exc, ClientError) else "Client interrupted")
                save_receipt()
                raise ClientError("Upload outcome needs reconciliation; inspect the saved receipt and document status before retrying. " + receipt["error"]) from None
            receipt["result"] = response
            track_id = response.get("track_id")
            receipt["outcome"] = "accepted" if response.get("status") in ("success", "partial_success") and isinstance(track_id, str) and track_id else "server_response"
            save_receipt()
            return {**receipt, "receipt_file": str(receipt_path.resolve()), "indexed": False}
    except OSError:
        raise ClientError("Upload file or receipt I/O failed. If a receipt exists, inspect it and server status before retrying.") from None


def run(args):
    # Validate question input before any network request.
    query = read_query(args) if args.command in ("retrieve", "answer") else None
    if args.command == "track" and (not args.track_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", args.track_id)):
        raise ClientError("Invalid track ID; use the exact returned ingestion identifier.")
    client = Client(args.base_url, args.timeout)
    if args.command == "workspaces":
        return client.workspaces()
    entry = client.select(args.workspace)
    scope = {"workspace": args.workspace}
    if args.command == "upload":
        return upload_file(client, entry, args)
    if args.command == "formats":
        result = client.request("/documents/supported_file_types", **scope)
    elif args.command == "status":
        counts = client.request("/documents/status_counts", **scope)
        pipeline = client.request("/documents/pipeline_status", **scope)
        keep = ("busy", "job_start", "docs", "batchs", "cur_batch", "recovery_required", "recovery_kind")
        result = {"inventory": entry, "status_counts": counts,
                  "pipeline": {key: pipeline[key] for key in keep if key in pipeline}}
    elif args.command == "documents":
        payload = {"page": args.page, "page_size": args.page_size, "sort_field": "file_path", "sort_direction": "asc"}
        if args.status:
            payload["status_filters"] = args.status
        result = client.request("/documents/paginated", payload=payload, **scope)
    elif args.command == "track":
        result = client.request("/documents/track_status/" + urllib.parse.quote(args.track_id, safe=""), **scope)
    elif args.command == "schema":
        result = filtered_schema(client.request("/openapi.json", **scope), args.route)
    else:
        payload = {"query": query, "mode": args.mode, "top_k": args.top_k,
                   "chunk_top_k": args.chunk_top_k, "max_total_tokens": args.max_total_tokens,
                   "include_references": True}
        if args.rerank is not None:
            payload["enable_rerank"] = args.rerank
        if args.command == "answer":
            payload.update(include_chunk_content=True, stream=False)
        result = client.request("/query/data" if args.command == "retrieve" else "/query", payload=payload, **scope)
    return {"workspace": args.workspace, "operation": args.command, "result": result}


def main():
    try:
        print(json.dumps(run(parser().parse_args()), ensure_ascii=False, indent=2))
        return 0
    except (ClientError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ClientError) else "Invalid client configuration."
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 1
    except KeyboardInterrupt:
        print('{"error":"Interrupted; the server may still finish an in-flight query."}', file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
