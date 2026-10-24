#!/usr/bin/env python3
"""Reproduce PostgreSQL cold-start contention with the pinned graph adapter.

Creates an internal container network and a fresh PostgreSQL container, with no
published ports or host data mounts. Synthetic graph rows are the only corpus.
The unpatched adapter must fail under a held reader transaction; the packaged
startup hook must start independent processes under that same lock. Neither
the fixture nor the graph adapter uses model services or vector extensions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid


CHILD_CHECK = r'''
import asyncio, json, os
from lightrag.kg.shared_storage import initialize_share_data, finalize_share_data
from lightrag.kg.pgtable_impl import PGTableGraphStorage
from lightrag.kg.postgres_impl import ClientManager

async def main():
    if os.environ['SMOKE_HOOK'] == '1':
        from postgres_startup import install_postgres_graph_startup
        install_postgres_graph_startup(json.loads(os.environ['SMOKE_MANIFEST']))
    initialize_share_data()
    graph = PGTableGraphStorage(namespace='chunk_entity_relation', workspace='',
                                global_config={}, embedding_func=None)
    result = {'ok': False}
    try:
        await graph.initialize()
        result.update(ok=True, workspace=graph.workspace, node=await graph.get_node('A'))
    except Exception as error:
        result.update(error_type=type(error).__name__, error=str(error))
    finally:
        await graph.finalize()
        result['references_after_finalize'] = ClientManager._instances['ref_count']
        result['pool_released'] = ClientManager._instances['db'] is None
        finalize_share_data()
    print(json.dumps(result))

asyncio.run(main())
'''


RUNTIME_CHECKS = r'''
import asyncio, hashlib, json, os, sys, time
import asyncpg
from lightrag import __version__

checks = []
def check(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)
    print('PASS '+name, file=sys.stderr, flush=True)

identities = ('startup-a', 'startup_b', 'adjacent')
workspaces = {name: 'ws_'+hashlib.sha256(name.encode()).hexdigest()[:32] for name in identities}
connection_args = dict(host='postgres', database='startup_smoke', user='postgres',
                       password='synthetic-startup-smoke', timeout=10, command_timeout=15)

async def child(name, hook):
    env = os.environ.copy()
    env.update(POSTGRES_WORKSPACE=workspaces[name], SMOKE_HOOK=str(int(hook)),
               SMOKE_MANIFEST=json.dumps({'workspace_id': name, 'postgres_workspace': workspaces[name],
                                         'database': 'startup_smoke'}))
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', CHILD_CHECK,
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError('Synthetic child failed: '+stderr.decode(errors='replace')[-4000:])
    try:
        return json.loads(stdout.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError('Synthetic child returned no result: '+stderr.decode(errors='replace')[-4000:])

async def fingerprint(connection):
    tables = await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    contents = {}
    for row in tables:
        name = row['tablename']
        quoted = '"'+name.replace('"', '""')+'"'
        contents[name] = await connection.fetchval(
            'SELECT coalesce(jsonb_agg(value ORDER BY value::text),\'[]\'::jsonb)::text '
            'FROM (SELECT to_jsonb(t) value FROM '+quoted+' t) rows')
    columns = await connection.fetch("""SELECT table_name,column_name,data_type,is_nullable,column_default
        FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position""")
    constraints = await connection.fetch("""SELECT c.conname, c.conrelid::regclass::text relation,
        pg_get_constraintdef(c.oid) definition, c.convalidated FROM pg_constraint c
        JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' ORDER BY 2,1""")
    indexes = await connection.fetch("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY 1,2")
    payload = dict(contents=contents, columns=[dict(r) for r in columns],
                   constraints=[dict(r) for r in constraints], indexes=[dict(r) for r in indexes])
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

async def main():
    check('exact pinned LightRAG adapter', __version__ == '1.5.7')
    # Bootstrap is explicit, before reader locks or hook installation. This is
    # deliberately the unmodified upstream adapter, not a copied SQL fixture.
    bootstrap = await child('startup-a', False)
    check('upstream bootstrap succeeds on the disposable database', bootstrap['ok'])
    admin = await asyncpg.connect(**connection_args)
    reader = await asyncpg.connect(**connection_args)
    try:
        for name, workspace in workspaces.items():
            for node in ('A', 'Z'):
                await admin.execute("""INSERT INTO lightrag_graph_nodes(workspace,namespace,id,properties)
                    VALUES($1,'chunk_entity_relation',$2,$3::jsonb)""", workspace, node,
                    json.dumps({'entity_id': node, 'description': name+' Δ данные', 'nested': [1,False,None]}))
            await admin.execute("""INSERT INTO lightrag_graph_edges(workspace,namespace,src_id,tgt_id,properties)
                VALUES($1,'chunk_entity_relation','A','Z',$2::jsonb)""", workspace,
                json.dumps({'description': name, 'weight': 1.75}))
        baseline = await fingerprint(admin)
        transaction = reader.transaction(readonly=True)
        await transaction.start()
        try:
            await reader.fetch('SELECT * FROM lightrag_graph_nodes LIMIT 1')
            await reader.fetch('SELECT * FROM lightrag_graph_edges LIMIT 1')
            reader_pid = await reader.fetchval('SELECT pg_backend_pid()')
            held = await admin.fetchval("""SELECT count(*) FROM pg_locks WHERE pid=$1 AND granted
                AND mode='AccessShareLock' AND relation IN
                ('lightrag_graph_nodes'::regclass,'lightrag_graph_edges'::regclass)""", reader_pid)
            check('reader holds both shared graph table locks', held == 2)
            old = await child('startup-a', False)
            check('unpatched cold start reproduces lock timeout', not old['ok'] and
                  old.get('error_type') == 'LockNotAvailableError' and 'lock timeout' in old.get('error',''))
            check('failed original start preserves all table contents and schema', await fingerprint(admin) == baseline)
            for cycle in range(2):
                # Separate interpreters create separate native pools and shared
                # storage locks: this cannot pass merely through process-local serialization.
                results = await asyncio.gather(*(child(name, True) for name in identities))
                for name, result in zip(identities, results, strict=True):
                    if not result['ok']:
                        raise AssertionError('Fixed cold start failed for '+name+': '+json.dumps(result))
                    check('concurrent cold cycle '+str(cycle+1)+' selects '+name,
                          result['ok'] and result['workspace'] == workspaces[name] and
                          result['node']['description'] == name+' Δ данные')
                    check('concurrent cold cycle '+str(cycle+1)+' releases '+name,
                          result['references_after_finalize'] == 0 and result['pool_released'])
            check('reader transaction is still open after successful cold starts',
                  await reader.fetchval('SELECT pg_backend_pid()') == reader_pid and reader.is_in_transaction())
            check('cold reads preserve every table row, property, timestamp and schema', await fingerprint(admin) == baseline)
        finally:
            await transaction.rollback()

        async def reject(name):
            before = await fingerprint(admin)
            result = await child('startup-a', True)
            check(name+' fails closed', not result['ok'] and result.get('error_type') in ('RuntimeError','ValueError'))
            check(name+' releases the failed client', result['pool_released'] and result['references_after_finalize'] == 0)
            check(name+' is not repaired or mutated at startup', await fingerprint(admin) == before)

        await admin.execute('ALTER TABLE lightrag_graph_edges DROP CONSTRAINT fk_lightrag_graph_edges_src')
        await reject('missing endpoint foreign key')
        await admin.execute("""ALTER TABLE lightrag_graph_edges ADD CONSTRAINT fk_lightrag_graph_edges_src
            FOREIGN KEY(workspace,namespace,src_id) REFERENCES lightrag_graph_nodes(workspace,namespace,id) ON DELETE CASCADE""")
        await admin.execute('ALTER TABLE lightrag_graph_nodes ALTER COLUMN properties DROP NOT NULL')
        await reject('incompatible nullable properties column')
        await admin.execute('ALTER TABLE lightrag_graph_nodes ALTER COLUMN properties SET NOT NULL')
        await admin.execute("""INSERT INTO lightrag_graph_edges(workspace,namespace,src_id,tgt_id,properties)
            VALUES($1,'chunk_entity_relation','Z','A','{"legacy":"preserve exactly"}'::jsonb)""", workspaces['startup-a'])
        await reject('legacy noncanonical edge')
        await admin.execute("DELETE FROM lightrag_graph_edges WHERE workspace=$1 AND src_id='Z' AND tgt_id='A'", workspaces['startup-a'])
        await admin.execute('ALTER TABLE lightrag_graph_edges RENAME TO saved_graph_edges')
        await reject('missing graph relation')
        await admin.execute('ALTER TABLE saved_graph_edges RENAME TO lightrag_graph_edges')
        check('fixture restores the original complete fingerprint', await fingerprint(admin) == baseline)
        recovered = await child('startup-a', True)
        check('valid workspace starts after explicit fixture restoration', recovered['ok'] and recovered['pool_released'])
        print(json.dumps({'passed':True, 'checks':checks, 'provider_calls':0,
                          'baseline_sha256':baseline, 'independent_cold_starts':6,
                          'original_failure':old['error_type'], 'postgres_version':await admin.fetchval('SHOW server_version')}))
    finally:
        await reader.close()
        await admin.close()

asyncio.run(main())
'''


def smoke(engine: str, image: str, postgres_image: str, module_file: Path | None) -> dict:
    token = uuid.uuid4().hex[:12]
    network = "lightrag-startup-smoke-" + token
    database, client = network + "-db", network + "-client"
    network_created = database_created = False
    started = time.monotonic()
    result = {"passed": False, "engine": engine, "image": image,
              "postgres_image": postgres_image, "network": "owned internal network; no published ports",
              "data_scope": "synthetic ephemeral PostgreSQL only", "provider_calls": 0,
              "hook_source": "explicit local module" if module_file else "packaged image module"}

    def run(*arguments: str, timeout: float = 120, program: str | None = None) -> str:
        process = subprocess.run([engine, *arguments], input=program, capture_output=True,
                                 text=True, timeout=timeout)
        if process.returncode:
            raise RuntimeError(f"{engine} {arguments[0]} failed:\n{process.stderr[-6000:]}")
        return process.stdout.strip()

    try:
        result["image_id"] = run("image", "inspect", image, "--format", "{{.Id}}")
        result["postgres_image_id"] = run("image", "inspect", postgres_image, "--format", "{{.Id}}")
        run("network", "create", "--internal", network)
        network_created = True
        run("run", "-d", "--name", database, "--network", network, "--network-alias", "postgres",
            "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
            "--tmpfs", "/var/lib/postgresql:rw,nosuid,nodev,size=256m",
            "-e", "PGDATA=/var/lib/postgresql/smoke-data",
            "-e", "POSTGRES_DB=startup_smoke", "-e", "POSTGRES_PASSWORD=synthetic-startup-smoke",
            postgres_image, "-c", "lock_timeout=500ms", "-c", "statement_timeout=15s")
        database_created = True
        deadline = time.monotonic() + 45
        while True:
            try:
                run("exec", database, "pg_isready", "-U", "postgres", "-d", "startup_smoke", timeout=10)
                break
            except RuntimeError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Disposable PostgreSQL did not become ready")
                time.sleep(.25)
        program = "CHILD_CHECK = " + repr(CHILD_CHECK) + "\n" + RUNTIME_CHECKS
        if module_file:
            program = ("import os, pathlib, sys\n"
                       "root=pathlib.Path('/tmp/startup-hook'); root.mkdir()\n"
                       f"(root/'postgres_startup.py').write_text({module_file.read_text()!r})\n"
                       "sys.path.insert(0,str(root)); os.environ['PYTHONPATH']=str(root)+':'+os.environ.get('PYTHONPATH','')\n"
                       + program)
        environment = {"POSTGRES_HOST": "postgres", "POSTGRES_PORT": "5432", "POSTGRES_USER": "postgres",
                       "POSTGRES_PASSWORD": "synthetic-startup-smoke", "POSTGRES_DATABASE": "startup_smoke",
                       "POSTGRES_MAX_CONNECTIONS": "2", "POSTGRES_SSL_MODE": "disable",
                       "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"}
        arguments = ["run", "--rm", "-i", "--name", client, "--network", network,
                     "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
                     "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m"]
        for key, value in environment.items():
            arguments.extend(("-e", key + "=" + value))
        output = run(*arguments, "--entrypoint", "python", image, "-", timeout=240, program=program)
        result.update(json.loads(output.splitlines()[-1]))
    except (AssertionError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        # Remove only names generated by this invocation, including a client
        # that outlived a timed-out CLI. Never prune or touch caller resources.
        subprocess.run([engine, "rm", "-f", client], capture_output=True, timeout=30)
        if database_created:
            subprocess.run([engine, "rm", "-f", database], capture_output=True, timeout=30)
        if network_created:
            cleanup = subprocess.run([engine, "network", "rm", network], capture_output=True, text=True, timeout=30)
            if cleanup.returncode:
                result["passed"] = False
                result["cleanup_error"] = cleanup.stderr[-2000:]
        result["seconds"] = round(time.monotonic() - started, 2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument("--image", required=True)
    parser.add_argument("--postgres-image", default="postgres:18.3-bookworm")
    parser.add_argument("--module-file", type=Path, help="Test a local hook against an older pinned image")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = smoke(args.engine, args.image, args.postgres_image, args.module_file)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
