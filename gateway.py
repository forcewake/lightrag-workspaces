"""Workspace routing for the pinned LightRAG server; one process per corpus."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from postgres_backend import load_workspace_manifest, selected_backend

ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
READ_POSTS = {"/documents/paginated", "/query", "/query/stream", "/query/data",
              "/api/chat", "/api/generate", "/login"}


def scope_swagger_html(content: bytes, prefix: str) -> bytes:
    """Scope the pinned upstream docs URLs without rebuilding its HTML."""
    encoded = prefix.encode("ascii")
    for quote in (b"'", b'"'):
        for path in (b"/openapi.json", b"/docs/oauth2-redirect"):
            content = content.replace(quote + path + quote, quote + encoded + path + quote)
        content = content.replace(quote + b"/static/swagger-ui/",
                                  quote + encoded + b"/static/swagger-ui/")
    return content


@dataclass
class Config:
    storage_root: Path
    input_root: Path
    runtime_root: Path
    static_root: Path
    default_workspace: str = "default"
    max_workers: int = 2
    startup_timeout: float = 240
    idle_seconds: float = 600
    embedding_dim: int = 3072
    storage_backend: str = "file"
    embedding_model: str | None = None
    postgres_database: str | None = None
    legacy_storage_workspace: str | None = None

    def __post_init__(self) -> None:
        if not ID_PATTERN.fullmatch(self.default_workspace):
            raise ValueError("LIGHTRAG_DEFAULT_WORKSPACE must be one exact directory identifier")
        if self.legacy_storage_workspace is not None and not ID_PATTERN.fullmatch(self.legacy_storage_workspace):
            raise ValueError("LIGHTRAG_LEGACY_STORAGE_WORKSPACE must be one exact directory identifier")
        if not 1 <= self.max_workers <= 100:
            raise ValueError("LIGHTRAG_MAX_ACTIVE_WORKSPACES must be between 1 and 100")
        if self.embedding_dim < 1:
            raise ValueError("EMBEDDING_DIM must be a positive integer")
        if self.storage_backend not in ("file", "postgres"):
            raise ValueError("LIGHTRAG_STORAGE_BACKEND must be file or postgres")
        if self.storage_backend == "postgres" and (not self.embedding_model or not self.postgres_database):
            raise ValueError("EMBEDDING_MODEL and POSTGRES_DATABASE are required for PostgreSQL workspaces")
        if not 0 < self.startup_timeout < float("inf"):
            raise ValueError("LIGHTRAG_WORKSPACE_STARTUP_TIMEOUT must be positive and finite")
        if not 0 < self.idle_seconds < float("inf"):
            raise ValueError("LIGHTRAG_WORKSPACE_IDLE_SECONDS must be positive and finite")

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            Path(os.environ.get("WORKING_DIR", "/app/data/rag_storage")),
            Path(os.environ.get("INPUT_DIR", "/app/data/inputs")),
            Path(os.environ.get("LIGHTRAG_MULTIWORKSPACE_RUNTIME", "/app/data/workspace-runtime")),
            Path(os.environ.get("LIGHTRAG_MULTIWORKSPACE_STATIC", "/opt/lightrag-multiworkspace/webui")),
            os.environ.get("LIGHTRAG_DEFAULT_WORKSPACE") or os.environ.get("WORKSPACE") or "default",
            int(os.environ.get("LIGHTRAG_MAX_ACTIVE_WORKSPACES", "2")),
            startup_timeout=float(os.environ.get("LIGHTRAG_WORKSPACE_STARTUP_TIMEOUT", "240")),
            idle_seconds=float(os.environ.get("LIGHTRAG_WORKSPACE_IDLE_SECONDS", "600")),
            embedding_dim=int(os.environ.get("EMBEDDING_DIM", "3072")),
            storage_backend=selected_backend(),
            embedding_model=os.environ.get("EMBEDDING_MODEL"),
            postgres_database=os.environ.get("POSTGRES_DATABASE"),
            legacy_storage_workspace=os.environ.get("LIGHTRAG_LEGACY_STORAGE_WORKSPACE") or None,
        )


def workspace_metadata(config: Config) -> list[dict[str, Any]]:
    """Read metadata only; discovery never initializes or creates a corpus."""
    root = Path(config.storage_root).resolve()
    if not root.is_dir():
        return []
    result = []
    for folder in sorted(root.iterdir()):
        if not ID_PATTERN.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        counts: dict[str, int] = {}
        item: dict[str, Any] = {"id": folder.name, "available": True, "readOnly": False}
        if config.storage_backend == "postgres":
            # PostgreSQL discovery accepts only explicitly registered control
            # directories. Original JSON files are never consulted in this mode.
            manifest_path = folder / ".postgres-workspace.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = load_workspace_manifest(manifest_path, folder.name)
                item.update(dimension=manifest["embedding_dim"],
                            postgresWorkspace=manifest["postgres_workspace"],
                            preserveBacklog=manifest["preserve_backlog"],
                            readOnly=bool(manifest.get("read_only") or manifest["preserve_backlog"]),
                            _postgresManifest=manifest)
                if (item["dimension"] != config.embedding_dim
                        or manifest["embedding_model"] != config.embedding_model
                        or manifest["database"] != config.postgres_database):
                    raise ValueError("Saved model or database identity disagrees with the configuration")
            except (ValueError, OSError, TypeError, KeyError):
                item.update(available=False, readOnly=True, readOnlyReason=(
                    "PostgreSQL workspace registration needs inspection before opening."
                ))
            # Counts become authoritative only after the read-only database
            # registry check. Never show preserved JSON counts as current data.
            item.update(documents=0, processed=0, failed=0, pending=0, processing=0)
            result.append(item)
            continue
        try:
            status = folder / "kv_store_doc_status.json"
            if status.exists():
                documents = json.loads(status.read_text())
                if not isinstance(documents, dict):
                    raise ValueError("Document metadata must be a mapping")
                for document in documents.values():
                    if not isinstance(document, dict) or not isinstance(document.get("status"), str):
                        raise ValueError("Invalid document metadata")
                    key = document.get("status", "unknown")
                    counts[key] = counts.get(key, 0) + 1
            dimensions = set()
            for vectors in folder.glob("vdb_*.json"):
                with vectors.open() as stream:
                    match = re.search(r'"embedding_dim"\s*:\s*(\d+)', stream.read(512))
                if match:
                    dimensions.add(int(match[1]))
                else:
                    raise ValueError("Vector metadata cannot be read")
            item["dimension"] = next(iter(dimensions)) if len(dimensions) == 1 else None
            if dimensions and dimensions != {config.embedding_dim}:
                item.update(available=False, readOnly=True,
                            readOnlyReason="The saved embedding dimensions need a matching model configuration.")
        except (ValueError, OSError, TypeError):
            item.update(available=False, readOnly=True,
                        readOnlyReason="Saved workspace metadata needs inspection before opening.")
        item.update(documents=sum(counts.values()), processed=counts.get("processed", 0),
                    failed=counts.get("failed", 0), pending=counts.get("pending", 0),
                    processing=sum(counts.get(key, 0) for key in
                                   ("processing", "parsing", "analyzing", "preprocessed")))
        if item["pending"] or item["processing"]:
            item.update(readOnly=True, readOnlyReason=(
                "Earlier unfinished imports are preserved. Uploads and changes are paused; search remains available."
            ))
        result.append(item)
    return result


def workspace_startup_memory(storage_root: Path, workspace_id: str, storage_backend: str = "file") -> int:
    """Conservative admission estimate; no corpus content is parsed here.

    Vector JSON is encoded on disk and is transiently retained while decoding
    its dense matrix. GraphML has larger Python/XML overhead. Text/cache JSON
    is counted separately rather than treating every saved byte as a matrix.
    The Docker memory ceiling remains the hard backstop; this estimate is a
    scheduling allowance, not a claim about a measured process peak.
    """
    total = 384 * 1024 * 1024
    root = storage_root.resolve()
    folder = root / workspace_id
    if not ID_PATTERN.fullmatch(workspace_id) or folder.is_symlink() or folder.resolve().parent != root:
        raise HTTPException(404, "Unknown workspace; no new workspace was created")
    if not folder.is_dir():
        raise HTTPException(404, "Unknown workspace; no new workspace was created")
    if storage_backend == "postgres":
        # Corpus matrices and graph objects remain in PostgreSQL. This fixed
        # process allowance still participates in host/cgroup admission.
        load_workspace_manifest(folder / ".postgres-workspace.json", workspace_id)
        return total
    for file in folder.iterdir():
        if not file.is_file() or file.is_symlink():
            continue
        if file.name.startswith("vdb_") and file.suffix == ".json":
            weight = 2.25
        elif file.suffix == ".graphml":
            weight = 4
        elif file.name.startswith("kv_store_") and file.suffix == ".json":
            weight = 2 if "cache" in file.name or file.name in (
                "kv_store_full_docs.json", "kv_store_text_chunks.json") else 4
        else:
            continue
        total += int(file.stat().st_size * weight)
    return total


def available_worker_memory() -> int | None:
    """Use host/cgroup headroom; reclaimable file cache is not anonymous RAM."""
    limits: list[int] = []
    try:
        meminfo = Path("/proc/meminfo").read_text()
        match = re.search(r"^MemAvailable:\s+(\d+)\s+kB$", meminfo, re.M)
        if match:
            limits.append(int(match[1]) * 1024)
    except OSError:
        pass
    for root, limit_name, usage_name, stat_name, reclaim_key in (
        (Path("/sys/fs/cgroup"), "memory.max", "memory.current", "memory.stat", "inactive_file"),
        (Path("/sys/fs/cgroup/memory"), "memory.limit_in_bytes", "memory.usage_in_bytes", "memory.stat", "total_inactive_file"),
    ):
        try:
            limit_text = (root / limit_name).read_text().strip()
            if limit_text == "max":
                continue
            ceiling = int(limit_text)
            if ceiling >= 1 << 60:
                continue
            current = int((root / usage_name).read_text())
            reclaimable = 0
            try:
                stats = dict(line.split() for line in (root / stat_name).read_text().splitlines())
                reclaimable = min(current, int(stats.get(reclaim_key, 0)))
            except (OSError, ValueError):
                pass
            limits.append(max(0, ceiling - current + reclaimable))
        except (OSError, ValueError):
            continue
    if not limits:
        return None  # Non-Linux unit tests; deployed workers run on Linux.
    reserve = int(os.environ.get("LIGHTRAG_WORKSPACE_MEMORY_HEADROOM_BYTES", str(2 * 1024**3)))
    if reserve < 0:
        raise RuntimeError("Workspace memory headroom must not be negative")
    return max(0, min(limits) - reserve)


@dataclass
class Worker:
    workspace_id: str
    port: int
    process: asyncio.subprocess.Process
    base_url: str
    ready: asyncio.Task | None = None
    references: int = 0
    last_used: float = field(default_factory=time.monotonic)
    log_file: Any = None
    log_task: asyncio.Task | None = None
    accepted_requests: bool = False
    startup_memory_bytes: int = 0


class WorkerManager:
    def __init__(self, config: Config):
        self.config = config
        self.workers: dict[str, Worker] = {}
        self.lock = asyncio.Lock()
        self.sweeper: asyncio.Task | None = None
        self.preserved_backlogs = {item["id"] for item in workspace_metadata(config)
                                   if item.get("pending") or item.get("processing") or item.get("preserveBacklog")}
        self.postgres_inventory = None
        if config.storage_backend == "postgres":
            from postgres_inventory import PostgresInventory
            self.postgres_inventory = PostgresInventory()
        self.closing = False

    @staticmethod
    def _warning(message: str, *args: Any) -> None:
        import logging
        logging.getLogger(__name__).warning(message, *args)

    @staticmethod
    def _startup_failed(worker: Worker) -> bool:
        return bool(not worker.accepted_requests and worker.ready and worker.ready.done()
                    and (worker.ready.cancelled() or worker.ready.exception() is not None))

    def _open_log(self, workspace_id: str) -> Any:
        from logging.handlers import RotatingFileHandler

        class PrivateRotatingLog(RotatingFileHandler):
            def _open(self):
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(self.baseFilename, flags, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    return os.fdopen(fd, "a", encoding="utf-8", errors="replace")
                except BaseException:
                    os.close(fd)
                    raise

            def shouldRollover(self, record):
                if self.stream is None:
                    self.stream = self._open()
                payload = (self.format(record) + self.terminator).encode("utf-8", "replace")
                return self.maxBytes > 0 and self.stream.tell() + len(payload) >= self.maxBytes

        log_dir = self.config.runtime_root / "supervisor-logs"
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_dir.chmod(0o700)
        log_path = log_dir / f"{workspace_id}.log"
        for suffix in (".1", ".2"):
            backup = log_path.with_name(log_path.name + suffix)
            if backup.is_symlink():
                raise RuntimeError("Supervisor log must not be a symlink")
            if backup.exists():
                backup.chmod(0o600)
        return PrivateRotatingLog(log_path, maxBytes=10 * 1024 * 1024, backupCount=2,
                                  encoding="utf-8", errors="replace")

    async def _capture_output(self, worker: Worker) -> None:
        import codecs
        import logging

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = await worker.process.stdout.read(65536)
                text = decoder.decode(chunk, final=not chunk)
                if text:
                    record = logging.LogRecord("lightrag.workspace.stdout", logging.INFO,
                                               "", 0, text, (), None)
                    await asyncio.to_thread(worker.log_file.handle, record)
                if not chunk:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._warning("Output capture stopped for workspace %s", worker.workspace_id)

    async def _spawn(self, workspace_id: str, port: int) -> Worker:
        log_file = self._open_log(workspace_id)
        environment = dict(os.environ)
        environment["LIGHTRAG_MULTIWORKSPACE_RUNTIME"] = str(self.config.runtime_root)
        environment["LIGHTRAG_WORKER_BUDGET_DIVISOR"] = str(self.config.max_workers)
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("worker.py")),
                "--workspace-id", workspace_id, "--storage-root", str(self.config.storage_root),
                "--input-root", str(self.config.input_root), "--port", str(port),
                "--prefix", f"/workspaces/{workspace_id}",
                env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except BaseException:
            log_file.close()
            raise
        worker = Worker(workspace_id, port, process, f"http://127.0.0.1:{port}", log_file=log_file)
        worker.log_task = asyncio.create_task(self._capture_output(worker))
        worker.ready = asyncio.create_task(self._wait_ready(worker))
        return worker

    async def inventory(self) -> list[dict[str, Any]]:
        items = await asyncio.to_thread(workspace_metadata, self.config)
        if self.postgres_inventory is not None:
            items = await self.postgres_inventory.refresh(items)
        for item in items:
            # Only the queues found before this gateway started are frozen.
            # New, explicitly submitted uploads must remain manageable.
            if (item["id"] not in self.preserved_backlogs and item.get("available")
                    and not (self.config.storage_backend == "postgres" and item.get("readOnly"))):
                item["readOnly"] = False
                item.pop("readOnlyReason", None)
            elif item["id"] in self.preserved_backlogs and item.get("available"):
                item.update(readOnly=True, readOnlyReason=(
                    "Earlier unfinished imports are preserved. Uploads and changes are paused; search remains available."
                ))
            worker = self.workers.get(item["id"])
            item["loaded"] = bool(worker and worker.process.returncode is None and worker.ready and worker.ready.done() and not worker.ready.cancelled() and worker.ready.exception() is None)
        return items

    async def start(self) -> None:
        if self.config.max_workers < 1:
            raise RuntimeError("At least one workspace process is required")
        if self.config.max_workers > 100:
            raise RuntimeError("The active-workspace limit exceeds the internal port range")
        self.config.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config.runtime_root.chmod(0o700)
        self.closing = False
        self.sweeper = asyncio.create_task(self._sweep())

    async def _idle(self, worker: Worker) -> bool:
        if worker.references or (worker.ready and not worker.ready.done()):
            return False
        if worker.process.returncode is not None:
            return True
        # A failed cold start has never served a request or accepted an upload.
        # It can be closed even if its health endpoint never became available.
        if self._startup_failed(worker):
            return True
        if worker.ready is None or worker.ready.cancelled() or worker.ready.exception() is not None:
            return False
        try:
            internal_headers = {}
            if os.environ.get("LIGHTRAG_API_KEY"):
                internal_headers["X-API-Key"] = os.environ["LIGHTRAG_API_KEY"]
            async with httpx.AsyncClient(timeout=3, trust_env=False, headers=internal_headers) as client:
                response = await client.get(worker.base_url + "/health")
                health = response.json()
            if (response.status_code != 200 or not isinstance(health, dict)
                    or health.get("status") != "healthy"
                    or health.get("selected_workspace_id") != worker.workspace_id
                    or health.get("pipeline_busy") is not False
                    or health.get("pipeline_active") is not False
                    or any(key not in health for key in ("llm_queue_status", "embedding_queue_status", "rerank_queue_status"))):
                return False
            llm_queues = health.get("llm_queue_status", {})
            if not isinstance(llm_queues, dict):
                return False
            queues = list(llm_queues.values()) + [health.get("embedding_queue_status", {}),
                                                health.get("rerank_queue_status", {})]
            if any(not isinstance(queue, dict) or queue.get("in_flight") or queue.get("queued")
                   or queue.get("running") for queue in queues):
                return False
            return not any(health.get(key) for key in
                           ("pipeline_busy", "pipeline_active", "pipeline_scanning",
                            "pipeline_destructive_busy", "pipeline_pending_enqueues"))
        except (httpx.HTTPError, ValueError, TypeError):
            return False

    async def _terminate(self, worker: Worker) -> None:
        if worker.process.returncode is None:
            try:
                worker.process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(worker.process.wait(), timeout=90)
            except asyncio.TimeoutError as error:
                # Keep a possibly-writing worker registered; never force kill it.
                raise RuntimeError("Workspace is still closing; retry shortly") from error
        if worker.ready:
            if not worker.ready.done():
                worker.ready.cancel()
            await asyncio.gather(worker.ready, return_exceptions=True)
        if worker.log_task:
            try:
                await asyncio.wait_for(asyncio.shield(worker.log_task), timeout=5)
            except asyncio.TimeoutError:
                worker.log_task.cancel()
                await asyncio.gather(worker.log_task, return_exceptions=True)
            except Exception:
                self._warning("Output capture could not drain for workspace %s", worker.workspace_id)
        if self.workers.get(worker.workspace_id) is worker:
            self.workers.pop(worker.workspace_id, None)
        if worker.log_file:
            worker.log_file.close()

    async def _sweep(self) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    async with self.lock:
                        for worker in list(self.workers.values()):
                            try:
                                if ((self._startup_failed(worker) or time.monotonic() - worker.last_used > self.config.idle_seconds)
                                        and await self._idle(worker)):
                                    await self._terminate(worker)
                            except Exception:
                                self._warning("Workspace %s could not be swept; it remains registered", worker.workspace_id)
                except Exception:
                    self._warning("Workspace sweep failed; the next sweep will retry")
        except asyncio.CancelledError:
            return

    async def stop(self) -> None:
        self.closing = True
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        results = await asyncio.gather(*(self._terminate(worker) for worker in list(self.workers.values())),
                                       return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                self._warning("A workspace did not close cleanly; other shutdowns were still attempted")
        if self.postgres_inventory is not None:
            await self.postgres_inventory.close()

    async def _wait_ready(self, worker: Worker) -> None:
        deadline = time.monotonic() + self.config.startup_timeout
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            while time.monotonic() < deadline:
                if worker.process.returncode is not None:
                    raise HTTPException(503, "The selected workspace could not start; its data is retained.")
                try:
                    response = await client.get(worker.base_url + "/health")
                    health = response.json()
                    if (response.status_code == 200 and isinstance(health, dict)
                            and health.get("status") == "healthy"
                            and health.get("selected_workspace_id") == worker.workspace_id):
                        return
                except (httpx.HTTPError, ValueError, TypeError):
                    pass
                await asyncio.sleep(0.5)
        raise HTTPException(503, "The selected workspace is taking longer to open; retry shortly.")

    @asynccontextmanager
    async def lease(self, workspace_id: str):
        async with self.lock:
            if self.closing:
                raise HTTPException(503, "Workspace gateway is closing; retry shortly.")
            worker = self.workers.get(workspace_id)
            if worker and (worker.process.returncode is not None
                           or (not worker.references and self._startup_failed(worker))):
                await self._terminate(worker)
                worker = None
            if worker is None:
                if self.config.storage_backend == "postgres":
                    memory_needed = await asyncio.to_thread(workspace_startup_memory, self.config.storage_root,
                                                           workspace_id, "postgres")
                else:
                    memory_needed = await asyncio.to_thread(workspace_startup_memory, self.config.storage_root, workspace_id)
                while True:
                    memory_available = available_worker_memory()
                    # Processes that have just spawned may not have loaded any
                    # vectors yet. Reserve their full startup allowance before
                    # admitting another cold load; current RSS alone races it.
                    warming = sum(item.startup_memory_bytes for item in self.workers.values()
                                  if item.process.returncode is None and item.ready and not item.ready.done())
                    memory_ok = memory_available is None or memory_needed + warming <= memory_available
                    if len(self.workers) < self.config.max_workers and memory_ok:
                        break
                    for victim in sorted(self.workers.values(), key=lambda item: item.last_used):
                        if await self._idle(victim):
                            await self._terminate(victim)
                            break
                    else:
                        if not memory_ok:
                            raise HTTPException(503, "The selected workspace needs more free memory. "
                                                "Finish current work and retry; saved data has not changed.")
                        raise HTTPException(503, "All workspace slots are busy. Retry when a running request finishes.")
                used = {item.port for item in self.workers.values()}
                port = next(value for value in range(19621, 19721) if value not in used)
                worker = await self._spawn(workspace_id, port)
                worker.startup_memory_bytes = memory_needed
                self.workers[workspace_id] = worker
            worker.references += 1
        try:
            await asyncio.shield(worker.ready)
            worker.accepted_requests = True
            yield worker
        finally:
            async with self.lock:
                worker.references -= 1
                worker.last_used = time.monotonic()
                if not worker.references and self._startup_failed(worker):
                    try:
                        await self._terminate(worker)
                    except Exception:
                        self._warning("Failed startup for workspace %s remains registered for cleanup", workspace_id)


def create_app(config: Config, manager: Any = None, transport: Any = None) -> FastAPI:
    from starlette.background import BackgroundTask

    manager = manager or WorkerManager(config)
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(None, connect=5, pool=10), trust_env=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await client.aclose()
            await manager.stop()

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    application.state.manager = manager

    @application.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def route(request: Request, path: str):
        if "\\" in request.url.path or any(part in (".", "..") for part in request.url.path.split("/")):
            raise HTTPException(400, "Invalid path")
        inventory = await manager.inventory()
        registry = {item["id"]: item for item in inventory}
        if request.url.path in ("/workspaces", "/workspaces/"):
            if request.method not in ("GET", "HEAD"):
                raise HTTPException(405, "Workspace discovery is read-only")
            return JSONResponse({"defaultWorkspace": config.default_workspace, "workspaces": inventory}, headers={"Cache-Control": "no-store"})

        values = [value.decode("latin1") for key, value in request.scope["headers"] if key.lower() == b"lightrag-workspace"]
        if len(values) > 1 or (values and (not values[0] or values[0] != values[0].strip() or not ID_PATTERN.fullmatch(values[0]))):
            raise HTTPException(400, "Supply one non-empty exact LIGHTRAG-WORKSPACE identifier")
        header_id = values[0] if values else None
        upstream_path = request.url.path
        path_id = None
        if upstream_path.startswith("/workspaces/"):
            parts = upstream_path.split("/", 3)
            path_id = parts[2]
            if not ID_PATTERN.fullmatch(path_id):
                raise HTTPException(400, "Invalid workspace identifier")
            upstream_path = "/" + parts[3] if len(parts) == 4 else "/"
        if path_id and header_id and path_id != header_id:
            raise HTTPException(400, "Workspace path and header disagree")
        selected = path_id or header_id or config.default_workspace
        if selected not in registry:
            raise HTTPException(404, "Unknown workspace; no new workspace was created")
        metadata = registry[selected]
        prefix = "/workspaces/" + selected
        identity_headers = {"LIGHTRAG-WORKSPACE": selected, "X-LightRAG-Workspace": selected}

        if upstream_path == "/":
            return RedirectResponse(prefix + "/webui/", status_code=307, headers=identity_headers)
        if upstream_path in ("/webui", "/workspace"):
            return RedirectResponse(prefix + upstream_path + "/", status_code=307, headers=identity_headers)
        entry = next((name for name in ("webui", "workspace") if upstream_path.startswith("/" + name + "/")), None)
        if entry:
            if request.method not in ("GET", "HEAD"):
                raise HTTPException(405, "UI assets are read-only")
            if not path_id:
                return RedirectResponse(prefix + upstream_path, status_code=307, headers=identity_headers)
            relative = upstream_path[len(entry) + 2:]
            if not relative:
                html_name = "index.html" if entry == "webui" else "workspace.html"
                html = (config.static_root / html_name).read_text()
                runtime = {"apiPrefix": prefix, "workspaceId": selected,
                           "defaultWorkspace": config.default_workspace,
                           "legacyStorageWorkspace": config.legacy_storage_workspace or config.default_workspace,
                           "workspaces": inventory}
                script = "<script>window.__LIGHTRAG_CONFIG__=" + json.dumps(runtime).replace("<", "\\u003c") + ";</script>"
                html = html.replace("<head>", "<head>" + script, 1)
                return Response(html, media_type="text/html", headers={**identity_headers, "Cache-Control": "no-store"})
            static_root = config.static_root.resolve()
            file_path = (static_root / relative).resolve()
            if static_root not in file_path.parents or not file_path.is_file() or file_path.suffix == ".html":
                raise HTTPException(404, "UI asset not found")
            return FileResponse(file_path, headers=identity_headers)

        if not metadata.get("available", True):
            raise HTTPException(409, metadata.get("readOnlyReason") or "Workspace metadata needs inspection")
        if metadata.get("readOnly") and request.method not in ("GET", "HEAD", "OPTIONS") and not (request.method == "POST" and upstream_path.rstrip("/") in READ_POSTS):
            return JSONResponse({"detail": metadata.get("readOnlyReason"), "workspace": selected, "code": "legacy_queue_preserved"}, status_code=409, headers=identity_headers)

        lease = manager.lease(selected)
        worker = await lease.__aenter__()
        upstream = None
        cleanup_task = None

        async def cleanup():
            nonlocal cleanup_task
            if cleanup_task is None:
                async def finish():
                    try:
                        if upstream is not None:
                            await upstream.aclose()
                    finally:
                        await lease.__aexit__(None, None, None)
                cleanup_task = asyncio.create_task(finish())
            # Client cancellation must not abandon a live lease. The same task
            # also serves the pre-first-chunk disconnect/background fallback.
            await asyncio.shield(cleanup_task)

        try:
            query = request.scope.get("query_string", b"").decode("ascii")
            url = worker.base_url + upstream_path + ("?" + query if query else "")
            headers = [(key.decode("latin1"), value.decode("latin1")) for key, value in request.scope["headers"]
                       if key.decode("latin1").lower() not in HOP_HEADERS | {"lightrag-workspace", "accept-encoding"}]
            outbound = client.build_request(request.method, url, headers=headers, content=request.stream())
            upstream = await client.send(outbound, stream=True)
            response_headers = {key: value for key, value in upstream.headers.items()
                                if key.lower() not in HOP_HEADERS | {"content-encoding", "lightrag-workspace", "x-lightrag-workspace"}}
            response_headers.update(identity_headers)
            location = response_headers.get("location")
            if location and urlsplit(location).netloc == urlsplit(worker.base_url).netloc:
                location_path = urlsplit(location).path
                response_headers["location"] = location_path if location_path.startswith(prefix + "/") else prefix + location_path

            if (request.method == "GET" and upstream_path == "/docs" and upstream.status_code == 200
                    and upstream.headers.get("content-type", "").split(";", 1)[0].lower() == "text/html"):
                content = scope_swagger_html(await upstream.aread(), prefix)
                # These describe the upstream bytes, which have now changed.
                for name in ("etag", "content-md5", "digest"):
                    response_headers.pop(name, None)
                await cleanup()
                return Response(content, status_code=upstream.status_code, headers=response_headers)

            async def body():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await cleanup()

            return StreamingResponse(body(), status_code=upstream.status_code, headers=response_headers,
                                     background=BackgroundTask(cleanup))
        except BaseException:
            await cleanup()
            raise

    return application


app = create_app(Config.from_env())
