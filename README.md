# RawConvert

Safely convert Canon **CR2/CR3** RAW files to **JPEG**, **HEIC**, or **lossy
DNG** on macOS to reclaim disk space — with verification before anything is
removed, and **no automatic deletion, ever**.

One stdlib-only Python script; nothing to `pip install`.

## ⚠️ Understand the trade first

RAW → JPEG/HEIC is one-way. You permanently lose the RAW editing latitude
(highlight recovery, white balance, etc.). Lossy DNG stays a RAW file
(editable) and shrinks files an estimated 45–95% depending on the camera —
real-world 45 MP CR3s measured at ~6% of original size, beating JPEG/HEIC
while staying editable. JPEG/HEIC are "developed" but save a predictable
~75–90%. Back up the drive before the final cleanup step if you can.

**[FORMATS.md](FORMATS.md) explains the trade-offs in depth** — what each
format keeps and loses, size expectations, compatibility, and how to decide.

## Download

1. Grab the latest `RawConvert-x.y.z.zip` from the
   [Releases page](../../releases) and unzip it anywhere (your Applications
   or Desktop folder is fine).
2. **Right-click `RawConvert.command` and choose Open**, then click **Open**
   in the dialog. macOS shows a caution message for anything downloaded
   outside the App Store — this one-time step tells it you trust the app.
   After that, a normal double-click works.
3. Your browser opens with the RawConvert wizard. Follow the six steps —
   nothing is ever deleted; originals are set aside for *you* to review.

On a brand-new Mac, the first launch may offer to install Apple's free
"Command Line Tools" — click Install, let it finish, and launch again.

## GUI for non-technical users

Double-click **`RawConvert.command`** — a local page opens in your browser
and walks you through the whole flow: tool checkup → choose folder →
compare formats on one photo → convert with live progress → review the
set-aside originals in Finder. The GUI has **no way to delete photos**;
like the CLI, it only stages originals into `_rawconvert_trash/` for you to
review and empty yourself.

Notes for first use:
- If macOS blocks the double-click (a downloaded `.command` file),
  right-click it and choose **Open** once.
- On a brand-new Mac, the first `python3` run may prompt to install Apple's
  Command Line Tools — accept and relaunch.
- The page is served only to your own Mac (`127.0.0.1`) with a per-session
  token; nothing is exposed to the network.
- Keep the Terminal window it opens in the background; closing it stops the
  app (a running conversion can always be resumed by starting again).

## Requirements

| Tool | Needed for | Install |
|---|---|---|
| macOS `sips` | everything | built in |
| [exiftool](https://exiftool.org) | best JPEGs (camera-rendered, full metadata) | macOS package from exiftool.org, or `brew install exiftool` |
| [Adobe DNG Converter](https://helpx.adobe.com/camera-raw/using/adobe-dng-converter.html) | `--to dng` only | free download |

Check what you have:

```sh
python3 rawconvert.py doctor
```

## Commands

| Command | What it does | Key options |
|---|---|---|
| `doctor` | Check which external tools are installed, with install links | |
| `scan FOLDER` | Inventory RAW files: counts, sizes, estimated savings | `--no-recurse` |
| `compare RAWFILE` | Convert **one** file to every available format — JPEG via *both* engines (camera-embedded and sips re-render) — and open the results in Preview | `--quality` |
| `convert FOLDER` | Convert all RAW files with per-file progress and ETA (idempotent — re-run to resume) | `--to FMT`, `--sample N`, `--quality`, `--render`, `--output DIR`, `--no-recurse`, `--batch-size N`, `--quiet`, `--dry-run` |
| `process FOLDER` | **One-shot pipeline**: scan → convert → verify → stage originals for manual deletion | same options as `convert` |
| `verify FOLDER` | Validate outputs: existence, readability, pixel dimensions | `--to FMT` |
| `status FOLDER` | Per-format size comparison table from the manifest | |
| `cleanup FOLDER` | Stage verified originals + rejected-format outputs for manual deletion | `--keep FMT`, `--dry-run` |

`FMT` is `jpeg`, `heic`, or `dng` — **`dng` is the default everywhere**
(it measured best on real CR3s; see [FORMATS.md](FORMATS.md)). All folder
commands recurse into subfolders by default and skip hidden files (including
the `._*` metadata sidecars macOS scatters on FAT/exFAT drives).

## Workflow

```sh
# 1. See what's on the drive
python3 rawconvert.py scan /Volumes/MyDrive/Photos

# 2. Pick one representative photo and compare all formats side by side —
#    converts it to every available format (JPEG via both engines: the
#    camera's embedded JPEG and a sips re-render at --quality) and opens
#    everything in Preview
python3 rawconvert.py compare /Volumes/MyDrive/Photos/2023/IMG_0421.CR3

# (optional) trial a larger sample per format and compare aggregate sizes
python3 rawconvert.py convert /Volumes/MyDrive/Photos --to jpeg --sample 10
python3 rawconvert.py convert /Volumes/MyDrive/Photos --to heic --sample 10
python3 rawconvert.py convert /Volumes/MyDrive/Photos --to dng  --sample 10
python3 rawconvert.py status  /Volumes/MyDrive/Photos

# 3. Once you've picked a format, run the whole pipeline in one command —
#    scan, convert, verify, and stage originals (dng is the default;
#    add --to heic/jpeg to override). Safe to interrupt & re-run.
python3 rawconvert.py process /Volumes/MyDrive/Photos

# 4. Spot-check, then empty the staging folder YOURSELF
#    /Volumes/MyDrive/Photos/_rawconvert_trash/
```

Prefer step-by-step control? The pipeline stages are also standalone
commands: `convert`, then `verify`, then `cleanup` (same defaults and
options). Failed files never have their originals staged either way.

Add `--dry-run` to `convert` or `cleanup` to preview without changing
anything, and `--no-recurse` to `scan` or `convert` to limit a run to the top
level of a folder.

### Writing outputs to a different drive

By default outputs sit next to their RAW files. Use `--output DIR` to mirror
the source folder structure somewhere else — for example a second external
drive:

```sh
python3 rawconvert.py convert /Volumes/MyDrive/Photos --to heic \
    --output /Volumes/OtherDrive/PhotosConverted
# /Volumes/MyDrive/Photos/2021/trip/IMG_001.CR3
#   -> /Volumes/OtherDrive/PhotosConverted/2021/trip/IMG_001.heic
```

The manifest remembers where each output went, so `verify`, `status`, and
`cleanup` are unchanged — you still point them at the *source* folder.
During `cleanup`, rejected-format outputs are staged in a
`_rawconvert_trash/` on the drive they live on (moves never cross volumes).

## When conversions fail

Each failure is classified with an error code and appended to
`_rawconvert_errors.log` in the scanned folder, including the raw engine
output, a diagnosis, and concrete debug steps:

| Code | Meaning |
|---|---|
| RC01 | macOS `._*` metadata sidecar, not a real photo (now skipped automatically) |
| RC02 | File under 1 MB — truncated or not a real RAW |
| RC03 | macOS can't decode this RAW (corrupt file, or camera not supported by this macOS) |
| RC04 | Required tool not installed (see `doctor`) |
| RC05 | Destination drive full |
| RC06 | Permission denied (check Privacy & Security settings / read-only mount) |
| RC99 | Unknown — engine output preserved in the log |

Failed files are never touched by `cleanup`, so nothing is at risk while you
investigate.

## Safety model

- Outputs are written next to their RAW (`IMG_0001.CR3` → `IMG_0001.heic`),
  or mirrored under `--output DIR`, via a `.partial` temp name —
  interruptions never leave corrupt outputs.
- State lives in `_rawconvert_manifest.csv` in the target folder; re-running
  `convert` skips finished files, so a mid-batch drive disconnect is harmless.
- Existing files the tool didn't create are **never overwritten** (reported
  as collisions).
- `cleanup` only touches originals whose output passed `verify`, and it only
  *moves* them into `_rawconvert_trash/` on the same drive. Deleting that
  folder is always a manual, human step.

## Files the tool creates

| File | Where | Purpose |
|---|---|---|
| `_rawconvert_manifest.csv` | scanned folder | Per-file conversion state (drives resume, `verify`, `status`, `cleanup`) |
| `_rawconvert_errors.log` | scanned folder | Classified failures with diagnosis and debug steps |
| `_rawconvert_trash/` | scanned folder (and output drive, for rejected formats) | Staging area for `cleanup` — emptied only by you |
| `rawconvert_compare_*/` | system temp folder | Throwaway outputs from `compare` |

## Notes

- JPEG uses the full-resolution JPEG your camera already embedded in the RAW
  (via exiftool) — camera-accurate colors, very fast, but its quality was
  fixed at shoot time, so `--quality` doesn't apply. If the embedded JPEG is
  missing or small, the file is re-rendered with Apple's RAW engine (`sips`),
  where `--quality` does apply. Pass `--render` to force the re-render path
  for every JPEG and take full control of quality/size (HEIC is always
  rendered, so `--quality` always applies there).
- HEIC (`sips`) is typically smaller than JPEG at similar quality; CR3
  support depends on your macOS version / camera model.
- DNG uses Adobe DNG Converter with lossy compression (`-lossy`). By default
  the converter app is launched once per file. `--batch-size N` converts N
  files per launch instead — but benchmarks on real 82 MB CR3s (DNG
  Converter 16.x, M-series Mac) found batching **slower** than per-file
  (~19 s vs ~25 s per 10 files) with ~50% more CPU: the converter is heavily
  multithreaded per conversion, so launch overhead (~0.2 s) is negligible and
  batch mode schedules work less efficiently. **Recommendation: keep the
  default.** The flag remains for machines where the trade-off differs —
  measure with `--sample` before using it. Batched outputs go through a
  hidden staging folder, so interrupt-safety is unchanged and per-file
  failures within a batch are still detected and logged individually.
- EXIF/GPS/capture-date metadata and file modification times are preserved
  (fully with exiftool installed).

## Development

```sh
python3 -m unittest -v   # no external tools or network needed
```
