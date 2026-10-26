#!/usr/bin/env python3
"""Inside-container, offline API-key contract check. Synthetic fixtures only.

Run the existing image with --network none --memory 4g --cpus 2,
--pids-limit 512, an owned empty bind/volume at /auth-fixture, this file
mounted read-only, and --entrypoint python. No ports or production mounts.
The shared key is generated in memory, passed only to this container's child
gateway, and never included in the receipt. Provider hosts are loopback port 1.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = Path('/auth-fixture')
WORKSPACES = ('default', 'research')
RECEIPT = {'passed': False, 'network_required': False,
           'production_mounts_required': False, 'checks': []}


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    RECEIPT['checks'].append(label)
    print('PASS ' + label, flush=True)


def request(path, headers=None, timeout=260):
    req = urllib.request.Request('http://127.0.0.1:9621' + path,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def body(result):
    return json.loads(result[2])


def hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob('*')) if path.is_file()}


def main():
    started = time.monotonic()
    process = None
    log = None
    owned_empty_fixture = False
    try:
        if not ROOT.is_dir() or any(ROOT.iterdir()):
            raise RuntimeError('/auth-fixture must be an owned EMPTY directory')
        owned_empty_fixture = True
        key = secrets.token_urlsafe(48)
        env = os.environ.copy()
        # Eliminate inherited connection settings before defining the entirely
        # synthetic file-storage process. No real provider/DB value is consumed.
        for name in list(env):
            if any(part in name for part in ('POSTGRES', 'NEO4J', 'MILVUS', 'QDRANT',
                                             'REDIS', 'MONGO', 'LLM_', 'EMBEDDING_',
                                             'LIGHTRAG_', 'AUTH_')) or name == 'TOKEN_SECRET':
                env.pop(name)
        env.update({
            'WORKING_DIR': str(ROOT / 'storage'), 'INPUT_DIR': str(ROOT / 'inputs'),
            'LIGHTRAG_MULTIWORKSPACE_RUNTIME': str(ROOT / 'runtime'),
            'LIGHTRAG_MULTIWORKSPACE_STATIC': '/opt/lightrag-multiworkspace/webui',
            'LIGHTRAG_DEFAULT_WORKSPACE': 'default',
            'LIGHTRAG_MAX_ACTIVE_WORKSPACES': '2',
            'LIGHTRAG_WORKSPACE_STARTUP_TIMEOUT': '240',
            'LIGHTRAG_STORAGE_BACKEND': 'file', 'WORKSPACE': '',
            'LIGHTRAG_KV_STORAGE': 'JsonKVStorage',
            'LIGHTRAG_DOC_STATUS_STORAGE': 'JsonDocStatusStorage',
            'LIGHTRAG_VECTOR_STORAGE': 'NanoVectorDBStorage',
            'LIGHTRAG_GRAPH_STORAGE': 'NetworkXStorage',
            'LLM_BINDING': 'openai', 'LLM_BINDING_HOST': 'http://127.0.0.1:1/v1',
            'LLM_BINDING_API_KEY': 'synthetic-offline', 'LLM_MODEL': 'synthetic-offline',
            'EMBEDDING_BINDING': 'openai', 'EMBEDDING_BINDING_HOST': 'http://127.0.0.1:1/v1',
            'EMBEDDING_BINDING_API_KEY': 'synthetic-offline',
            'EMBEDDING_MODEL': 'synthetic-offline', 'EMBEDDING_DIM': '12',
            'EMBEDDING_TOKEN_LIMIT': '8192', 'MAX_ASYNC_LLM': '4',
            'EMBEDDING_FUNC_MAX_ASYNC': '4', 'MAX_ASYNC_RERANK': '4',
            'MAX_PARALLEL_INSERT': '2', 'RERANK_BINDING': 'null',
            'LIGHTRAG_API_KEY': key, 'WHITELIST_PATHS': '/health',
            'AUTH_ACCOUNTS': '', 'TOKEN_SECRET': secrets.token_urlsafe(48),
            'PYTHONPATH': '/opt/lightrag-multiworkspace:/app',
        })
        for workspace in WORKSPACES:
            initialized = subprocess.run(
                [sys.executable, '/opt/lightrag-multiworkspace/scripts/init_workspace.py', workspace],
                env=env, capture_output=True, text=True, timeout=30, check=False)
            check('isolated fixture initialized: ' + workspace, initialized.returncode == 0)
            directory = ROOT / 'storage' / workspace
            for name in ('kv_store_doc_status.json', 'kv_store_full_docs.json',
                         'kv_store_text_chunks.json', 'kv_store_llm_response_cache.json'):
                (directory / name).write_text('{}\n')
            for name in ('vdb_entities.json', 'vdb_relationships.json', 'vdb_chunks.json'):
                (directory / name).write_text(json.dumps({'embedding_dim': 12, 'data': [], 'matrix': ''}) + '\n')
        before = hashes(ROOT / 'storage')
        log = (ROOT / 'gateway.log').open('wb')
        process = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'gateway:app', '--host', '127.0.0.1',
             '--port', '9621', '--workers', '1'], env=env, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 45
        while True:
            if process.poll() is not None:
                raise RuntimeError('isolated gateway exited during startup')
            try:
                if request('/livez', timeout=2)[0] == 200:
                    break
            except (OSError, urllib.error.URLError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError('isolated gateway readiness exceeded 45 seconds')
            time.sleep(.5)
        good = {'X-API-Key': key}
        bad = {'X-API-Key': 'synthetic-incorrect-key'}
        inventory = body(request('/workspaces'))
        check('public discovery lists exactly two synthetic fixtures',
              sorted(row['id'] for row in inventory['workspaces']) == list(WORKSPACES))
        check('discovery does not wake workers', all(not row['loaded'] for row in inventory['workspaces']))
        for path in ('/livez', '/readyz', '/workspaces/default/webui/', '/workspaces/default/workspace/'):
            check('public shell/liveness: ' + path, request(path)[0] == 200)
        for path in ('/maintenance', '/workspaces/default/status'):
            check('management missing key denied: ' + path, request(path)[0] == 401)
            check('management wrong key denied: ' + path, request(path, bad)[0] == 401)
            check('management correct key accepted: ' + path, request(path, good)[0] == 200)
        for workspace in WORKSPACES:
            prefix = '/workspaces/' + workspace
            public_health = request(prefix + '/health')
            check('public limited health: ' + workspace,
                  public_health[0] == 200 and body(public_health).get('status') == 'healthy'
                  and 'configuration' not in body(public_health))
            auth_status = request(prefix + '/auth-status')
            guest_token = body(auth_status).get('access_token')
            check('guest UI bootstrap remains available: ' + workspace,
                  auth_status[0] == 200 and bool(guest_token)
                  and body(auth_status).get('auth_configured') is False)
            guest = {'Authorization': 'Bearer ' + guest_token}
            for route in ('/auth/verify', '/documents/status_counts', '/graph/label/list',
                          '/api/tags', '/api/version'):
                path = prefix + route
                check('missing key denied: ' + path, request(path)[0] == 403)
                check('wrong key denied: ' + path, request(path, bad)[0] == 403)
                check('guest token alone denied: ' + path, request(path, guest)[0] == 403)
                accepted = request(path, good)
                check('correct key accepted: ' + path,
                      accepted[0] == 200 and accepted[1].get('x-lightrag-workspace',
                          accepted[1].get('X-LightRAG-Workspace')) == workspace)
            check('browser guest token plus API key accepted: ' + workspace,
                  request(prefix + '/auth/verify', {**guest, **good})[0] == 200)
            check('management guest token alone denied: ' + workspace,
                  request(prefix + '/status', guest)[0] == 401)
            selected = request('/auth/verify', {**good, 'LIGHTRAG-WORKSPACE': workspace})
            check('header workspace selection forwards key: ' + workspace,
                  selected[0] == 200 and selected[1].get('x-lightrag-workspace',
                      selected[1].get('X-LightRAG-Workspace')) == workspace)
            docs = request(prefix + '/docs')
            check('scoped Swagger stays available: ' + workspace,
                  docs[0] == 200 and (prefix + '/openapi.json').encode() in docs[2])
            schema = body(request(prefix + '/openapi.json'))
            schemes = schema.get('components', {}).get('securitySchemes', {})
            check('Swagger advertises X-API-Key: ' + workspace,
                  any(s.get('type') == 'apiKey' and s.get('name') == 'X-API-Key'
                      for s in schemes.values()))
        check('maintenance owner token does not bypass key',
              request('/maintenance', {'X-Maintenance-Token': 'synthetic-owner-only'})[0] == 401)
        after = hashes(ROOT / 'storage')
        check('all original synthetic corpus file bytes preserved',
              all(after.get(path) == digest for path, digest in before.items()))
        check('Docker liveness contract remains unauthenticated', request('/livez')[0] == 200)
        RECEIPT['passed'] = True
    except Exception as error:
        # Descriptions and exception classes only; no response dumps or env.
        RECEIPT['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
                RECEIPT['passed'] = False
                RECEIPT['shutdown_error'] = 'isolated gateway exceeded graceful shutdown'
        if log is not None:
            log.close()
        if process is not None:
            # Uvicorn re-raises the captured SIGTERM after its lifespan shutdown.
            # Require the completion marker as well as the expected exit code.
            RECEIPT['gateway_exit_code'] = process.returncode
            shutdown_complete = b'Application shutdown complete.' in (ROOT / 'gateway.log').read_bytes()
            if process.returncode not in (0, -signal.SIGTERM) or not shutdown_complete:
                RECEIPT['passed'] = False
                RECEIPT['shutdown_error'] = 'isolated gateway did not shut down gracefully'
        RECEIPT['seconds'] = round(time.monotonic() - started, 2)
        RECEIPT['gateway_stopped'] = process is None or process.poll() is not None
        if owned_empty_fixture:
            (ROOT / 'receipt.json').write_text(json.dumps(RECEIPT, indent=2) + '\n')
            (ROOT / 'receipt.json').chmod(0o600)
        print(json.dumps(RECEIPT, indent=2), flush=True)
    return 0 if RECEIPT['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
