#!/usr/bin/env python3
"""rawconvert — safely convert Canon CR2/CR3 RAW files to JPEG, HEIC, or lossy DNG.

Workflow: doctor -> scan -> convert (--sample first) -> status -> verify -> cleanup.
Nothing is ever hard-deleted: cleanup moves files into _rawconvert_trash/ for
manual review. See README.md.

Stdlib-only; shells out to sips (built-in), exiftool (optional) and
Adobe DNG Converter (optional).
"""
from __future__ import annotations

import csv
import datetime
import os
from pathlib import Path

RAW_EXTS = {".cr2", ".cr3"}
FORMATS = {"jpeg": ".jpg", "heic": ".heic", "dng": ".dng"}
TRASH_DIRNAME = "_rawconvert_trash"
MANIFEST_NAME = "_rawconvert_manifest.csv"
MANIFEST_FIELDS = [
    "source_relpath", "format", "output_relpath", "src_bytes",
    "out_bytes", "engine", "status", "timestamp", "note",
]
# statuses: converted | verified | failed | collision | cleaned


class EngineError(RuntimeError):
    """A conversion engine failed for one file."""


def find_raw_files(root: Path) -> list:
    """All CR2/CR3 files under root (case-insensitive), skipping the trash dir."""
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in RAW_EXTS:
            continue
        if TRASH_DIRNAME in path.relative_to(root).parts:
            continue
        found.append(path)
    return found


class Manifest:
    """Per-folder conversion state, one CSV row per (source file, format)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / MANIFEST_NAME
        self._rows = {}  # (source_relpath, format) -> dict

    def load(self) -> None:
        if not self.path.exists():
            return
        with open(self.path, newline="") as f:
            for row in csv.DictReader(f):
                self._rows[(row["source_relpath"], row["format"])] = row

    def get(self, rel: str, fmt: str):
        return self._rows.get((rel, fmt))

    def set(self, rel: str, fmt: str, **fields) -> None:
        row = self._rows.setdefault(
            (rel, fmt), {field: "" for field in MANIFEST_FIELDS})
        row["source_relpath"] = rel
        row["format"] = fmt
        for key, value in fields.items():
            if key not in MANIFEST_FIELDS:
                raise KeyError("unknown manifest field: %s" % key)
            row[key] = str(value)
        row["timestamp"] = datetime.datetime.now().isoformat(timespec="seconds")

    def rows(self) -> list:
        return [self._rows[key] for key in sorted(self._rows)]

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp~")
        with open(tmp, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            for row in self.rows():
                writer.writerow(row)
        os.replace(tmp, self.path)
