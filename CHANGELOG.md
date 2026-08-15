# Changelog

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
