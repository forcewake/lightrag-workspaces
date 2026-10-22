#!/usr/bin/env python3
"""Compare stock/bounded loading on an isolated synthetic native-format index."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time


def file_hash(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def memory_high_water():
    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    maximum_bytes = maximum if sys.platform == "darwin" else maximum * 1024
    result = {"peak_rss_bytes": maximum_bytes,
              "peak_rss_source": "getrusage.ru_maxrss",
              "ru_maxrss_bytes": maximum_bytes}
    if sys.platform.startswith("linux"):
        # ru_maxrss can retain the fixture-generating parent's fork/exec peak.
        # VmHWM belongs to this process's current address space after exec.
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                fields = line.split()
                if fields and fields[0] == "VmHWM:" and fields[2:] == ["kB"]:
                    result.update(peak_rss_bytes=int(fields[1]) * 1024,
                                  peak_rss_source="proc.status.VmHWM")
                    break
        except (OSError, ValueError):
            pass
    return result


def measure(mode, path, dimension):
    import numpy as np
    import nano_vectordb.dbs as nano
    if mode == "bounded":
        from worker import load_nano_storage_bounded, normalize_nano_in_place
        nano.load_storage = load_nano_storage_bounded
        nano.NanoVectorDB.pre_process = normalize_nano_in_place
    before = file_hash(path)
    start = time.monotonic()
    database = nano.NanoVectorDB(dimension, storage_file=str(path))
    load_seconds = time.monotonic() - start
    matrix = database._NanoVectorDB__storage["matrix"]
    query = np.arange(1, dimension + 1, dtype=np.float32)
    result = database.query(query, top_k=10)
    payload = {"mode": mode, "rows": len(database), "dimension": dimension,
               "matrix_sha256": hashlib.sha256(memoryview(matrix)).hexdigest(),
               "metadata_sha256": hashlib.sha256(json.dumps(database._NanoVectorDB__storage["data"], sort_keys=True).encode()).hexdigest(),
               "query_ids": [value["__id__"] for value in result],
               "query_scores": [float(value["__metrics__"]) for value in result],
               "load_seconds": round(load_seconds, 4), "file_unchanged": file_hash(path) == before,
               "python": sys.version.split()[0], "numpy": np.__version__}
    payload.update(memory_high_water())
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=4096)
    parser.add_argument("--dimension", type=int, default=3072)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("nano-memory-benchmark.json"))
    parser.add_argument("--mode", choices=("stock", "bounded"))
    parser.add_argument("--file", type=Path)
    args = parser.parse_args()
    if args.mode:
        print(json.dumps(measure(args.mode, args.file, args.dimension)), flush=True)
        return
    import numpy as np
    import nano_vectordb.dbs as nano
    with tempfile.TemporaryDirectory(prefix="lightrag-synthetic-vectors-") as directory:
        fixture = Path(directory) / "synthetic.json"
        random = np.random.default_rng(1234)
        matrix = random.normal(size=(args.rows, args.dimension)).astype(np.float32)
        data = [{"__id__": f"synthetic-{index:06}", "content": "Synthetic Unicode 中文 😃 fixture"}
                for index in range(args.rows)]
        storage = {"embedding_dim": args.dimension, "data": data, "matrix": nano.array_to_buffer_string(matrix)}
        with fixture.open("w") as stream:
            json.dump(storage, stream, ensure_ascii=False)
        del storage, matrix, data
        results = []
        for mode in ("stock", "bounded"):
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--mode", mode,
                                      "--file", str(fixture), "--dimension", str(args.dimension)],
                                     capture_output=True, text=True, check=True, timeout=180)
            results.append(json.loads(process.stdout))
        stock, bounded = results
        equivalent = all(stock[key] == bounded[key] for key in
                         ("rows", "dimension", "matrix_sha256", "metadata_sha256", "query_ids", "query_scores"))
        report = {"passed": equivalent and all(item["file_unchanged"] for item in results),
                  "fixture_file_bytes": fixture.stat().st_size, "fixture_contains_non_bmp_metadata": True,
                  "results": results,
                  "peak_reduction_percent": round(100 * (1 - bounded["peak_rss_bytes"] / stock["peak_rss_bytes"]), 2),
                  "no_real_corpus_read": True}
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)
        if not report["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
