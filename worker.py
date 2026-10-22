#!/usr/bin/env python3
"""Run one existing LightRAG 1.5.7 workspace in its own process.

The public workspace name selects a directory; LightRAG's internal namespace
stays empty so legacy names (including hyphens) are never rewritten. Starting a
worker neither resumes ingestion nor performs compatibility writes. Normal
explicit API operations retain their upstream behavior.

Legacy graph/vector retrieval remains available without the auxiliary backfill.
Deleting or reprocessing old documents can still require recovery-anchor indexes
that this bootstrap deliberately does not manufacture during startup. Upstream
fails closed for those operations. A later controlled compatibility migration
must be backed up and verified separately. Ordinary future cache commits remain
enabled, including persistence of an in-memory legacy-cache conversion after a
query actually changes that cache.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import functools
import inspect
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterator

from postgres_backend import (
    POSTGRES_STORAGES, configure_postgres_workspace, install_postgres_vector_namespace, selected_backend,
    validate_postgres_readiness,
)
from table_headers import install_table_header_preservation


WORKSPACE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SUPPORTED_VERSION = "1.5.7"
DEFAULT_RUNTIME_ROOT = "/app/data/workspace-runtime"
SUPPORTED_NANO_VERSION = "0.0.4.3"


class _VectorJSONReader:
    """Bound input text to one metadata value or one 64 KiB base64 segment.

    CPython's json.load reads an entire file into one Unicode string. A single
    non-ASCII metadata character can double or quadruple that multi-GB string.
    The native format's enormous matrix string is therefore decoded separately.
    """

    def __init__(self, stream: Any, chunk_size: int = 65536):
        self.stream = stream
        self.chunk_size = chunk_size
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def more(self) -> bool:
        if self.eof:
            return False
        remaining = self.buffer[self.position:]
        chunk = self.stream.read(self.chunk_size)
        self.buffer = remaining + chunk
        self.position = 0
        if not chunk:
            self.eof = True
        return bool(chunk)

    def skip_space(self) -> None:
        while True:
            while self.position < len(self.buffer) and self.buffer[self.position] in " \t\r\n":
                self.position += 1
            if self.position < len(self.buffer) or not self.more():
                return

    def peek(self) -> str:
        self.skip_space()
        return self.buffer[self.position:self.position + 1]

    def expect(self, character: str) -> None:
        if self.peek() != character:
            raise ValueError("Malformed native vector JSON structure")
        self.position += 1

    def value(self) -> Any:
        self.skip_space()
        if self.peek() not in '"{[-0123456789tfnNI':
            raise ValueError("Malformed native vector metadata token")
        while True:
            if len(self.buffer) - self.position > 32 * 1024 * 1024:
                raise ValueError("Native vector metadata value exceeds its bounded limit")
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
            except json.JSONDecodeError as error:
                if self.more():
                    continue
                raise ValueError("Malformed native vector metadata") from error
            # A number can appear complete at the end of an input block even
            # though its remaining digits/exponent are in the following block.
            if end == len(self.buffer) and not self.eof:
                if self.more():
                    continue
                # more() compacted the buffer, so decode at its new position.
                continue
            if end < len(self.buffer) and self.buffer[end] not in " \t\r\n,:]}":
                if not self.eof and self.more():
                    continue
                raise ValueError("Malformed native vector metadata token")
            self.position = end
            return value

    def array(self) -> list[Any]:
        self.expect("[")
        values: list[Any] = []
        if self.peek() == "]":
            self.position += 1
            return values
        while True:
            values.append(self.value())
            next_character = self.peek()
            if next_character == "]":
                self.position += 1
                return values
            self.expect(",")
            if self.peek() == "]":
                raise ValueError("Native vector metadata array has a trailing comma")

    def native_base64_chunks(self) -> Iterator[str]:
        self.expect('"')
        while True:
            end = self.buffer.find('"', self.position)
            escape = self.buffer.find("\\", self.position)
            boundary = min(value for value in (len(self.buffer), end, escape) if value >= 0)
            piece = self.buffer[self.position:boundary]
            self.position = boundary
            if piece:
                yield piece
            if boundary == end:
                self.position += 1
                return
            if boundary == escape:
                # Native json.dump does not escape base64, but accept legal
                # escaped slashes/ASCII codepoints from older JSON formatters.
                while len(self.buffer) - self.position < 2:
                    if not self.more():
                        raise ValueError("Truncated vector matrix JSON escape")
                length = 6 if self.buffer[self.position + 1] == "u" else 2
                while len(self.buffer) - self.position < length:
                    if not self.more():
                        raise ValueError("Truncated vector matrix Unicode escape")
                encoded = self.buffer[self.position:self.position + length]
                try:
                    value = json.loads('"' + encoded + '"')
                except json.JSONDecodeError as error:
                    raise ValueError("Invalid vector matrix JSON escape") from error
                self.position += length
                yield value
                continue
            if not self.more():
                raise ValueError("Unterminated native vector matrix")


def load_nano_storage_bounded(file_name: str, *, chunk_size: int = 65536) -> Any:
    """Load the unchanged native JSON format into one writable matrix buffer.

    The vector buffer's virtual upper bound comes from the existing file size;
    only decoded matrix pages are touched/charged. No temporary file, mmap of
    the corpus, index rewrite, embedding call, or on-disk migration occurs.
    """
    import base64
    import numpy as np

    path = Path(file_name)
    if chunk_size < 1:
        raise ValueError("Vector JSON input chunk size must be positive")
    if not path.exists():
        return None
    result: dict[str, Any] = {}
    matrix_buffer = None
    decoded_bytes = 0
    with path.open(encoding="utf-8") as stream:
        reader = _VectorJSONReader(stream, chunk_size)
        reader.expect("{")
        while reader.peek() != "}":
            name = reader.value()
            if not isinstance(name, str) or name in result:
                raise ValueError("Invalid or duplicate native vector JSON key")
            reader.expect(":")
            if name == "matrix":
                matrix_buffer = np.empty((path.stat().st_size // 4 + 1) * 3, dtype=np.uint8)
                carry = b""
                padded = False
                for piece in reader.native_base64_chunks():
                    try:
                        encoded = carry + piece.encode("ascii")
                    except UnicodeEncodeError as error:
                        raise ValueError("Native vector matrix is not ASCII base64") from error
                    if padded and encoded:
                        raise ValueError("Native vector matrix has data after padding")
                    take = len(encoded) // 4 * 4
                    if take:
                        block = encoded[:take]
                        decoded = base64.b64decode(block, validate=True)
                        if decoded_bytes + len(decoded) > matrix_buffer.size:
                            raise ValueError("Native vector matrix exceeds its file-size bound")
                        matrix_buffer[decoded_bytes:decoded_bytes + len(decoded)] = np.frombuffer(decoded, dtype=np.uint8)
                        decoded_bytes += len(decoded)
                        padded = b"=" in block
                    carry = encoded[take:]
                if carry:
                    raise ValueError("Native vector matrix has incomplete base64")
                result[name] = None  # Marks presence without retaining encoded text.
            elif name == "data":
                result[name] = reader.array()
            else:
                result[name] = reader.value()
            if reader.peek() == "}":
                break
            reader.expect(",")
            if reader.peek() == "}":
                raise ValueError("Native vector JSON has a trailing comma")
        reader.expect("}")
        if reader.peek():
            raise ValueError("Native vector JSON has trailing data")
    dimension = result.get("embedding_dim")
    if type(dimension) is not int or dimension < 1 or matrix_buffer is None:
        raise ValueError("Native vector JSON lacks matrix/dimension metadata")
    if decoded_bytes % np.dtype(np.float32).itemsize:
        raise ValueError("Native vector matrix has an invalid byte length")
    result["matrix"] = matrix_buffer[:decoded_bytes].view(np.float32).reshape(-1, dimension)
    if not isinstance(result.get("data"), list) or len(result["data"]) != result["matrix"].shape[0]:
        raise ValueError("Native vector metadata/matrix row counts differ")
    return result


def normalize_nano_in_place(database: Any, block_bytes: int = 8 * 1024 * 1024) -> None:
    """Match upstream float32 row normalization with bounded temporaries."""
    import numpy as np

    if database.metric != "cosine":
        return
    storage = database._NanoVectorDB__storage
    matrix = storage["matrix"]
    if not matrix.flags.writeable:
        raise ValueError("Bounded native vector matrix must be writable")
    rows = max(1, block_bytes // max(1, matrix.shape[1] * matrix.dtype.itemsize))
    with np.errstate(divide="ignore", invalid="ignore"):
        for start in range(0, matrix.shape[0], rows):
            block = matrix[start:start + rows]
            np.divide(block, np.linalg.norm(block, axis=-1, keepdims=True), out=block)


def install_bounded_nano_loader() -> None:
    import importlib.metadata
    import nano_vectordb.dbs as nano

    if importlib.metadata.version("nano-vectordb") != SUPPORTED_NANO_VERSION:
        raise RuntimeError("Bounded vector loading requires the pinned nano-vectordb version")
    if not callable(getattr(nano, "load_storage", None)) or not callable(getattr(nano.NanoVectorDB, "pre_process", None)):
        raise RuntimeError("Unsupported native vector loading hooks")
    nano.load_storage = load_nano_storage_bounded
    nano.NanoVectorDB.pre_process = normalize_nano_in_place


def apply_provider_budgets(args: Any, divisor: int, roles: Any) -> dict[str, int]:
    """Partition the original concurrency budget across all allowed workers.

    This is deliberately a static partition: an idle worker does not lend its
    share to another process. It bounds the aggregate without touching provider
    identity or introducing a second request queue across processes.
    """
    if divisor < 1:
        raise ValueError("Worker budget divisor must be positive")
    attributes = ["max_async", "embedding_func_max_async", "rerank_max_async"]
    attributes.extend(f"{role.name}_llm_max_async" for role in roles)
    original: dict[str, int] = {}
    for name in attributes:
        value = getattr(args, name, None)
        if value is None:
            continue  # Upstream role inheritance uses the already capped base.
        if not isinstance(value, int) or value < divisor:
            raise ValueError(
                f"Configured {name} budget must be at least the active-workspace limit ({divisor}); "
                "reduce LIGHTRAG_MAX_ACTIVE_WORKSPACES instead of multiplying provider concurrency"
            )
        original[name] = value
    parallel = getattr(args, "max_parallel_insert", None)
    if parallel is not None and (not isinstance(parallel, int) or parallel < 1):
        raise ValueError("Configured max_parallel_insert must be positive")
    for name, value in original.items():
        setattr(args, name, value // divisor)
    if parallel is not None:
        # Pipeline parallelism does not itself add provider capacity; those
        # calls still pass through the stricter LLM/embedding/rerank budgets.
        args.max_parallel_insert = max(1, int(parallel) // divisor)
    return {name: getattr(args, name) for name in original}


def resolve_workspace(storage_root: Path, workspace_id: str) -> Path:
    """Resolve only an existing direct child; never manufacture a new corpus."""
    if not WORKSPACE_PATTERN.fullmatch(workspace_id):
        raise ValueError("Invalid workspace ID")
    root = storage_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Storage root must be an existing directory")
    candidate = root / workspace_id
    if candidate.is_symlink():
        raise ValueError("Workspace aliases are not allowed")
    selected = candidate.resolve(strict=True)
    if selected.parent != root or not selected.is_dir():
        raise ValueError("Workspace must be an existing directory inside storage root")
    return selected


def resolve_input_directory(input_root: Path, workspace_id: str) -> Path:
    """Keep uploads out of every other workspace, including through symlinks."""
    if not WORKSPACE_PATTERN.fullmatch(workspace_id):
        raise ValueError("Invalid workspace ID")
    root = input_root.resolve()
    candidate = root / workspace_id
    if candidate.is_symlink() or candidate.resolve().parent != root:
        raise ValueError("Workspace input directory must be a direct child")
    if candidate.exists() and not candidate.is_dir():
        raise ValueError("Workspace input path is not a directory")
    return candidate


@contextlib.contextmanager
def workspace_lock(runtime_root: Path, storage_root: Path, workspace_id: str) -> Iterator[None]:
    """Hold a Linux/macOS advisory lock outside the corpus for the whole worker."""
    if not WORKSPACE_PATTERN.fullmatch(workspace_id):
        raise ValueError("Invalid workspace ID")
    runtime = runtime_root.resolve()
    corpus_root = storage_root.resolve(strict=True)
    if runtime == corpus_root or corpus_root in runtime.parents:
        raise ValueError("Runtime files must be outside the storage root")
    lock_dir = runtime / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_dir / f"{workspace_id}.lock", flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"Workspace {workspace_id!r} already has a worker") from error
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        yield
    finally:
        os.close(fd)
        # Do not unlink: a contender may already hold an FD to this inode.


def flatten_legacy_cache(data: dict[str, Any], make_key: Callable[..., str]) -> dict[str, Any]:
    """Mirror upstream cache compatibility conversion, without writing a file."""
    if not data:
        return data
    first_key = next(iter(data))
    if ":" in first_key and len(first_key.split(":")) == 3:
        return data
    converted: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict) and all(
            isinstance(entry, dict) and "return" in entry for entry in value.values()
        ):
            for cache_hash, entry in value.items():
                converted[make_key(key, entry.get("cache_type", "extract"), cache_hash)] = entry
        else:
            converted[key] = value
    return converted


def install_startup_preservation(*, file_backends: bool = True) -> None:
    """Patch only 1.5.7 startup compatibility hooks, keeping API writes intact."""
    from lightrag import __version__
    from lightrag.storage_migrations import _StorageMigrationMixin
    from lightrag.utils import logger

    if __version__ != SUPPORTED_VERSION:
        raise RuntimeError(f"Worker requires LightRAG {SUPPORTED_VERSION}; found {__version__}")
    if not inspect.iscoroutinefunction(_StorageMigrationMixin.check_and_migrate_data):
        raise RuntimeError("Unsupported LightRAG migration hook")
    async def skip_startup_backfill(self: Any) -> None:
        logger.info("Workspace startup compatibility backfills are disabled; existing indexes retained")

    _StorageMigrationMixin.check_and_migrate_data = skip_startup_backfill
    if not file_backends:
        return

    from lightrag.kg import json_doc_status_impl, json_kv_impl, nano_vector_db_impl, networkx_impl
    from lightrag.utils import generate_cache_key

    if not inspect.iscoroutinefunction(json_kv_impl.JsonKVStorage._migrate_legacy_cache_structure):
        raise RuntimeError("Unsupported LightRAG cache migration hook")

    async def cache_compatibility_in_memory(self: Any, data: dict[str, Any]) -> dict[str, Any]:
        return flatten_legacy_cache(data, generate_cache_key)

    def retain_orphan_files(*args: Any, **kwargs: Any) -> None:
        # These aliases are used only by the file storage constructors.
        return None

    json_kv_impl.JsonKVStorage._migrate_legacy_cache_structure = cache_compatibility_in_memory
    for module in (json_kv_impl, json_doc_status_impl, nano_vector_db_impl, networkx_impl):
        if not callable(getattr(module, "reap_orphan_tmp_files", None)):
            raise RuntimeError(f"Unsupported startup cleanup hook in {module.__name__}")
        module.reap_orphan_tmp_files = retain_orphan_files


class WorkspaceIdentityMiddleware:
    """Attach canonical gateway identity without changing the storage namespace."""

    def __init__(self, app: Any, workspace_id: str):
        self.app = app
        self.workspace_id = workspace_id

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        async def identified_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() != b"x-lightrag-workspace"]
                headers.append((b"x-lightrag-workspace", self.workspace_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, identified_send)


def identify_app(app: Any, workspace_id: str) -> None:
    app.state.selected_workspace_id = workspace_id
    app.add_middleware(WorkspaceIdentityMiddleware, workspace_id=workspace_id)
    found_health = False
    for route in app.routes:
        if getattr(route, "path", None) != "/health" or not hasattr(route, "dependant"):
            continue
        original = route.dependant.call

        @functools.wraps(original)
        async def health_with_identity(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            result = await _original(*args, **kwargs)
            if isinstance(result, dict):
                result = dict(result)
                result["selected_workspace_id"] = workspace_id
                result["startup_compatibility_writes"] = False
            return result

        route.dependant.call = health_with_identity
        found_health = True
    if not found_health:
        raise RuntimeError("Unsupported LightRAG health endpoint")


def parse_worker_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if args.prefix != f"/workspaces/{args.workspace_id}":
        parser.error("prefix must match /workspaces/<workspace-id>")
    return args


def run_worker(args: argparse.Namespace) -> None:
    selected = resolve_workspace(args.storage_root, args.workspace_id)
    input_directory = resolve_input_directory(args.input_root, args.workspace_id)
    runtime = Path(os.environ.get("LIGHTRAG_MULTIWORKSPACE_RUNTIME", DEFAULT_RUNTIME_ROOT)).resolve()
    with workspace_lock(runtime, args.storage_root, args.workspace_id):
        backend = selected_backend()
        if backend == "postgres":
            manifest = configure_postgres_workspace(selected, args.workspace_id)
            asyncio.run(validate_postgres_readiness(manifest))
        log_dir = runtime / "logs" / args.workspace_id
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        input_directory.mkdir(parents=True, exist_ok=True)
        os.environ.update({
            "WORKSPACE": "",
            "WORKING_DIR": str(selected),
            "INPUT_DIR": str(input_directory),
            "HOST": "127.0.0.1",
            "PORT": str(args.port),
            "WORKERS": "1",
            "LOG_DIR": str(log_dir),
            "LIGHTRAG_API_PREFIX": args.prefix,
        })
        os.environ.pop("LIGHTRAG_GUNICORN_MODE", None)
        # LightRAG initializes its own parser lazily. Do not feed it our argv.
        sys.argv = ["lightrag-server"]
        install_startup_preservation(file_backends=backend == "file")
        install_table_header_preservation()
        if backend == "file":
            install_bounded_nano_loader()
        else:
            install_postgres_vector_namespace()

        from lightrag.api import lightrag_server as server
        from lightrag.api.config import global_args, update_uvicorn_mode_config
        from lightrag import ROLES
        import uvicorn

        expected_storages = POSTGRES_STORAGES if backend == "postgres" else {
            "kv_storage": "JsonKVStorage",
            "doc_status_storage": "JsonDocStatusStorage",
            "vector_storage": "NanoVectorDBStorage",
            "graph_storage": "NetworkXStorage",
        }
        for name, expected in expected_storages.items():
            if getattr(global_args, name) != expected:
                raise RuntimeError(f"Worker requires {name}={expected} for {backend} workspace isolation")
        apply_provider_budgets(global_args,
                               int(os.environ.get("LIGHTRAG_WORKER_BUDGET_DIVISOR", "1")), ROLES)
        global_args.ssl = False
        update_uvicorn_mode_config()
        server.configure_logging()
        app = server.create_app(global_args)
        identify_app(app, args.workspace_id)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_config=None, workers=1)


def main() -> None:
    args = parse_worker_args()
    try:
        run_worker(args)
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"Workspace worker refused startup: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
