"""Black-box routing and isolation checks for the workspace gateway.

All upstream traffic is simulated: these checks never contact a server, call an
AI service, or open an existing LightRAG corpus.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import Config, Worker, WorkerManager, create_app, workspace_metadata  # noqa: E402


WORKSPACES = tuple(
    {"id": workspace_id, "readOnly": read_only, "available": True,
     "documents": 10, "processed": 8, "failed": 0,
     "pending": 2 if read_only else 0, "processing": 0, "dimension": 3072,
     "readOnlyReason": "Legacy queue is frozen" if read_only else None}
    for workspace_id, read_only in (
        ("default", False),
        ("research", False),
        ("archive", True),
        ("meeting-notes", True),
        ("meeting_notes", False),
    )
)


class FakeManager:
    """Track actual leases, rather than trusting the response's label alone."""

    def __init__(self):
        self.leases = []
        self.active = set()
        self.base_urls = {
            item["id"]: f"http://127.0.0.1:{11000 + index}"
            for index, item in enumerate(WORKSPACES)
        }
        self.ids_by_host = {
            httpx.URL(url).netloc: workspace_id
            for workspace_id, url in self.base_urls.items()
        }

    async def inventory(self):
        return [dict(item) for item in WORKSPACES]

    async def start(self):
        pass

    async def stop(self):
        pass

    @asynccontextmanager
    async def lease(self, workspace_id):
        self.leases.append(workspace_id)
        self.active.add(workspace_id)
        try:
            yield SimpleNamespace(base_url=self.base_urls[workspace_id])
        finally:
            self.active.discard(workspace_id)


class EventStream(httpx.AsyncByteStream):
    def __init__(self, check):
        self.check = check

    async def __aiter__(self):
        self.check()
        yield b'{"response":"first","done":false}\n'
        await asyncio.sleep(0)
        self.check()
        yield b'{"response":"second","done":true}\n'


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("storage", "inputs", "runtime", "static"):
            (self.root / name).mkdir()
        for item in WORKSPACES:
            (self.root / "storage" / item["id"]).mkdir()
        self.static = self.root / "static"
        (self.static / "index.html").write_text(
            '<!doctype html><html><head></head><body><div id="root"></div>'
            '<script type="module" src="/webui/assets/main.js"></script></body></html>'
        )
        (self.static / "workspace.html").write_text(
            '<!doctype html><html><head></head><body><div id="root"></div></body></html>'
        )
        (self.static / "assets").mkdir()
        (self.static / "assets" / "main.js").write_text("console.log('asset')")
        self.manager = FakeManager()
        self.requests = []
        self.config = Config(
            storage_root=self.root / "storage",
            input_root=self.root / "inputs",
            runtime_root=self.root / "runtime",
            static_root=self.static,
            default_workspace="default",
            max_workers=2,
        )
        self.app = create_app(
            self.config,
            manager=self.manager,
            transport=httpx.MockTransport(self.upstream),
        )
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://gateway.test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.temp.cleanup()

    async def upstream(self, request):
        body = await request.aread()
        self.requests.append(request)
        workspace_id = self.manager.ids_by_host[request.url.netloc]
        return httpx.Response(
            200,
            json={
                "workspace": workspace_id,
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query.decode(),
                "body": body.decode(),
                "workspace_header": request.headers.get("LIGHTRAG-WORKSPACE"),
                "authorization": request.headers.get("Authorization"),
                "api_key": request.headers.get("X-API-Key"),
            },
        )

    def assert_rejected(self, response):
        self.assertIn(response.status_code, (400, 404, 409, 422))

    async def test_inventory_and_static_ui_do_not_start_workers(self):
        inventory = await self.client.get("/workspaces")
        self.assertEqual(inventory.status_code, 200)
        self.assertIn("meeting-notes", inventory.text)
        for path in (
            "/workspaces/research/webui/",
            "/workspaces/meeting-notes/workspace/",
            "/workspaces/research/webui/assets/main.js",
        ):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.manager.leases, [])
        self.assertEqual(self.requests, [])
        redirect = await self.client.get("/webui/")
        self.assertIn(redirect.status_code, (302, 307, 308))
        self.assertEqual(redirect.headers["Location"], "/workspaces/default/webui/")
        self.assertEqual(self.manager.leases, [])

    async def test_ui_receives_selected_workspace_runtime_configuration(self):
        response = await self.client.get("/workspaces/research/webui/")
        self.assertEqual(response.status_code, 200)
        for expected in ("apiPrefix", "/workspaces/research", "workspaceId", "research",
                         "defaultWorkspace", "default", "workspaces"):
            self.assertIn(expected, response.text)
        self.assertEqual(self.manager.leases, [])

    async def test_default_and_explicit_routes_reach_the_selected_backend(self):
        cases = (
            ("/health", {}, "default"),
            ("/health", {"LIGHTRAG-WORKSPACE": "research"}, "research"),
            ("/workspaces/research/health", {}, "research"),
            ("/workspaces/meeting-notes/health", {}, "meeting-notes"),
            ("/health", {"LIGHTRAG-WORKSPACE": "meeting_notes"}, "meeting_notes"),
            ("/workspaces/research/health", {"LIGHTRAG-WORKSPACE": "research"}, "research"),
        )
        for path, headers, expected in cases:
            with self.subTest(path=path, headers=headers):
                response = await self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["workspace"], expected)
                self.assertEqual(response.json()["path"], "/health")
                self.assertEqual(response.headers.get("LIGHTRAG-WORKSPACE"), expected)
                self.assertIsNone(response.json()["workspace_header"])
                self.assertEqual(self.manager.leases[-1], expected)

    async def test_invalid_explicit_selector_never_falls_back_or_leases(self):
        cases = (
            ("/health", [("LIGHTRAG-WORKSPACE", "")]),
            ("/health", [("LIGHTRAG-WORKSPACE", " ")]),
            ("/health", [("LIGHTRAG-WORKSPACE", "missing")]),
            ("/health", [("LIGHTRAG-WORKSPACE", "RESEARCH")]),
            ("/health", [("LIGHTRAG-WORKSPACE", "research,default")]),
            ("/health", [("LIGHTRAG-WORKSPACE", "../default")]),
            ("/health", [("LIGHTRAG-WORKSPACE", "research"), ("LIGHTRAG-WORKSPACE", "research")]),
            ("/health", [("LIGHTRAG-WORKSPACE", "research"), ("LIGHTRAG-WORKSPACE", "default")]),
            ("/workspaces/research/health", [("LIGHTRAG-WORKSPACE", "default")]),
            ("/workspaces/missing/health", []),
            ("/workspaces/%2e%2e/health", []),
            ("/workspaces/default%2f..%2farchive/health", []),
        )
        for path, headers in cases:
            with self.subTest(path=path, headers=headers):
                response = await self.client.get(path, headers=headers)
                self.assert_rejected(response)
        self.assertEqual(self.manager.leases, [])
        self.assertEqual(self.requests, [])

    async def test_request_method_body_query_and_credentials_are_forwarded(self):
        payload = '{"query":"a & b","history":[{"content":"hello"}]}'
        response = await self.client.post(
            "/workspaces/research/query?tag=a%2Fb&tag=c%20d&blank=",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer test-token",
                "X-API-Key": "test-api-key",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workspace"], "research")
        self.assertEqual(body["method"], "POST")
        self.assertEqual(body["path"], "/query")
        self.assertEqual(body["query"], "tag=a%2Fb&tag=c%20d&blank=")
        self.assertEqual(body["body"], payload)
        self.assertEqual(body["authorization"], "Bearer test-token")
        self.assertEqual(body["api_key"], "test-api-key")

    async def test_read_only_workspaces_allow_queries_but_reject_mutations(self):
        allowed = (
            ("GET", "/health"),
            ("GET", "/documents"),
            ("POST", "/documents/paginated"),
            ("POST", "/query"),
            ("POST", "/query/stream"),
            ("POST", "/query/data"),
            ("POST", "/api/chat"),
            ("POST", "/api/generate"),
            ("POST", "/login"),
        )
        denied = (
            ("DELETE", "/documents"),
            ("POST", "/documents/upload"),
            ("POST", "/documents/text"),
            ("POST", "/documents/scan"),
            ("POST", "/documents/reprocess_failed"),
            ("POST", "/graph/entity"),
            ("PUT", "/config"),
            ("POST", "/query/extra"),
        )
        for method, path in allowed:
            with self.subTest(method=method, path=path, allowed=True):
                response = await self.client.request(method, "/workspaces/archive" + path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["workspace"], "archive")
        self.manager.leases.clear()
        self.requests.clear()
        for method, path in denied:
            with self.subTest(method=method, path=path, allowed=False):
                response = await self.client.request(method, "/workspaces/archive" + path)
                self.assertIn(response.status_code, (403, 409, 423), response.text)
        self.assertEqual(self.manager.leases, [])
        self.assertEqual(self.requests, [])
        writable = await self.client.post("/workspaces/research/documents/text", json={"text": "test"})
        self.assertEqual(writable.status_code, 200)
        self.assertEqual(writable.json()["workspace"], "research")

    async def test_concurrent_requests_cannot_change_each_others_workspace(self):
        reached = set()
        both = asyncio.Event()

        async def concurrent_upstream(request):
            workspace_id = self.manager.ids_by_host[request.url.netloc]
            reached.add(workspace_id)
            if len(reached) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=2)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"workspace": workspace_id})

        app = create_app(self.config, manager=self.manager, transport=httpx.MockTransport(concurrent_upstream))
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as client:
            first, second = await asyncio.gather(
                client.post("/workspaces/research/query", json={"query": "one"}),
                client.post("/query", headers={"LIGHTRAG-WORKSPACE": "default"}, json={"query": "two"}),
            )
        self.assertEqual(first.json()["workspace"], "research")
        self.assertEqual(second.json()["workspace"], "default")
        self.assertEqual(first.headers.get("LIGHTRAG-WORKSPACE"), "research")
        self.assertEqual(second.headers.get("LIGHTRAG-WORKSPACE"), "default")
        self.assertEqual(set(self.manager.leases), {"research", "default"})

    async def test_stream_preserves_response_and_workspace_lease_until_completion(self):
        async def stream_upstream(request):
            workspace_id = self.manager.ids_by_host[request.url.netloc]
            self.assertEqual(workspace_id, "research")
            self.assertIn("research", self.manager.active)
            return httpx.Response(
                200,
                stream=EventStream(lambda: self.assertIn("research", self.manager.active)),
                headers={"content-type": "application/x-ndjson"},
            )

        app = create_app(self.config, manager=self.manager, transport=httpx.MockTransport(stream_upstream))
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as client:
            async with client.stream("POST", "/workspaces/research/query/stream", json={"query": "test"}) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("LIGHTRAG-WORKSPACE"), "research")
                self.assertIn("application/x-ndjson", response.headers.get("content-type", ""))
                lines = [json.loads(line) async for line in response.aiter_lines() if line]
        self.assertEqual(lines, [{"response": "first", "done": False}, {"response": "second", "done": True}])
        self.assertEqual(self.manager.active, set())

    async def test_upstream_failure_releases_only_the_selected_workspace(self):
        async def failed_upstream(request):
            raise httpx.ConnectError("Simulated backend failure", request=request)

        app = create_app(self.config, manager=self.manager, transport=httpx.MockTransport(failed_upstream))
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://gateway.test",
        ) as client:
            response = await client.get("/workspaces/research/health")
        self.assertGreaterEqual(response.status_code, 500)
        self.assertEqual(self.manager.leases, ["research"])
        self.assertEqual(self.manager.active, set())

    async def test_eviction_requires_a_valid_idle_health_response(self):
        manager = WorkerManager(self.config)
        ready = asyncio.create_task(asyncio.sleep(0))
        await ready
        worker = Worker("research", 11001, SimpleNamespace(returncode=None),
                        "http://127.0.0.1:11001", ready=ready)

        class HealthClient:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url):
                return self.response

        cases = (
            (200, {}, False),
            (200, {"status": "healthy", "selected_workspace_id": "default"}, False),
            (503, {"status": "healthy", "selected_workspace_id": "research"}, False),
            (200, {"status": "healthy", "selected_workspace_id": "research", "pipeline_busy": True}, False),
            (200, {"status": "healthy", "selected_workspace_id": "research", "pipeline_active": True}, False),
            (200, {"status": "healthy", "selected_workspace_id": "research", "pipeline_busy": False}, False),
            (200, {"status": "healthy", "selected_workspace_id": "research", "pipeline_busy": False,
                   "pipeline_active": False, "llm_queue_status": {}, "embedding_queue_status": {},
                   "rerank_queue_status": {}}, True),
        )
        for status, payload, expected in cases:
            with self.subTest(status=status, payload=payload):
                response = httpx.Response(status, json=payload)
                with patch("gateway.httpx.AsyncClient", return_value=HealthClient(response)):
                    self.assertEqual(await manager._idle(worker), expected)

    async def test_corrupt_workspace_metadata_does_not_hide_other_workspaces(self):
        bad = self.root / "storage" / "archive" / "kv_store_doc_status.json"
        bad.write_text('["unexpected legacy metadata"]')
        before = bad.read_bytes()
        registry = {item["id"]: item for item in workspace_metadata(self.config)}
        self.assertFalse(registry["archive"]["available"])
        self.assertTrue(registry["research"]["available"])
        self.assertEqual(bad.read_bytes(), before)

    async def test_idle_sweeper_retries_when_graceful_close_is_still_in_progress(self):
        manager = WorkerManager(self.config)
        worker = Worker("research", 11001, SimpleNamespace(returncode=None),
                        "http://127.0.0.1:11001", last_used=0)
        manager.workers["research"] = worker
        manager._idle = AsyncMock(return_value=True)
        manager._terminate = AsyncMock(side_effect=[RuntimeError("still closing"), None])
        sleeps = 0

        async def advance_sweeps(delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 2:
                raise asyncio.CancelledError

        # Make the worker idle independently of the host's monotonic uptime.
        clock = SimpleNamespace(monotonic=lambda: self.config.idle_seconds + 1)
        with patch("gateway.time", clock), patch("gateway.asyncio.sleep", side_effect=advance_sweeps):
            await manager._sweep()
        self.assertEqual(manager._terminate.await_count, 2)

    async def test_shutdown_attempts_all_workers_even_after_sweeper_failure(self):
        manager = WorkerManager(self.config)
        manager.workers = {
            workspace_id: Worker(workspace_id, 11000 + index,
                                 SimpleNamespace(returncode=None),
                                 f"http://127.0.0.1:{11000 + index}")
            for index, workspace_id in enumerate(("research", "default"))
        }

        async def failed_sweeper():
            raise RuntimeError("old cleanup failure")

        manager.sweeper = asyncio.create_task(failed_sweeper())
        with self.assertRaises(RuntimeError):
            await manager.sweeper
        manager._terminate = AsyncMock()
        try:
            await manager.stop()
        except RuntimeError:
            pass  # Reporting the old failure is allowed after every close attempt.
        self.assertEqual(
            {call.args[0].workspace_id for call in manager._terminate.await_args_list},
            {"research", "default"},
        )


if __name__ == "__main__":
    unittest.main()
