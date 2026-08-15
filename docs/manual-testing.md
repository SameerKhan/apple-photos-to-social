# Manual test checklist

The unit suite never touches a real library, so everything below needs a Mac with
Photos and a real library. Run before a release.

## Permissions and preflight

- [ ] `photos2social doctor` on a machine that has never been granted Automation
      access. Expect a permission prompt, and a clear message if it is denied.
- [ ] Deny the prompt, re-run `doctor`. It should report Photos as unavailable and
      exit non-zero rather than hanging.
- [ ] Quit Photos entirely, then run `doctor`. It should still work; Photos is
      launched in the background.

## Privacy, the part that matters most

- [ ] No config at all: `review` must refuse and exit 2.
- [ ] `--allow-unfiltered`: must proceed, having said clearly what that means.
- [ ] Misspell an album in `exclude_albums`: `review` must stop and name it.
- [ ] Real album in `exclude_albums`: assets in it must not appear in any sheet,
      and must show as `excluded_private` in `stats` afterwards.
- [ ] Delete that album from config and re-run: previously excluded assets must
      still not appear, because the ledger made the exclusion sticky.
- [ ] Geofence over a known location: assets there are withheld.
- [ ] An asset with no GPS, with zones configured and
      `require_location_for_zones = true`: withheld.
- [ ] `mark <uuid> excluded_private`, then re-run with `resurface_settled = true`:
      the asset must stay private, because automated writes cannot undo it.

## The ledger promise

- [ ] Run `review --days 30` twice. The second run must report zero new assets.
- [ ] Re-import a photo already reviewed so it gets a new asset id. It must be
      caught by visual match, not re-surfaced.
- [ ] `coverage` shows the months worked through.

## Export correctness

- [ ] Two assets sharing a filename stem in one window: both must appear, with the
      correct pixels for each.
- [ ] A HEIC asset: exported as JPEG, hashed without pillow-heif installed.
- [ ] Interrupt a run with Ctrl-C mid-export. The run row must be marked `failed`,
      and the next run must not consume the partial export.
- [ ] Library set to Optimize Mac Storage: exporting still works, and is slower.
- [ ] Fill the disk close to the configured floor: the run must refuse to start.

## Other

- [ ] `--include-videos` with ffmpeg present: videos appear in sheets.
- [ ] Same, with ffmpeg removed from PATH: videos are skipped, no crash.
- [ ] A system set to a non-English locale, or month-first dates: parsing holds.
- [ ] An album name containing a quote or a backslash.
- [ ] `purge` deletes the workspace and keeps the ledger.
