"""Exercise ASGI disconnects, including before the first proxy response chunk."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import Config, create_app


class LeaseManager:
    def __init__(self):
        self.active = 0
        self.releases = 0
        self.released = asyncio.Event()

    async def inventory(self):
        return [{"id": "research", "available": True, "readOnly": False}]

    async def start(self):
        pass

    async def stop(self):
        pass

    @asynccontextmanager
    async def lease(self, workspace_id):
        self.active += 1
        try:
            yield SimpleNamespace(base_url="http://127.0.0.1:19621")
        finally:
            self.active -= 1
            self.releases += 1
            self.released.set()


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, slow_close=False):
        self.closing = asyncio.Event()
        self.close_permitted = asyncio.Event()
        self.closed = False
        if not slow_close:
            self.close_permitted.set()

    async def __aiter__(self):
        await asyncio.Event().wait()  # Client disconnects before the first chunk.
        yield b"unreachable"

    async def aclose(self):
        self.closing.set()
        await self.close_permitted.wait()
        self.closed = True


class StreamCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config(root / "storage", root / "inputs", root / "runtime", root / "static",
                             default_workspace="research")

    async def run_response(self, manager, stream):
        app = create_app(self.config, manager=manager,
                         transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)))
        disconnected = asyncio.Event()
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                disconnected.set()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
                 "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/health",
                 "raw_path": b"/health", "query_string": b"", "root_path": "", "headers": [],
                 "server": ("gateway.test", 80), "client": ("127.0.0.1", 12345)}
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)

    async def test_disconnect_before_first_chunk_closes_upstream_and_releases_once(self):
        manager = LeaseManager()
        stream = SlowStream()
        await asyncio.wait_for(self.run_response(manager, stream), timeout=2)
        self.assertTrue(stream.closed)
        self.assertEqual(manager.active, 0)
        self.assertEqual(manager.releases, 1)

    async def test_cancellation_during_upstream_close_cannot_abandon_worker_lease(self):
        manager = LeaseManager()
        stream = SlowStream(slow_close=True)
        response = asyncio.create_task(self.run_response(manager, stream))
        await asyncio.wait_for(stream.closing.wait(), timeout=2)
        self.assertEqual(manager.active, 1)
        response.cancel()
        stream.close_permitted.set()
        await asyncio.gather(response, return_exceptions=True)
        await asyncio.wait_for(manager.released.wait(), timeout=2)
        self.assertTrue(stream.closed)
        self.assertEqual(manager.active, 0)
        self.assertEqual(manager.releases, 1)


if __name__ == "__main__":
    unittest.main()
