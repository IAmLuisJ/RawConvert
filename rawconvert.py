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
import shutil
import subprocess
import sys
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


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

DNG_CONVERTER_APP = ("/Applications/Adobe DNG Converter.app"
                     "/Contents/MacOS/Adobe DNG Converter")
EXIFTOOL_URL = "https://exiftool.org"
DNG_CONVERTER_URL = ("https://helpx.adobe.com/camera-raw/using/"
                     "adobe-dng-converter.html")


def run(cmd):
    """Run a command; return (returncode, stdout_bytes, stderr_text)."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc.returncode, proc.stdout, proc.stderr.decode(errors="replace")


def have_sips() -> bool:
    return shutil.which("sips") is not None


def have_exiftool() -> bool:
    return shutil.which("exiftool") is not None


def dng_converter():
    """Path to the Adobe DNG Converter binary, or None if not installed."""
    return DNG_CONVERTER_APP if os.path.exists(DNG_CONVERTER_APP) else None


def image_dimensions(path: Path):
    """(width, height) in pixels, or None if the file can't be read."""
    rc, out, _ = run(["sips", "-g", "pixelWidth", "-g", "pixelHeight",
                      str(path)])
    if rc == 0:
        width = height = None
        for line in out.decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("pixelWidth:"):
                width = _int_or_none(line.split(":", 1)[1])
            elif line.startswith("pixelHeight:"):
                height = _int_or_none(line.split(":", 1)[1])
        if width and height:
            return (width, height)
    if have_exiftool():
        rc, out, _ = run(["exiftool", "-s3", "-ImageWidth", "-ImageHeight",
                          str(path)])
        parts = out.decode(errors="replace").split()
        if rc == 0 and len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return (int(parts[0]), int(parts[1]))
    return None


def _int_or_none(text):
    try:
        return int(text.strip())
    except ValueError:
        return None


def human_size(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return "%.1f %s" % (nbytes, unit)
        nbytes /= 1024.0
    return "%.1f TB" % nbytes


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_doctor() -> int:
    """Report tool availability per output format. Returns 0 if sips is OK."""
    print("rawconvert doctor — %s, Python %s" %
          (sys.platform, sys.version.split()[0]))
    print()
    sips_ok = have_sips()
    exif_ok = have_exiftool()
    dng_ok = dng_converter() is not None

    print("  sips (macOS built-in): %s" % ("OK" if sips_ok else "MISSING"))
    if exif_ok:
        print("  exiftool: OK")
    else:
        print("  exiftool: MISSING — install from %s" % EXIFTOOL_URL)
        print("      (or `brew install exiftool` if you use Homebrew)")
    if dng_ok:
        print("  Adobe DNG Converter: OK")
    else:
        print("  Adobe DNG Converter: MISSING — free download: %s"
              % DNG_CONVERTER_URL)
    print()
    print("Format readiness:")
    print("  jpeg: %s" % (
        "ready (embedded-JPEG extraction + full metadata)" if exif_ok and sips_ok
        else "usable via sips re-render; install exiftool for camera-rendered"
             " JPEGs and full metadata" if sips_ok else "NOT READY"))
    print("  heic: %s" % ("ready" if sips_ok else "NOT READY"))
    print("  dng:  %s" % ("ready" if dng_ok
                          else "NOT READY — install Adobe DNG Converter"))
    return 0 if sips_ok else 1


def cmd_scan(root: Path) -> int:
    """Inventory RAW files: counts, sizes, and estimated savings."""
    files = find_raw_files(root)
    total = 0
    by_ext = {}
    for path in files:
        size = path.stat().st_size
        total += size
        ext = path.suffix.lower()
        by_ext[ext] = (by_ext.get(ext, (0, 0))[0] + 1,
                       by_ext.get(ext, (0, 0))[1] + size)
    print("%d RAW files, %s total, under %s" %
          (len(files), human_size(total), root))
    for ext in sorted(by_ext):
        count, size = by_ext[ext]
        print("  %s: %d files, %s" % (ext, count, human_size(size)))
    if files:
        print("Estimated space after conversion: ~%s as JPEG/HEIC (~75%% saved),"
              " ~%s as lossy DNG (~55%% saved)" %
              (human_size(total * 0.25), human_size(total * 0.45)))
    return 0
