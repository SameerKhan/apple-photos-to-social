# Changelog

## 0.2.1 (2026-08-16)

Hardening after adversarial review of 0.2.0.

- **The safety guarantee is now actually enforced.** `repair()` refuses outright if an
  image carries any judgement fault, even alongside a repairable one. Previously a
  low-key silhouette with a patch of blown sky would have been edited.
- **Removed the `sharpen_soft` override.** An escape hatch on a safety guarantee is
  not a guarantee.
- **Blown highlights are no longer auto-repaired.** Clipped pixels hold nothing to
  recover; the operation only turned white into flat grey. Reported instead.
- **`unfixable_blur` renamed `low_detail`** and no longer described as motion blur.
  Laplacian variance measures detail, and a sharp photo of a plain wall scores just as
  low as a smeared one.
- Repairs preserve alpha, colour mode and EXIF, and are named by asset id so duplicate
  camera filenames cannot overwrite each other.
- Diagnosis and repair failures are contained per image instead of failing the run.
- Images under 3px no longer produce a NaN sharpness that silently read as sharp.
- LinkedIn's aspect floor corrected from 0.33 to 0.80 (4:5), so a phone portrait is no
  longer reported as fitting. Manifest now says "too wide" where that is what happened.

## 0.2.0 (2026-08-16)

- **Per-image quality diagnosis** (`quality.py`, `photos2social diagnose`): exposure,
  dynamic range, shadow and highlight clipping, colour cast, sharpness, and
  face-region exposure.
- **Conservative repair.** Only blown highlights, underexposed faces and strong colour
  casts are auto-fixed. Underexposure, crushed shadows and softness are reported and
  left alone, because they are frequently the photographer's intent rather than a
  mistake. Severe motion blur is reported as unusable rather than sharpened.
- Colour cast is measured over near-neutral mid-tone pixels only. Plain grey-world
  produced 57 false positives on a real camera roll, including calling an orange
  takeaway box a 149% red cast.
- **Face-aware 4:5 cropping** via Apple Vision, on device, optional extra.
- Manifest gains `ratio`, `fits`, `diagnosis` and `repaired_file` columns.
- numpy is now a runtime dependency.

## 0.1.1 (2026-08-15)

Fixes from an adversarial review of the 0.1.0 release. Four were confirmed by
executing them, not by reading.

- **Privacy:** a `NaN` geofence radius passed validation and then silently matched
  nothing, because `distance <= nan` is always False. Non-finite coordinates and
  radii are now rejected at load.
- **Privacy:** album and zone exclusions are now evaluated before the screenshot
  and size filters, so an excluded asset is always recorded as private rather than
  being dropped for the wrong reason and losing that protection later.
- **Privacy:** `.gitignore` media rules were case-sensitive, so `IMG_0001.JPG` was
  not ignored on Linux and a later `git add .` could publish it.
- **Safety:** `export_assets` refuses a symlinked target directory, which could
  otherwise have had its contents deleted outside the workspace.
- History screening no longer compares an asset against its own stored hash, which
  could drop it and demote a `posted` status to `seen`.
- `--limit 0` now means zero rather than unlimited.
- A run directory left behind by a previous ledger is cleared rather than mixed in.
- `_pick_export` no longer returns a video for a photo, or the reverse; that
  mapping could not be consumed downstream and looked like a silent success.
- `scan` no longer creates a ledger just to answer a question.
- Documentation corrected: the package opens no sockets, but a Photos export can
  make Photos itself download from iCloud; and `review` already records what it
  showed you, so `mark` records why rather than being required for suppression.

## 0.1.0 (2026-08-15)

First release.

- Reads an Apple Photos library over Apple Events, so **no Full Disk Access** is
  required. Whole-library metadata fetch takes about a minute at ~50k assets.
- SQLite ledger records every reviewed asset with a perceptual hash, so repeat
  passes over old months do not re-surface the same photos. Assets reviewed and
  not chosen stay suppressed.
- Cross-run visual deduplication: a scene already settled in the ledger is skipped
  even when it arrives again as a different asset id.
- Burst clustering collapses near-identical frames taken close together into one
  reviewable moment.
- Labelled contact sheets plus a `manifest.csv` mapping sheet index to asset id.
- Privacy: album exclusion, geofenced zones, sticky exclusions that automated runs
  cannot undo, refusal to run with no filters configured unless explicitly allowed,
  and refusal to run when a configured album does not resolve.
- Owner-only permissions on the ledger and every exported file.
- `doctor`, `scan`, `review`, `mark`, `stats`, `coverage`, `purge` commands.

### Known limitations

- macOS only.
- No face or person filtering. Apple's AppleScript interface does not expose it.
- Videos are opt-in and require ffmpeg.
