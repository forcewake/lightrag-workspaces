"""Lifecycle, active-lease, and bounded-log checks without a real worker/server."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import signal
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import Config, Worker, WorkerManager, available_worker_memory, workspace_startup_memory


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.signals = []

    def send_signal(self, sent):
        self.signals.append(sent)
        self.returncode = 0

    async def wait(self):
        self.returncode = 0
        return 0


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = self.root / "storage"
        self.storage.mkdir()
        for name in ("research", "default"):
            (self.storage / name).mkdir()
        self.config = Config(self.storage, self.root / "inputs", self.root / "runtime", self.root / "static")
        self.manager = WorkerManager(self.config)

    def ready_worker(self, workspace_id="research", error=None):
        ready = asyncio.get_running_loop().create_future()
        if error:
            ready.set_exception(error)
        else:
            ready.set_result(None)
        return Worker(workspace_id, 19621, FakeProcess(), "http://127.0.0.1:19621", ready=ready)

    async def test_active_stream_lease_prevents_eviction_until_released(self):
        worker = self.ready_worker()
        self.manager.workers["research"] = worker
        with patch("gateway.httpx.AsyncClient") as health_client:
            async with self.manager.lease("research"):
                self.assertEqual(worker.references, 1)
                self.assertFalse(await self.manager._idle(worker))
                health_client.assert_not_called()
            self.assertEqual(worker.references, 0)
            self.assertTrue(worker.accepted_requests)

    async def test_background_pipeline_prevents_eviction_after_http_lease_released(self):
        worker = self.ready_worker()
        client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json={
            "status": "healthy", "selected_workspace_id": "research", "pipeline_busy": False,
            "pipeline_active": True,
        })))
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch("gateway.httpx.AsyncClient", return_value=context):
            self.assertFalse(await self.manager._idle(worker))
        self.assertEqual(worker.process.signals, [])

    async def test_failed_cold_start_is_closed_and_next_request_can_retry(self):
        first = self.ready_worker(error=HTTPException(503, "cold start failed"))
        second = self.ready_worker()
        self.manager._spawn = AsyncMock(side_effect=[first, second])
        with self.assertRaises(HTTPException):
            async with self.manager.lease("research"):
                self.fail("Failed startup accepted a request")
        self.assertFalse(first.accepted_requests)
        self.assertEqual(first.references, 0)
        self.assertEqual(first.process.signals, [signal.SIGTERM])
        self.assertNotIn("research", self.manager.workers)
        async with self.manager.lease("research") as selected:
            self.assertIs(selected, second)
        self.assertEqual(self.manager._spawn.await_count, 2)

    async def test_unobserved_failed_start_can_be_reaped_without_health(self):
        worker = self.ready_worker(error=HTTPException(503, "startup timeout"))
        with patch("gateway.httpx.AsyncClient") as health_client:
            self.assertTrue(await self.manager._idle(worker))
            health_client.assert_not_called()
        await self.manager._terminate(worker)

    async def test_sweep_continues_after_one_termination_fails_and_retries_next_round(self):
        self.manager.workers = {name: self.ready_worker(name) for name in ("research", "default")}
        self.manager.config.idle_seconds = -1
        self.manager._idle = AsyncMock(return_value=True)
        self.manager._terminate = AsyncMock(side_effect=[RuntimeError("still closing"), None, None, None])
        with patch("gateway.asyncio.sleep", new=AsyncMock(side_effect=[None, None, asyncio.CancelledError()])), \
                self.assertLogs("gateway", level="WARNING"):
            await self.manager._sweep()
        self.assertEqual(self.manager._terminate.await_count, 4)
        self.assertEqual(self.manager._terminate.await_args_list[1].args[0].workspace_id, "default")
        self.assertEqual(self.manager._terminate.await_args_list[2].args[0].workspace_id, "research")

    async def test_shutdown_attempts_every_worker_despite_individual_failure(self):
        self.manager.workers = {name: self.ready_worker(name) for name in ("research", "default")}
        self.manager._terminate = AsyncMock(side_effect=[RuntimeError("still closing"), None])
        with self.assertLogs("gateway", level="WARNING"):
            await self.manager.stop()
        self.assertEqual(self.manager._terminate.await_count, 2)
        self.assertTrue(self.manager.closing)

    async def test_failed_process_launch_closes_log_handler(self):
        handler = SimpleNamespace(close=Mock())
        self.manager._open_log = Mock(return_value=handler)
        with patch("gateway.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("launch failed"))):
            with self.assertRaises(OSError):
                await self.manager._spawn("research", 19621)
        handler.close.assert_called_once()
        self.assertFalse(self.manager.workers)

    async def test_memory_admission_releases_idle_worker_before_loading_large_workspace(self):
        idle = self.ready_worker("default")
        self.manager.workers["default"] = idle
        chosen = self.ready_worker("research")
        self.manager._spawn = AsyncMock(return_value=chosen)
        self.manager._idle = AsyncMock(return_value=True)
        with patch("gateway.workspace_startup_memory", return_value=10), \
                patch("gateway.available_worker_memory", side_effect=[5, 20]):
            async with self.manager.lease("research"):
                self.assertEqual(idle.process.signals, [signal.SIGTERM])
                self.assertNotIn("default", self.manager.workers)
        self.assertEqual(chosen.startup_memory_bytes, 10)

    async def test_memory_admission_does_not_terminate_active_worker_or_spawn(self):
        busy = self.ready_worker("default")
        busy.references = 1
        self.manager.workers["default"] = busy
        self.manager._spawn = AsyncMock()
        with patch("gateway.workspace_startup_memory", return_value=10), \
                patch("gateway.available_worker_memory", return_value=5):
            with self.assertRaises(HTTPException) as caught:
                async with self.manager.lease("research"):
                    self.fail("Admitted over available memory")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("memory", caught.exception.detail)
        self.assertEqual(busy.process.signals, [])
        self.manager._spawn.assert_not_awaited()

    async def test_concurrent_cold_loads_reserve_memory_before_rss_increases(self):
        pending = asyncio.get_running_loop().create_future()
        warming = Worker("default", 19622, FakeProcess(), "http://127.0.0.1:19622",
                         ready=pending, startup_memory_bytes=10)
        self.manager.workers["default"] = warming
        self.manager._spawn = AsyncMock()
        try:
            with patch("gateway.workspace_startup_memory", return_value=10), \
                    patch("gateway.available_worker_memory", return_value=15):
                with self.assertRaises(HTTPException):
                    async with self.manager.lease("research"):
                        self.fail("Cold loads exceeded memory allowance")
            self.assertEqual(warming.process.signals, [])
            self.manager._spawn.assert_not_awaited()
        finally:
            pending.cancel()

    async def test_component_estimate_does_not_treat_text_cache_as_vector_matrix(self):
        folder = self.storage / "research"
        with (folder / "vdb_entities.json").open("wb") as file:
            file.truncate(400)
        with (folder / "kv_store_llm_response_cache.json").open("wb") as file:
            file.truncate(400)
        with (folder / "graph_chunk_entity_relation.graphml").open("wb") as file:
            file.truncate(400)
        self.assertEqual(workspace_startup_memory(self.storage, "research"), 384 * 1024**2 + 900 + 800 + 1600)

    async def test_memory_headroom_uses_container_limit_and_reclaimable_file_cache(self):
        contents = {
            "/proc/meminfo": "MemAvailable: 10485760 kB\n",  # 10 GiB on host
            "/sys/fs/cgroup/memory.max": str(8 * 1024**3),
            "/sys/fs/cgroup/memory.current": str(6 * 1024**3),
            "/sys/fs/cgroup/memory.stat": f"inactive_file {1024**3}\n",
        }

        def read_text(path, *args, **kwargs):
            if str(path) in contents:
                return contents[str(path)]
            raise FileNotFoundError(str(path))

        with patch("gateway.Path.read_text", new=read_text), \
                patch.dict("os.environ", {"LIGHTRAG_WORKSPACE_MEMORY_HEADROOM_BYTES": str(2 * 1024**3)}):
            self.assertEqual(available_worker_memory(), 1024**3)

    async def test_supervisor_output_has_two_backups_with_private_bounded_files(self):
        handler = self.manager._open_log("research")
        self.addCleanup(handler.close)
        self.assertEqual(handler.maxBytes, 10 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 2)
        # Unicode proves rollover counts encoded bytes, not Python characters.
        payload = "ж" * (256 * 1024)
        for _ in range(65):
            handler.handle(logging.LogRecord("test", logging.INFO, "", 0, payload, (), None))
        handler.flush()
        folder = self.config.runtime_root / "supervisor-logs"
        files = sorted(folder.iterdir())
        self.assertEqual([file.name for file in files], ["research.log", "research.log.1", "research.log.2"])
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for file in files:
            self.assertLessEqual(file.stat().st_size, 10 * 1024 * 1024)
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
