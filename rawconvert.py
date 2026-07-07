#!/usr/bin/env python3
"""rawconvert — safely convert Canon CR2/CR3 RAW files to JPEG, HEIC, or lossy DNG.

Workflow: doctor -> scan -> convert (--sample first) -> status -> verify -> cleanup.
Nothing is ever hard-deleted: cleanup moves files into _rawconvert_trash/ for
manual review. See README.md.

Stdlib-only; shells out to sips (built-in), exiftool (optional) and
Adobe DNG Converter (optional).
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RAW_EXTS = {".cr2", ".cr3"}
FORMATS = {"jpeg": ".jpg", "heic": ".heic", "dng": ".dng"}
TRASH_DIRNAME = "_rawconvert_trash"
MANIFEST_NAME = "_rawconvert_manifest.csv"
ERROR_LOG_NAME = "_rawconvert_errors.log"
MANIFEST_FIELDS = [
    "source_relpath", "format", "output_relpath", "output_root", "src_bytes",
    "out_bytes", "engine", "status", "timestamp", "note",
]
# output_root: absolute path when outputs live outside the scanned folder
# (e.g. another drive); empty when they sit next to their sources.
# statuses: converted | verified | failed | collision | cleaned


class EngineError(RuntimeError):
    """A conversion engine failed for one file."""


def find_raw_files(root: Path, recurse: bool = True) -> list:
    """All CR2/CR3 files under root (case-insensitive), skipping the trash dir.

    With recurse=False, only the top level of root is considered.
    """
    found = []
    for path in sorted(root.rglob("*") if recurse else root.glob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in RAW_EXTS:
            continue
        if path.name.startswith("."):
            # hidden files, esp. "._*" AppleDouble metadata sidecars that
            # macOS writes on FAT/exFAT drives — not real photos
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
                # tolerate manifests written before newer columns existed
                row = {field: row.get(field) or "" for field in MANIFEST_FIELDS}
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


def extract_embedded_jpeg(src: Path, dst: Path) -> bool:
    """Extract the camera-rendered full-size JPEG embedded in a RAW file.

    Returns False when exiftool is missing, no embedded JPEG exists, or the
    embedded image is undersized (<90% of the RAW width) — callers then fall
    back to a sips re-render.
    """
    if not have_exiftool():
        return False
    src_dims = image_dimensions(src)
    for tag in ("-JpgFromRaw", "-PreviewImage"):
        rc, out, _ = run(["exiftool", "-b", tag, str(src)])
        if rc != 0 or not out:
            continue
        dst.write_bytes(out)
        if src_dims:
            got = image_dimensions(dst)
            if not got or max(got) < 0.9 * max(src_dims):
                dst.unlink()
                continue
        return True
    return False


def sips_convert(src: Path, dst: Path, fmt: str, quality: int) -> None:
    """Render src to dst via Apple's RAW engine (fmt: 'jpeg' or 'heic')."""
    args = ["sips", "-s", "format", fmt,
            "-s", "formatOptions", str(quality),
            str(src), "--out", str(dst)]
    rc, out, err = run(args)
    if rc != 0 or not dst.exists():
        detail = err.strip() or out.decode(errors="replace").strip()
        raise EngineError("sips failed: %s" % detail)


def dng_convert(src: Path, dst: Path) -> None:
    """Convert src to lossy-compressed DNG via Adobe DNG Converter."""
    app = dng_converter()
    if app is None:
        raise EngineError("Adobe DNG Converter is not installed "
                          "(run: rawconvert.py doctor)")
    rc, _, err = run([app, "-lossy", "-p1",
                      "-d", str(dst.parent), "-o", dst.name, str(src)])
    if rc != 0 or not dst.exists():
        raise EngineError("DNG Converter failed: %s" % err.strip())


def dng_convert_batch(srcs, staging_dir: Path) -> str:
    """Convert many RAWs in ONE Adobe DNG Converter launch.

    Outputs land in staging_dir as <stem>.dng. The converter reports errors
    per file on stderr but keeps going, so the return code is unreliable for
    batches — callers must treat each missing/empty output as that file's
    failure. Returns the batch stderr for attribution.
    """
    app = dng_converter()
    if app is None:
        raise EngineError("Adobe DNG Converter is not installed "
                          "(run: rawconvert.py doctor)")
    _, _, err = run([app, "-lossy", "-p1", "-d", str(staging_dir)]
                    + [str(s) for s in srcs])
    return err


def copy_metadata(src: Path, dst: Path) -> None:
    """Copy EXIF/GPS/date tags from the RAW to the output (needs exiftool)."""
    if not have_exiftool():
        return
    run(["exiftool", "-overwrite_original", "-quiet",
         "-TagsFromFile", str(src),
         "-all:all", "--previewimage", "--jpgfromraw", "--thumbnailimage",
         str(dst)])


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


def scan_stats(root: Path, recurse: bool = True) -> dict:
    """Structured inventory of RAW files under root (no printing)."""
    total = 0
    by_ext = {}
    files = find_raw_files(root, recurse=recurse)
    for path in files:
        size = path.stat().st_size
        total += size
        entry = by_ext.setdefault(path.suffix.lower(),
                                  {"count": 0, "bytes": 0})
        entry["count"] += 1
        entry["bytes"] += size
    return {
        "files": len(files),
        "total_bytes": total,
        "by_ext": by_ext,
        "est_jpeg_heic_bytes": int(total * 0.25),
        "est_dng_low_bytes": int(total * 0.06),
        "est_dng_high_bytes": int(total * 0.55),
    }


def cmd_scan(root: Path, recurse: bool = True) -> int:
    """Inventory RAW files: counts, sizes, and estimated savings."""
    stats = scan_stats(root, recurse=recurse)
    print("%d RAW files, %s total, under %s" %
          (stats["files"], human_size(stats["total_bytes"]), root))
    for ext in sorted(stats["by_ext"]):
        entry = stats["by_ext"][ext]
        print("  %s: %d files, %s"
              % (ext, entry["count"], human_size(entry["bytes"])))
    if stats["files"]:
        print("Estimated space after conversion:")
        print("  jpeg/heic: ~%s  (~75%% saved)"
              % human_size(stats["est_jpeg_heic_bytes"]))
        print("  lossy dng: ~%s - %s  (45%%-94%% saved; varies by camera —"
              " high-megapixel CR3s measured near the top end)"
              % (human_size(stats["est_dng_low_bytes"]),
                 human_size(stats["est_dng_high_bytes"])))
    return 0


MANIFEST_FLUSH_EVERY = 25


def resolve_output(root: Path, row: dict):
    """Absolute path of a manifest row's output, honoring its output_root."""
    if not row["output_relpath"]:
        return None
    base = Path(row["output_root"]) if row["output_root"] else root
    return base / row["output_relpath"]


# ---------------------------------------------------------------------------
# Failure classification and error log
# ---------------------------------------------------------------------------

MIN_PLAUSIBLE_RAW_BYTES = 1024 * 1024  # real CR2/CR3 files are many MB

FAILURE_CODES = {
    "RC01": ("HIDDEN_METADATA_FILE",
             "This is a macOS metadata sidecar (AppleDouble '._*' file)"
             " created on FAT/exFAT drives, not a real photo.",
             ["Nothing to fix — it contains Finder metadata, no image.",
              "rawconvert now skips these automatically; re-run convert.",
              "To stop macOS creating them: `dot_clean /Volumes/<drive>`."]),
    "RC02": ("NOT_A_VALID_RAW",
             "The file is far too small to be a real CR2/CR3 photo"
             " (under 1 MB); it is likely truncated or a stray file.",
             ["Check its size in Finder — a real RAW is typically 20-40 MB.",
              "If this photo matters, restore it from another copy/card.",
              "Otherwise it is safe to leave; cleanup will never touch it."]),
    "RC03": ("UNSUPPORTED_OR_CORRUPT",
             "macOS could not decode this RAW — either the file is corrupt"
             " or this camera's RAW flavor isn't supported by this macOS"
             " version.",
             ["Open the file in Preview or press Space in Finder — does it"
              " render? If not, the file is likely corrupt.",
              "Run `exiftool <file>` — if exiftool also can't read it,"
              " corruption is almost certain.",
              "If it renders fine elsewhere, check Apple's supported-camera"
              " list (search 'Apple ProRAW supported cameras sips') or"
              " update macOS.",
              "For --to jpeg with exiftool installed, the embedded JPEG"
              " may still extract even when sips can't decode the RAW."]),
    "RC04": ("ENGINE_MISSING",
             "The external tool needed for this format is not installed.",
             ["Run `python3 rawconvert.py doctor` and follow the install"
              " links."]),
    "RC05": ("DISK_FULL",
             "The destination drive has no space left.",
             ["Free space on the output drive, or point --output at a"
              " different drive.",
              "Re-run the same convert command — finished files are"
              " skipped automatically."]),
    "RC06": ("PERMISSION_DENIED",
             "macOS blocked access to the file or destination folder.",
             ["Check System Settings > Privacy & Security > Files and"
              " Folders (or Full Disk Access) for your terminal app.",
              "Check the drive isn't mounted read-only: `mount | grep"
              " Volumes`."]),
    "RC99": ("UNKNOWN",
             "Unrecognized failure — the raw engine output is preserved"
             " below.",
             ["Read the engine error text above.",
              "Try converting the single file manually, e.g. `sips -s"
              " format jpeg <file> --out /tmp/test.jpg`, to reproduce.",
              "If several files fail the same way, re-run with --sample on"
              " a copy and report the log."]),
}


def classify_failure(src: Path, src_bytes: int, message: str) -> dict:
    """Map a failed conversion to an error code with debug steps."""
    text = message.lower()
    if src.name.startswith("._"):
        code = "RC01"
    elif src_bytes < MIN_PLAUSIBLE_RAW_BYTES:
        code = "RC02"
    elif "not installed" in text:
        code = "RC04"
    elif "no space left" in text or "disk full" in text:
        code = "RC05"
    elif "permission denied" in text or "read-only" in text:
        code = "RC06"
    elif "cannot extract image" in text or "unable to decode" in text:
        code = "RC03"
    else:
        code = "RC99"
    name, diagnosis, steps = FAILURE_CODES[code]
    return {"code": code, "name": name, "diagnosis": diagnosis,
            "steps": steps}


def log_failure(root: Path, rel: str, src: Path, fmt: str, engine: str,
                message: str) -> dict:
    """Append a classified failure to the error log; returns the class info."""
    try:
        src_bytes = src.stat().st_size
    except OSError:
        src_bytes = 0
    info = classify_failure(src, src_bytes, message)
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")
    lines = [
        "[%s] %s %s" % (timestamp, info["code"], info["name"]),
        "  file: %s (%s)" % (rel, human_size(src_bytes)),
        "  operation: convert --to %s (engine: %s)" % (fmt, engine or "n/a"),
        "  engine error: %s" % message.replace("\n", "\n      "),
        "  diagnosis: %s" % info["diagnosis"],
        "  debug steps:",
    ]
    lines += ["    %d. %s" % (i, step)
              for i, step in enumerate(info["steps"], 1)]
    with open(root / ERROR_LOG_NAME, "a") as f:
        f.write("\n".join(lines) + "\n\n")
    return info


def _root_field(out_base: Path, root: Path) -> str:
    """Manifest output_root value: empty for in-place outputs."""
    return "" if out_base == root else str(out_base)


def _eta(elapsed: float, done: int, remaining: int) -> str:
    seconds = elapsed / done * remaining
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % (seconds / 60)
    return "%.1fh" % (seconds / 3600)


DNG_STAGING_DIRNAME = ".rawconvert_dng_staging"


def _emit(obj) -> None:
    """One machine-readable JSON event per line (used by the GUI)."""
    print(json.dumps(obj), flush=True)


def cmd_convert(root: Path, fmt: str, quality: int = 90, sample=None,
                dry_run: bool = False, output_root=None,
                recurse: bool = True, force_render: bool = False,
                quiet: bool = False, batch_size: int = 1,
                progress_json: bool = False) -> dict:
    """Convert every RAW under root to fmt. Idempotent and resumable.

    With output_root, outputs mirror the source folder structure under that
    directory (e.g. on another drive) instead of sitting next to the RAWs.
    With force_render, JPEGs skip the embedded-JPEG extraction and are always
    re-rendered by sips, so `quality` takes effect.
    With batch_size > 1 (DNG only), that many files are converted per Adobe
    DNG Converter launch, amortizing the app's startup cost.
    """
    ext = FORMATS[fmt]
    out_base = Path(output_root).expanduser().resolve() if output_root else root
    manifest = Manifest(root)
    manifest.load()
    files = find_raw_files(root, recurse=recurse)
    if sample:
        files = files[:sample]
    counts = {"converted": 0, "skipped": 0, "failed": 0, "collision": 0}
    pending = 0
    started = time.monotonic()
    use_batch = fmt == "dng" and batch_size > 1
    if batch_size > 1 and fmt != "dng" and not progress_json:
        print("Note: --batch-size applies to DNG only; ignored for %s." % fmt)
    if progress_json:
        if not dry_run:
            _emit({"type": "start", "total": len(files), "format": fmt})
    elif not quiet and not dry_run:
        print("%d RAW files to process under %s" % (len(files), root),
              flush=True)

    def record_success(index, src, rel, dst, out_rel, engine):
        nonlocal pending
        copy_metadata(src, dst)
        shutil.copystat(str(src), str(dst))
        manifest.set(rel, fmt, output_relpath=out_rel,
                     output_root=_root_field(out_base, root),
                     src_bytes=src.stat().st_size,
                     out_bytes=dst.stat().st_size,
                     engine=engine, status="converted", note="")
        counts["converted"] += 1
        pending += 1
        if progress_json:
            out_bytes = dst.stat().st_size
            src_size = src.stat().st_size
            _emit({"type": "progress", "index": index, "total": len(files),
                   "source": rel, "output": out_rel,
                   "src_bytes": src_size, "out_bytes": out_bytes,
                   "eta_seconds": int((time.monotonic() - started)
                                      / counts["converted"]
                                      * (len(files) - index))})
        elif not quiet:
            out_bytes = dst.stat().st_size
            src_size = src.stat().st_size
            print("[%d/%d] %s -> %s  %s (%.0f%% of RAW)  ETA %s"
                  % (index, len(files), rel, out_rel,
                     human_size(out_bytes),
                     100.0 * out_bytes / src_size if src_size else 0,
                     _eta(time.monotonic() - started,
                          counts["converted"], len(files) - index)),
                  flush=True)

    def record_failure(index, src, rel, out_rel, engine, message):
        nonlocal pending
        info = log_failure(root, rel, src, fmt, engine, message)
        manifest.set(rel, fmt, output_relpath=out_rel,
                     output_root=_root_field(out_base, root),
                     engine=engine, status="failed",
                     note=("[%s] %s: %s"
                           % (info["code"], info["name"], message))[:300])
        counts["failed"] += 1
        pending += 1
        if progress_json:
            _emit({"type": "failed", "source": rel, "code": info["code"],
                   "name": info["name"], "diagnosis": info["diagnosis"]})
        else:
            print("FAILED [%s %s]: %s — %s"
                  % (info["code"], info["name"], rel, info["diagnosis"]))

    batch = []  # (index, src, rel, dst, out_rel); all share one dst.parent

    def flush_batch():
        if not batch:
            return
        staging = batch[0][3].parent / DNG_STAGING_DIRNAME
        staging.mkdir(exist_ok=True)
        err = dng_convert_batch([entry[1] for entry in batch], staging)
        for index, src, rel, dst, out_rel in batch:
            produced = staging / (src.stem + ".dng")
            if produced.exists() and produced.stat().st_size > 0:
                os.replace(produced, dst)
                record_success(index, src, rel, dst, out_rel,
                               "dngconverter-batch")
            else:
                record_failure(index, src, rel, out_rel, "dngconverter-batch",
                               "no output produced in batch mode;"
                               " batch stderr: %s" % err.strip())
        batch.clear()
        try:
            staging.rmdir()
        except OSError:
            pass  # stale files from an interrupted run; harmless

    for index, src in enumerate(files, 1):
        rel = str(src.relative_to(root))
        dst = out_base / src.relative_to(root).with_suffix(ext)
        out_rel = str(dst.relative_to(out_base))
        row = manifest.get(rel, fmt)

        if row and row["status"] in ("converted", "verified"):
            prev = resolve_output(root, row)
            if prev is not None and prev.exists():
                counts["skipped"] += 1
                continue
        if dst.exists() and (row is None or row["status"] == "collision"):
            # An output we did not create — never overwrite it.
            counts["collision"] += 1
            if progress_json:
                _emit({"type": "collision", "source": rel,
                       "output": str(dst)})
            else:
                print("COLLISION: %s already exists and was not created by"
                      " rawconvert; skipping %s" % (dst, rel))
            if not dry_run:
                manifest.set(rel, fmt, output_relpath=out_rel,
                             output_root=_root_field(out_base, root),
                             status="collision",
                             note="pre-existing output; not overwritten")
                pending += 1
            continue
        if dry_run:
            print("DRY-RUN: would convert %s -> %s" % (rel, dst))
            counts["converted"] += 1
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)

        if use_batch:
            if batch and batch[0][3].parent != dst.parent:
                flush_batch()
            batch.append((index, src, rel, dst, out_rel))
            if len(batch) >= batch_size:
                flush_batch()
        else:
            partial = dst.with_name(dst.name + ".partial")
            engine = ""
            try:
                if fmt == "jpeg":
                    if (not force_render
                            and extract_embedded_jpeg(src, partial)):
                        engine = "exiftool-embedded"
                    else:
                        engine = "sips"
                        sips_convert(src, partial, "jpeg", quality)
                elif fmt == "heic":
                    engine = "sips"
                    sips_convert(src, partial, "heic", quality)
                else:
                    engine = "dngconverter"
                    dng_convert(src, partial)
                os.replace(partial, dst)
                record_success(index, src, rel, dst, out_rel, engine)
            except EngineError as exc:
                if partial.exists():
                    partial.unlink()
                record_failure(index, src, rel, out_rel, engine, str(exc))

        if pending >= MANIFEST_FLUSH_EVERY:
            manifest.save()
            pending = 0

    if not dry_run:
        flush_batch()
    if not dry_run and pending:
        manifest.save()
    if progress_json:
        if not dry_run:
            _emit(dict(counts, type="convert_summary"))
    else:
        print("convert --to %s: %d converted, %d skipped, %d failed,"
              " %d collisions%s" %
              (fmt, counts["converted"], counts["skipped"], counts["failed"],
               counts["collision"], " (dry run)" if dry_run else ""))
        if counts["failed"]:
            print("Failure details and debug steps: %s"
                  % (root / ERROR_LOG_NAME))
    return counts


def _dims_match(src_dims, dst_dims) -> bool:
    """True if dimensions match within tolerance, allowing 90° rotation.

    Canon sensor dimensions reported for the RAW can differ from the rendered
    image by a few dozen pixels, so allow 2% (min 16 px) per axis.
    """
    def close(a, b):
        return abs(a - b) <= max(16, 0.02 * max(a, b))

    for cand in (dst_dims, (dst_dims[1], dst_dims[0])):
        if close(src_dims[0], cand[0]) and close(src_dims[1], cand[1]):
            return True
    return False


TIFF_MAGICS = (b"II*\x00", b"MM\x00*")


def _check_output(src: Path, dst: Path, fmt: str):
    """(ok, note) for one converted output."""
    if not dst.exists():
        return False, "output missing"
    if dst.stat().st_size == 0:
        return False, "output is zero bytes"
    if fmt == "dng":
        with open(dst, "rb") as f:
            if f.read(4) not in TIFF_MAGICS:
                return False, "not a valid TIFF/DNG header"
    src_dims = image_dimensions(src)
    dst_dims = image_dimensions(dst)
    if src_dims and dst_dims:
        if not _dims_match(src_dims, dst_dims):
            return False, ("dimension mismatch: raw %sx%s vs output %sx%s"
                           % (src_dims + dst_dims))
    elif dst_dims is None and fmt != "dng":
        return False, "output unreadable"
    return True, ""


def cmd_verify(root: Path, fmt: str) -> dict:
    """Validate every converted output for fmt; mark rows verified/failed."""
    manifest = Manifest(root)
    manifest.load()
    counts = {"verified": 0, "failed": 0}
    for row in manifest.rows():
        if row["format"] != fmt or row["status"] not in ("converted",
                                                         "verified"):
            continue
        src = root / row["source_relpath"]
        dst = resolve_output(root, row)
        ok, note = _check_output(src, dst, fmt)
        if ok:
            manifest.set(row["source_relpath"], fmt, status="verified",
                         note="")
            counts["verified"] += 1
        else:
            manifest.set(row["source_relpath"], fmt, status="failed",
                         note=note)
            counts["failed"] += 1
            print("VERIFY FAILED: %s (%s): %s"
                  % (row["source_relpath"], fmt, note))
    manifest.save()
    print("verify --to %s: %d verified, %d failed"
          % (fmt, counts["verified"], counts["failed"]))
    if counts["failed"]:
        print("Failed files stay protected: cleanup will not touch their"
              " originals.")
    return counts


def cmd_status(root: Path) -> int:
    """Per-format size comparison table from the manifest."""
    manifest = Manifest(root)
    manifest.load()
    stats = {}  # fmt -> [files, verified, src_bytes, out_bytes]
    for row in manifest.rows():
        if row["status"] not in ("converted", "verified", "cleaned"):
            continue
        entry = stats.setdefault(row["format"], [0, 0, 0, 0])
        entry[0] += 1
        if row["status"] in ("verified", "cleaned"):
            entry[1] += 1
        entry[2] += int(row["src_bytes"] or 0)
        entry[3] += int(row["out_bytes"] or 0)
    if not stats:
        print("No conversions recorded yet under %s" % root)
        return 0
    print("%-6s %10s %12s %12s %8s %10s" %
          ("format", "files", "raw size", "out size", "ratio", "saved"))
    for fmt in sorted(stats):
        files, verified, src_b, out_b = stats[fmt]
        ratio = (100.0 * out_b / src_b) if src_b else 0.0
        print("%-6s %10d %12s %12s %7.0f%% %10s   (%d verified)" %
              (fmt, files, human_size(src_b), human_size(out_b), ratio,
               human_size(src_b - out_b), verified))
    print("\nratio = output size as %% of RAW size (lower saves more space).")
    print("Open a few outputs in Preview to judge quality before cleanup.")
    return 0


def _stage(path: Path, dest: Path, dry_run: bool) -> None:
    if dry_run:
        print("DRY-RUN: would move %s -> %s" % (path, dest))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(path), str(dest))


def cmd_cleanup(root: Path, keep: str, dry_run: bool = False) -> dict:
    """Stage verified originals + rejected-format outputs into the trash dir.

    Only sources whose `keep`-format output is VERIFIED are staged. Nothing is
    deleted; the user reviews _rawconvert_trash/ and empties it manually.
    """
    manifest = Manifest(root)
    manifest.load()
    trash = root / TRASH_DIRNAME
    counts = {"originals_staged": 0, "rejected_staged": 0}

    for row in manifest.rows():
        rel = row["source_relpath"]
        if row["format"] == keep and row["status"] == "verified":
            src = root / rel
            if src.exists():
                _stage(src, trash / "originals" / rel, dry_run)
                counts["originals_staged"] += 1
                if not dry_run:
                    manifest.set(rel, keep, status="cleaned")
        elif (row["format"] != keep
              and row["status"] in ("converted", "verified")):
            out = resolve_output(root, row)
            if out is not None and out.exists():
                # stage on the drive the output lives on, so the move is
                # instant and never crosses volumes
                out_trash = (Path(row["output_root"]) if row["output_root"]
                             else root) / TRASH_DIRNAME
                _stage(out, out_trash / "rejected" / row["output_relpath"],
                       dry_run)
                counts["rejected_staged"] += 1
                if not dry_run:
                    manifest.set(rel, row["format"], status="cleaned")

    if not dry_run:
        manifest.save()
    print("cleanup --keep %s: %d originals and %d rejected outputs staged"
          " in %s%s" % (keep, counts["originals_staged"],
                        counts["rejected_staged"], trash,
                        " (dry run)" if dry_run else ""))
    if not dry_run and (counts["originals_staged"]
                        or counts["rejected_staged"]):
        print("Review that folder, then delete it yourself when satisfied.")
    return counts


def cmd_process(root: Path, fmt: str, quality: int = 90, sample=None,
                output_root=None, recurse: bool = True,
                force_render: bool = False, quiet: bool = False,
                batch_size: int = 1, dry_run: bool = False,
                progress_json: bool = False) -> int:
    """One-shot pipeline: scan, convert, verify, then stage cleanup.

    Only files whose outputs pass verification have their originals staged;
    failures keep their originals in place. Nothing is ever hard-deleted —
    the trash folder is still emptied manually.
    """
    if progress_json:
        _emit({"type": "step", "name": "convert"})
    else:
        print("== Step 1/4: scan ==")
        cmd_scan(root, recurse=recurse)
        print("\n== Step 2/4: convert (--to %s) ==" % fmt)
    ccounts = cmd_convert(root, fmt, quality=quality, sample=sample,
                          dry_run=dry_run, output_root=output_root,
                          recurse=recurse, force_render=force_render,
                          quiet=quiet, batch_size=batch_size,
                          progress_json=progress_json)
    if dry_run:
        if not progress_json:
            print("\nDry run — nothing was written. Run without --dry-run"
                  " for the full pipeline.")
        return 0
    if progress_json:
        _emit({"type": "step", "name": "verify"})
        vcounts, _ = _swallow_stdout(cmd_verify, root, fmt)
        _emit({"type": "step", "name": "cleanup"})
        scounts, _ = _swallow_stdout(cmd_cleanup, root, fmt)
    else:
        print("\n== Step 3/4: verify ==")
        vcounts = cmd_verify(root, fmt)
        print("\n== Step 4/4: cleanup (stage originals) ==")
        scounts = cmd_cleanup(root, fmt)
    failed = ccounts["failed"] + vcounts["failed"]
    if progress_json:
        _emit({"type": "summary", "converted": ccounts["converted"],
               "skipped": ccounts["skipped"],
               "verified": vcounts["verified"],
               "verify_failed": vcounts["failed"],
               "staged": scounts["originals_staged"], "failed": failed,
               "trash": str(root / TRASH_DIRNAME),
               "error_log": str(root / ERROR_LOG_NAME)})
    else:
        print("\nprocess complete: %d converted, %d verified, %d originals"
              " staged, %d failed"
              % (ccounts["converted"], vcounts["verified"],
                 scounts["originals_staged"], failed))
        if failed:
            print("Failed files kept their originals in place — details in"
                  " %s" % (root / ERROR_LOG_NAME))
        print("Spot-check %s and delete it yourself when satisfied."
              % (root / TRASH_DIRNAME))
    return 1 if failed else 0


def _swallow_stdout(func, *args, **kwargs):
    """Run func with its stdout captured; returns (result, output_text)."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = func(*args, **kwargs)
    return result, buf.getvalue()


def open_in_preview(paths) -> None:
    run(["open", "-a", "Preview"] + [str(p) for p in paths])


def cmd_compare(src: Path, quality: int = 90, out_dir=None,
                progress_json: bool = False) -> dict:
    """Convert one RAW to every available format/engine and open in Preview.

    JPEG is produced twice when possible — once from the camera's embedded
    JPEG (exiftool) and once re-rendered by Apple's RAW engine at `quality` —
    so the two engines can be compared directly. Outputs go to a temp folder
    (not next to the source) and are not recorded in any manifest — this is a
    throwaway quality/size trial.
    """
    if src.suffix.lower() not in RAW_EXTS:
        sys.exit("compare expects a single CR2/CR3 file, got: %s" % src)
    if out_dir is None:
        out_dir = Path(tempfile.mkdtemp(prefix="rawconvert_compare_"))
    src_bytes = src.stat().st_size
    results = {}

    def say(text):
        if not progress_json:
            print(text)

    def skipped(key, reason):
        if progress_json:
            _emit({"type": "compare_skipped", "format": key,
                   "reason": reason})

    say("Comparing formats for %s (%s)" % (src.name, human_size(src_bytes)))
    if progress_json:
        _emit({"type": "compare_start", "source": src.name,
               "src_bytes": src_bytes})

    def report(key, label, dst):
        copy_metadata(src, dst)
        out_bytes = dst.stat().st_size
        results[key] = dst
        if progress_json:
            _emit({"type": "compare", "format": key, "bytes": out_bytes,
                   "ratio": round(100.0 * out_bytes / src_bytes, 1),
                   "path": str(dst)})
        else:
            print("  %-24s %10s  %3.0f%% of RAW"
                  % (label, human_size(out_bytes),
                     100.0 * out_bytes / src_bytes))

    def failed(key, label, exc):
        if progress_json:
            _emit({"type": "compare_failed", "format": key,
                   "error": str(exc)})
        else:
            print("  %-24s FAILED — %s" % (label, exc))

    # JPEG, camera's embedded rendering (quality fixed at shoot time)
    label = "jpeg (camera-embedded):"
    if not have_exiftool():
        say("  %-24s skipped — exiftool not installed (see: doctor)" % label)
        skipped("jpeg-embedded", "exiftool not installed")
    else:
        dst = out_dir / (src.stem + "_embedded.jpg")
        try:
            if extract_embedded_jpeg(src, dst):
                report("jpeg-embedded", label, dst)
            else:
                say("  %-24s skipped — no usable full-size embedded JPEG"
                    % label)
                skipped("jpeg-embedded", "no usable embedded JPEG")
        except EngineError as exc:
            failed("jpeg-embedded", label, exc)

    # JPEG, Apple's RAW engine at the requested quality
    label = "jpeg (sips q%d):" % quality
    dst = out_dir / (src.stem + "_rendered.jpg")
    try:
        sips_convert(src, dst, "jpeg", quality)
        report("jpeg-rendered", label, dst)
    except EngineError as exc:
        failed("jpeg-rendered", label, exc)

    # HEIC (always rendered)
    label = "heic (sips q%d):" % quality
    dst = out_dir / (src.stem + ".heic")
    try:
        sips_convert(src, dst, "heic", quality)
        report("heic", label, dst)
    except EngineError as exc:
        failed("heic", label, exc)

    # Lossy DNG
    label = "dng (lossy):"
    if dng_converter() is None:
        say("  %-24s skipped — Adobe DNG Converter not installed"
            " (see: doctor)" % label)
        skipped("dng", "Adobe DNG Converter not installed")
    else:
        dst = out_dir / (src.stem + ".dng")
        try:
            dng_convert(src, dst)
            report("dng", label, dst)
        except EngineError as exc:
            failed("dng", label, exc)

    if results:
        say("Outputs kept in %s" % out_dir)
        say("Opening %d files in Preview — flip through with arrow keys"
            " and zoom to 100%% to judge quality." % len(results))
        open_in_preview(sorted(results.values()))
    else:
        say("Nothing to open — every conversion failed.")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _folder(text: str) -> Path:
    path = Path(text).expanduser().resolve()
    if not path.is_dir():
        raise argparse.ArgumentTypeError("not a folder: %s" % text)
    return path


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _existing_file(text: str) -> Path:
    path = Path(text).expanduser().resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError("not a file: %s" % text)
    return path


def _require_engine(fmt: str) -> None:
    if not have_sips():
        sys.exit("sips not found — this tool requires macOS.")
    if fmt == "dng" and dng_converter() is None:
        sys.exit("Converting to DNG requires Adobe DNG Converter.\n"
                 "Free download: %s\nRun `rawconvert.py doctor` to re-check."
                 % DNG_CONVERTER_URL)
    if fmt == "jpeg" and not have_exiftool():
        print("Note: exiftool not found — JPEGs will be re-rendered by sips"
              " instead of using the camera's embedded JPEG, and metadata"
              " copying is limited. Install from %s (see: doctor)."
              % EXIFTOOL_URL)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="rawconvert.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check required external tools")

    no_recurse_help = ("only process files directly inside FOLDER, ignoring"
                       " its subfolders (default: recurse into all)")
    render_help = ("JPEG only: always re-render with Apple's RAW engine at"
                   " --quality, instead of extracting the camera's embedded"
                   " JPEG (slower, Apple rendering instead of Canon's, but"
                   " gives full quality/size control)")

    p = sub.add_parser("scan", help="inventory RAW files and estimate savings")
    p.add_argument("folder", type=_folder)
    p.add_argument("--no-recurse", action="store_true", help=no_recurse_help)

    def add_convert_options(p):
        p.add_argument("folder", type=_folder)
        p.add_argument("--no-recurse", action="store_true",
                       help=no_recurse_help)
        p.add_argument("--to", choices=sorted(FORMATS), default="dng",
                       dest="fmt", help="output format (default: dng)")
        p.add_argument("--quality", type=int, default=90,
                       help="JPEG/HEIC quality 1-100 (default 90)")
        p.add_argument("--sample", type=int, metavar="N",
                       help="convert only the first N files (for format"
                            " trials)")
        p.add_argument("--output", metavar="DIR", type=Path,
                       help="write outputs under DIR (e.g. another drive),"
                            " mirroring the source folder structure, instead"
                            " of next to the RAW files")
        p.add_argument("--render", action="store_true", help=render_help)
        p.add_argument("--batch-size", type=_positive_int, default=1,
                       metavar="N",
                       help="DNG only: convert N files per Adobe DNG"
                            " Converter launch (default 1; benchmarks show"
                            " the default is usually fastest)")
        p.add_argument("--quiet", action="store_true",
                       help="suppress per-file progress output (failures and"
                            " the final summary are still shown)")
        p.add_argument("--dry-run", action="store_true",
                       help="show what would happen without writing anything")
        p.add_argument("--progress-json", action="store_true",
                       help="machine-readable JSON progress output, one"
                            " event per line (used by the GUI)")

    add_convert_options(sub.add_parser(
        "convert", help="convert RAW files (idempotent)"))

    add_convert_options(sub.add_parser(
        "process",
        help="one-shot pipeline: scan, convert, verify, then stage originals"
             " for manual deletion"))

    p = sub.add_parser(
        "compare",
        help="convert ONE file to every available format — including both"
             " JPEG engines — and open the results in Preview")
    p.add_argument("file", type=_existing_file, metavar="RAWFILE")
    p.add_argument("--quality", type=int, default=90,
                   help="JPEG/HEIC quality 1-100 (default 90)")
    p.add_argument("--progress-json", action="store_true",
                   help="machine-readable JSON output (used by the GUI)")

    p = sub.add_parser("verify", help="validate converted outputs")
    p.add_argument("folder", type=_folder)
    p.add_argument("--to", choices=sorted(FORMATS), default="dng",
                   dest="fmt", help="format to verify (default: dng)")

    p = sub.add_parser("status", help="per-format size comparison")
    p.add_argument("folder", type=_folder)

    p = sub.add_parser(
        "cleanup",
        help="stage verified originals (and rejected-format outputs) into"
             " %s/ for manual deletion" % TRASH_DIRNAME)
    p.add_argument("folder", type=_folder)
    p.add_argument("--keep", choices=sorted(FORMATS), default="dng",
                   help="the format you decided to keep (default: dng)")
    p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "scan":
        return cmd_scan(args.folder, recurse=not args.no_recurse)
    if args.command == "convert":
        _require_engine(args.fmt)
        counts = cmd_convert(args.folder, args.fmt, quality=args.quality,
                             sample=args.sample, dry_run=args.dry_run,
                             output_root=args.output,
                             recurse=not args.no_recurse,
                             force_render=args.render, quiet=args.quiet,
                             batch_size=args.batch_size,
                             progress_json=args.progress_json)
        return 1 if counts["failed"] else 0
    if args.command == "process":
        _require_engine(args.fmt)
        return cmd_process(args.folder, args.fmt, quality=args.quality,
                           sample=args.sample, output_root=args.output,
                           recurse=not args.no_recurse,
                           force_render=args.render, quiet=args.quiet,
                           batch_size=args.batch_size, dry_run=args.dry_run,
                           progress_json=args.progress_json)
    if args.command == "compare":
        results = cmd_compare(args.file, quality=args.quality,
                              progress_json=args.progress_json)
        return 0 if results else 1
    if args.command == "verify":
        counts = cmd_verify(args.folder, args.fmt)
        return 1 if counts["failed"] else 0
    if args.command == "status":
        return cmd_status(args.folder)
    if args.command == "cleanup":
        cmd_cleanup(args.folder, args.keep, dry_run=args.dry_run)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
