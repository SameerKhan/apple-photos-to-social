"""Thin bridge to the macOS Photos app over Apple Events.

Why Apple Events and not the library file directly: reading
``~/Pictures/Photos Library.photoslibrary`` requires Full Disk Access, which is a
permission the user has to grant by hand in System Settings. Talking to the
Photos *app* is a separate permission (Automation) that is commonly already
granted, so this path works on far more machines with zero setup.

Everything here is read-only except :func:`export_assets`, which asks Photos to
write copies out to a directory you choose. Nothing mutates the library.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

# Photos joins AppleScript lists with ", " when osascript prints them, which is
# ambiguous for any value that can itself contain a comma (filenames, dates).
# For those we ask AppleScript to join with this sentinel instead.
DELIM = "|@|"

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}

# AppleScript renders dates in the machine's locale. The two orders below cover
# day-first ("Wednesday, 4 February 2015 at 1:13:09 AM") and month-first
# ("Wednesday, February 4, 2015 at 1:13:09 AM"). Which one you get depends on
# system region, so we detect per-payload rather than assuming.
_DAY_FIRST = re.compile(
    r"date\s+\w+,\s+(\d{1,2})\s+(\w+)\s+(\d{4})\s+at\s+(\d{1,2}):(\d{2}):(\d{2})\s*([AP]M)?", re.I)
_MONTH_FIRST = re.compile(
    r"date\s+\w+,\s+(\w+)\s+(\d{1,2}),\s+(\d{4})\s+at\s+(\d{1,2}):(\d{2}):(\d{2})\s*([AP]M)?", re.I)


class PhotosError(RuntimeError):
    """Raised when osascript fails or Photos refuses the request."""


def run(script: str, timeout: int = 900) -> str:
    """Execute an AppleScript snippet and return its stdout.

    Raises :class:`PhotosError` with the AppleScript error text on failure, which
    is far more useful than a bare CalledProcessError.
    """
    proc = subprocess.run(
        ["osascript", "-"], input=script, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise PhotosError(proc.stderr.strip() or f"osascript exited {proc.returncode}")
    return proc.stdout.rstrip("\n")


def quote(value: str) -> str:
    """Escape a Python string for embedding inside an AppleScript string literal.

    Backslash must be escaped before the quote, or the escape we just inserted
    gets re-escaped. Control characters are stripped rather than encoded: no
    legitimate album name or export path contains them, and passing them through
    would let a crafted value terminate the literal and inject script.
    """
    cleaned = "".join(ch for ch in value if ch >= " " or ch == "\t")
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


# \Z rather than $: `$` also matches before a trailing newline, so "abc\n"
# would pass and then break out of the generated AppleScript string literal.
UUID_SAFE = re.compile(r"\A[A-Za-z0-9/_.-]+\Z")


def validate_uuid(value: str) -> str:
    """Reject anything that is not a plausible Photos identifier.

    Photos ids look like ``A61761AF-9FE0-4733-8562-C8B8040E8580/L0/001``. Ids
    reach us from our own bulk fetch, but they are still interpolated into script
    source and used to build directory names, so they are validated not trusted.

    The dot cases matter specifically: ``.`` and ``..`` satisfy the character
    class, and would produce an export directory that resolves to the export root
    or its parent, whose contents are then deleted before the export runs.
    """
    if not value or not UUID_SAFE.match(value):
        raise PhotosError(f"refusing to embed suspicious asset id: {value!r}")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise PhotosError(f"refusing to embed path-traversing asset id: {value!r}")
    return value


def is_available() -> tuple[bool, str]:
    """Whether Photos is reachable, plus the reason when it is not.

    Returns the reason rather than swallowing it so a CLI can tell the difference
    between "Automation permission denied" and "osascript is missing", which need
    different fixes from the user.
    """
    try:
        run('tell application "Photos" to return count of media items', timeout=120)
        return True, "ok"
    except FileNotFoundError:
        return False, "osascript not found; this tool requires macOS"
    except subprocess.TimeoutExpired:
        return False, ("Photos did not respond in time. If a permission dialog is open on "
                       "screen, accept it and retry.")
    except PhotosError as exc:
        return False, str(exc)


def count_media_items() -> int:
    return int(run('tell application "Photos" to return count of media items'))


def _parse_dates(text: str) -> list[datetime]:
    day_first = len(_DAY_FIRST.findall(text)) >= len(_MONTH_FIRST.findall(text))
    rx = _DAY_FIRST if day_first else _MONTH_FIRST
    out: list[datetime] = []
    for m in rx.finditer(text):
        a, b, year, hh, mm, ss, ampm = m.groups()
        day, mon = (a, b) if day_first else (b, a)
        hour = int(hh)
        if ampm:
            up = ampm.upper()
            if up == "PM" and hour != 12:
                hour += 12
            elif up == "AM" and hour == 12:
                hour = 0
        out.append(datetime(int(year), MONTHS[mon.lower()], int(day), hour, int(mm), int(ss)))
    return out


def _fetch_plain(prop: str) -> list[str]:
    """Fetch a property whose values can never contain a comma (ids, numbers, booleans)."""
    raw = run(f'tell application "Photos" to get {prop} of every media item')
    return [p.strip() for p in raw.split(",") if p.strip()]


def _fetch_delimited(prop: str) -> list[str]:
    """Fetch a property whose values may contain commas, joined with DELIM instead."""
    script = f'''tell application "Photos"
\tset tid to AppleScript's text item delimiters
\tset AppleScript's text item delimiters to "{DELIM}"
\tset out to ({prop} of every media item) as text
\tset AppleScript's text item delimiters to tid
\treturn out
end tell'''
    raw = run(script)
    # An empty library yields empty output, which would otherwise split into a
    # single empty string and desynchronise every list length check.
    return [p.strip() for p in raw.split(DELIM)] if raw.strip() else []


VIDEO_EXT = {".MOV", ".MP4", ".M4V", ".AVI"}


@dataclass(frozen=True)
class Asset:
    uuid: str
    filename: str
    captured_at: datetime
    favorite: bool
    width: int
    height: int

    @property
    def kind(self) -> str:
        dot = self.filename.rfind(".")
        ext = self.filename[dot:].upper() if dot >= 0 else ""
        return "video" if ext in VIDEO_EXT else "photo"

    @property
    def orientation(self) -> str:
        if self.height > self.width:
            return "vertical"
        return "square" if self.height == self.width else "horizontal"

    @property
    def is_screenshot(self) -> bool:
        # Screenshots land as PNG from iOS; a real camera photo never does.
        return self.filename.upper().endswith(".PNG")


def fetch_all_assets() -> list[Asset]:
    """Pull every asset's core metadata in a handful of bulk Apple Events.

    This is deliberately all-or-nothing rather than paged: Photos answers a
    whole-library property fetch in seconds, whereas per-item lookups take
    minutes. Filtering happens locally afterwards.

    A ``whose`` clause filtered on ``date`` raises AppleScript error -1700 in
    Photos, which is why there is no server-side date filter here.
    """
    dates = _parse_dates(run('tell application "Photos" to get date of every media item'))
    uuids = _fetch_plain("id")
    names = _fetch_delimited("filename")
    favs = _fetch_plain("favorite")
    heights = _fetch_plain("height")
    widths = _fetch_plain("width")

    lengths = {"date": len(dates), "id": len(uuids), "filename": len(names),
               "favorite": len(favs), "height": len(heights), "width": len(widths)}
    if len(set(lengths.values())) != 1:
        raise PhotosError(
            "Property lists came back at different lengths, so row alignment cannot be "
            f"trusted: {lengths}. Re-run; if it persists, please file an issue."
        )

    out: list[Asset] = []
    for i in range(len(dates)):
        try:
            w, h = int(widths[i]), int(heights[i])
        except ValueError:
            w = h = 0
        out.append(Asset(
            uuid=uuids[i],
            filename=names[i],
            captured_at=dates[i],
            favorite=favs[i].strip().lower() == "true",
            width=w,
            height=h,
        ))
    return out


@dataclass
class AlbumLookup:
    """Result of resolving configured album names to asset ids.

    ``missing`` matters for privacy: an album named in config that does not exist
    is almost always a typo, and silently treating it as "excludes nothing" turns
    a privacy filter into a no-op. Callers are expected to refuse to run.
    """
    ids: set[str]
    missing: list[str]


def album_asset_ids(album_names: Sequence[str]) -> AlbumLookup:
    """Ids of every asset in the named albums, plus any album that did not resolve."""
    found: set[str] = set()
    missing: list[str] = []
    for name in album_names:
        script = f'''tell application "Photos"
\ttry
\t\tset theAlbum to album "{quote(name)}"
\ton error
\t\treturn "__NO_SUCH_ALBUM__"
\tend try
\tset out to (id of media items of theAlbum)
\tset tid to AppleScript's text item delimiters
\tset AppleScript's text item delimiters to "{DELIM}"
\tset s to out as text
\tset AppleScript's text item delimiters to tid
\treturn s
end tell'''
        raw = run(script)
        if raw.strip() == "__NO_SUCH_ALBUM__":
            missing.append(name)
            continue
        found.update(p.strip() for p in raw.split(DELIM) if p.strip())
    return AlbumLookup(ids=found, missing=missing)


NO_GPS = "none"
LOOKUP_FAILED = "failed"


@dataclass
class LocationLookup:
    """Locations, plus an explicit record of which lookups could not be answered.

    "No GPS on this asset" and "the lookup errored" are different facts and must
    not collapse into the same empty result. A geofence cannot protect an asset
    whose location is unknown, so the caller has to decide the policy, and it can
    only do that if it is told which case occurred.
    """
    found: dict[str, tuple[float, float]]
    no_gps: set[str]
    failed: set[str]


def fetch_locations(uuids: Sequence[str], *, batch_size: int = 150) -> LocationLookup:
    """Lat/lon for the given assets, batched.

    Looked up per-asset rather than as one bulk property fetch because a bulk
    fetch flattens each two-element location list into the same stream as its
    neighbours, leaving no way to re-associate a pair with its asset. Batching
    keeps any single generated script small enough for AppleScript to compile.
    """
    result = LocationLookup(found={}, no_gps=set(), failed=set())
    ids = [validate_uuid(u) for u in uuids]
    for start in range(0, len(ids), batch_size):
        chunk = ids[start:start + batch_size]
        lines = "".join(f'\tset end of rows to (getLoc("{u}"))\n' for u in chunk)
        script = f'''on getLoc(theId)
\ttell application "Photos"
\t\ttry
\t\t\tset m to media item id theId
\t\t\tset loc to location of m
\t\t\tif loc is missing value then return theId & "{DELIM}{NO_GPS}{DELIM}"
\t\t\treturn theId & "{DELIM}" & (item 1 of loc as text) & "{DELIM}" & (item 2 of loc as text)
\t\ton error
\t\t\treturn theId & "{DELIM}{LOOKUP_FAILED}{DELIM}"
\t\tend try
\tend tell
end getLoc

set rows to {{}}
{lines}set AppleScript's text item delimiters to linefeed
return rows as text'''
        try:
            raw = run(script)
        except (PhotosError, subprocess.TimeoutExpired):
            # A whole failed batch is unknown-location, never silently allowed.
            result.failed.update(chunk)
            continue
        seen: set[str] = set()
        for line in raw.splitlines():
            parts = line.split(DELIM)
            if len(parts) != 3:
                continue
            uid, a, b = parts
            seen.add(uid)
            if a == NO_GPS:
                result.no_gps.add(uid)
            elif a == LOOKUP_FAILED:
                result.failed.add(uid)
            else:
                try:
                    result.found[uid] = (float(a), float(b))
                except ValueError:
                    result.failed.add(uid)
        result.failed.update(set(chunk) - seen)
    return result


def uuid_to_dirname(uuid: str) -> str:
    """Filesystem-safe, reversible directory name for an asset id.

    Ids contain slashes. A plain slash-to-underscore swap is not injective: the
    ids ``A/B`` and ``A_B`` would land in the same directory and clobber each
    other, so percent-encoding is used instead. The percent itself is escaped
    first or the encoding is ambiguous.
    """
    return uuid.replace("%", "%25").replace("/", "%2F")


# Extensions Pillow can open. Used to pick the right file when Photos writes more
# than one, which it does for Live Photos (a still plus a .mov) and RAW+JPEG pairs.
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".gif", ".bmp"}


def _pick_export(files: Sequence[Path], want_video: bool) -> Path | None:
    """Choose the file that matches the asset kind.

    Directory iteration order is arbitrary, so without this a Live Photo can hand
    back its ``.mov`` sidecar for a still asset. Pillow then fails to open it and
    the asset silently disappears from the review.
    """
    if not files:
        return None
    images = [f for f in files if f.suffix.lower() in IMAGE_EXT]
    videos = [f for f in files if f.suffix.lower() in {".mov", ".mp4", ".m4v"}]
    # No cross-kind fallback: handing a .mov back for a still just moves the
    # failure downstream into Pillow, where it looks like a successful export
    # that mysteriously vanishes. Returning None reports it as a real failure.
    group = videos if want_video else images
    if not group:
        return None
    # Largest wins within a group: for RAW+JPEG the bigger file is the
    # full-quality one, and for stills it is never the sidecar.
    return max(group, key=lambda p: p.stat().st_size)


def export_assets(uuids: Iterable[str], dest_root: str | Path, *, originals: bool = False,
                  video_uuids: set[str] | None = None, progress=None) -> dict[str, Path]:
    """Export each asset into its own subdirectory of ``dest_root``.

    Returns a uuid -> exported file mapping.

    One directory per asset, rather than one shared directory, because Photos
    renames on export: a ``.HEIC`` source becomes ``.jpeg``, and a name that
    already exists gains a numeric suffix like ``IMG_0001 2.jpeg``. Matching an
    asset to its exported file by filename in a shared directory is therefore
    ambiguous whenever two assets share a stem, and can silently return a stale
    file from an earlier run. Isolating each export removes the ambiguity: the
    directory has exactly one file, so no matching is required at all.

    ``originals=False`` (the default) exports Photos' rendered derivative, which
    is already a JPEG and does not force a full-resolution download from iCloud.
    Use ``originals=True`` only for the assets you are actually going to publish.
    """
    root = Path(dest_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ids = [validate_uuid(u) for u in uuids]
    using = " with using originals" if originals else ""
    mapping: dict[str, Path] = {}

    for i, uid in enumerate(ids, 1):
        target = root / uuid_to_dirname(uid)
        # mkdir(exist_ok=True) is satisfied by a symlink pointing at a directory
        # elsewhere, and the clearing loop below would then empty that directory.
        if target.is_symlink():
            raise PhotosError(f"refusing to export into a symlinked path: {target}")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A pre-existing file would make Photos add a numeric suffix, so the
        # directory is emptied completely first and stays single-occupancy.
        # Symlinks are unlinked rather than followed, so a link planted here can
        # never cause a delete outside the export root.
        for stale in target.iterdir():
            if stale.is_symlink() or stale.is_file():
                stale.unlink()
            elif stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
        script = (f'tell application "Photos"\n'
                  f'\tset theItems to {{media item id "{uid}"}}\n'
                  f'\texport theItems to POSIX file "{quote(str(target))}"{using}\n'
                  f'end tell')
        try:
            run(script)
        except PhotosError:
            # One unexportable asset must not abort the batch. It is simply
            # absent from the mapping, and the caller treats it as unreviewed.
            if progress:
                progress(i, len(ids))
            continue
        files = [p for p in target.iterdir() if p.is_file()]
        chosen = _pick_export(files, want_video=uid in (video_uuids or set()))
        if chosen is not None:
            mapping[uid] = chosen
        if progress:
            progress(i, len(ids))
    return mapping
