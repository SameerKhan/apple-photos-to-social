"""The review pipeline: library -> filter -> export -> dedupe -> contact sheets.

Ordering matters here and is deliberate. Metadata is cheap (seconds for a whole
library) and pixels are expensive (seconds *per asset*), so every filter that can
run on metadata runs before a single file is exported.

Privacy posture: this module fails closed. If a configured privacy filter cannot
be evaluated, the run stops rather than exporting the assets it could not check.
The alternative, warning and continuing, is how family photos end up in front of
a model.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from . import applescript as ps
from . import imaging
from .config import Config, haversine_m
from .ledger import (EXCLUDED_PRIVATE, SEEN, SETTLED, SETTLED_STRICT, TERMINAL,
                     Ledger, hamming)

TERMINAL_STATUSES = tuple(sorted(TERMINAL))

Progress = Callable[[str], None]


class PrivacyRefusal(RuntimeError):
    """Raised instead of exporting when privacy filters cannot be trusted."""


@dataclass
class ReviewResult:
    window_days: int
    total_in_library: int
    in_window: int
    skipped_known: int = 0
    skipped_screenshot: int = 0
    skipped_small: int = 0
    skipped_album: int = 0
    skipped_zone: int = 0
    skipped_unknown_location: int = 0
    skipped_not_allowlisted: int = 0
    skipped_seen_before_visually: int = 0
    videos_deferred: int = 0
    exported: int = 0
    export_failures: int = 0
    moments: int = 0
    run_dir: Path | None = None
    manifest_path: Path | None = None
    sheets: list[Path] = field(default_factory=list)
    frames: list[imaging.Frame] = field(default_factory=list)
    burst_counts: dict[str, int] = field(default_factory=dict)
    candidates: list[ps.Asset] = field(default_factory=list)

    def summary(self) -> str:
        bits = [
            f"library {self.total_in_library:,}",
            f"window {self.in_window:,}",
            f"exported {self.exported:,}",
            f"moments {self.moments:,}",
        ]
        skips = {
            "already reviewed": self.skipped_known,
            "seen before (visual match)": self.skipped_seen_before_visually,
            "screenshots": self.skipped_screenshot,
            "too small": self.skipped_small,
            "private album": self.skipped_album,
            "geofenced": self.skipped_zone,
            "location unknown": self.skipped_unknown_location,
            "not in allowlist": self.skipped_not_allowlisted,
            "videos deferred": self.videos_deferred,
            "export failed": self.export_failures,
        }
        dropped = ", ".join(f"{k} {v}" for k, v in skips.items() if v)
        if dropped:
            bits.append(f"skipped: {dropped}")
        return " | ".join(bits)


def _naive(dt: datetime | None) -> datetime | None:
    """Photos returns naive local datetimes; normalise callers' input to match.

    Comparing a naive datetime to an aware one raises TypeError, and a caller
    passing ``datetime.now(timezone.utc)`` is an easy mistake to make.
    """
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def select_window(assets: Sequence[ps.Asset], *, days: int | None = None,
                  since: datetime | None = None, until: datetime | None = None
                  ) -> list[ps.Asset]:
    """Assets captured inside a time window, newest first."""
    since, until = _naive(since), _naive(until)
    if since is None:
        since = datetime.now() - timedelta(days=days or 30)
    hits = [a for a in assets
            if a.captured_at >= since and (until is None or a.captured_at <= until)]
    return sorted(hits, key=lambda a: a.captured_at, reverse=True)


def apply_filters(assets: Sequence[ps.Asset], cfg: Config, ledger: Ledger,
                  result: ReviewResult, *, include_videos: bool = False,
                  allow_unfiltered: bool = False, dry_run: bool = False) -> list[ps.Asset]:
    """Drop everything that should not reach the eyeball stage.

    Runs on metadata plus, if geofences are configured, one batched Apple Event
    for locations. No pixels are touched.

    Raises :class:`PrivacyRefusal` when a configured filter cannot be evaluated,
    or when no filter is configured at all and the caller has not explicitly
    accepted that.
    """
    if not cfg.privacy_configured and not (allow_unfiltered or cfg.allow_unfiltered):
        raise PrivacyRefusal(
            "No privacy filters are configured, so every photo in the window would be "
            "exported and shown to a model, including photos of family. Configure "
            "privacy.exclude_albums or privacy.exclude_zones, or pass allow_unfiltered "
            "to accept that explicitly."
        )

    # `resurface_settled` is a debugging aid, and it must never reach back into
    # privacy. Excluded assets stay excluded no matter what: their status in the
    # ledger is a decision about confidentiality, not about review bookkeeping.
    if cfg.resurface_settled:
        settled = ledger.uuids_with_status(TERMINAL_STATUSES)
    else:
        settled = ledger.settled_uuids(include_seen=not cfg.resurface_seen)

    album_ids: set[str] = set()
    if cfg.exclude_albums:
        lookup = ps.album_asset_ids(cfg.exclude_albums)
        if lookup.missing:
            raise PrivacyRefusal(
                "These albums are named in privacy.exclude_albums but do not exist in "
                f"Photos: {', '.join(lookup.missing)}. A misspelled album excludes "
                "nothing, so the run is stopping rather than exporting unprotected."
            )
        album_ids = lookup.ids

    # Allowlist mode: the safest configuration, and the one the README recommends.
    # When set, nothing outside these albums is ever a candidate.
    include_ids: set[str] | None = None
    if cfg.include_albums:
        inc = ps.album_asset_ids(cfg.include_albums)
        if inc.missing:
            raise PrivacyRefusal(
                "These albums are named in privacy.include_albums but do not exist in "
                f"Photos: {', '.join(inc.missing)}. In allowlist mode a missing album "
                "would silently widen what gets reviewed, so the run is stopping."
            )
        include_ids = inc.ids

    kept: list[ps.Asset] = []
    excluded_private: list[tuple[ps.Asset, str]] = []
    for a in assets:
        if a.uuid in settled:
            result.skipped_known += 1
            continue
        # Privacy is evaluated BEFORE the cheap content filters. Otherwise a
        # screenshot or a video that happens to live in a private album is
        # dropped for the wrong reason and never recorded as private, so it
        # loses that protection the moment the other filter stops applying.
        if a.uuid in album_ids:
            result.skipped_album += 1
            excluded_private.append((a, "album"))
            continue
        if include_ids is not None and a.uuid not in include_ids:
            result.skipped_not_allowlisted += 1
            continue
        if cfg.exclude_screenshots and a.is_screenshot:
            result.skipped_screenshot += 1
            continue
        if cfg.min_pixels and a.width * a.height and a.width * a.height < cfg.min_pixels:
            result.skipped_small += 1
            continue
        if a.kind == "video" and not include_videos:
            result.videos_deferred += 1
            continue
        kept.append(a)

    if cfg.exclude_zones and kept:
        loc = ps.fetch_locations([a.uuid for a in kept])
        allowed: list[ps.Asset] = []
        for a in kept:
            point = loc.found.get(a.uuid)
            if point is not None:
                hit = next((z for z in cfg.exclude_zones
                            if haversine_m(point[0], point[1], z.latitude, z.longitude)
                            <= z.radius_m), None)
                if hit:
                    result.skipped_zone += 1
                    excluded_private.append((a, f"zone:{hit.name}"))
                    continue
                allowed.append(a)
            elif a.uuid in loc.failed:
                # Unknown is not the same as safe. A geofence cannot clear an
                # asset whose location could not be read.
                result.skipped_unknown_location += 1
                continue
            elif cfg.require_location_for_zones:
                result.skipped_unknown_location += 1
                continue
            else:
                allowed.append(a)
        kept = allowed

    # Persist automatic exclusions so they are sticky. Without this, removing or
    # renaming an album in config silently re-exposes everything it protected.
    #
    # Skipped under dry_run: the command promises to write nothing, and writing
    # privacy decisions would be a surprising side effect of a preview.
    if not dry_run:
        for asset, why in excluded_private:
            ledger.record(uuid=asset.uuid, filename=asset.filename,
                          captured_at=asset.captured_at, kind=asset.kind,
                          status=EXCLUDED_PRIVATE, note=why, reason=f"auto:{why}")

    return kept


def screen_against_history(frames: Sequence[imaging.Frame], ledger: Ledger, cfg: Config,
                           result: ReviewResult, assets: dict[str, ps.Asset]
                           ) -> list[imaging.Frame]:
    """Drop frames that visually match something already settled in the ledger.

    This is the cross-run half of deduplication. Burst clustering only collapses
    repeats *within* one run; this catches the same scene arriving again months
    later as a different asset, which is what happens with re-exports, duplicate
    imports and photos shared back by other people.

    A dropped frame is recorded as ``seen`` before it is discarded. Without that,
    its (new) asset id is never settled, so the next run re-exports and re-hashes
    it forever, which is exactly the loop the ledger exists to break.
    """
    history = ledger.all_hashes(SETTLED if not cfg.resurface_seen else SETTLED_STRICT)
    if not history:
        return list(frames)
    kept: list[imaging.Frame] = []
    for frame in frames:
        if frame.phash is None:
            kept.append(frame)
            continue
        # Skip the asset's own history row. With resurface_settled the asset is
        # deliberately back in the candidate set, and matching it against itself
        # both drops it again and rewrites its status.
        if any(hamming(stored, frame.phash) <= cfg.history_max_distance
               for uuid, stored, _st in history if uuid != frame.uuid):
            result.skipped_seen_before_visually += 1
            asset = assets.get(frame.uuid)
            if asset is not None:
                ledger.record(uuid=asset.uuid, filename=asset.filename,
                              captured_at=asset.captured_at, kind=asset.kind,
                              status=SEEN, phash=frame.phash,
                              reason="auto:visual-match-with-history")
            continue
        kept.append(frame)
    return kept


def run_review(cfg: Config, *, days: int | None = None, since: datetime | None = None,
               until: datetime | None = None, include_videos: bool = False,
               limit: int | None = None, allow_unfiltered: bool = False,
               dry_run: bool = False, log: Progress | None = None) -> ReviewResult:
    """Full pass: fetch, filter, export, hash, cluster, build sheets, record.

    ``dry_run`` stops after filtering. Nothing is exported, no pixels are read and
    the ledger is not written, so it is safe to use to see what a run would touch.
    """
    say = log or (lambda _m: None)

    ok, why = ps.is_available()
    if not ok:
        raise ps.PhotosError(f"Cannot talk to the Photos app: {why}")

    say("Reading library metadata from Photos")
    assets = ps.fetch_all_assets()
    window = select_window(assets, days=days or cfg.default_days, since=since, until=until)

    result = ReviewResult(
        window_days=days or cfg.default_days,
        total_in_library=len(assets),
        in_window=len(window),
    )

    # A dry run against a machine that has never run this must not bring a ledger
    # into existence: "writes nothing" has to mean nothing, including the file.
    if dry_run and not Path(cfg.ledger_path).expanduser().exists():
        ledger_cm = _NullLedger()
    else:
        ledger_cm = Ledger(cfg.ledger_path)

    with ledger_cm as ledger:
        candidates = apply_filters(window, cfg, ledger, result,
                                   include_videos=include_videos,
                                   allow_unfiltered=allow_unfiltered,
                                   dry_run=dry_run)
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must not be negative")
            candidates = candidates[:limit]
        result.candidates = candidates
        say(f"{len(candidates)} candidates after filtering")

        if dry_run or not candidates:
            return result

        _check_disk_budget(cfg, len(candidates))

        run_id = ledger.start_run(window=_window_label(window))
        recorded = 0
        try:
            run_dir = cfg.workspace / f"run_{run_id:05d}"
            # Run ids restart at 1 with a fresh ledger, so a directory of this
            # name can already exist and hold sheets built under looser privacy
            # settings. Start clean rather than mixing two runs in one folder.
            if run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=True)
            export_dir = run_dir / "export"
            thumb_dir = run_dir / "thumbs"
            for d in (run_dir, export_dir, thumb_dir):
                d.mkdir(parents=True, exist_ok=True, mode=0o700)
                # mkdir's mode is ignored for a directory that already exists, so
                # a pre-existing permissive directory is repaired explicitly.
                _harden(d, 0o700)
            result.run_dir = run_dir

            say(f"Exporting {len(candidates)} assets (derivatives, not originals)")
            mapping = ps.export_assets(
                [a.uuid for a in candidates], export_dir, originals=False,
                video_uuids={a.uuid for a in candidates if a.kind == "video"},
                progress=lambda done, total: _export_progress(cfg, say, done, total))
            result.export_failures = len(candidates) - len(mapping)

            by_uuid = {a.uuid: a for a in candidates}
            frames: list[imaging.Frame] = []
            for a in candidates:
                src = mapping.get(a.uuid)
                if src is None:
                    continue
                # Thumbnails are named by uuid, not by source stem, so two assets
                # sharing a filename cannot overwrite one another.
                thumb = thumb_dir / f"{ps.uuid_to_dirname(a.uuid)}.jpg"
                try:
                    if a.kind == "video":
                        grabbed = thumb_dir / f"{ps.uuid_to_dirname(a.uuid)}_frame.jpg"
                        if not imaging.video_frame(src, grabbed):
                            continue
                        imaging.downscale(grabbed, thumb, cfg.thumbnail_edge)
                    else:
                        imaging.downscale(src, thumb, cfg.thumbnail_edge)
                    h = imaging.dhash(thumb)
                    _harden(src, 0o600)
                    _harden(thumb, 0o600)
                except OSError:
                    continue
                frames.append(imaging.Frame(uuid=a.uuid, path=thumb, filename=a.filename,
                                            captured_at=a.captured_at, phash=h))
            result.exported = len(frames)

            frames = screen_against_history(frames, ledger, cfg, result, by_uuid)

            say("Clustering bursts")
            clusters = imaging.cluster_bursts(
                frames, max_distance=cfg.burst_max_distance,
                max_gap_seconds=cfg.burst_max_gap_seconds)
            reps = [c[0] for c in clusters]
            result.moments = len(clusters)
            result.burst_counts = {c[0].uuid: len(c) for c in clusters}
            result.frames = reps

            say(f"Building contact sheets for {len(reps)} distinct moments")
            result.sheets = imaging.contact_sheets(
                reps, run_dir / "sheets", counts=result.burst_counts)
            result.manifest_path = _write_manifest(run_dir, reps, result.burst_counts)

            # Everything that reached the eyeball stage is recorded, not just the
            # cluster representatives, or the non-representative burst frames come
            # back as new on the next pass.
            for frame in frames:
                a = by_uuid.get(frame.uuid)
                if a is None:
                    continue
                ledger.record(uuid=a.uuid, filename=a.filename, captured_at=a.captured_at,
                              kind=a.kind, status=SEEN, phash=frame.phash,
                              reason=f"run:{run_id}")
                recorded += 1
            ledger.finish_run(run_id, examined=len(candidates), recorded=recorded)
        except BaseException:
            ledger.finish_run(run_id, examined=len(candidates), recorded=recorded,
                              state="failed")
            raise

    return result


def _harden(path: Path, mode: int) -> None:
    """Force owner-only permissions, tolerating filesystems that refuse."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def free_bytes(cfg: Config) -> int:
    base = cfg.workspace if cfg.workspace.exists() else cfg.workspace.parent
    return shutil.disk_usage(base if base.exists() else Path.home()).free


def _check_disk_budget(cfg: Config, count: int) -> None:
    """Refuse to start an export that could fill the disk.

    Exports also trigger iCloud downloads when the library is set to optimise
    storage, so the space needed is not bounded by what is already local.
    """
    cfg.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    free = free_bytes(cfg)
    projected = count * cfg.assumed_bytes_per_asset
    if free - projected < cfg.min_free_bytes:
        raise RuntimeError(
            f"Refusing to export {count} assets: projected {projected / 1e9:.1f} GB would "
            f"leave less than the configured {cfg.min_free_bytes / 1e9:.1f} GB free "
            f"({free / 1e9:.1f} GB free now). Narrow the window, set a limit, or "
            "lower export.min_free_gb."
        )


class _NullLedger:
    """Stand-in used by a dry run when no ledger exists yet.

    Reports an empty history and swallows writes, so a preview on a fresh machine
    leaves the filesystem exactly as it found it.
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settled_uuids(self, include_seen: bool = True) -> set[str]:
        return set()

    def uuids_with_status(self, statuses) -> set[str]:
        return set()

    def all_hashes(self, statuses=()) -> list:
        return []

    def record(self, **kwargs) -> str:
        return kwargs.get("status", "seen")

    # Not reachable today, because run_review returns on dry_run before a run is
    # started. Present so that a later reordering fails visibly rather than with
    # an AttributeError halfway through a run.
    def start_run(self, window=None, note=None) -> int:
        return -1

    def finish_run(self, run_id, examined=0, recorded=0, state="completed") -> None:
        return None


class DiskExhausted(RuntimeError):
    """Raised mid-export when free space falls through the configured floor."""


def _export_progress(cfg: Config, say: Progress, done: int, total: int) -> None:
    """Report progress and abort if the disk is filling.

    The pre-flight estimate cannot bound how much iCloud decides to download, so
    free space is re-checked periodically rather than trusted once.
    """
    say(f"  exported {done}/{total}")
    if done % 25 == 0 and free_bytes(cfg) < cfg.min_free_bytes:
        raise DiskExhausted(
            f"Free space fell below the configured floor of "
            f"{cfg.min_free_bytes / 1e9:.1f} GB after {done} assets. Stopping. "
            f"Run `photos2social purge` to reclaim exported pixels.")


def _window_label(window: Sequence[ps.Asset]) -> str | None:
    if not window:
        return None
    lo = min(a.captured_at for a in window)
    hi = max(a.captured_at for a in window)
    return f"{lo:%Y-%m-%d}..{hi:%Y-%m-%d}"


def _write_manifest(run_dir: Path, frames: Sequence[imaging.Frame],
                    counts: dict[str, int]) -> Path:
    """Map contact-sheet numbers back to asset ids.

    Without this the sheets are unusable for follow-up: a reviewer says "#37" and
    there is nothing that turns that into a uuid to mark posted.
    """
    import csv
    path = run_dir / "manifest.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["index", "uuid", "filename", "captured_at", "burst_count",
                    "ratio", "fits", "thumbnail"])
        for i, f in enumerate(frames, 1):
            ratio, fits = "", ""
            try:
                from PIL import Image
                with Image.open(f.path) as im:
                    fit = imaging.platform_fit(*im.size)
                    ratio = f"{im.size[0] / im.size[1]:.3f}"
                    # Only the platforms that will REJECT it are worth writing down.
                    bad = [k for k, ok in fit.items() if not ok]
                    fits = "ok" if not bad else "too tall for " + "/".join(sorted(bad))
            except OSError:
                pass
            w.writerow([i, f.uuid, f.filename, f.captured_at.isoformat(),
                        counts.get(f.uuid, 1), ratio, fits, str(f.path)])
    return path
