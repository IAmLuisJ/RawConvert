# Choosing a format: JPEG vs HEIC vs lossy DNG

RawConvert can turn your CR2/CR3 files into three different formats. They sit
at different points on one axis: **how much you keep vs how much space you
save**. This doc explains what you're actually trading away with each choice.

## What a RAW file is (and why it's big)

A CR2/CR3 is not an image — it's the sensor's raw readout: 14 bits of
brightness per photosite, before white balance, sharpening, or color
rendering are applied. That's why a 24-megapixel photo costs 25–40 MB, and
also why RAW is so forgiving in editing: blown skies can be pulled back,
shadows lifted several stops, white balance changed after the fact with no
penalty.

Every conversion below gives up some or all of that latitude. **The
conversion is one-way — once the original is deleted, the latitude is gone
for good.** That's the real price, not the megabytes.

## The three options

### JPEG — maximum compatibility

- **What it is:** the photo *developed*: 8 bits per channel, one fixed
  interpretation of exposure and color, lossy-compressed.
- **Size:** roughly 20–25% of the RAW at quality 90.
- **What you keep:** the picture as rendered, full resolution, all
  EXIF/GPS/date metadata.
- **What you lose:** almost all editing latitude. Recovering highlights or
  pushing shadows on a JPEG quickly shows banding and blocking. 8-bit color
  can band in smooth gradients (skies) after editing.
- **Compatibility:** universal — every device, browser, website, TV, and
  photo service ever made. The safest bet for "will this open in 20 years?"
- **RawConvert detail:** two engines produce JPEGs. The *camera-embedded*
  JPEG (extracted via exiftool) is the camera's own rendering — Canon's
  colors, quality fixed at shoot time, extraction is nearly instant. The
  *sips re-render* uses Apple's RAW engine and obeys `--quality`. Run
  `compare` on one photo to see both; use `--render` on `convert` if you
  prefer the size-controlled Apple rendering for the full run.

### HEIC — best size-to-quality, modern ecosystems

- **What it is:** the same "developed photo" idea as JPEG, but with a modern
  codec (HEVC) and **10 bits per channel**.
- **Size:** typically 30–50% smaller than an equivalent-quality JPEG — often
  8–12% of the RAW.
- **What you keep:** everything JPEG keeps, plus 10-bit color, which resists
  banding and tolerates *mild* re-editing better.
- **What you lose:** same story as JPEG — the latitude is gone; 10-bit just
  degrades more gracefully.
- **Compatibility:** excellent inside Apple's world (it's the iPhone default)
  and fine on Windows 10+/Android; patchier with old software, some web
  uploads, and cheap smart TVs. Sharing sometimes requires an export step.
- **RawConvert detail:** always rendered by Apple's engine; `--quality`
  always applies. Apple's RAW decoding of CR3 depends on your macOS version
  and camera model — `verify` catches any file it couldn't handle.

### Lossy DNG — the only option that stays RAW

- **What it is:** Adobe's RAW container with lossy compression applied to the
  sensor data. It is still a real RAW file: white balance is still
  unapplied, and editors like Lightroom treat it exactly like a CR3.
- **Size:** roughly 40–55% of the original — real savings, but far less than
  JPEG/HEIC.
- **What you keep:** most of the editing latitude. Highlight recovery, WB
  changes, and heavy tone edits still work. Metadata carries over.
- **What you lose:** some shadow-recovery headroom and fine detail in the
  deepest tones (the lossy compression quantizes the sensor data), and the
  file is no longer the camera-original CR3 (some Canon-specific processing
  options in Canon's own software won't apply).
- **Compatibility:** opens in Adobe apps, Apple Photos, and most serious
  photo tools — but it is *not* a "double-click it anywhere" format like
  JPEG. Think of it as an archival negative, not a shareable photo.
- **RawConvert detail:** requires the free Adobe DNG Converter
  (`doctor` checks for it). `--quality` does not apply.

## Side by side

| | JPEG | HEIC | Lossy DNG |
|---|---|---|---|
| Typical size vs RAW | ~20–25% | ~8–15% | ~40–55% |
| Bit depth | 8-bit | 10-bit | RAW data |
| Editing latitude | minimal | minimal (degrades nicer) | most retained |
| Still a RAW file | no | no | **yes** |
| Opens anywhere | **everywhere** | most modern devices | photo software only |
| Quality knob in this tool | `--render --quality` (or camera-fixed) | `--quality` | none |
| Extra install | exiftool (recommended) | none | Adobe DNG Converter |

## How to decide

Ask one question first: **will anyone ever want to re-edit these photos
seriously?**

- **"No — these are memories/archives; finished looks are fine."**
  Choose **HEIC** if the photos live in the Apple/modern ecosystem
  (best savings), or **JPEG** if maximum compatibility and
  future-proof-anywhere matters more than the extra space.
- **"Maybe — some of these are portfolio/family-history keepers."**
  Choose **lossy DNG** for those folders. Half the space, latitude kept.
  It's the only choice you can't regret from an editing standpoint.
- **Mixed drive?** Mix formats. The tool works per-folder: DNG for the
  keeper folders, HEIC/JPEG for everything else. `status` shows the savings
  per format as you go.

Whatever you pick, run `compare` on a couple of representative photos first —
one well-exposed, one tricky (backlit, high-contrast) — and pixel-peep the
results in Preview at 100% before committing the whole drive.

## A note on "quality" numbers

JPEG/HEIC quality settings (the `--quality` flag) are not percentages of
anything real — they map to how aggressively the codec throws away detail.
Rules of thumb at full resolution:

- **90 (default):** visually transparent for prints and screens; the safe
  archival choice.
- **80:** fine for screen viewing; artifacts only under pixel-peeping.
  Meaningful extra savings.
- **70 and below:** visible artifacts in gradients and fine texture start
  appearing; only for space emergencies.

Remember the camera-embedded JPEG ignores `--quality` entirely — its quality
was decided by the camera when you pressed the shutter.
