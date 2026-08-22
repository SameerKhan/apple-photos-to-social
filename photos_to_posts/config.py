"""Configuration loading.

TOML rather than YAML so the package has no parsing dependency: ``tomllib`` is in
the standard library from Python 3.11.

Everything personal lives here and nowhere else in the codebase. That is the
whole point of the file: the tool ships generic, and your names, albums, home
coordinates, brand guides and channel ids stay on your machine.
"""
from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATHS = (
    Path("./photos-to-posts.toml"),
    Path("~/.config/photos-to-posts/config.toml").expanduser(),
    Path("~/.photos-to-posts/config.toml").expanduser(),
)


@dataclass
class Zone:
    """A circular area whose photos should never be surfaced (home, school, clinic)."""
    name: str
    latitude: float
    longitude: float
    radius_m: float = 250.0


@dataclass
class Config:
    ledger_path: Path = Path("~/.photos-to-posts/ledger.db").expanduser()
    workspace: Path = Path("~/.photos-to-posts/work").expanduser()

    default_days: int = 30
    exclude_screenshots: bool = True
    min_pixels: int = 0
    burst_max_distance: int = 12
    burst_max_gap_seconds: int = 180
    resurface_settled: bool = False
    # Opt out of the no-filters refusal permanently, for someone reviewing their
    # own library who accepts that everything in a window becomes a candidate.
    # Defaults to False so a fresh install always fails closed.
    allow_unfiltered: bool = False
    # When False, an asset reviewed and not chosen stays suppressed on later runs.
    # That is the whole point of the ledger, so it defaults to False.
    resurface_seen: bool = False
    # Hamming distance at which a candidate counts as "I have seen this scene
    # before" against ledger history. Tighter than burst clustering because a
    # false positive here silently hides a photo you have never reviewed.
    history_max_distance: int = 6
    # A photo you PUBLISHED should suppress its near-neighbours far more aggressively
    # than one you merely looked at. Two studio frames 7 apart are the same picture to
    # a viewer; at history_max_distance = 6 they were treated as unrelated and one was
    # re-proposed after the other had already gone out. Defaults to the burst radius,
    # because a pair that would collapse into one moment INSIDE a run must not count as
    # two different photos ACROSS runs.
    published_max_distance: int = 12

    exclude_albums: list[str] = field(default_factory=list)
    # Allowlist. When non-empty, ONLY assets in these albums are ever
    # candidates. The safest way to use this tool on a family library.
    include_albums: list[str] = field(default_factory=list)
    exclude_zones: list[Zone] = field(default_factory=list)
    # With zones configured, should an asset whose location is unknown be treated
    # as outside the zone (False) or withheld (True)? True is the safe default
    # once you care enough about location to configure a geofence at all.
    require_location_for_zones: bool = True

    thumbnail_edge: int = 900
    # Rough per-asset sizes for the pre-export disk check. These must be separate:
    # a photo derivative is single-digit MB, while a 4K video derivative is commonly
    # 100-500 MB. Using the photo figure for both under-estimates a video run by
    # 10x to 50x and fills the disk mid-export.
    assumed_bytes_per_asset: int = 8_000_000
    assumed_bytes_per_video: int = 200_000_000
    min_free_bytes: int = 5_000_000_000

    voice_guides: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    source_path: Path | None = None

    @property
    def privacy_configured(self) -> bool:
        """True when at least one automatic privacy filter exists.

        Worth checking before a run: with nothing configured, the only thing
        standing between a family photo and a draft post is human review.
        """
        return bool(self.include_albums or self.exclude_albums or self.exclude_zones)


def _expand(p: str) -> Path:
    return Path(p).expanduser()


def load_config(path: str | Path | None = None) -> Config:
    """Load config from an explicit path, else the first default that exists.

    Missing file is not an error: the defaults are usable, just with no privacy
    filters, which :attr:`Config.privacy_configured` lets callers warn about.
    """
    candidates = [Path(path).expanduser()] if path else list(DEFAULT_CONFIG_PATHS)
    found = next((p for p in candidates if p.is_file()), None)
    if found is None:
        if path:
            raise FileNotFoundError(f"config not found: {path}")
        return Config()

    with found.open("rb") as fh:
        raw = tomllib.load(fh)

    cfg = Config(source_path=found)
    ledger = raw.get("ledger", {})
    if "path" in ledger:
        cfg.ledger_path = _expand(ledger["path"])

    workspace = raw.get("workspace", {})
    if "path" in workspace:
        cfg.workspace = _expand(workspace["path"])

    review = raw.get("review", {})
    cfg.default_days = int(review.get("default_days", cfg.default_days))
    cfg.exclude_screenshots = bool(review.get("exclude_screenshots", cfg.exclude_screenshots))
    cfg.min_pixels = int(review.get("min_pixels", cfg.min_pixels))
    cfg.burst_max_distance = int(review.get("burst_max_distance", cfg.burst_max_distance))
    cfg.burst_max_gap_seconds = int(
        review.get("burst_max_gap_seconds", cfg.burst_max_gap_seconds))
    cfg.resurface_settled = bool(review.get("resurface_settled", cfg.resurface_settled))
    cfg.resurface_seen = bool(review.get("resurface_seen", cfg.resurface_seen))
    cfg.allow_unfiltered = bool(review.get("allow_unfiltered", cfg.allow_unfiltered))
    cfg.history_max_distance = int(review.get("history_max_distance", cfg.history_max_distance))
    # Only enforce the ordering when the user actually chose a value. Raising
    # history_max_distance alone used to make load_config raise, because the published
    # default of 12 was then below it, which rejects a perfectly reasonable config.
    if "published_max_distance" in review:
        cfg.published_max_distance = int(review["published_max_distance"])
    else:
        cfg.published_max_distance = max(cfg.published_max_distance,
                                         cfg.history_max_distance)

    privacy = raw.get("privacy", {})
    cfg.exclude_albums = list(privacy.get("exclude_albums", []))
    cfg.include_albums = list(privacy.get("include_albums", []))
    cfg.require_location_for_zones = bool(
        privacy.get("require_location_for_zones", cfg.require_location_for_zones))
    for z in privacy.get("exclude_zones", []) or []:
        lat, lon = float(z["latitude"]), float(z["longitude"])
        # isfinite before the range test: NaN fails every comparison, so a NaN
        # would slip past `-90 <= lat <= 90` being False only by luck, and a NaN
        # radius passes `radius <= 0` outright and then silently disables the
        # zone, because `distance <= nan` is also False. A privacy filter that
        # quietly matches nothing is worse than no filter at all.
        if not (math.isfinite(lat) and math.isfinite(lon)):
            raise ValueError(f"privacy.exclude_zones: coordinates must be finite: {lat},{lon}")
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError(f"privacy.exclude_zones: coordinates out of range: {lat},{lon}")
        radius = float(z.get("radius_m", 250.0))
        if not math.isfinite(radius) or radius <= 0:
            raise ValueError(f"privacy.exclude_zones: radius_m must be finite and positive, "
                             f"got {radius}")
        cfg.exclude_zones.append(Zone(name=str(z.get("name", "zone")), latitude=lat,
                                      longitude=lon, radius_m=radius))

    export = raw.get("export", {})
    cfg.thumbnail_edge = int(export.get("thumbnail_edge", cfg.thumbnail_edge))
    if "min_free_gb" in export:
        cfg.min_free_bytes = int(float(export["min_free_gb"]) * 1_000_000_000)
    if "assumed_mb_per_asset" in export:
        cfg.assumed_bytes_per_asset = int(float(export["assumed_mb_per_asset"]) * 1_000_000)
    if "assumed_mb_per_video" in export:
        cfg.assumed_bytes_per_video = int(float(export["assumed_mb_per_video"]) * 1_000_000)

    if cfg.default_days <= 0:
        raise ValueError("review.default_days must be positive")
    if cfg.thumbnail_edge <= 0:
        raise ValueError("export.thumbnail_edge must be positive")
    if cfg.min_free_bytes < 0:
        raise ValueError("export.min_free_gb must not be negative")
    if cfg.assumed_bytes_per_asset <= 0:
        raise ValueError("export.assumed_mb_per_asset must be positive")
    if cfg.assumed_bytes_per_video <= 0:
        raise ValueError("export.assumed_mb_per_video must be positive")
    if cfg.history_max_distance < 0:
        raise ValueError("review.history_max_distance must not be negative")
    if cfg.published_max_distance < cfg.history_max_distance:
        raise ValueError("review.published_max_distance must be >= history_max_distance:"
                         " something already published cannot be screened more loosely"
                         " than something merely seen. Either raise it or leave it unset,"
                         " in which case it follows history_max_distance automatically.")

    voice = raw.get("voice", {})
    cfg.voice_guides = list(voice.get("guides", []))
    cfg.notes = raw.get("notes", {})
    return cfg


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Used for geofence tests."""
    from math import asin, cos, radians, sin, sqrt
    r = 6371000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))
