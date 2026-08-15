"""Per-image diagnosis, and conservative repair of only the unambiguous faults.

## Why this module is careful

The naive version of this feature auto-corrects everything it can measure. That is
wrong, and there is a specific photograph that proves it: a dawn shot of a jetty with
14.5% of its pixels in crushed shadow. By every metric it is underexposed. It is also
a deliberate low-key silhouette, and "fixing" it brightens away the only reason it was
worth posting.

**A measurement cannot distinguish a mistake from an intention.** Underexposure is a
defect in a conference photo and the entire point of a silhouette. So faults are split
into two classes:

- **Unambiguous**, where no plausible artistic reading exists: blown highlights, a
  face too dark to read, a strong colour cast on otherwise neutral surfaces. These are
  auto-repaired.
- **Ambiguous**, where the "fault" may be the intent: overall underexposure, crushed
  shadows, softness. These are reported and left alone for a human to judge.

## Why not Photoshop

Every repair here is arithmetic on an array. Driving a GUI application that must be
open, through a scripting bridge, to do arithmetic, buys nothing. Photoshop earns its
place on operations that genuinely are not reimplementable, such as frequency
separation and dodge-and-burn on a hero image, done deliberately by a person.

Requires numpy. Without it, diagnosis and repair are skipped and the pipeline runs
exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageFilter

__all__ = ["Diagnosis", "available", "diagnose", "repair", "AUTO_FIXABLE", "NEEDS_JUDGEMENT"]

# Faults with no plausible artistic reading. Repaired automatically.
AUTO_FIXABLE = ("blown_highlights", "face_underexposed", "colour_cast")

# Faults that are frequently the photographer's intent. Reported, never touched.
NEEDS_JUDGEMENT = ("underexposed", "overexposed", "crushed_shadows", "soft", "flat")


def available() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except ImportError:
        return False


@dataclass
class Diagnosis:
    """What is measurably true about one image, and what should be done about it."""
    path: Path
    width: int = 0
    height: int = 0
    mean: float = 0.0
    dynamic_range: float = 0.0
    shadow_clip: float = 0.0
    highlight_clip: float = 0.0
    cast: float = 0.0
    cast_direction: str = "none"
    sharpness: float = 0.0
    face_mean: float | None = None
    faults: list[str] = field(default_factory=list)

    @property
    def auto_fixable(self) -> list[str]:
        return [f for f in self.faults if f in AUTO_FIXABLE]

    @property
    def needs_judgement(self) -> list[str]:
        return [f for f in self.faults if f in NEEDS_JUDGEMENT]

    @property
    def unusable(self) -> bool:
        """Motion blur this severe cannot be sharpened. Discard rather than edit."""
        return "unfixable_blur" in self.faults

    def summary(self) -> str:
        if not self.faults:
            return "ok"
        parts = []
        if self.auto_fixable:
            parts.append("auto: " + ", ".join(self.auto_fixable))
        if self.needs_judgement:
            parts.append("your call: " + ", ".join(self.needs_judgement))
        if self.unusable:
            parts.append("UNUSABLE, motion blurred")
        return " | ".join(parts)


def _luma(arr):
    return arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114


def _neutral_mask(flat, np):
    """Pixels close enough to grey to say anything about white balance.

    Plain grey-world is not usable here. Measured against a real camera roll it called
    an orange takeaway box a 149% red cast, along with a neon sign and a drinks menu.
    Those are coloured subjects, not casts. Restricting to near-neutral mid-tones means
    a tungsten-lit room still registers while a red object does not.
    """
    mx, mn = flat.max(axis=1), flat.min(axis=1)
    mid = (mx + mn) / 2
    denom = np.where(mx + mn > 255, 510 - mx - mn, mx + mn)
    # np.where evaluates BOTH branches, so a plain division here warns on the
    # zero-denominator pixels even though the result is discarded. Divide only
    # where it is safe.
    sat = np.divide(mx - mn, denom, out=np.zeros_like(denom), where=denom > 1e-6)
    return (sat < 0.25) & (mid > 40) & (mid < 220)


def diagnose(path: str | Path, face_box: tuple[float, float, float, float] | None = None,
             ) -> Diagnosis | None:
    """Measure one image. Returns None if numpy is unavailable or the file is unreadable."""
    try:
        import numpy as np
    except ImportError:
        return None

    path = Path(path)
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            arr = np.asarray(im, dtype=np.float32)
    except OSError:
        return None

    lum = _luma(arr)
    flat_lum = lum.ravel()
    hist, _ = np.histogram(flat_lum, bins=256, range=(0, 255))
    total = flat_lum.size

    d = Diagnosis(path=path, width=w, height=h)
    d.mean = float(flat_lum.mean())
    d.dynamic_range = float(np.percentile(flat_lum, 99) - np.percentile(flat_lum, 1))
    d.shadow_clip = float(hist[:4].sum() / total * 100)
    d.highlight_clip = float(hist[252:].sum() / total * 100)

    # Laplacian variance on full resolution. Downscaling destroys the signal.
    k = lum.astype(np.float32)
    lap = -4 * k[1:-1, 1:-1] + k[:-2, 1:-1] + k[2:, 1:-1] + k[1:-1, :-2] + k[1:-1, 2:]
    d.sharpness = float(lap.var())

    flat = arr.reshape(-1, 3)
    sel = _neutral_mask(flat, np)
    if sel.sum() >= flat.shape[0] * 0.02:
        means = flat[sel].mean(axis=0)
        d.cast = float((means.max() - means.min()) / max(means.mean(), 1e-6) * 100)
        d.cast_direction = ["red", "green", "blue"][int(means.argmax())]

    if face_box:
        fx, fy, fw, fh = face_box
        x0, x1 = max(0, int(fx * w)), min(w, int((fx + fw) * w))
        y0, y1 = max(0, int(fy * h)), min(h, int((fy + fh) * h))
        if x1 > x0 and y1 > y0:
            d.face_mean = float(lum[y0:y1, x0:x1].mean())

    # --- classify -----------------------------------------------------------
    if d.highlight_clip > 2.0:
        d.faults.append("blown_highlights")
    if d.cast > 25:
        d.faults.append("colour_cast")
    if d.face_mean is not None and d.face_mean < 75:
        d.faults.append("face_underexposed")

    if d.mean < 70:
        d.faults.append("underexposed")
    elif d.mean > 185:
        d.faults.append("overexposed")
    if d.shadow_clip > 2.0:
        d.faults.append("crushed_shadows")
    if d.dynamic_range < 150:
        d.faults.append("flat")
    if d.sharpness < 20:
        d.faults.append("unfixable_blur")
    elif d.sharpness < 60:
        d.faults.append("soft")
    return d


def repair(diagnosis: Diagnosis, dst: str | Path, *, sharpen_soft: bool = False) -> Path | None:
    """Repair only the unambiguous faults. Returns None if there was nothing to do.

    ``sharpen_soft`` is off by default: softness is frequently shallow depth of field
    rather than a mistake, and an unsharp mask on intentional bokeh looks worse than
    leaving it alone.
    """
    try:
        import numpy as np
    except ImportError:
        return None

    todo = diagnosis.auto_fixable
    if sharpen_soft and "soft" in diagnosis.faults:
        todo = todo + ["soft"]
    if not todo:
        return None

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(diagnosis.path) as im:
        arr = np.asarray(im.convert("RGB"), dtype=np.float32)

    if "blown_highlights" in todo:
        x = arr / 255.0
        m = np.clip((x.mean(axis=2, keepdims=True) - 0.75) * 4, 0, 1)
        arr = np.clip(x * (1 - m * 0.25) * 255, 0, 255)

    if "face_underexposed" in todo:
        # Lift shadows, which is where an underexposed face lives, without flattening
        # the midtones the rest of the frame depends on.
        x = arr / 255.0
        m = np.clip(1.0 - x.mean(axis=2, keepdims=True) * 2.2, 0, 1)
        arr = np.clip((x * (1 - m) + (x ** 0.65) * m) * 255, 0, 255)

    if "colour_cast" in todo:
        flat = arr.reshape(-1, 3)
        sel = _neutral_mask(flat, np)
        if sel.sum() >= flat.shape[0] * 0.02:
            means = flat[sel].mean(axis=0)
            gain = means.mean() / np.clip(means, 1e-6, None)
            # Partial correction only. A fully neutralised photo of a warm room looks
            # wrong; the warmth is part of the scene.
            gain = 1 + (gain - 1) * 0.7
            arr = np.clip(arr * gain, 0, 255)

    out = Image.fromarray(arr.astype("uint8"))
    if "soft" in todo:
        out = out.filter(ImageFilter.UnsharpMask(radius=1.4, percent=85, threshold=3))
    out.save(dst, quality=95)
    return dst
