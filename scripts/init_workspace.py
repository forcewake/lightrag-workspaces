#!/usr/bin/env python3
"""Explicitly create an empty corpus directory; routed query requests never create one."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re

WORKSPACE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def initialize_workspace(storage_root: Path, input_root: Path, workspace_id: str) -> bool:
    """Return True for a new empty workspace; existing data is never rewritten."""
    if not WORKSPACE_PATTERN.fullmatch(workspace_id):
        raise ValueError("Use 1-128 ASCII letters, digits, dots, hyphens or underscores; start with a letter or digit")
    if storage_root.is_symlink() or input_root.is_symlink():
        raise ValueError("Storage and input roots must not be symbolic links")
    storage_root, input_root = storage_root.resolve(), input_root.resolve()
    if storage_root.is_relative_to(input_root) or input_root.is_relative_to(storage_root):
        raise ValueError("Storage and input roots must be separate, non-nested directories")
    storage, inputs = storage_root / workspace_id, input_root / workspace_id
    for folder in (storage, inputs):
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            raise ValueError("Workspace paths must be real directories, never links or files")
    if storage.is_dir():
        return False
    storage_root.mkdir(parents=True, exist_ok=True)
    input_root.mkdir(parents=True, exist_ok=True)
    made_inputs = not inputs.exists()
    if made_inputs:
        inputs.mkdir(mode=0o700)
    try:
        storage.mkdir(mode=0o700)
    except FileExistsError:
        if storage.is_symlink() or not storage.is_dir():
            if made_inputs:
                inputs.rmdir()
            raise ValueError("Workspace changed while it was being initialized") from None
        return False
    except BaseException:
        if made_inputs:
            inputs.rmdir()
        raise
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace_id", help="Exact directory name; never sanitized or renamed")
    parser.add_argument("--storage-root", type=Path,
                        default=Path(os.environ.get("WORKING_DIR", "/app/data/rag_storage")))
    parser.add_argument("--input-root", type=Path,
                        default=Path(os.environ.get("INPUT_DIR", "/app/data/inputs")))
    args = parser.parse_args(argv)
    try:
        created = initialize_workspace(args.storage_root, args.input_root, args.workspace_id)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Workspace initialization refused: {error}\n")
    print(f"{'Created empty workspace' if created else 'Kept existing workspace unchanged'}: {args.workspace_id}")


if __name__ == "__main__":
    main()
