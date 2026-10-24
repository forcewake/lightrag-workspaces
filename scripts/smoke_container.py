#!/usr/bin/env python3
"""Offline container smoke check using synthetic data and an owned temporary volume.

No published port, real provider, existing data directory or existing container
is used. Build the image first, then pass --engine docker (or podman).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid


HTTP_REQUEST = r'''
import json, sys, urllib.error, urllib.request
path, headers, timeout = sys.argv[1], json.loads(sys.argv[2]), float(sys.argv[3])
try:
    with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:9621" + path, headers=headers), timeout=timeout) as response:
        result = {"status": response.status, "headers": dict(response.headers), "body": response.read().decode()}
except urllib.error.HTTPError as error:
    result = {"status": error.code, "headers": dict(error.headers), "body": error.read().decode()}
print(json.dumps(result))
'''
SEED = r'''
import json, os
from pathlib import Path
root = Path(os.environ["WORKING_DIR"])
for name in ("default", "research"):
    folder = root / name
    for filename in ("kv_store_doc_status.json", "kv_store_full_docs.json", "kv_store_text_chunks.json", "kv_store_llm_response_cache.json"):
        (folder / filename).write_text("{}\n")
    for filename in ("vdb_entities.json", "vdb_relationships.json", "vdb_chunks.json"):
        (folder / filename).write_text(json.dumps({"embedding_dim": 12, "data": [], "matrix": ""}) + "\n")
'''
HASHES = r'''
import hashlib, json, os
from pathlib import Path
root = Path(os.environ["WORKING_DIR"])
print(json.dumps({str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(root.rglob("*")) if path.is_file()}))
'''


def smoke(engine: str, image: str, memory: str, startup_timeout: float) -> dict:
    token = uuid.uuid4().hex[:12]
    name = "lightrag-workspaces-smoke-" + token
    volume = name + "-data"
    result = {"passed": False, "image": image, "engine": engine,
              "network": "none", "provider_calls": "not exercised", "checks": []}
    container_created = volume_created = False
    started_at = time.monotonic()

    def run(*arguments: str, timeout: float = 300) -> str:
        process = subprocess.run([engine, *arguments], capture_output=True, text=True, timeout=timeout)
        if process.returncode:
            raise RuntimeError(f"{engine} {arguments[0]} failed: {process.stderr[-3000:].strip()}")
        return process.stdout.strip()

    def check(description: str, condition: bool) -> None:
        if not condition:
            raise AssertionError(description)
        result["checks"].append(description)
        print("PASS " + description, flush=True)

    def request(path: str, headers: dict | None = None) -> dict:
        return json.loads(run("exec", name, "python", "-c", HTTP_REQUEST, path,
                              json.dumps(headers or {}), str(startup_timeout), timeout=startup_timeout + 20))

    def ready() -> dict:
        deadline = time.monotonic() + 45
        while True:
            try:
                response = request("/workspaces")
                if response["status"] == 200:
                    return json.loads(response["body"])
            except (RuntimeError, json.JSONDecodeError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("Gateway did not become ready within 45 seconds")
            time.sleep(.5)

    def hashes() -> dict:
        return json.loads(run("exec", name, "python", "-c", HASHES))

    env = {
        "WORKING_DIR": "/smoke-data/rag_storage", "INPUT_DIR": "/smoke-data/inputs",
        "LIGHTRAG_MULTIWORKSPACE_RUNTIME": "/smoke-data/runtime",
        "LIGHTRAG_DEFAULT_WORKSPACE": "default", "LIGHTRAG_MAX_ACTIVE_WORKSPACES": "2",
        "LLM_BINDING": "openai", "LLM_BINDING_HOST": "http://127.0.0.1:1/v1",
        "LLM_BINDING_API_KEY": "synthetic-offline", "LLM_MODEL": "synthetic-offline",
        "EMBEDDING_BINDING": "openai", "EMBEDDING_BINDING_HOST": "http://127.0.0.1:1/v1",
        "EMBEDDING_BINDING_API_KEY": "synthetic-offline", "EMBEDDING_MODEL": "synthetic-offline",
        "EMBEDDING_DIM": "12", "EMBEDDING_TOKEN_LIMIT": "8192",
        "MAX_ASYNC_LLM": "4", "EMBEDDING_FUNC_MAX_ASYNC": "4", "MAX_ASYNC_RERANK": "4",
        "MAX_PARALLEL_INSERT": "2", "RERANK_BINDING": "null",
    }
    common = ["--network", "none", "--memory", memory, "--cpus", "2", "--pids-limit", "512",
              "-v", f"{volume}:/smoke-data"]
    for key, value in env.items():
        common.extend(("-e", f"{key}={value}"))
    try:
        result["image_id"] = run("image", "inspect", image, "--format", "{{.Id}}")
        run("volume", "create", volume)
        volume_created = True
        for workspace in ("default", "research"):
            output = run("run", "--rm", *common, "--entrypoint", "python", image,
                         "/opt/lightrag-multiworkspace/scripts/init_workspace.py", workspace)
            check("explicit initialization: " + workspace, output == "Created empty workspace: " + workspace)
        run("run", "--rm", *common, "--entrypoint", "python", image, "-c", SEED)
        run("run", "-d", "--name", name, *common, image)
        container_created = True
        inventory = ready()
        check("discovery lists exactly the initialized workspaces",
              inventory["defaultWorkspace"] == "default" and
              sorted(item["id"] for item in inventory["workspaces"]) == ["default", "research"])
        check("discovery starts no workers", all(not item["loaded"] for item in inventory["workspaces"]))
        before = hashes()
        corpus_files = {name: value for name, value in before.items() if not name.startswith(".workspace-management/")}
        management_files = {name for name in before if name.startswith(".workspace-management/")}
        check("native fixtures exist before worker startup", len(corpus_files) == 14)
        check("gateway adds only its owned control lock before explicit management", management_files == {".workspace-management/gateway.lock"})
        for path in ("/workspaces/research/webui/", "/workspaces/research/workspace/"):
            response = request(path)
            check("workspace configuration: " + path,
                  response["status"] == 200 and '"workspaceId": "research"' in response["body"] and
                  '"defaultWorkspace": "default"' in response["body"])
        for path, headers, status in (
            ("/workspaces/missing/health", {}, 404),
            ("/workspaces/research/health", {"LIGHTRAG-WORKSPACE": "default"}, 400),
            ("/health", {"LIGHTRAG-WORKSPACE": "../escape"}, 400),
        ):
            check("invalid selector returns " + str(status) + ": " + path, request(path, headers)["status"] == status)
        for workspace in ("default", "research"):
            for path, headers in ((f"/workspaces/{workspace}/health", {}),
                                  ("/health", {"LIGHTRAG-WORKSPACE": workspace})):
                response = request(path, headers)
                health = json.loads(response["body"])
                check("real worker selection: " + workspace + " " + path,
                      response["status"] == 200 and health.get("status") == "healthy" and
                      health.get("selected_workspace_id") == workspace and
                      health.get("startup_compatibility_writes") is False)
            response = request(f"/workspaces/{workspace}/documents/status_counts")
            check("document status read: " + workspace, response["status"] == 200)
            response = request(f"/workspaces/{workspace}/docs")
            check("scoped Swagger: " + workspace, response["status"] == 200 and
                  f"/workspaces/{workspace}/openapi.json" in response["body"])
            response = request(f"/workspaces/{workspace}/openapi.json")
            schema = json.loads(response["body"])
            check("scoped schema server: " + workspace, response["status"] == 200 and
                  {"url": f"/workspaces/{workspace}"} in schema.get("servers", []) and
                  "/query" in schema.get("paths", {}))
        after = hashes()
        check("cold reads preserve original fixture bytes", all(after.get(path) == digest for path, digest in before.items()))
        check("unknown request did not create a workspace",
              sorted(item["id"] for item in ready()["workspaces"]) == ["default", "research"])
        run("restart", "--time", "120", name, timeout=180)
        check("restart preserves workspace discovery", ready()["defaultWorkspace"] == "default")
        response = request("/health")
        check("worker reopens after restart", response["status"] == 200 and
              json.loads(response["body"]).get("selected_workspace_id") == "default")
        after = hashes()
        check("restart preserves original fixture bytes", all(after.get(path) == digest for path, digest in before.items()))
        output = run("exec", name, "python", "/opt/lightrag-multiworkspace/scripts/init_workspace.py", "default")
        check("initialization preserves existing data", output == "Kept existing workspace unchanged: default")
        run("stop", "--time", "120", name, timeout=180)
        state = json.loads(run("inspect", name))[0]["State"]
        check("clean shutdown without OOM", state["ExitCode"] == 0 and not state.get("OOMKilled", False))
        result["passed"] = True
    except (AssertionError, RuntimeError, OSError, subprocess.SubprocessError, KeyError, ValueError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
        if container_created:
            logs = subprocess.run([engine, "logs", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            print(logs.stdout[-6000:], file=sys.stderr)
    finally:
        try:
            if container_created:
                run("rm", "-f", name)
            if volume_created:
                run("volume", "rm", volume)
            result["temporary_resources_removed"] = True
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            result["cleanup_error"] = str(error)
            result["passed"] = False
        result["seconds"] = round(time.monotonic() - started_at, 2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument("--image", required=True, help="Already-built image to verify")
    parser.add_argument("--memory", default="4g", help="Memory ceiling for owned temporary containers")
    parser.add_argument("--startup-timeout", type=float, default=260)
    parser.add_argument("--output", type=Path, help="Optional JSON receipt")
    args = parser.parse_args()
    if args.startup_timeout <= 0:
        parser.error("--startup-timeout must be positive")
    result = smoke(args.engine, args.image, args.memory, args.startup_timeout)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
