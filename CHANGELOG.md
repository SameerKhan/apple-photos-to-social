# Changelog

## 0.4.5 (2026-08-18)

**A photo already published was offered again as a fresh candidate.** Sameer caught it;
the tool did not.

Two frames from one studio session sat 7 apart by perceptual hash. `history_max_distance`
was 6, so the cross-run screen treated them as unrelated and re-proposed the second after
the first had gone out on Instagram. Meanwhile `burst_max_distance` was 12, meaning the
very same pair WOULD have collapsed into a single moment had they appeared in one run.
The two thresholds disagreed, and the photo fell straight through the gap between them.

Underneath that, `screen_against_history` applied one flat radius to every status, so a
photo you PUBLISHED suppressed its neighbours no more strongly than one you had merely
glanced at.

- New `review.published_max_distance` (default 12, must be >= `history_max_distance`,
  enforced at config load). Rows in `posted` or `shortlisted` now screen at the wider
  radius; merely-`seen` rows keep the tighter one, so real candidates still surface.

## 0.4.4 (2026-08-17)

Six defects found by a three-model review of the 0.4.x work. Two were mine to be
embarrassed about: I changed the retention rule and never revisited the migration that
implements it.

- **The migration destroyed review history.** Opening any pre-0.4 ledger nulled
  `captured_at` on every `seen` row, irreversibly, which is precisely the coverage data
  0.4.3 had just been written to preserve. Both external reviewers caught it; the local
  leg did not.
- **The migration left a plaintext library inventory.** `asset_cache` holds raw ids,
  filenames and dates. Hashing `assets`, `status_history` and `publications` while
  leaving it intact meant an "opaque" upgraded ledger was still a readable index of the
  library. It is now cleared on migration, and the README says what the cache is.
- **`review --dry-run` crashed on a fresh machine.** `_NullLedger` is the stand-in used
  when no ledger exists, and it lacked the `digest()` method the pipeline calls per
  asset, so the run died with `AttributeError` before printing anything.
- **`report` printed a hash instead of a filename.** `publications()` returned the
  digest even where the plain identity had been deliberately retained.
- **Setting a status by digest erased identity.** A digest identifies a row but cannot
  reconstruct what it points at, so treating it as non-legible wiped an audit trail that
  could never be rebuilt. Digest-keyed updates now leave stored identity alone.
- **An automated exclusion outranking `posted` is now deliberate and pinned.** A privacy
  filter should be able to hide something already published; the publication event
  survives in its own table, so the history is not lost.

## 0.4.3 (2026-08-17)

**`seen` rows now keep their capture time.**

The opaque ledger discarded the capture date along with the identifier, which broke the
question the ledger exists to answer. On a real ledger `coverage` could describe only
94 of 257 rows, because the other 163 had no date, so the tool could not say which
months it had already been through.

Capture time is now retained for every status. A merely-seen asset still records no
Photos identifier and no filename.

The tradeoff was put to the owner explicitly and this is his ruling: an exact timestamp
plus a perceptual hash makes a seen photo findable again by date. The README now states
plainly that the retention rule limits what gets written down about choices you did not
make, and is not an anonymity guarantee, particularly against anyone holding both the
ledger and the library.

## 0.4.2 (2026-08-17)

**An automated pass could erase a human decision, and did.**

`_write` protected only `TERMINAL` statuses, so a routine `review` run recorded
`shortlisted` and `posted` assets back down to `seen`. This was not hypothetical: in the
real ledger it wiped 14 shortlisted picks and **one `posted` record**, which is the
publication log the whole legible-for-decisions rule exists to protect.

- Added `STATUS_RANK`. An automated write may only move an asset UP the intent ladder;
  only the explicit `set_status` path can move it down.

**The downgrade also leaked identity.**

The UPDATE used `COALESCE(?, plain_uuid)`, which keeps whatever was already stored. A
row dropping to a non-legible status therefore retained the uuid and filename it was
supposed to shed, and 13 `seen` rows in the real ledger ended up carrying identifying
uuids. Identity is now assigned rather than coalesced, so the retention rule is applied
in both directions, on promotion and on demotion.

**Decisions could not name what they decided about.**

`set_status` re-attached the uuid but never the filename or capture time, so 42
`excluded_private` rows recorded an exclusion nobody could put a name to. `set_status`
now accepts `filename` and `captured_at`, and `mark-sheet` passes both from the
manifest it already has open.

## 0.4.1 (2026-08-17)

**Sharpness was measuring megapixels.**

Laplacian variance is not scale invariant, and the error was not small: one unchanged
photograph from a real camera roll scored **13 at 4032px and 625 at 800px**, a 48x
swing across resolutions. The `soft < 60` and `low_detail < 20` thresholds were
therefore partly a function of file size.

That mattered because both are *judgement* faults, and any judgement fault makes
`repair()` refuse an image outright. The practical effect was backwards from the
intent: the **highest-resolution originals were the ones denied a colour-cast repair**,
for a reason that was an artifact of their dimensions.

- Sharpness is now measured on a copy normalised to a fixed long edge
  (`SHARPNESS_REF_PX = 1024`), so the number means the same thing for a 48MP original
  and a 1080px export. Verified stable: the same image now scores 782, 782, 783, 792,
  798, 787 across six sizes where it previously ranged 13 to 625.
- Re-measured across 99 real exports, the flag counts fall from 28 `low_detail` and
  31 `soft` to 0 and 5.

**The run summary claimed images were skipped when they were not.**

`auto-repaired` and `very low detail` were entries in the `skipped:` dict. Neither is a
skip: a repaired image is included *and improved*, and a low-detail one is included and
merely flagged. A run printing `skipped: ... very low detail 32` reads as thirty-two
photographs discarded, when all thirty-two were in the contact sheets. They now print
under `included:`.

## 0.4.0 (2026-08-16)

**The ledger no longer remembers what it merely looked at.**

A ledger built to prevent re-reviewing old photos does not need to know which photos
they were. Before this release it stored the Photos identifier, the filename and the
capture date for every asset it had ever seen, which meant a suppression list doubled
as a readable index of someone's camera roll.

- **Identity is now a salted digest.** The primary key is
  `sha256(per-ledger-salt + uuid)`, truncated. Dedup and status lookups work exactly as
  before because they only ever needed equality, never the value. The salt is generated
  once per ledger with `secrets` and stored inside it, so the digests are not reversible
  by precomputation across libraries.
- **Only decisions stay legible.** `shortlisted`, `posted`, `excluded_private` and
  `excluded_junk` keep the plain identifier, filename and date, because a publication
  log that cannot say what was published is useless and an exclusion you cannot audit is
  not an exclusion. `seen` keeps none of it.
- **A real leak is closed:** `set_status` was writing the raw identifier into
  `status_history` regardless of status.
- **The migration checks constraints, not proxies.** An earlier attempt tested for the
  presence of the new column, which a half-applied migration satisfies while leaving the
  old `NOT NULL` schema in place. It now reads `PRAGMA table_info` and rebuilds whenever
  the real constraints are stale, so a failed run is safe to retry.

## 0.3.1 (2026-08-16)

Two defects found by the first real video run, not by review.

- **Apple Event timeout on export.** 16 of 60 assets failed with AppleScript error
  -1712, almost all of them videos. Apple Events have their own 120-second ceiling
  independent of the subprocess timeout, and a large video export exceeds it. Exports
  now raise it explicitly with `with timeout of`.
- **A killed run was stranded at `state: 'running'` forever.** A process terminated by
  a signal cannot run its own cleanup. SIGTERM and SIGINT are now converted to
  exceptions so the existing failure path fires, and `start_run` reconciles any orphan
  it finds as `interrupted`.

## 0.3.0 (2026-08-16)

Driven by a measured diagnosis: after all prior work the ledger held **13 of 49,513
assets**, and 33% of a month's capture was video the tool never looked at.

- **`mark-sheet <run> <index> <status>`.** The contact sheet prints `#37` but `mark`
  wanted a 36-character asset id copied out of a CSV. That friction is why almost
  nothing was ever marked. Accepts a run directory, a run number, or `latest`.
- **`report`** shows what was published, where and when.
- **Publication events are now their own table.** `assets.destination` held a single
  string, so posting one photo to two platforms overwrote the first and lost its
  timestamp.
- **Video disk budgeting is separate from photos.** One figure was used for both; a 4K
  video derivative is 100 to 500 MB against an 8 MB photo, so a video run
  under-estimated free space by 10x to 50x and would have filled the disk mid-export.
- **Video review samples across the clip** (five points between 10% and 90%) instead of
  grabbing a single frame at 1.0s, which on a dive clip is the surface of the water.
  Duration comes from ffprobe, the sharpest sample becomes the representative, and a
  filmstrip is written so the arc of a clip is visible.
- **Photos and videos cluster separately.** A still and a clip of the same scene are two
  posts, not one duplicated moment.
- **Library metadata can be cached in the ledger** rather than re-parsing ~50k
  AppleScript records every run.
- Manifest gains `kind`, `favorite` and `filmstrip` columns. `favorite` was already
  being fetched and never used, and it is the only real human-preference signal
  available.

**Deliberately not built: a candidate scoring system.** Two independent reviews
rejected it. "Clean beats flagged" contradicts this tool's own rule that a measurement
cannot tell a defect from an intention, and a face-detection bonus would have
systematically promoted family photographs on an unfiltered library. Sortable evidence
columns instead of a number.

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
