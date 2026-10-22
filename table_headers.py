"""Preserve native Markdown column headers in the text that LightRAG indexes.

The pinned Markdown adapter separates GFM headers from data rows. Including
them in the IR body lets the stock sidecar writer and chunker account for their
text, hashes, provenance and token cost before indexing. Existing indexes are
never rewritten by this hook.
"""
from __future__ import annotations

from dataclasses import replace
import functools
import hashlib
import importlib
import inspect
from typing import Any, Callable


SUPPORTED_VERSION = "1.5.7"
BUILD_TABLE_SHA256 = "55170f23bbfd5d9f311ded003507fc0e0df4ae77201af143acf2c10396663629"
_ORIGINAL_ATTRIBUTE = "_workspaces_table_headers_original"


def _build_header_complete_table(original: Callable, placeholder: str, spec: dict) -> Any:
    # HTML already includes its <thead>. Other adapters, including DOCX, do not
    # use this Markdown-specific function and keep their native representation.
    header = spec.get("header") if spec.get("kind") != "html" else None
    if header:
        if (not isinstance(header, list) or not all(
            isinstance(row, list) and row and all(isinstance(cell, str) for cell in row)
            for row in header
        )):
            raise ValueError("Native Markdown table header must be a nonempty string grid")
        header = [row.copy() for row in header]
    table = original(placeholder, spec)
    if not header:
        return table
    # Always prepend: this adapter's input contains data rows only. A data row
    # equal to the header is still data and must never be heuristically removed.
    rows = header + table.rows
    return replace(table, rows=rows, num_rows=len(rows), table_header=header,
                   num_cols=max(table.num_cols, max(map(len, header))))


def install_table_header_preservation() -> bool:
    """Install the pinned, process-local adapter hook; return whether it changed.

    Refuse upstream drift before changing anything. Repeated calls validate the
    original implementation again and leave the same wrapper in place.
    """
    lightrag = importlib.import_module("lightrag")
    builder = importlib.import_module("lightrag.parser.markdown.ir_builder")
    if lightrag.__version__ != SUPPORTED_VERSION:
        raise RuntimeError(f"Table headers require LightRAG {SUPPORTED_VERSION}; found {lightrag.__version__}")
    current = builder._build_table
    original = getattr(current, _ORIGINAL_ATTRIBUTE, current)
    try:
        digest = hashlib.sha256(inspect.getsource(original).encode()).hexdigest()
    except (OSError, TypeError) as error:
        raise RuntimeError("Cannot verify the pinned Markdown table adapter") from error
    if digest != BUILD_TABLE_SHA256:
        raise RuntimeError("Unsupported LightRAG Markdown table adapter; source SHA256 differs")
    if original is not current:
        return False

    @functools.wraps(original)
    def header_complete_table(placeholder: str, spec: dict) -> Any:
        return _build_header_complete_table(original, placeholder, spec)

    setattr(header_complete_table, _ORIGINAL_ATTRIBUTE, original)
    builder._build_table = header_complete_table
    return True
