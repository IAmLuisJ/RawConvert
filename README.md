# RawConvert

Safely convert Canon **CR2/CR3** RAW files to **JPEG**, **HEIC**, or **lossy
DNG** on macOS to reclaim disk space — with verification before anything is
removed, and **no automatic deletion, ever**.

One stdlib-only Python script; nothing to `pip install`.

## ⚠️ Understand the trade first

RAW → JPEG/HEIC is one-way. You permanently lose the RAW editing latitude
(highlight recovery, white balance, etc.). Lossy DNG stays a RAW file
(editable, ~50% smaller); JPEG/HEIC are smaller still (~75%) but "developed".
Back up the drive before the final cleanup step if you can.

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

## Workflow

```sh
# 1. See what's on the drive
python3 rawconvert.py scan /Volumes/MyDrive/Photos

# 2. Trial-convert a small sample to each candidate format
python3 rawconvert.py convert /Volumes/MyDrive/Photos --to jpeg --sample 10
python3 rawconvert.py convert /Volumes/MyDrive/Photos --to heic --sample 10
python3 rawconvert.py convert /Volumes/MyDrive/Photos --to dng  --sample 10

# 3. Compare sizes; open the outputs in Preview to compare quality
python3 rawconvert.py status /Volumes/MyDrive/Photos

# 4. Full conversion in your chosen format (safe to interrupt & re-run)
python3 rawconvert.py convert /Volumes/MyDrive/Photos --to heic

# 5. Verify every output (existence, readability, pixel dimensions)
python3 rawconvert.py verify /Volumes/MyDrive/Photos --to heic

# 6. Stage originals + losing-format samples for deletion
python3 rawconvert.py cleanup /Volumes/MyDrive/Photos --keep heic

# 7. Spot-check, then empty the staging folder YOURSELF
#    /Volumes/MyDrive/Photos/_rawconvert_trash/
```

Add `--dry-run` to `convert` or `cleanup` to preview without changing anything.

`scan` and `convert` recurse into subfolders by default; add `--no-recurse`
to limit a run to the top level of the given folder (handy for trialing one
folder without touching what's inside it).

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

- Outputs are written next to their RAW (`IMG_0001.CR3` → `IMG_0001.heic`)
  via a `.partial` temp name — interruptions never leave corrupt outputs.
- State lives in `_rawconvert_manifest.csv` in the target folder; re-running
  `convert` skips finished files, so a mid-batch drive disconnect is harmless.
- Existing files the tool didn't create are **never overwritten** (reported
  as collisions).
- `cleanup` only touches originals whose output passed `verify`, and it only
  *moves* them into `_rawconvert_trash/` on the same drive. Deleting that
  folder is always a manual, human step.

## Notes

- JPEG uses the full-resolution JPEG your camera already embedded in the RAW
  (via exiftool) — camera-accurate colors, very fast. If it's missing or
  small, the file is re-rendered with Apple's RAW engine (`sips`).
- HEIC (`sips`) is typically smaller than JPEG at similar quality; CR3
  support depends on your macOS version / camera model.
- DNG uses Adobe DNG Converter with lossy compression (`-lossy`).
- EXIF/GPS/capture-date metadata and file modification times are preserved
  (fully with exiftool installed).

## Development

```sh
python3 -m unittest -v   # no external tools or network needed
```
