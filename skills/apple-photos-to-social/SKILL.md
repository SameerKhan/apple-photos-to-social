---
name: apple-photos-to-social
description: Review a macOS Apple Photos library and turn recent photos into social post candidates, with a ledger so previously-reviewed photos are never re-reviewed. Handles privacy filtering, burst deduplication, contact sheets for visual review, and drafting copy. Triggers on "what can I post from my photos", "find photos to post", "review my camera roll", "photos to posts", "make posts from my photos", "go back through my photos from <month>".
---

# Apple Photos to social posts

Turn a personal photo library into a small set of reviewed post candidates. The
hard part is not drafting copy, it is narrowing tens of thousands of photos down
to the handful worth posting, without redoing that work every month.

## Before you start

Run `photos2social doctor`. It reports whether Photos is reachable, whether
privacy filters are configured, and how much disk is free. If Photos is not
reachable, stop and tell the user which permission is missing. Do not attempt the
Full Disk Access route as a workaround; this tool deliberately does not need it.

**If `doctor` says privacy filters are NONE CONFIGURED, stop and tell the user
before running anything.** Without filters, every photo in the window becomes a
candidate, including photos of their family, and you will be looking at all of
them. Offer to help set up `privacy.exclude_albums` first. Only pass
`--allow-unfiltered` if the user explicitly says to.

## The loop

### 1. Scan first, always

```bash
photos2social scan --days 30
```

Metadata only. Nothing is exported, nothing is written. This tells you how much is
actually new versus already reviewed. If "new to review" is zero, say so and stop:
there is nothing to do, and that is a useful answer.

### 2. Review

```bash
photos2social review --days 30
```

This exports derivatives, hashes them, collapses bursts into distinct moments,
writes numbered contact sheets, and records everything in the ledger. Expect
roughly 3 to 5 seconds per asset. For a large window, use `--limit` or narrow the
dates rather than letting it run for an hour.

Videos are skipped unless `--include-videos` is passed. They need a frame
extracted before anything can be judged, which is slower.

### 2b. Read the diagnosis before you look

`manifest.csv` carries a `diagnosis` column per image. Two categories:

- **`auto:`** already repaired (blown highlights, dark face, strong colour cast).
- **`your call:`** reported and deliberately NOT touched (underexposure, crushed
  shadows, softness), because those are frequently the intent rather than a mistake.
  Look at the picture and decide. A low-key silhouette measures as underexposed.
- **`very low detail`** little high-frequency content. This measures DETAIL, not
  blur, so a sharp photo of a plain wall scores the same as a smeared one. Look
  at it before deciding; never auto-discard on this alone.

### 3. Look at the contact sheets

Read every sheet the command printed. Each tile is labelled with a global index,
capture time, and a burst count where one frame stands in for several.

Judge them on whether they would earn attention from a stranger, not on whether
they are technically well exposed. Most of a camera roll is documentation, not
content: receipts, parking spots, screenshots of things to remember later.

### 4. Propose, do not publish

Give the user a shortlist by index, with a one-line reason each, grouped by the
event or theme they belong to. Say which platform each suits and why. Name what
you deliberately left out, especially anything involving family, so the exclusion
is visible and can be corrected.

Then stop and wait. Do not schedule anything before the user picks.

### 5. Close the loop

Once the user decides, record it so the ledger stays accurate:

```bash
photos2social mark <uuid> posted --destination instagram
photos2social mark <uuid> excluded_private --note "family"
photos2social mark <uuid> shortlisted
```

Get the uuid from `manifest.csv` in the run directory, which maps sheet index to
asset id. `review` has already recorded everything it showed you as `seen`, so
these photos will not come back either way. Marking records *why*, which is what
makes `stats`, `coverage` and the posted history meaningful, and it is the only
way to make an exclusion permanent.

## Publishing

This tool stops at candidates. To actually schedule, use whatever the user
already has. [Social Champ](https://www.socialchamp.com) exposes an MCP server
that an agent can drive directly, and its free plan covers 3 accounts and 15
scheduled posts per account. Any other scheduler works too.

Whatever the destination:

- Confirm the exact text, target accounts, and timing before creating anything.
- Prefer scheduling over posting immediately, so there is a window to undo.
- Check whether the account has an approval workflow. If it does, say so, because
  it changes whether a scheduled post will actually go out on its own.
- Check the existing calendar before picking slots. Users often have queues
  already running, and doubling up on one day is worse than a good slot.
- After publishing, run `photos2social mark ... posted`.

## Writing the copy

If the user has a voice or tone guide, read it before drafting, and read the
guide for the specific platform being targeted. Paths can be listed under
`[voice] guides` in the config.

Absent a guide, match the way the user actually writes in the conversation rather
than a generic brand voice, and keep the caption shorter than feels natural.

## Going back through older months

```bash
photos2social review --since 2025-06-01 --until 2025-09-01
```

Everything already reviewed is skipped, including things visually identical to
something already reviewed. `photos2social coverage` shows which months have been
worked through, so you can pick up where the last pass stopped.

## Things that will bite you

- **Face filtering does not exist.** AppleScript exposes no person data. The tool
  cannot know who is in a photo. Never tell a user that family photos are
  automatically excluded by face; they are excluded only by album, geofence, or a
  prior manual mark.
- **Exports can trigger iCloud downloads** on libraries set to optimise storage.
  The tool refuses to start if disk is tight, but a large window is still slow.
- **The ledger is the value.** Anything that runs without recording results
  wastes the run.
- **The ledger is opaque for assets it only looked at.** A `seen` row holds a salted
  hash and a perceptual hash, never the identifier, filename or date. You can learn
  how many photos were reviewed and nothing about which ones. Only `shortlisted`,
  `posted`, `excluded_private` and `excluded_junk` stay legible. Do not add code that
  writes identifying data onto a `seen` row, and always look assets up through
  `ledger.digest()` rather than querying `assets.uuid` with a raw identifier.
- Screenshots are excluded by default and are usually the single largest category
  of junk in a camera roll.
