"""Follow Swagger's actual links without inheriting a workspace header."""
import json
import re
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway import Config, create_app


HTML = b'''<!doctype html><html data-lightrag-docs-dark="1"><head>
<link rel="stylesheet" href="/static/swagger-ui/swagger-ui.css">
<link rel="icon" href='/static/swagger-ui/favicon-32x32.png'>
</head><body><script src="/static/swagger-ui/swagger-ui-bundle.js"></script>
<script>const ui = SwaggerUIBundle({url: '/openapi.json', persistAuthorization: true,
oauth2RedirectUrl: window.location.origin + '/docs/oauth2-redirect'});
window.addEventListener('message', function () { /* upstream theme listener */ });
</script></body></html>'''


class Manager:
    def __init__(self):
        self.active = 0
        self.releases = 0
        self.backends = {"research": "http://127.0.0.1:11001", "default": "http://127.0.0.1:11000"}

    async def inventory(self):
        return [{"id": name, "available": True, "readOnly": False} for name in self.backends]

    async def start(self):
        pass

    async def stop(self):
        pass

    @asynccontextmanager
    async def lease(self, workspace_id):
        self.active += 1
        try:
            yield SimpleNamespace(base_url=self.backends[workspace_id])
        finally:
            self.active -= 1
            self.releases += 1


class Stream(httpx.AsyncByteStream):
    def __init__(self, content=HTML, fail=False):
        self.content = content
        self.fail = fail
        self.closed = 0

    async def __aiter__(self):
        yield self.content[:17]
        if self.fail:
            raise httpx.ReadError("simulated docs read failure")
        yield self.content[17:]

    async def aclose(self):
        self.closed += 1


class SwaggerScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = Config(root / "storage", root / "inputs", root / "runtime", root / "static")

    async def test_docs_links_schema_assets_and_oauth_keep_the_selected_workspace(self):
        manager = Manager()
        seen = []
        streams = []

        async def upstream(request):
            selected = next(name for name, base in manager.backends.items()
                            if request.url.netloc == httpx.URL(base).netloc)
            seen.append((selected, request))
            if request.url.path == "/docs":
                self.assertEqual(request.url.query, b"theme=dark")
                self.assertEqual(request.headers["authorization"], "Bearer synthetic-test-token")
                stream = Stream()
                streams.append(stream)
                return httpx.Response(200, stream=stream, headers={
                    "content-type": "text/html; charset=utf-8", "content-length": str(len(HTML)),
                    "content-security-policy": "script-src 'self'", "x-theme": "dark",
                    "etag": '"upstream-bytes"', "content-md5": "upstream-bytes", "digest": "upstream-bytes"})
            if request.url.path == "/openapi.json":
                return httpx.Response(200, json={"openapi": "3.1.0", "selected_workspace_id": selected,
                                               "servers": [{"url": "/workspaces/" + selected}]})
            return httpx.Response(200, json={"selected_workspace_id": selected, "path": request.url.path})

        app = create_app(self.config, manager=manager, transport=httpx.MockTransport(upstream))
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
            for path, selector, selected in (
                ("/workspaces/research/docs", {}, "research"),
                ("/docs", {"LIGHTRAG-WORKSPACE": "research"}, "research"),
                ("/workspaces/default/docs", {}, "default"),
                ("/docs", {"LIGHTRAG-WORKSPACE": "default"}, "default"),
            ):
                with self.subTest(path=path, selector=selector):
                    response = await client.get(path + "?theme=dark", headers={
                        **selector, "Authorization": "Bearer synthetic-test-token"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["LIGHTRAG-WORKSPACE"], selected)
                    self.assertEqual(response.headers["content-security-policy"], "script-src 'self'")
                    self.assertEqual(response.headers["x-theme"], "dark")
                    self.assertEqual(int(response.headers["content-length"]), len(response.content))
                    for name in ("etag", "content-md5", "digest"):
                        self.assertNotIn(name, response.headers)
                    self.assertIn('data-lightrag-docs-dark="1"', response.text)
                    self.assertIn("persistAuthorization: true", response.text)
                    self.assertIn("/* upstream theme listener */", response.text)
                    prefix = "/workspaces/" + selected
                    schema_path = re.search(r"url: '([^']+)'", response.text).group(1)
                    self.assertEqual(schema_path, prefix + "/openapi.json")
                    # A browser follows these URLs without the original selector header.
                    schema = await client.get(schema_path)
                    self.assertEqual(schema.json()["selected_workspace_id"], selected)
                    self.assertEqual(schema.json()["servers"], [{"url": prefix}])
                    for asset in ("swagger-ui.css", "favicon-32x32.png", "swagger-ui-bundle.js"):
                        scoped = prefix + "/static/swagger-ui/" + asset
                        self.assertIn(scoped, response.text)
                        self.assertEqual((await client.get(scoped)).json()["selected_workspace_id"], selected)
                    redirect = prefix + "/docs/oauth2-redirect"
                    self.assertIn(redirect, response.text)
                    self.assertEqual((await client.get(redirect)).json()["selected_workspace_id"], selected)
                    self.assertEqual(manager.active, 0)
                    self.assertEqual(streams[-1].closed, 1)
        self.assertEqual(manager.releases, len(seen))

    async def test_docs_disabled_auth_failures_and_non_html_are_unchanged(self):
        for status, content_type in ((401, "text/html"), (403, "text/html"),
                                     (404, "text/html"), (200, "application/json")):
            with self.subTest(status=status, content_type=content_type):
                manager = Manager()
                stream = Stream()
                app = create_app(self.config, manager=manager, transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, stream=stream, headers={
                        "content-type": content_type, "www-authenticate": "Bearer", "etag": '"original"'})))
                async with app.router.lifespan_context(app), httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
                    response = await client.get("/workspaces/research/docs")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content, HTML)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                self.assertEqual(response.headers["etag"], '"original"')
                self.assertEqual(manager.active, 0)
                self.assertEqual(manager.releases, 1)
                self.assertEqual(stream.closed, 1)

    async def test_buffer_failure_closes_upstream_and_releases_lease(self):
        manager = Manager()
        stream = Stream(fail=True)
        app = create_app(self.config, manager=manager, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, headers={"content-type": "text/html"})))
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://gateway.test") as client:
            response = await client.get("/workspaces/research/docs")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(manager.active, 0)
        self.assertEqual(manager.releases, 1)
        self.assertEqual(stream.closed, 1)


if __name__ == "__main__":
    unittest.main()
