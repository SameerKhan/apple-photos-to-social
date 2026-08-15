"""Perceptual hashing, burst clustering, and labelled contact sheets.

A camera roll is mostly repetition. Twelve frames of the same pose are one
decision, not twelve, so everything here exists to collapse a review set down to
the number of *moments* it actually contains before a human (or a model) looks at
it.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
)


def load_font(size: int):
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def dhash(path: str | Path, size: int = 8) -> int:
    """64-bit difference hash.

    Compares each pixel with its right-hand neighbour, so the result tracks
    structure rather than absolute brightness. Two frames of the same shot land
    within a few bits of each other; unrelated photos are typically 25+ apart.
    """
    with Image.open(path) as im:
        small = im.convert("L").resize((size + 1, size), Image.LANCZOS)
        # tobytes() on an 8-bit greyscale image is row-major one byte per pixel,
        # which is exactly the layout wanted here, and avoids the deprecated
        # getdata() path.
        px = small.tobytes()
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if px[base + col] < px[base + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def video_frame(path: str | Path, out: str | Path, *, at_seconds: float = 1.0) -> bool:
    """Grab one frame from a video so it can be hashed and eyeballed like a photo.

    Returns False if ffmpeg is missing or the grab fails; callers should treat
    videos as un-reviewable rather than crashing.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-ss", str(at_seconds), "-i", str(path),
             "-frames:v", "1", "-q:v", "3", str(out)],
            capture_output=True, timeout=120)
        return proc.returncode == 0 and Path(out).exists()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@dataclass
class Frame:
    """One reviewable image on disk, with the metadata needed to label it."""
    uuid: str
    path: Path
    filename: str
    captured_at: datetime
    phash: int | None = None


def cluster_bursts(frames: Sequence[Frame], *, max_distance: int = 12,
                   max_gap_seconds: int = 180) -> list[list[Frame]]:
    """Group frames that are the same moment.

    Both conditions must hold: visually close *and* close in time. Visual
    similarity alone would merge two different visits to the same room; the time
    window keeps a cluster to one actual moment.

    Frames are returned newest-first within and across clusters, and the first
    frame of each cluster is its representative.
    """
    ordered = sorted(frames, key=lambda f: f.captured_at, reverse=True)
    clusters: list[list[Frame]] = []
    for frame in ordered:
        if frame.phash is None:
            clusters.append([frame])
            continue
        for cluster in clusters:
            head = cluster[0]
            if head.phash is None:
                continue
            gap = abs((head.captured_at - frame.captured_at).total_seconds())
            if hamming(head.phash, frame.phash) <= max_distance and gap <= max_gap_seconds:
                cluster.append(frame)
                break
        else:
            clusters.append([frame])
    return clusters


def contact_sheets(frames: Sequence[Frame], out_dir: str | Path, *,
                   columns: int = 5, rows: int = 5, cell: int = 400,
                   label_height: int = 34, counts: dict[str, int] | None = None,
                   prefix: str = "sheet") -> list[Path]:
    """Tile frames into numbered, labelled JPEG sheets.

    Each tile carries a global index, the capture time, and a burst count when the
    frame stands in for more than one shot, so a reviewer can refer to "#37"
    unambiguously and know it represents four near-identical frames.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    per_sheet = columns * rows
    f_index, f_label = load_font(30), load_font(19)
    written: list[Path] = []

    for sheet_no in range((len(frames) + per_sheet - 1) // per_sheet):
        chunk = frames[sheet_no * per_sheet:(sheet_no + 1) * per_sheet]
        sheet = Image.new("RGB", (columns * cell, rows * (cell + label_height)), (18, 18, 20))
        draw = ImageDraw.Draw(sheet)
        for i, frame in enumerate(chunk):
            global_i = sheet_no * per_sheet + i + 1
            cx, cy = (i % columns) * cell, (i // columns) * (cell + label_height)
            try:
                with Image.open(frame.path) as im:
                    im = im.convert("RGB")
                    im.thumbnail((cell - 8, cell - 8), Image.LANCZOS)
                    sheet.paste(im, (cx + (cell - im.width) // 2, cy + (cell - im.height) // 2))
            except OSError:
                draw.text((cx + 20, cy + cell // 2), "(unreadable)", font=f_label,
                          fill=(200, 80, 80))
            label = f"#{global_i}  {frame.captured_at:%m-%d %H:%M}"
            n = (counts or {}).get(frame.uuid, 1)
            if n > 1:
                label += f"  x{n}"
            draw.rectangle([cx, cy + cell, cx + cell, cy + cell + label_height], fill=(0, 0, 0))
            draw.text((cx + 6, cy + cell + 7), label, font=f_label, fill=(255, 210, 90))
            draw.text((cx + 9, cy + 7), str(global_i), font=f_index, fill=(255, 255, 0),
                      stroke_width=3, stroke_fill=(0, 0, 0))
        path = out_dir / f"{prefix}_{sheet_no + 1:02d}.jpg"
        sheet.save(path, quality=82, optimize=True)
        written.append(path)
    return written


# Aspect-ratio windows each platform will accept for a feed image, as width/height.
# Instagram is by far the tightest, and a phone photo straight out of the camera roll
# is usually too tall for it: a 3:2 portrait is 0.667 against a 0.75 floor. Catching
# that here is much cheaper than having a scheduler reject the post later.
PLATFORM_RATIOS = {
    "instagram": (0.75, 1.91),   # 4:5 to 1.91:1
    "facebook": (0.30, 3.00),
    "x": (0.33, 3.00),
    "linkedin": (0.33, 3.00),
}


def platform_fit(width: int, height: int) -> dict[str, bool]:
    """Which platforms will accept this image as a feed post, by aspect ratio alone."""
    if not width or not height:
        return {k: False for k in PLATFORM_RATIOS}
    r = width / height
    return {name: lo <= r <= hi for name, (lo, hi) in PLATFORM_RATIOS.items()}


# Where a face should sit vertically in a portrait crop. Roughly the upper third,
# which is where portrait framing conventionally puts the eyes.
EYE_LINE = 0.36


def crop_to_ratio(src: str | Path, dst: str | Path, ratio: float = 0.8,
                  top_share: float = 0.30, use_faces: bool = True) -> Path:
    """Crop to a target width/height ratio.

    When a face is found (and PyObjC is installed) the crop is composed so the face
    lands on :data:`EYE_LINE`. Otherwise it falls back to ``top_share``, trimming
    mostly from the bottom, because a centre crop on a standing portrait is as
    likely to take the top of someone's head as it is to take the floor.

    Measured on a real five-frame studio set: face placement moved the crop by up to
    186px versus the fixed fraction, and the difference was largest on seated shots,
    where a fixed fraction left dead ceiling and cut the subject's hands.

    0.8 (4:5) is Instagram's portrait sweet spot and the tallest ratio it accepts.
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        if w / h >= ratio:
            im.save(dst, quality=94)   # already wide enough
            return dst
        new_h = int(round(w / ratio))

        top = None
        if use_faces:
            from . import faces
            focus = faces.focus_point(src)
            if focus is not None:
                top = int(round(focus * h - EYE_LINE * new_h))
        if top is None:
            top = int(round((h - new_h) * top_share))
        top = max(0, min(top, h - new_h))

        im.crop((0, top, w, top + new_h)).save(dst, quality=94)
    return dst


def downscale(src: str | Path, dst: str | Path, max_edge: int = 900) -> Path:
    """Write a smaller copy, preserving aspect. Used to keep sheets cheap to build."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)
        im.save(dst, quality=85, optimize=True)
    return dst


def hash_directory(paths: Sequence[Path], progress: Callable[[int, int], None] | None = None
                   ) -> dict[Path, int]:
    out: dict[Path, int] = {}
    for i, p in enumerate(paths, 1):
        try:
            out[p] = dhash(p)
        except OSError:
            continue
        if progress:
            progress(i, len(paths))
    return out
