#!/usr/bin/env python3
"""Explicit, private Docker/PostgreSQL backup, restore drill and application update.

Run on the Docker host (or an operator container with the same absolute paths).
No operation runs on import. No command restores into the source database.
Only the standard library, Docker CLI, GNU cp and curl are required.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.parse
import xml.etree.ElementTree as ET


OWNER = "org.lightrag-workspaces.operations"
FORMAT = 1
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
DATABASE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}\Z")
SNAPSHOT = re.compile(r"[0-9A-F]+-[0-9A-F]+-[0-9]+\Z", re.I)
WORKSPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
FINGERPRINT_V1 = "sorted-jsonb-copy-sha256-v1"
FINGERPRINT_V2 = "sorted-jsonb-row-sha256-v2"
FINGERPRINT_ALGORITHMS = {FINGERPRINT_V1, FINGERPRINT_V2}
ROW_DIGEST = re.compile(rb"[0-9a-f]{64}\Z")


def require(value, message):
    if not value:
        raise RuntimeError(message)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def compare_restored_fingerprint(source, restored):
    """Allow only pg_dump's absence of an already-invalid index."""
    expected, actual = copy.deepcopy(source), copy.deepcopy(restored)
    for value in (expected, actual):
        checksum = value.pop("sha256", None)
        require(value.get("algorithm") in FINGERPRINT_ALGORITHMS and checksum == digest(value),
                "Invalid database fingerprint receipt")
    actual_indexes = actual["schema"].get("indexes") or []
    identities = {(index["tablename"], index["indexname"]) for index in actual_indexes}
    omitted = []
    retained = []
    for index in expected["schema"].get("indexes") or []:
        if index.get("indisvalid") is False and (index["tablename"], index["indexname"]) not in identities:
            omitted.append(copy.deepcopy(index))
        else:
            retained.append(index)
    expected["schema"]["indexes"] = retained or None
    require(expected == actual, "Restored rows/schema/extensions differ")
    return {"source_sha256": source["sha256"], "restored_sha256": restored["sha256"],
            "remaining_schema_rows_extensions_matched": True,
            "omitted_invalid_source_indexes": omitted,
            "omission_reason": "pg_dump excludes invalid source indexes" if omitted else None}


def compare_restored_service_fingerprint(before, after, omitted_invalid_indexes):
    """Permit only native recreation of the exact declared omitted indexes."""
    expected, actual = copy.deepcopy(before), copy.deepcopy(after)
    for value in (expected, actual):
        checksum = value.pop("sha256", None)
        require(value.get("algorithm") in FINGERPRINT_ALGORITHMS and checksum == digest(value),
                "Invalid database fingerprint receipt")
    identities = {(index["tablename"], index["indexname"]) for index in expected["schema"].get("indexes") or []}
    permitted = {(index["tablename"], index["indexname"]): index for index in omitted_invalid_indexes
                 if index.get("indisvalid") is False}
    retained, recreated = [], []
    for index in actual["schema"].get("indexes") or []:
        identity = index["tablename"], index["indexname"]
        if identity not in identities:
            source = permitted.get(identity)
            require(source is not None and index == source | {"indisvalid": True, "indisready": True},
                    "Restored service added an unexpected or changed index")
            recreated.append(copy.deepcopy(index))
        else:
            retained.append(index)
    actual["schema"]["indexes"] = retained or None
    require(expected == actual, "Restored service changed corpus rows or remaining schema")
    return {"before_sha256": before["sha256"], "after_sha256": after["sha256"],
            "database_rows_matched": True, "remaining_schema_matched": True,
            "schema_unchanged": not recreated, "recreated_invalid_source_indexes": recreated}


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def save(path, value):
    path = Path(path)
    require(not path.is_symlink(), "Receipt must not be a symlink")
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "Expected a regular JSON file")
    return json.loads(path.read_text())


def private_directory(path, *, create=False):
    path = Path(path)
    require(path.is_absolute() and not path.is_symlink(), "Use an absolute non-symlink directory")
    require(path.resolve() == path, "Directory ancestors must not contain symlinks")
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
    require(path.is_dir() and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0,
            "Operation/config directory must have mode 0700")
    return path


def disjoint(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a != b and a not in b.parents and b not in a.parents


def canonical_mounts(mounts):
    """Ignore Docker's inspection order, preserving every mount field exactly."""
    require(isinstance(mounts, list), "Container mounts must be a list")
    destinations = set()
    for mount in mounts:
        require(isinstance(mount, dict), "Container mount must be an object")
        destination = mount.get("Destination")
        require(isinstance(destination, str) and destination.startswith("/")
                and not destination.startswith("//") and "\0" not in destination
                and posixpath.normpath(destination) == destination, "Malformed container mount destination")
        require(destination not in destinations, "Duplicate container mount destination")
        destinations.add(destination)
    return sorted(mounts, key=lambda mount: mount["Destination"])


def run(arguments, *, data=None, output=None, timeout=3600):
    # Error output may contain credentials or document text: deliberately do not
    # interpolate it into console errors. A private operation receipt records stage.
    result = subprocess.run([str(arg) for arg in arguments], input=data,
                            stdout=output or subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    require(result.returncode == 0, "Command failed: " + str(arguments[0]) +
            " (exit " + str(result.returncode) + "; private output withheld)")
    return result.stdout


def hash_command(arguments, data, timeout=3600, *, expected_rows=None):
    """Stream hashes; v2 also verifies every fixed-width digest and row count."""
    require(expected_rows is None or (type(expected_rows) is int and expected_rows >= 0), "Invalid expected row count")
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        expired = threading.Event()

        def kill():
            expired.set()
            process.kill()

        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            process.stdin.write(data)
            process.stdin.close()
            value = hashlib.sha256()
            pending, rows = b"", 0
            for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
                value.update(block)
                if expected_rows is not None:
                    lines = (pending + block).split(b"\n")
                    pending = lines.pop()
                    require(len(pending) <= 64 and all(ROW_DIGEST.fullmatch(line) for line in lines),
                            "Invalid database row digest framing")
                    rows += len(lines)
            require(process.wait() == 0 and not expired.is_set(), "Database fingerprint failed/timed out")
            require(expected_rows is None or (not pending and rows == expected_rows),
                    "Database fingerprint row count/framing differs")
            return value.hexdigest()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


def tree_manifest(root):
    """Content, directories, links, ownership, modes, nanosecond mtime and xattrs.

    Symlinks are recorded without following them. Sockets/devices/FIFOs fail
    closed. Hardlink groups use relative names, not host-dependent inode numbers.
    """
    root = Path(root)
    require(hasattr(os, "listxattr"), "Run file operations on Linux with extended-attribute support")
    require(root.is_dir() and not root.is_symlink(), "Tree root must be a real directory")
    entries, inodes = [], {}
    paths = [root]
    for directory, directories, files in os.walk(root, followlinks=False):
        paths.extend(Path(directory) / name for name in sorted(directories + files))
    for path in sorted(set(paths), key=lambda item: str(item.relative_to(root))):
        info = path.lstat()
        row = {"path": str(path.relative_to(root)), "mode": stat.S_IMODE(info.st_mode),
               "uid": info.st_uid, "gid": info.st_gid, "mtime_ns": info.st_mtime_ns}
        if stat.S_ISREG(info.st_mode):
            row.update(kind="file", bytes=info.st_size, sha256=sha256(path))
            if info.st_nlink > 1:
                inodes.setdefault((info.st_dev, info.st_ino), []).append(row["path"])
        elif stat.S_ISDIR(info.st_mode):
            row["kind"] = "directory"
        elif stat.S_ISLNK(info.st_mode):
            row.update(kind="symlink", target=os.readlink(path))
        else:
            raise RuntimeError("Unsupported special file in authoritative tree")
        row["xattrs"] = {name: os.getxattr(path, name, follow_symlinks=False).hex()
                         for name in sorted(os.listxattr(path, follow_symlinks=False))}
        entries.append(row)
    links = sorted(sorted(names) for names in inodes.values() if len(names) > 1)
    result = {"entries": entries, "hardlinks": links}
    return result | {"sha256": digest(result)}


def clone_tree(source, destination, *, reflink="auto"):
    source, destination = Path(source), Path(destination)
    require(disjoint(source, destination) and not destination.exists(), "Copy target overlaps or exists")
    run(["cp", "-a", "--reflink=" + reflink, "--", source, destination])


def image_config(old, candidate, *, image):
    """Preserve explicit environment/mounts/resources/ports/network aliases.

    Unsupported namespace sharing or static networking must be adapted by an
    operator; do not silently manufacture a nearly equivalent container.
    """
    host = old["HostConfig"]
    networks = old["NetworkSettings"]["Networks"]
    require(len(networks) == 1, "Multiple networks need an explicit deployment adapter")
    network, endpoint = next(iter(networks.items()))
    require(host["NetworkMode"] == network and not network.startswith("container:"),
            "Unsupported network mode")
    require(not endpoint.get("IPAMConfig") and not endpoint.get("Links") and not endpoint.get("DriverOpts"),
            "Static networking/links/options need an explicit deployment adapter")
    require(not host.get("AutoRemove") and not host.get("VolumesFrom") and not host.get("ContainerIDFile"),
            "Auto-remove, inherited volumes and container ID files are unsupported")
    require(all(item.get("Type") == "bind" for item in canonical_mounts(old["Mounts"])),
            "This update adapter requires explicit bind mounts")
    payload = copy.deepcopy(old["Config"])
    payload.update(Image=image, Entrypoint=candidate["Config"].get("Entrypoint"),
                   Cmd=candidate["Config"].get("Cmd") or [],
                   Healthcheck=candidate["Config"].get("Healthcheck"))
    payload["Labels"] = dict(payload.get("Labels") or {}) | {
        key: value for key, value in (candidate["Config"].get("Labels") or {}).items()
        if key.startswith("org.opencontainers.image.")}
    payload["HostConfig"] = copy.deepcopy(host)
    generated = {old["Id"], old["Id"][:12], old["Name"].lstrip("/")}
    aliases = [value for value in endpoint.get("Aliases") or [] if value not in generated]
    payload["NetworkingConfig"] = {"EndpointsConfig": {network: {"Aliases": aliases}}}
    return payload


def immutable_reference(requested, image):
    # A caller tag can move between inspect and create. Prefer only a digest
    # already attached to the inspected image; source builds use the local ID.
    if requested in (image.get("RepoDigests") or []) and "@sha256:" in requested:
        return requested
    return image["Id"]


def require_default_command(container, image):
    require(container["Config"].get("Entrypoint") == image["Config"].get("Entrypoint") and
            (container["Config"].get("Cmd") or []) == (image["Config"].get("Cmd") or []),
            "Custom application entrypoint/command needs an explicitly reviewed update adapter")


def validate_retained(original, retained):
    require(retained["Id"] == original["Id"] and retained["Image"] == original["Image"]
            and retained["Config"] == original["Config"]
            and canonical_mounts(retained["Mounts"]) == canonical_mounts(original["Mounts"]),
            "Retained application configuration/mounts changed; do not start it")
    current_host = copy.deepcopy(retained["HostConfig"])
    original_restart = original["HostConfig"]["RestartPolicy"]
    current_restart = current_host["RestartPolicy"]
    require(current_restart == original_restart or
            (current_restart.get("Name") == "no" and current_restart.get("MaximumRetryCount", 0) == 0),
            "Retained restart policy was changed outside this operation")
    current_host["RestartPolicy"] = original_restart
    require(current_host == original["HostConfig"], "Retained host settings changed; do not start it")


def stable_inventory(inventory):
    return {"defaultWorkspace": inventory["defaultWorkspace"], "workspaces": {
        row["id"]: {key: row.get(key) for key in
                    ("id", "documents", "processed", "failed", "pending", "processing", "readOnly", "storageBackend")}
        for row in inventory["workspaces"]}}


def validate_quality(suites, origin, report_directory):
    if not suites:
        return []
    from lightrag_check import load_suite
    require(isinstance(origin, str), "Set the expected origin in operator config before quality checks")
    parsed = urllib.parse.urlsplit(origin)
    require(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment, "Invalid expected quality origin")
    expected = origin.rstrip("/")
    directory = private_directory(report_directory)
    result = []
    for path in suites:
        require(Path(path).is_file() and not Path(path).is_symlink(), "Quality suite must be a regular file")
        suite, checksum = load_suite(path)
        require(suite["origin"].rstrip("/") == expected, "Quality suite targets another origin")
        result.append({"suite": str(Path(path).resolve()), "sha256": checksum,
                       "origin": suite["origin"], "workspace": suite["workspace"], "report_directory": str(directory)})
    return result


def drill_environment(original, password, database):
    environment = {}
    for key, value in original.items():
        upper = key.upper()
        if upper.startswith("AUTH_") or any(word in upper for word in ("PASSWORD", "TOKEN", "SECRET", "CREDENTIAL")):
            continue
        if "KEY" in upper:
            # Provider clients can validate that a key exists during startup.
            # This deliberately useless value is never an original credential.
            environment[key] = "synthetic-isolated-drill-only"
        else:
            environment[key] = value
    environment.update(POSTGRES_HOST="127.0.0.1", POSTGRES_PORT="5432", POSTGRES_USER="postgres",
                       POSTGRES_PASSWORD=password, POSTGRES_DATABASE=database,
                       WORKING_DIR="/app/data/rag_storage", INPUT_DIR="/app/data/inputs",
                       LIGHTRAG_MULTIWORKSPACE_RUNTIME="/app/data/workspace-runtime")
    return environment


class Operations:
    def __init__(self, config):
        self.config = config
        require(config.get("format") == FORMAT, "Unsupported configuration format")
        for key in ("application", "postgres_container"):
            require(isinstance(config.get(key), str) and NAME.fullmatch(config[key]), "Invalid container name")
        require(DATABASE.fullmatch(config.get("database", "")), "Invalid database identifier")
        require(DATABASE.fullmatch(config.get("postgres_user", "postgres")), "Invalid PostgreSQL role")
        self.root = private_directory(config["backup_root"])
        self.engine = config.get("engine", "docker")
        require(self.engine == "docker", "Only Docker is supported and tested")
        self.app = config["application"]
        self.pg = config["postgres_container"]
        self.database = config["database"]
        self.user = config.get("postgres_user", "postgres")
        self.token = None

    def docker(self, *args, **kwargs):
        return run([self.engine, *args], **kwargs)

    def inspect(self, name, *, image=False):
        return json.loads(self.docker(*(["image"] if image else []), "inspect", name))[0]

    def require_source_pins(self, app, postgres):
        for key, value in (("expected_application_id", app["Id"]), ("expected_postgres_id", postgres["Id"])):
            require(isinstance(self.config.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", self.config[key])
                    and self.config[key] == value,
                    "Refresh the reviewed " + key + " from plan before this operation")

    def source_binding(self, app, postgres):
        """Prove app connection, configured Docker PG and every manifest agree.

        A fresh unpredictable application_name is observed through both clients
        while the app's read-only connection remains open. This handles Docker
        aliases and LAN/published ports without guessing host/IP equivalence.
        """
        nonce = "workspace-ops-" + uuid.uuid4().hex
        script = '''import asyncio,json,os,pathlib,sys
from postgres_backend import postgres_connection_kwargs,load_workspace_manifest
import asyncpg
p=json.loads(sys.stdin.readline())
async def main():
 options=postgres_connection_kwargs()
 options['server_settings']={**options.get('server_settings',{}),'application_name':p['nonce']}
 c=await asyncpg.connect(**options)
 try:
  async with c.transaction(readonly=True):
   await c.execute("SET LOCAL statement_timeout='15s'")
   identity=await c.fetchrow("SELECT current_database() database,pg_backend_pid() pid,db_instance_id::text marker FROM lightrag_workspace_database WHERE singleton=true")
   if not identity or identity['database']!=p['database']:raise RuntimeError('Application database mismatch')
   root=pathlib.Path(os.environ.get('WORKING_DIR','/app/data/rag_storage'))
   manifests={}
   for path in sorted(root.glob('*/.postgres-workspace.json')):
    value=load_workspace_manifest(path,path.parent.name)
    if value['database']!=identity['database'] or value['db_instance_id']!=identity['marker']:raise RuntimeError('Manifest database identity mismatch')
    manifests[path.parent.name]=value['postgres_workspace']
   if not manifests:raise RuntimeError('No registered PostgreSQL workspaces')
   print(json.dumps({**dict(identity),'manifests':manifests}),flush=True)
   await asyncio.to_thread(sys.stdin.readline)
 finally:await c.close()
asyncio.run(main())
'''
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([self.engine, "exec", "-i", app["Id"], "python", "-c", script],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
            timer = threading.Timer(45, process.kill)
            timer.start()
            try:
                process.stdin.write(canonical({"nonce": nonce, "database": self.database}) + b"\n")
                process.stdin.flush()
                raw = process.stdout.readline()
                require(raw, "Application/manifest source binding failed (private details withheld)")
                observed = json.loads(raw)
                require(type(observed.get("pid")) is int and observed["pid"] > 0, "Invalid app database session")
                seen = self.sql("SELECT count(*) FROM pg_stat_activity WHERE pid=" + str(observed["pid"]) +
                                " AND application_name='" + nonce + "' AND datname=current_database();")
                require(seen == "1", "Configured PostgreSQL container is not the application's database server")
                marker = self.sql("SELECT db_instance_id::text FROM lightrag_workspace_database WHERE singleton=true;")
                require(marker == observed["marker"], "Configured PostgreSQL database marker differs")
                inventory = self.json_api("/workspaces")
                require(set(observed["manifests"]) == {row["id"] for row in inventory["workspaces"]},
                        "Some workspace IDs lack a matching PostgreSQL manifest")
                require(self.inspect(self.app)["Id"] == app["Id"] and self.inspect(self.pg)["Id"] == postgres["Id"],
                        "Source container identity changed during binding verification")
                return {"database": self.database, "database_marker": marker,
                        "workspaces": observed["manifests"], "application_id": app["Id"], "postgres_id": postgres["Id"]}
            finally:
                timer.cancel()
                if process.poll() is None:
                    try:
                        process.stdin.write(b"\n")
                        process.stdin.flush()
                        process.wait(timeout=20)
                    except (BrokenPipeError, subprocess.TimeoutExpired):
                        process.kill()
                process.wait()
                process.stdin.close()
                process.stdout.close()

    def prepare_fixture_directory(self, directory, image, user, *, postgres=False):
        require(directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()),
                "Ownership changes are limited to a new empty fixture directory")
        if not user and not postgres:
            return {"uid": 0, "gid": 0, "entrypoint_manages_user": True}
        probe_user = user or "postgres"
        arguments = ["run", "--rm", "--network", "none", "--read-only", "--memory", "64m", "--pids-limit", "32",
                     "--user", probe_user, "--entrypoint", "id", image]
        uid = self.docker(*arguments, "-u").decode().strip()
        gid = self.docker(*arguments, "-g").decode().strip()
        require(uid.isdecimal() and gid.isdecimal(), "Cannot resolve recorded container user in its exact image")
        if postgres and int(uid) == 0:
            # Standard PostgreSQL entrypoints start as root then drop to the
            # image's postgres OS account. Its nested PGDATA needs traversal of
            # the new mounted parent, even when initdb chowns only the child.
            index = arguments.index("--user") + 1
            arguments[index] = "postgres"
            uid = self.docker(*arguments, "-u").decode().strip()
            gid = self.docker(*arguments, "-g").decode().strip()
            require(uid.isdecimal() and gid.isdecimal() and int(uid) != 0,
                    "Root PostgreSQL image needs a resolvable postgres OS account")
        os.chown(directory, int(uid), int(gid))
        return {"uid": int(uid), "gid": int(gid), "entrypoint_manages_user": not bool(user)}

    def psql_args(self, *, container=None, database=None, user=None):
        return [self.engine, "exec", "-i", container or self.pg, "psql", "-X", "-q", "-A", "-t",
                "-v", "ON_ERROR_STOP=1", "-U", user or self.user, "-d", database or self.database]

    def sql(self, query, **kwargs):
        return run(self.psql_args(**kwargs), data=query.encode()).decode().strip()

    def api(self, path, *, method="GET", body=None, container=None, token=None):
        require(path.startswith("/") and "\n" not in path, "Invalid API path")
        script = '''import json,os,sys,urllib.request
p=json.load(sys.stdin)
h={"Content-Type":"application/json"}
if os.environ.get("AUTH_ACCOUNTS"):
 from workspace_management import internal_auth_headers
 h.update(internal_auth_headers())
elif os.environ.get("LIGHTRAG_API_KEY"):h["X-API-Key"]=os.environ["LIGHTRAG_API_KEY"]
if p.get("token"):h["X-Maintenance-Token"]=p["token"]
body=None if p["body"] is None else json.dumps(p["body"]).encode()
r=urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:9621"+p["path"],data=body,method=p["method"],headers=h),timeout=300)
print(json.dumps({"status":r.status,"headers":dict(r.headers),"body":r.read().decode()}))
'''
        result = json.loads(self.docker("exec", "-i", container or self.app, "python", "-c", script,
                                       data=canonical({"path": path, "method": method, "body": body, "token": token})))
        require(result["status"] == 200, "API check failed")
        return result

    def json_api(self, path, **kwargs):
        return json.loads(self.api(path, **kwargs)["body"])

    def mount_sources(self, app):
        destinations = self.config.get("mounts", {"control": "/app/data/rag_storage", "inputs": "/app/data/inputs"})
        require(set(destinations) == {"control", "inputs"}, "Define exactly control and inputs authoritative mounts")
        mounts = {row["Destination"]: row for row in canonical_mounts(app["Mounts"])}
        result = {}
        for label, destination in destinations.items():
            require(destination in mounts and mounts[destination]["Type"] == "bind", "Authoritative mount missing/not a bind")
            path = Path(mounts[destination]["Source"])
            require(path.is_dir() and not path.is_symlink(), "Mount must be accessible at the same host path")
            require(disjoint(path, self.root), "Backup root overlaps an authoritative mount")
            result[label] = path
        require(disjoint(*result.values()), "Authoritative mounts overlap")
        return result

    def exclusive_writers(self, app, sources):
        identifiers = self.docker("ps", "-q").decode().split()
        others = json.loads(self.docker("inspect", *identifiers)) if identifiers else []
        for container in others:
            if container["Id"] == app["Id"] or container["Id"] == self.config.get("operator_container_id"):
                continue
            for mount in container["Mounts"]:
                if mount.get("RW"):
                    require(all(disjoint(mount["Source"], source) for source in sources.values()),
                            "Another running container can write an authoritative mount")

    def idle_database(self):
        active = self.sql("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                          "AND pid<>pg_backend_pid() AND (state='active' OR xact_start IS NOT NULL);")
        require(active == "0", "External database work must finish before the consistent snapshot")

    def enter(self, directory):
        token = uuid.uuid4().hex + uuid.uuid4().hex
        save(directory / "maintenance-owner.json", {"ownerToken": token, "application": self.app})
        value = self.json_api("/maintenance", method="POST", body={"ownerToken": token})
        self.token = token
        deadline = time.monotonic() + self.config.get("drain_timeout_seconds", 300)
        while not value.get("drained"):
            require(time.monotonic() < deadline, "Maintenance drain timed out; retry/release using saved owner receipt")
            time.sleep(1)
            value = self.json_api("/maintenance", token=token)
        require(value.get("active") and value.get("ownerMatches"), "Maintenance gate ownership mismatch")
        return value

    def release(self):
        if self.token:
            self.json_api("/maintenance/release", method="POST", body={"ownerToken": self.token})
            self.token = None

    @contextmanager
    def snapshot(self):
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(self.psql_args(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
            try:
                process.stdin.write(b"BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
                                    b"SET LOCAL idle_in_transaction_session_timeout='2h';\n"
                                    b"SELECT pg_export_snapshot();\n")
                process.stdin.flush()
                # A timeout prevents a failed/unreachable database hanging the gate.
                timer = threading.Timer(30, process.kill)
                timer.start()
                try:
                    value = process.stdout.readline().decode().strip()
                finally:
                    timer.cancel()
                require(SNAPSHOT.fullmatch(value), "Could not export a PostgreSQL snapshot")
                yield value
                require(process.poll() is None, "Snapshot holder exited before verification completed")
            finally:
                if process.poll() is None:
                    try:
                        process.stdin.write(b"ROLLBACK;\n\\q\n")
                        process.stdin.flush()
                        process.wait(timeout=10)
                    except (BrokenPipeError, subprocess.TimeoutExpired):
                        process.kill()
                process.wait()
                process.stdin.close()
                process.stdout.close()

    def transaction(self, snapshot=None, *, algorithm=FINGERPRINT_V2):
        require(snapshot is None or SNAPSHOT.fullmatch(snapshot), "Invalid exported snapshot")
        require(algorithm in FINGERPRINT_ALGORITHMS, "Unsupported database fingerprint algorithm")
        prefix = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;" + (
            "SET TRANSACTION SNAPSHOT '" + snapshot + "';" if snapshot else "") + (
            "SET LOCAL statement_timeout='30min';SET LOCAL extra_float_digits=3;")
        if algorithm == FINGERPRINT_V2:
            prefix += ("SET LOCAL TimeZone='UTC';SET LOCAL DateStyle='ISO,YMD';"
                       "SET LOCAL IntervalStyle='postgres';SET LOCAL bytea_output='hex';")
        return prefix

    def fingerprint(self, snapshot=None, *, container=None, user=None, algorithm=FINGERPRINT_V2):
        prefix = self.transaction(snapshot, algorithm=algorithm)
        query = """SELECT json_build_object(
          'tables',(SELECT json_agg(tablename ORDER BY tablename) FROM pg_tables WHERE schemaname='public'),
          'columns',(SELECT json_agg(row_to_json(c) ORDER BY table_name,ordinal_position) FROM
            (SELECT table_name,ordinal_position,column_name,udt_name,data_type,is_nullable,column_default
             FROM information_schema.columns WHERE table_schema='public') c),
          'constraints',(SELECT json_agg(row_to_json(c) ORDER BY relation,name) FROM
            (SELECT conrelid::regclass::text relation,conname name,pg_get_constraintdef(oid) definition,convalidated
             FROM pg_constraint WHERE connamespace='public'::regnamespace) c),
          'indexes',(SELECT json_agg(row_to_json(i) ORDER BY tablename,indexname) FROM
            (SELECT p.tablename,p.indexname,p.indexdef,x.indisvalid,x.indisready
             FROM pg_indexes p JOIN pg_namespace n ON n.nspname=p.schemaname
             JOIN pg_class c ON c.relnamespace=n.oid AND c.relname=p.indexname
             JOIN pg_index x ON x.indexrelid=c.oid WHERE p.schemaname='public') i),
          'extensions',(SELECT json_agg(json_build_object('name',extname,'version',extversion) ORDER BY extname) FROM pg_extension)
        );"""
        schema = json.loads(self.sql(prefix + query + "COMMIT;", container=container, user=user))
        require(schema.get("tables") and "lightrag_workspace_database" in schema["tables"], "No registered LightRAG database")
        rows = []
        for table in schema["tables"]:
            # Quote identifiers rather than restricting legitimate existing names.
            identifier = 'public."' + table.replace('"', '""') + '"'
            count = int(self.sql(prefix + "SELECT count(*) FROM " + identifier + ";COMMIT;",
                                 container=container, user=user))
            if algorithm == FINGERPRINT_V1:
                query = prefix + "COPY (SELECT to_jsonb(t)::text AS row FROM " + identifier + (
                    " t ORDER BY to_jsonb(t)::text COLLATE \"C\") TO STDOUT;COMMIT;")
            else:
                # Materialize only a fixed-width digest at the sort node. Wide
                # document/vector JSONB rows must never enter its temp files.
                expression = "pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(to_jsonb(t)::text,'UTF8')),'hex')"
                query = prefix + "COPY (SELECT " + expression + " COLLATE \"C\" AS row_digest FROM " + identifier + (
                    " t ORDER BY row_digest) TO STDOUT;COMMIT;")
            checksum = hash_command(self.psql_args(container=container, user=user), query.encode(),
                                    expected_rows=count if algorithm == FINGERPRINT_V2 else None)
            rows.append({"table": table, "count": count, "sha256": checksum})
        value = {"algorithm": algorithm, "schema": schema, "tables": rows}
        return value | {"sha256": digest(value)}

    def plan(self):
        app, pg = self.inspect(self.app), self.inspect(self.pg)
        require(app["State"]["Running"] and pg["State"]["Running"], "Source application and PostgreSQL must run")
        sources = self.mount_sources(app)
        for container in (app, pg):
            require(all(disjoint(mount["Source"], self.root) for mount in container["Mounts"]),
                    "Backup root overlaps an application/PostgreSQL mount")
        self.exclusive_writers(app, sources)
        binding = self.source_binding(app, pg)
        inventory = self.json_api("/workspaces")
        database_bytes = int(self.sql("SELECT pg_database_size(current_database());"))
        source_bytes = sum(path.lstat().st_size for source in sources.values()
                           for directory, _, names in os.walk(source, followlinks=False)
                           for path in (Path(directory) / name for name in names) if path.is_file() and not path.is_symlink())
        free = shutil.disk_usage(self.root).free
        required = database_bytes * 2 + source_bytes + 2 * 1024 ** 3
        require(free >= required, "Insufficient headroom for verified backup (database twice plus source files and 2 GiB)")
        return {"format": FORMAT, "application_id": app["Id"], "application_image": app["Image"],
                "postgres_id": pg["Id"], "postgres_image": pg["Image"], "database": self.database,
                "sources": {key: str(path) for key, path in sources.items()}, "backup_root": str(self.root),
                "workspaces": len(inventory["workspaces"]), "default_workspace": inventory["defaultWorkspace"],
                "free_bytes": free, "required_headroom_bytes": required, "writes": False,
                "identity_pins_match": self.config.get("expected_application_id") == app["Id"] and
                    self.config.get("expected_postgres_id") == pg["Id"],
                "database_binding": binding,
                "strategy": "maintenance drain, exported snapshot, independent file copy, release, concurrent dump"}

    def operation(self, kind):
        identifier = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        directory = private_directory(self.root / (kind + "-" + identifier), create=True)
        record = {"format": FORMAT, "owner": OWNER, "id": identifier, "kind": kind,
                  "created_at": now(), "status": "running", "stage": "created"}
        save(directory / "operation.json", record)
        return directory, record

    def stage(self, directory, record, value, **fields):
        record.update(stage=value, updated_at=now(), **fields)
        save(directory / "operation.json", record)

    def backup(self):
        self.plan()
        directory, record = self.operation("backup")
        started = time.monotonic()
        try:
            app, pg = self.inspect(self.app), self.inspect(self.pg)
            self.require_source_pins(app, pg)
            sources = self.mount_sources(app)
            save(directory / "application.json", app)
            save(directory / "postgres.json", pg)
            save(directory / "operator-config.json", self.config)
            if self.config.get("unraid_template"):
                template = Path(self.config["unraid_template"])
                require(template.is_file() and not template.is_symlink(), "Invalid Unraid template")
                require(ET.fromstring(template.read_bytes()).findtext("Repository") == app["Config"]["Image"],
                        "Unraid template has a different current image")
                shutil.copyfile(template, directory / "template.xml")
                os.chmod(directory / "template.xml", 0o600)
            self.stage(directory, record, "draining")
            self.enter(directory)
            self.exclusive_writers(app, sources)
            save(directory / "source-binding.json", self.source_binding(app, pg))
            self.idle_database()
            inventory = self.json_api("/workspaces")
            save(directory / "inventory.json", inventory)
            save(directory / "postgres-identity.json", {"version": self.sql("SHOW server_version;"),
                 "marker": json.loads(self.sql("SELECT row_to_json(t) FROM lightrag_workspace_database t;"))})
            save(directory / "postgres-settings.json", {"shared_preload_libraries": self.sql("SHOW shared_preload_libraries;")})
            with self.snapshot() as snapshot:
                self.stage(directory, record, "copying-frozen-files", snapshot=snapshot)
                (directory / "files").mkdir(mode=0o700)
                manifests = {}
                for label, source in sources.items():
                    before = tree_manifest(source)
                    clone_tree(source, directory / "files" / label, reflink=self.config.get("reflink", "auto"))
                    copied = tree_manifest(directory / "files" / label)
                    require(before == copied == tree_manifest(source), "Authoritative files changed or copy differs")
                    manifests[label] = before
                save(directory / "files-manifest.json", manifests)
                require(self.inspect(self.app)["Id"] == app["Id"] and self.inspect(self.pg)["Id"] == pg["Id"],
                        "Source container identity changed while freezing")
                self.release()
                pause = time.monotonic() - started
                self.stage(directory, record, "dumping-exported-snapshot", write_pause_seconds=round(pause, 3))
                # The holder stays alive through both dump and fingerprints. New
                # uploads may proceed; all DB reads below import exactly its view.
                with (directory / "database.dump").open("xb") as stream:
                    os.chmod(directory / "database.dump", 0o600)
                    self.docker("exec", self.pg, "pg_dump", "-Fc", "-Z1", "--snapshot=" + snapshot,
                                "-U", self.user, "-d", self.database, output=stream)
                fingerprint = self.fingerprint(snapshot)
                save(directory / "database-fingerprint.json", fingerprint)
            self.stage(directory, record, "verifying-archive")
            dump = directory / "database.dump"
            self.archive_read(dump, self.pg, directory)
            files = [path for path in directory.iterdir() if path.is_file() and path.name != "operation.json"]
            checksums = {path.name: {"sha256": sha256(path), "bytes": path.stat().st_size} for path in files}
            self.stage(directory, record, "verified", status="verified", checksums=checksums,
                       file_manifests_sha256=digest(manifests), database_sha256=fingerprint["sha256"],
                       full_archive_read_verified=True, restore_tested=False,
                       duration_seconds=round(time.monotonic() - started, 3))
            return {"bundle": str(directory), "status": "verified", "write_pause_seconds": round(pause, 3),
                    "database_bytes": dump.stat().st_size, "restore_tested": False}
        except BaseException:
            self.stage(directory, record, "failed", status="failed", failure_stage=record["stage"])
            raise
        finally:
            self.release()

    def archive_read(self, dump, postgres, directory):
        with dump.open("rb") as stream:
            result = subprocess.run([self.engine, "exec", "-i", postgres, "pg_restore", "--list"],
                                    stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3600)
        require(result.returncode == 0 and b"TABLE DATA public lightrag_" in result.stdout,
                "Archive inventory is missing LightRAG data")
        listing = directory / "database.list"
        listing.write_bytes(result.stdout)
        os.chmod(listing, 0o600)
        with dump.open("rb") as stream:
            result = subprocess.run([self.engine, "exec", "-i", postgres, "pg_restore",
                                     "--exit-on-error", "--file=/dev/null"], stdin=stream,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
        require(result.returncode == 0, "Archive full read/decompression failed")

    def verify_bundle(self, bundle):
        bundle = private_directory(bundle)
        record = load(bundle / "operation.json")
        require(record.get("owner") == OWNER and record.get("format") == FORMAT and record.get("kind") == "backup"
                and record.get("status") == "verified" and record.get("full_archive_read_verified"),
                "Only a verified owned backup bundle is accepted")
        for name, proof in record["checksums"].items():
            require(Path(name).name == name and name not in {".", ".."}, "Unsafe checksum path")
            path = bundle / name
            require(path.is_file() and not path.is_symlink() and path.stat().st_size == proof["bytes"]
                    and sha256(path) == proof["sha256"], "Backup file changed or is incomplete")
        manifests = load(bundle / "files-manifest.json")
        require(set(manifests) == {"control", "inputs"} and digest(manifests) == record["file_manifests_sha256"],
                "Invalid authoritative file manifests")
        for label, expected in manifests.items():
            require(tree_manifest(bundle / "files" / label) == expected, "Backup tree does not match manifest")
        return record

    def scoped_checks(self, container):
        inventory = self.json_api("/workspaces", container=container)
        results = []
        for row in inventory["workspaces"]:
            name = row["id"]
            require(WORKSPACE.fullmatch(name), "Unexpected workspace ID")
            prefix = "/workspaces/" + name
            health = self.json_api(prefix + "/health", container=container)
            require(health.get("selected_workspace_id") == name and health.get("status") == "healthy",
                    "Restored workspace failed scoped health/identity")
            for suffix in ("/docs", "/openapi.json", "/webui/"):
                response = self.api(prefix + suffix, container=container)
                headers = {key.lower(): value for key, value in response["headers"].items()}
                require(headers.get("lightrag-workspace") == name, "Scoped response identity mismatch")
                if suffix == "/openapi.json":
                    schema = json.loads(response["body"])
                    require(schema.get("servers") == [{"url": prefix}], "OpenAPI escaped workspace scope")
                elif suffix == "/docs":
                    require(prefix + "/openapi.json" in response["body"], "Swagger schema lost workspace scope")
            results.append({"workspace": name, "health": True, "scoped_ui_and_api": True})
        require(results, "No restored workspaces")
        return {"workspaces": results, "browser_interaction_tested": False, "provider_calls": 0}

    def restore_drill(self, bundle, *, keep=False):
        bundle = Path(bundle)
        proof = self.verify_bundle(bundle)
        source_fingerprint = load(bundle / "database-fingerprint.json")
        algorithm = source_fingerprint.get("algorithm")
        require(algorithm in FINGERPRINT_ALGORITHMS, "Unsupported database fingerprint algorithm")
        fingerprint_payload = {key: value for key, value in source_fingerprint.items() if key != "sha256"}
        require(source_fingerprint.get("sha256") == digest(fingerprint_payload) == proof.get("database_sha256"),
                "Invalid database fingerprint receipt or backup binding")
        directory, record = self.operation("drill")
        self.stage(directory, record, "preparing", backup_id=proof["id"], backup_bundle=str(bundle))
        pg = load(bundle / "postgres.json")
        app = load(bundle / "application.json")
        password = uuid.uuid4().hex + uuid.uuid4().hex
        dbname = "lightrag-drill-db-" + record["id"].lower()
        appname = "lightrag-drill-app-" + record["id"].lower()
        created = []
        try:
            # New names and directories only. No command below connects to self.pg.
            data = directory / "postgres-data"
            data.mkdir(mode=0o700)
            pguser = pg["Config"].get("User") or ""
            ownership = self.prepare_fixture_directory(data, pg["Image"], pguser, postgres=True)
            save(directory / "postgres-data-owner.json", ownership)
            env = dict(item.split("=", 1) for item in pg["Config"].get("Env", []))
            pgdata = env.get("PGDATA", "/var/lib/postgresql/data")
            require(pgdata.startswith("/var/lib/postgresql/"), "Configure a reviewed PostgreSQL drill storage adapter")
            arguments = ["run", "-d", "--name", dbname, "--network", "none", "--label", OWNER + "=" + record["id"],
                         "--memory", self.config.get("drill_memory", "4g"), "--shm-size", "256m",
                         "--mount", "type=bind,src=" + str(data) + ",dst=/var/lib/postgresql",
                         "-e", "PGDATA=" + pgdata, "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_DB=" + self.database,
                         "-e", "POSTGRES_PASSWORD=" + password, pg["Image"], "postgres", "-c",
                         "shared_preload_libraries=" + load(bundle / "postgres-settings.json")["shared_preload_libraries"]]
            if pguser:
                arguments[1:1] = ["--user", pguser]
            self.docker(*arguments)
            created.append(dbname)
            self.stage(directory, record, "database-starting", containers=created)
            deadline = time.monotonic() + 120
            while True:
                try:
                    if self.sql("SELECT 1;", container=dbname, user="postgres") == "1":
                        break
                except RuntimeError:
                    pass
                require(time.monotonic() < deadline, "Isolated PostgreSQL did not become ready")
                time.sleep(1)
            require(self.inspect(dbname)["Id"] != self.inspect(self.pg)["Id"], "Restore target is source PostgreSQL")
            empty = self.sql("SELECT count(*) FROM pg_tables WHERE schemaname='public';", container=dbname, user="postgres")
            require(empty == "0", "Restore target is not empty")
            self.stage(directory, record, "restoring")
            with (bundle / "database.dump").open("rb") as stream:
                result = subprocess.run([self.engine, "exec", "-i", dbname, "pg_restore", "--exit-on-error",
                                         "--no-owner", "--no-privileges", "-U", "postgres", "-d", self.database],
                                        stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)
            require(result.returncode == 0, "Independent database restore failed; owned fixture retained")
            restored = self.fingerprint(container=dbname, user="postgres", algorithm=algorithm)
            comparison = compare_restored_fingerprint(source_fingerprint, restored)
            save(directory / "restored-fingerprint.json", restored)
            save(directory / "restore-comparison.json", comparison)
            self.stage(directory, record, "database-verified", database_sha256=restored["sha256"])
            for label in ("control", "inputs"):
                clone_tree(bundle / "files" / label, directory / label)
                require(tree_manifest(directory / label) == load(bundle / "files-manifest.json")[label],
                        "Restored file copy differs")
            # The recovered snapshot intentionally contains a maintenance gate.
            # Release it only in the isolated working copy after byte verification.
            maintenance = directory / "control" / ".workspace-management" / "maintenance.json"
            if maintenance.exists():
                require(maintenance.is_file() and not maintenance.is_symlink(), "Invalid copied maintenance gate")
                maintenance.unlink()
            runtime = directory / "runtime"
            runtime.mkdir(mode=0o700)
            appuser = app["Config"].get("User") or ""
            ownership = self.prepare_fixture_directory(runtime, app["Image"], appuser)
            save(directory / "runtime-owner.json", ownership)
            environment = drill_environment(dict(item.split("=", 1) for item in app["Config"].get("Env", [])),
                                            password, self.database)
            envfile = directory / "drill.env"
            require(all("\n" not in key + value for key, value in environment.items()), "Multiline environment requires an adapter")
            envfile.write_text("".join(key + "=" + value + "\n" for key, value in sorted(environment.items())))
            os.chmod(envfile, 0o600)
            arguments = ["run", "-d", "--name", appname, "--network", "container:" + dbname,
                         "--label", OWNER + "=" + record["id"], "--env-file", str(envfile),
                         "--memory", self.config.get("drill_app_memory", "8g")]
            if appuser:
                arguments += ["--user", appuser]
            for label, destination in (("control", "/app/data/rag_storage"), ("inputs", "/app/data/inputs"),
                                       ("runtime", "/app/data/workspace-runtime")):
                arguments += ["--mount", "type=bind,src=" + str(directory / label) + ",dst=" + destination]
            arguments += [app["Image"]]
            self.docker(*arguments)
            created.append(appname)
            self.stage(directory, record, "checking-scoped-service", containers=created)
            deadline = time.monotonic() + 90
            while True:
                try:
                    inventory = self.json_api("/workspaces", container=appname)
                    break
                except RuntimeError:
                    require(time.monotonic() < deadline, "Isolated gateway did not become ready")
                    time.sleep(1)
            require(stable_inventory(inventory) == stable_inventory(load(bundle / "inventory.json")),
                    "Restored workspace inventory differs")
            service = self.scoped_checks(appname)
            save(directory / "service-proof.json", service)
            after_service = self.fingerprint(container=dbname, user="postgres", algorithm=algorithm)
            service_comparison = compare_restored_service_fingerprint(
                restored, after_service, comparison["omitted_invalid_source_indexes"])
            save(directory / "after-service-fingerprint.json", after_service)
            save(directory / "after-service-comparison.json", service_comparison)
            self.stage(directory, record, "verified", status="verified", restore_tested=True,
                       isolated=True, extensions_matched=True, files_matched=True, scoped_ui_api=True,
                       omitted_invalid_source_indexes=comparison["omitted_invalid_source_indexes"],
                       recreated_invalid_source_indexes=service_comparison["recreated_invalid_source_indexes"],
                       database_rows_matched=True, schema_unchanged=service_comparison["schema_unchanged"],
                       provider_calls=0, browser_interaction_tested=False,
                       quality_hook="not run; use explicit quality acceptance separately")
            return {"drill": str(directory), "status": "verified", "workspaces": len(service["workspaces"]),
                    "omitted_invalid_source_indexes": comparison["omitted_invalid_source_indexes"],
                    "recreated_invalid_source_indexes": service_comparison["recreated_invalid_source_indexes"],
                    "schema_unchanged": service_comparison["schema_unchanged"],
                    "restore_tested": True, "browser_interaction_tested": False}
        except BaseException:
            self.stage(directory, record, "failed", status="failed", failure_stage=record["stage"], containers=created)
            # Keep failed fixtures stopped for diagnosis; never remove their data.
            keep = True
            raise
        finally:
            for name in reversed(created):
                current = self.inspect(name)
                require((current["Config"].get("Labels") or {}).get(OWNER) == record["id"], "Fixture ownership changed")
                self.docker("stop", "-t", "60", name)
                if not keep:
                    self.docker("rm", name)
            # Restore data remains private on disk even after successful container cleanup.

    def copy_bundle(self, bundle, destination):
        proof = self.verify_bundle(bundle)
        destination = Path(destination)
        require(destination.is_absolute() and disjoint(self.root, destination), "Copy must be outside backup root")
        for container in (self.inspect(self.app), self.inspect(self.pg)):
            require(all(disjoint(mount["Source"], destination) for mount in container["Mounts"]),
                    "Independent copy destination overlaps a live application/PostgreSQL mount")
        clone_tree(bundle, destination)
        require(tree_manifest(bundle) == tree_manifest(destination), "Off-host/mounted copy differs")
        self.verify_bundle(destination)
        receipt = {"backup_id": proof["id"], "destination": str(destination), "sha256": tree_manifest(destination)["sha256"], "at": now()}
        save(self.root / ("copy-" + proof["id"] + ".json"), receipt)
        return receipt

    def retention(self, keep, *, apply=False):
        require(type(keep) is int and keep >= 1, "Keep at least one verified backup")
        backups, referenced = [], set()
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.is_symlink() or not (directory / "operation.json").is_file():
                continue
            row = load(directory / "operation.json")
            if row.get("owner") != OWNER:
                continue
            if row.get("backup_id"):
                referenced.add(row["backup_id"])
            if row.get("kind") == "backup":
                backups.append((directory, row))
        verified = sorted((item for item in backups if item[1].get("status") == "verified"),
                          key=lambda item: item[1]["created_at"], reverse=True)
        protected = {row["id"] for _, row in verified[:keep]} | referenced
        candidates = [directory for directory, row in verified if row["id"] not in protected]
        if apply:
            for directory in candidates:
                self.verify_bundle(directory)
                require(directory.parent == self.root and directory.name.startswith("backup-"), "Unsafe retention target")
                containers = self.docker("ps", "-aq").decode().split()
                if containers:
                    for container in json.loads(self.docker("inspect", *containers)):
                        require(all(disjoint(mount["Source"], directory) for mount in container["Mounts"]),
                                "A container still references this backup; retention will not remove it")
                shutil.rmtree(directory)
        return {"apply": apply, "candidates": [str(path) for path in candidates],
                "kept_failed_and_referenced": True, "keep_latest": keep}

    def create_container(self, name, payload):
        require(NAME.fullmatch(name), "Invalid target container name")
        result = run(["curl", "--silent", "--show-error", "--max-time", "60", "--unix-socket",
                      self.config.get("docker_socket", "/var/run/docker.sock"), "-H", "Content-Type: application/json",
                      "-X", "POST", "--data-binary", "@-", "-w", "\n%{http_code}",
                      "http://localhost/v1.43/containers/create?name=" + name], data=canonical(payload))
        body, code = result.rsplit(b"\n", 1)
        require(code == b"201", "Docker create failed; restore previous container with rollback")
        return json.loads(body)["Id"]

    def update(self, bundle, candidate, acceptance, *, apply=False, quality_suites=(), quality_report_directory=None):
        qualities = validate_quality(quality_suites, self.config.get("origin"), quality_report_directory)
        backup = self.verify_bundle(bundle)
        app, image = self.inspect(self.app), self.inspect(candidate, image=True)
        self.require_source_pins(app, self.inspect(self.pg))
        check = load(acceptance)
        require(check.get("passed") is True and check.get("image_id") == image["Id"]
                and check.get("database_compatible") is True and check.get("application_only") is True,
                "Candidate needs an image-bound tested compatibility receipt")
        previous = load(Path(bundle) / "application.json")
        require(app["Id"] == previous["Id"] and app["Config"] == previous["Config"]
                and app["HostConfig"] == previous["HostConfig"]
                and canonical_mounts(app["Mounts"]) == canonical_mounts(previous["Mounts"]),
                "Live application changed since backup")
        require_default_command(app, self.inspect(app["Image"], image=True))
        reference = immutable_reference(candidate, image)
        payload = image_config(app, image, image=reference)
        if not apply:
            return {"apply": False, "old_image": app["Image"], "candidate_image": image["Id"],
                    "preserves": ["environment", "mounts", "resources", "ports", "workspace IDs", "database"],
                    "rollback": "previous container on current database only"}
        directory, record = self.operation("update")
        self.stage(directory, record, "preparing", backup_id=backup["id"], backup_bundle=str(bundle),
                   application=self.app, previous_id=app["Id"], candidate_image=image["Id"])
        save(directory / "application-before.json", app)
        save(directory / "acceptance.json", check)
        payload["Labels"][OWNER] = record["id"]
        save(directory / "candidate-payload.json", payload)
        postgres = self.inspect(self.pg)
        retained = self.app + "-before-" + record["id"].lower()
        pause_started = time.monotonic()
        try:
            self.enter(directory)
            self.exclusive_writers(app, self.mount_sources(app))
            save(directory / "source-binding.json", self.source_binding(app, postgres))
            self.idle_database()
            before = self.fingerprint()
            files = {key: tree_manifest(path) for key, path in self.mount_sources(app).items()}
            inventory = self.json_api("/workspaces")
            save(directory / "before-fingerprint.json", before)
            save(directory / "before-files.json", files)
            save(directory / "before-inventory.json", inventory)
            self.stage(directory, record, "stopping-previous", retained_container=retained)
            self.docker("update", "--restart=no", app["Id"])
            self.docker("stop", "-t", "120", app["Id"])
            stopped = self.inspect(app["Id"])
            require(not stopped["State"]["Running"] and not stopped["State"]["OOMKilled"]
                    and stopped["State"]["ExitCode"] == 0, "Previous application did not stop cleanly")
            self.docker("rename", app["Id"], retained)
            self.stage(directory, record, "creating-candidate")
            identifier = self.create_container(self.app, payload)
            self.stage(directory, record, "starting-candidate", candidate_id=identifier)
            self.docker("start", identifier)
            deadline = time.monotonic() + 120
            while True:
                try:
                    gate = self.json_api("/maintenance", token=self.token)
                    require(gate.get("active") and gate.get("ownerMatches") and gate.get("drained"), "Candidate lost gate")
                    break
                except RuntimeError:
                    require(time.monotonic() < deadline, "Candidate did not start with preserved maintenance gate")
                    time.sleep(1)
            require(self.fingerprint() == before, "Candidate startup changed database contents/schema")
            require({key: tree_manifest(path) for key, path in self.mount_sources(app).items()} == files,
                    "Candidate startup changed authoritative files")
            require(stable_inventory(self.json_api("/workspaces")) == stable_inventory(inventory), "Workspace inventory changed")
            require(self.inspect(self.pg)["Id"] == postgres["Id"] and
                    self.inspect(self.pg)["State"]["StartedAt"] == postgres["State"]["StartedAt"], "PostgreSQL restarted")
            current = self.inspect(self.app)
            require(current["Image"] == image["Id"]
                    and canonical_mounts(current["Mounts"]) == canonical_mounts(app["Mounts"])
                    and current["Config"]["Env"] == app["Config"]["Env"], "Candidate identity/configuration differs")
            if self.config.get("unraid_template"):
                template = Path(self.config["unraid_template"])
                original = template.read_bytes()
                root = ET.fromstring(original)
                require(root.findtext("Repository") == app["Config"]["Image"], "Template changed during update")
                (directory / "template-before.xml").write_bytes(original)
                os.chmod(directory / "template-before.xml", 0o600)
                root.find("Repository").text = reference
                updated_template = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                (directory / "template-after.xml").write_bytes(updated_template)
                os.chmod(directory / "template-after.xml", 0o600)
                atomic_bytes(template, updated_template)
            self.release()
            self.stage(directory, record, "checking-scoped-service",
                       write_pause_seconds=round(time.monotonic() - pause_started, 3), image_reference=reference)
            service = self.scoped_checks(self.app)
            save(directory / "service-proof.json", service)
            quality_reports = []
            for index, quality in enumerate(qualities):
                require(sha256(quality["suite"]) == quality["sha256"], "Quality suite changed after preflight")
                report_path = Path(quality["report_directory"]) / ("update-" + record["id"] + "-" + str(index) + ".json")
                run([sys.executable, Path(__file__).with_name("lightrag_check.py"), "--suite", quality["suite"],
                     "--report", report_path, "--timeout", "300", "--expected-suite-sha256", quality["sha256"],
                     "--expected-origin", quality["origin"], "--expected-workspace", quality["workspace"]])
                report = load(report_path)
                require(report.get("status") == "passed" and report.get("suite_sha256") == quality["sha256"],
                        "Post-update retrieval quality check failed")
                quality_reports.append({"path": str(report_path), "sha256": sha256(report_path)})
            self.stage(directory, record, "verified", status="verified", application_only=True,
                       previous_container_retained=True, database_restored=False, scoped_ui_api=True,
                       browser_interaction_tested=False, quality_reports=quality_reports,
                       retrieval_tested=bool(quality_reports))
            return {"update": str(directory), "status": "verified", "image_id": image["Id"],
                    "rollback_container": retained, "database_restored": False}
        except BaseException:
            self.stage(directory, record, "failed", status="failed", failure_stage=record["stage"])
            # Recovery is explicit and does not overwrite the current database.
            # Quality checks run after release, so do not claim it is still gated.
            raise RuntimeError("Update failed; inspect maintenance status. Use rollback --operation " + str(directory)) from None

    def rollback(self, directory, *, apply=False):
        directory = private_directory(directory)
        record = load(directory / "operation.json")
        require(record.get("owner") == OWNER and record.get("kind") == "update", "Not an owned application update")
        original = load(directory / "application-before.json")
        require(record["previous_id"] == original["Id"] and record["application"] == self.app, "Rollback identity mismatch")
        previous = self.inspect(record["previous_id"])
        validate_retained(original, previous)
        template = Path(self.config["unraid_template"]) if self.config.get("unraid_template") else None
        before_xml = rollback_template_bytes(directory, template) if template else None
        if not apply:
            return {"apply": False, "previous_id": previous["Id"], "database_restore": False,
                    "warning": "Runs previous application on current data; no schema downgrade"}
        existing = self.docker("ps", "-a", "--filter", "name=^/" + re.escape(self.app) + "$", "--format", "{{.ID}}").decode().split()
        require(len(existing) <= 1, "Ambiguous application container")
        current = self.inspect(existing[0]) if existing else None
        require(not previous["State"]["Running"] or (current is not None and current["Id"] == previous["Id"]),
                "Retained previous application is already running concurrently")
        if current and current["Id"] != previous["Id"]:
            if not record.get("candidate_id"):
                # Docker may have created the named container even if the create
                # reply was lost. Reconcile only our durable exact operation label
                # and prewritten configuration, never an unrelated same-name app.
                payload = load(directory / "candidate-payload.json")
                require((current["Config"].get("Labels") or {}).get(OWNER) == record["id"] and
                        current["Image"] == record["candidate_image"] and
                        canonical_mounts(current["Mounts"]) == canonical_mounts(original["Mounts"]) and
                        all(current["Config"].get(key) == payload.get(key) for key in
                            ("Image", "Env", "Entrypoint", "Cmd", "Labels")) and
                        current["HostConfig"] == payload["HostConfig"],
                        "Uncertain create result does not match the owned candidate; inspect manually")
                self.stage(directory, record, "candidate-reconciled", candidate_id=current["Id"])
            require(current["Id"] == record.get("candidate_id"), "Current service is not this operation's candidate")
            if current["State"]["Running"]:
                # Reuse saved gate if present, otherwise request a new drain.
                saved_token = load(directory / "maintenance-owner.json")["ownerToken"]
                gate = self.json_api("/maintenance", token=saved_token)
                if gate.get("active"):
                    require(gate.get("ownerMatches") and gate.get("drained"), "Candidate is not drained/owned")
                    self.token = saved_token
                else:
                    self.enter(directory)
                self.docker("update", "--restart=no", current["Id"])
                self.docker("stop", "-t", "120", current["Id"])
                stopped = self.inspect(current["Id"])
                require(not stopped["State"]["Running"] and not stopped["State"]["OOMKilled"]
                        and stopped["State"]["ExitCode"] == 0, "Candidate did not stop cleanly; keep both containers stopped")
            self.docker("rename", current["Id"], self.app + "-failed-" + record["id"].lower())
        if previous["Name"].lstrip("/") != self.app:
            self.docker("rename", previous["Id"], self.app)
        restart = original["HostConfig"]["RestartPolicy"]
        restart_flag = restart["Name"] or "no"
        if restart_flag == "on-failure" and restart.get("MaximumRetryCount"):
            restart_flag += ":" + str(restart["MaximumRetryCount"])
        self.docker("update", "--restart=" + restart_flag, previous["Id"])
        self.docker("start", previous["Id"])
        if before_xml is not None:
            require(rollback_template_bytes(directory, template) == before_xml,
                    "Unraid template rollback receipt changed after preflight")
            atomic_bytes(template, before_xml)
        self.token = load(directory / "maintenance-owner.json")["ownerToken"]
        deadline = time.monotonic() + 120
        while True:
            try:
                self.release()
                break
            except RuntimeError:
                require(time.monotonic() < deadline, "Previous service failed to restart; gate retained")
                time.sleep(1)
        proof = self.scoped_checks(self.app)
        restored = self.inspect(self.app)
        validate_retained(original, restored)
        require(restored["HostConfig"]["RestartPolicy"] == original["HostConfig"]["RestartPolicy"] and
                restored["State"]["Running"] and not restored["State"]["OOMKilled"], "Restored application did not match original settings")
        save(directory / "rollback-proof.json", proof)
        self.stage(directory, record, "rolled-back", status="rolled-back", database_restored=False)
        return {"status": "rolled-back", "database_restored": False, "previous_id": previous["Id"]}


def rollback_template_bytes(directory, template):
    """Validate even an interrupted template preparation before any app mutation."""
    before = Path(directory) / "template-before.xml"
    after = Path(directory) / "template-after.xml"
    if not before.exists() and not before.is_symlink():
        return None
    require(before.is_file() and not before.is_symlink(), "Invalid Unraid template rollback receipt")
    original = before.read_bytes()
    permitted = [original]
    if after.exists() or after.is_symlink():
        require(after.is_file() and not after.is_symlink(), "Invalid Unraid template candidate receipt")
        permitted.append(after.read_bytes())
    require(template.is_file() and not template.is_symlink() and template.read_bytes() in permitted,
            "Unraid template changed outside this operation")
    return original


def atomic_bytes(path, data):
    require(path.is_file() and not path.is_symlink(), "Atomic replacement requires regular existing file")
    original = path.stat()
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(original.st_mode))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, original.st_uid, original.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Private operator JSON; see operations guide")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="Read-only inventory and proposed strategy")
    commands.add_parser("backup", help="Gate writes, capture matching files+snapshot, resume, dump and verify")
    verify = commands.add_parser("verify-bundle")
    verify.add_argument("--bundle", type=Path, required=True)
    drill = commands.add_parser("restore-drill", help="Restore only into newly owned isolated containers")
    drill.add_argument("--bundle", type=Path, required=True)
    drill.add_argument("--keep-containers", action="store_true")
    copying = commands.add_parser("copy", help="Verified copy to an independently mounted/off-host destination")
    copying.add_argument("--bundle", type=Path, required=True)
    copying.add_argument("--destination", type=Path, required=True)
    retention = commands.add_parser("retention", help="Keep newest N plus failed and all referenced bundles")
    retention.add_argument("--keep", type=int, default=7)
    retention.add_argument("--apply", action="store_true")
    updating = commands.add_parser("update", help="Plan or apply a tested application-only image update")
    updating.add_argument("--bundle", type=Path, required=True)
    updating.add_argument("--image", required=True)
    updating.add_argument("--acceptance", type=Path, required=True)
    updating.add_argument("--apply", action="store_true")
    updating.add_argument("--quality-suite", action="append", type=Path, default=[])
    updating.add_argument("--quality-report-dir", type=Path)
    rollback = commands.add_parser("rollback", help="Plan or restore retained application, never old database")
    rollback.add_argument("--operation", type=Path, required=True)
    rollback.add_argument("--apply", action="store_true")
    release = commands.add_parser("release-maintenance", help="Explicit recovery after an interrupted backup")
    release.add_argument("--operation", type=Path, required=True)
    args = parser.parse_args(argv)
    require(args.config.is_file() and not args.config.is_symlink() and
            stat.S_IMODE(args.config.stat().st_mode) & 0o077 == 0, "Configuration must be a private regular file (0600)")
    os.umask(0o077)
    operations = Operations(load(args.config))
    if args.command == "plan":
        result = operations.plan()
    elif args.command == "backup":
        result = operations.backup()
    elif args.command == "verify-bundle":
        value = operations.verify_bundle(args.bundle)
        result = {"status": value["status"], "id": value["id"]}
    elif args.command == "restore-drill":
        result = operations.restore_drill(args.bundle, keep=args.keep_containers)
    elif args.command == "copy":
        result = operations.copy_bundle(args.bundle, args.destination)
    elif args.command == "retention":
        result = operations.retention(args.keep, apply=args.apply)
    elif args.command == "update":
        result = operations.update(args.bundle, args.image, args.acceptance, apply=args.apply,
                                   quality_suites=args.quality_suite, quality_report_directory=args.quality_report_dir)
    elif args.command == "rollback":
        result = operations.rollback(args.operation, apply=args.apply)
    else:
        operation = load(args.operation / "operation.json")
        require(operation.get("owner") == OWNER, "Not an owned operation")
        operations.token = load(args.operation / "maintenance-owner.json")["ownerToken"]
        operations.release()
        result = {"maintenance_released": True}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
