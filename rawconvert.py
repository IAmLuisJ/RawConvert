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
import os
import shutil
import subprocess
import sys
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


def cmd_scan(root: Path, recurse: bool = True) -> int:
    """Inventory RAW files: counts, sizes, and estimated savings."""
    files = find_raw_files(root, recurse=recurse)
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


def cmd_convert(root: Path, fmt: str, quality: int = 90, sample=None,
                dry_run: bool = False, output_root=None,
                recurse: bool = True) -> dict:
    """Convert every RAW under root to fmt. Idempotent and resumable.

    With output_root, outputs mirror the source folder structure under that
    directory (e.g. on another drive) instead of sitting next to the RAWs.
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

    for src in files:
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
        partial = dst.with_name(dst.name + ".partial")
        engine = ""
        try:
            if fmt == "jpeg":
                if extract_embedded_jpeg(src, partial):
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
            copy_metadata(src, dst)
            shutil.copystat(str(src), str(dst))
            manifest.set(rel, fmt, output_relpath=out_rel,
                         output_root=_root_field(out_base, root),
                         src_bytes=src.stat().st_size,
                         out_bytes=dst.stat().st_size,
                         engine=engine, status="converted", note="")
            counts["converted"] += 1
        except EngineError as exc:
            if partial.exists():
                partial.unlink()
            info = log_failure(root, rel, src, fmt, engine, str(exc))
            manifest.set(rel, fmt, output_relpath=out_rel,
                         output_root=_root_field(out_base, root),
                         engine=engine, status="failed",
                         note=("[%s] %s: %s"
                               % (info["code"], info["name"],
                                  str(exc)))[:300])
            counts["failed"] += 1
            print("FAILED [%s %s]: %s — %s"
                  % (info["code"], info["name"], rel, info["diagnosis"]))
        pending += 1
        if pending >= MANIFEST_FLUSH_EVERY:
            manifest.save()
            pending = 0

    if not dry_run and pending:
        manifest.save()
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _folder(text: str) -> Path:
    path = Path(text).expanduser().resolve()
    if not path.is_dir():
        raise argparse.ArgumentTypeError("not a folder: %s" % text)
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

    p = sub.add_parser("scan", help="inventory RAW files and estimate savings")
    p.add_argument("folder", type=_folder)
    p.add_argument("--no-recurse", action="store_true", help=no_recurse_help)

    p = sub.add_parser("convert", help="convert RAW files (idempotent)")
    p.add_argument("folder", type=_folder)
    p.add_argument("--no-recurse", action="store_true", help=no_recurse_help)
    p.add_argument("--to", required=True, choices=sorted(FORMATS),
                   dest="fmt", help="output format")
    p.add_argument("--quality", type=int, default=90,
                   help="JPEG/HEIC quality 1-100 (default 90)")
    p.add_argument("--sample", type=int, metavar="N",
                   help="convert only the first N files (for format trials)")
    p.add_argument("--output", metavar="DIR", type=Path,
                   help="write outputs under DIR (e.g. another drive),"
                        " mirroring the source folder structure, instead of"
                        " next to the RAW files")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would happen without writing anything")

    p = sub.add_parser("verify", help="validate converted outputs")
    p.add_argument("folder", type=_folder)
    p.add_argument("--to", required=True, choices=sorted(FORMATS), dest="fmt")

    p = sub.add_parser("status", help="per-format size comparison")
    p.add_argument("folder", type=_folder)

    p = sub.add_parser(
        "cleanup",
        help="stage verified originals (and rejected-format outputs) into"
             " %s/ for manual deletion" % TRASH_DIRNAME)
    p.add_argument("folder", type=_folder)
    p.add_argument("--keep", required=True, choices=sorted(FORMATS),
                   help="the format you decided to keep")
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
                             recurse=not args.no_recurse)
        return 1 if counts["failed"] else 0
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
