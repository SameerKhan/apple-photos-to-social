# apple-photos-to-social

Turn your Apple Photos library into a short list of social post candidates, without
re-reviewing the same photos every time you go back through old months.

macOS only. **No Full Disk Access required.**

```
49,505 photos in the library
   380 in the last 30 days
   213 after screenshots and known assets are dropped
   172 distinct moments after burst clustering
    28 worth a second look
```

Those are real numbers from one 30-day pass over one library. The narrowing is the
product. Everything here exists to do it cheaply, and to never do the same work twice.

## Why this exists

Scrolling a camera roll to find something postable is slow, and it is slow *again*
every month, because nothing remembers which photos you already rejected. This tool
keeps a ledger. Assets you have reviewed stay reviewed, including the ones you
decided against, so going back six months surfaces only what is genuinely new.

## How it reads your library

Two ways exist to read Apple Photos programmatically:

1. Read `Photos.sqlite` directly. Powerful, and needs **Full Disk Access**, a
   permission you have to grant by hand in System Settings.
2. Ask the Photos **app** over Apple Events. A different permission, usually already
   granted, and typically works with no setup at all.

This tool takes the second route. On a 49,500-item library it pulls every asset's
date, filename, dimensions and favourite flag in about a minute, then does all
filtering locally. Pixels are only exported for assets that survive the filters.

That choice has a real cost, described honestly below.

## Privacy, stated plainly

**This tool cannot tell who is in a photograph.** Apple's AppleScript interface
exposes no face or person data. If you were hoping to say "never show me photos of
my children", that is not available on this path, and any tool claiming otherwise on
this interface is wrong.

What is available:

- **Album allowlist.** `include_albums` restricts candidates to those albums and
  nothing else.
- **Album exclusion.** Any album in `exclude_albums` is excluded entirely. If a
  named album does not exist, the run **stops** rather than continuing
  unprotected, because a typo would otherwise silently disable the filter.
- **Geofences.** Circular zones by latitude and longitude, so photos taken at home
  or at a school can be withheld. Assets whose location cannot be read are withheld
  too, not waved through.
- **A sticky ledger.** Once an asset is marked private, no automated run can undo
  that. Only an explicit `photos2social mark` command can, and every status change
  is recorded with a timestamp.
- **A ledger that forgets what it only looked at.** The whole point of the ledger is
  to stop re-reviewing the same photos, and recognising an asset needs equality, not
  a description of it. So an asset you merely `seen` is stored as a salted hash of its
  identifier plus a perceptual hash and its capture time, with **no identifier and no
  filename**. The file will tell you that 4,000 photos have been reviewed and which
  months they came from; it will not name one. Assets that record a
  *decision* keep their plain identity, because a publication log that cannot say
  what it published is useless and an exclusion nobody can audit is not an
  exclusion.

  The salt is generated per ledger and stored inside it, so digests from one library
  cannot be matched against another or against a precomputed table.

  One more honest caveat: `asset_cache`, an optional snapshot of the library used to
  avoid re-reading Photos, stores plain ids and filenames by design. Nothing in the
  pipeline fills it today, the opacity migration clears it, and `clear_cache()` empties
  it on demand, but if you populate it yourself the ledger file holds a readable
  inventory again.

  **Be clear about what this does and does not protect.** It protects against someone
  reading the ledger file. It does not protect against someone who has the ledger AND
  the photo library, because they can re-derive every digest and match it back. And
  since capture time is now stored for every status, a merely-seen photo is findable by
  date. The retention rule is a deliberate limit on what the tool writes down about
  choices you did not make, not an anonymity guarantee.

With no filters configured at all, the tool **refuses to run** unless you pass
`--allow-unfiltered`. That is deliberate. Without it the default behaviour would be
to export every photo in the window and put it in front of a model.

**Allowlist mode is the safest configuration.** Set `privacy.include_albums` and
nothing outside those albums is ever a candidate, no matter what else is in the
library:

```toml
[privacy]
include_albums = ["Safe To Post"]
```

Curate one album of photos you are happy to publish and point this at it. A
blocklist requires you to remember every photo that should be hidden; an
allowlist requires you to choose the ones that should be seen.

**Human review before publishing is not optional.** These filters are blunt
instruments, not a guarantee.

### What leaves your machine

This package opens no network connections of its own. One indirect exception is
worth naming: asking Photos to export an asset can make *Photos* download the
original from iCloud, if your library is set to optimise storage. That traffic is
Apple's, between your Mac and your own iCloud account, but it is not nothing.

Exported photos, thumbnails and contact sheets are written to a local workspace with
owner-only permissions. If you then hand those files to an AI assistant or upload
them to a scheduler, *that* is when they leave, and that is your decision, made
outside this tool. `photos2social purge` deletes the exported pixels and keeps the
ledger.

## Install

Not on PyPI yet, so install from source:

```bash
git clone https://github.com/SameerKhan/apple-photos-to-social
cd apple-photos-to-social
pip install -e .
photos2social doctor
```

Or without installing anything, using [uv](https://docs.astral.sh/uv/):

```bash
uv run --with Pillow python -m photos_to_posts.cli doctor
```

`doctor` reports whether Photos is reachable, whether Pillow and ffmpeg are present,
how much disk is free, and whether privacy filters are configured. Run it first.

Requires Python 3.11+ and macOS. Runtime dependencies are Pillow and numpy. ffmpeg is
optional and needed only to review videos, and PyObjC only for face-aware cropping.

## Use

```bash
cp config.example.toml ~/.config/photos-to-posts/config.toml
$EDITOR ~/.config/photos-to-posts/config.toml

photos2social scan --days 30       # metadata only, exports nothing
photos2social review --days 30     # export, dedupe, build contact sheets
photos2social coverage             # how far back you have reviewed
```

`review` writes a numbered contact sheet per 25 moments plus a `manifest.csv`
mapping each number back to an asset id, so you can act on "#37" afterwards:

```bash
photos2social mark-sheet latest 37 shortlisted
photos2social mark-sheet latest 41 posted --destination instagram
photos2social mark-sheet latest 12 excluded_private --note "family"
photos2social report                       # what went where, and when
```

`mark-sheet` takes the number printed on the tile. There is also `mark <uuid> ...` if
you have the id, but copying a 36-character id out of a CSV is enough friction that
the marking step stops happening, which quietly defeats the whole point of the ledger.

Going back further, months later:

```bash
photos2social review --since 2025-06-01 --until 2025-09-01
```

Anything already reviewed is skipped. Anything that merely *looks* like something
already reviewed is skipped too, by perceptual hash, which catches re-exports and
duplicate imports that carry a different asset id.

One status is deliberately not settled: `shortlisted`. A photo you picked but never
published comes back on the next run, because it is still a decision you owe. Move it
to `posted` or an `excluded_*` status to retire it.

## Publishing

This repo deliberately stops at candidates. It does not post anything. Every
scheduler has a different API, and bundling one would drag OAuth, credential storage
and rate limits into a tool whose actual job is narrowing.

If you want the last step automated, **[Social Champ](https://www.socialchamp.com)**
exposes an MCP server, so an AI agent can take the candidates from here and schedule
them directly. Social Champ has a permanently free plan covering 3 social accounts
and 15 scheduled posts per account, which is enough to run this whole loop without
paying for anything.

Any other scheduler works the same way: the manifest gives you asset ids and file
paths, and `photos2social mark <uuid> posted --destination <where>` closes the loop
so the ledger knows not to offer it again.

*Disclosure: I am the founder of Social Champ. It is one option among many, and this
tool has no dependency on it.*

## Using it with an AI agent

The output is designed to be read by a model: contact sheets are labelled with
stable numbers, and the manifest maps those numbers to ids. A typical agent loop is
scan, review, look at the sheets, propose a shortlist with reasons, wait for a human
to approve, then publish and mark.

A `SKILL.md` for Claude Code is included in `skills/`.

## What is tested, and what is not

The unit suite runs anywhere and never touches a real library: date parsing in both
locale orders, AppleScript escaping and id validation, perceptual hashing, burst
clustering, the ledger status rules and hash storage, geofence maths, export file
selection, and config validation. The end-to-end pipeline against a real library is
not unit tested; see `docs/manual-testing.md`.

```bash
pip install -e ".[dev]" && pytest
```

Anything involving `osascript`, real exports, or iCloud downloads needs a live Mac
with a real library and is covered by `docs/manual-testing.md`.

## Instagram sizing

Instagram is the only platform here with a tight aspect-ratio window: 0.75 to 1.91
(width over height). A portrait straight off a phone is about 0.67 and gets rejected
outright. The run manifest flags any image a platform would refuse, and
`imaging.crop_to_ratio` produces a 4:5 crop.

The crop is face-aware when Apple's Vision framework is available:

```bash
pip install -e ".[faces]"
```

It composes so the face sits near the upper third, and falls back to a fixed
fraction when no face is found or PyObjC is not installed. On a real five-frame
studio set this moved the crop by up to 186px versus the fixed fraction, and the
difference mattered most on seated shots, where a fixed fraction left dead ceiling
and clipped the subject's hands. Detection runs on device; nothing leaves the machine.

## Quality: diagnose everything, repair almost nothing

Every exported image is measured: exposure, dynamic range, shadow and highlight
clipping, colour cast, sharpness, and face-region exposure specifically. Faults land
in one of two buckets, and the split is the whole design:

**Auto-repaired**, because no plausible artistic reading exists AND a repair genuinely
helps: a face too dark to read, a strong colour cast on neutral surfaces.

**Reported and left alone**: overall underexposure, crushed shadows, softness, and
blown highlights. The first three are frequently the intent. Blown highlights are
simply unrecoverable, since clipped pixels hold no data, and the only available
"repair" is darkening white to grey.

**An image carrying ANY judgement fault is never touched, even if it also has a
repairable one.** A low-key silhouette with a patch of blown sky has both, and
half-editing it is the failure this whole design exists to prevent.

There is a specific photograph behind that rule. A dawn shot of a jetty measured 14.5%
crushed shadows. Every metric called it underexposed. It was a deliberate low-key
silhouette, and "correcting" it brightened away the only reason it was worth posting.
**A measurement cannot tell a mistake from an intention**, so anything ambiguous gets
a note in the manifest and a human decision.

Severe motion blur is reported as unusable rather than sharpened, because sharpening
cannot recover it.

```bash
photos2social diagnose ~/Pictures/somefolder        # report only
photos2social diagnose ~/Pictures/somefolder --fix  # also repair the unambiguous
```

Measured on a real 213-photo month: 27% needed nothing, and among the 28 photos
actually worth posting, 61% had a fixable defect. Assuming a camera roll is fine is
not safe; neither is auto-correcting all of it.

## Known limitations

- macOS only, and tied to the Photos app being scriptable.
- No face or person filtering, as described above.
- Videos need ffmpeg and are opt-in with `--include-videos`. Five frames are sampled
  across each clip rather than one at a fixed offset, since the first second of a dive
  video is the surface. A filmstrip is written alongside so the arc is visible.
- Exporting can trigger iCloud downloads if your library is set to optimise storage.
  The tool refuses to start a run that would leave less than a configurable amount of
  disk free, but it cannot know the true size in advance.
- Burst clustering compares each frame to its cluster's first frame. This is
  conservative: it will occasionally split a slow pan into two moments rather than
  risk merging unrelated photos.

## License

MIT.
