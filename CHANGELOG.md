# Changelog

## 1.0.0 — 2026-07-07

First public release.

- Convert Canon CR2/CR3 to lossy DNG (default), HEIC, or JPEG.
- Safety-first pipeline: convert → verify → stage originals for *manual*
  deletion; nothing is ever deleted automatically.
- `process` one-shot command; idempotent, resumable conversions.
- `compare` command: one photo in every format (both JPEG engines),
  opened side by side in Preview.
- Classified error log with plain-English debug steps.
- Convert to another drive with `--output`; `--sample`, `--quality`,
  `--render`, `--no-recurse`, `--batch-size`, `--quiet` options.
- Browser-based wizard for non-technical users (`RawConvert.command`).
