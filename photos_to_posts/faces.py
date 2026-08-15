"""Face detection via Apple's Vision framework, used only to place a crop.

Optional. Everything here degrades to "no faces found", and callers fall back to a
fixed-fraction crop, so the package works unchanged without PyObjC installed.

Why Vision rather than a bundled model or a cloud call: it ships with macOS, runs
entirely on device, needs no download and no key, and this package is macOS-only
anyway. Nothing leaves the machine.

    pip install "apple-photos-to-social[faces]"
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["FaceBox", "available", "detect_faces", "focus_point"]


@dataclass(frozen=True)
class FaceBox:
    """A detected face in top-left-origin fractions of the image (0.0 to 1.0)."""
    x: float
    y: float
    width: float
    height: float
    confidence: float

    @property
    def centre_y(self) -> float:
        return self.y + self.height / 2

    @property
    def centre_x(self) -> float:
        return self.x + self.width / 2


def available() -> bool:
    """True if the Vision bridge can be imported on this machine."""
    try:
        import Quartz  # noqa: F401
        import Vision  # noqa: F401
        return True
    except ImportError:
        return False


def detect_faces(path: str | Path, *, min_confidence: float = 0.5) -> list[FaceBox]:
    """Detect faces, strongest first. Returns [] if Vision is unavailable or errors.

    Never raises: a crop helper must not become a hard dependency on face detection
    succeeding, and a photo of a fish has no faces in it either way.
    """
    try:
        import Quartz
        import Vision
        from Foundation import NSURL
    except ImportError:
        return []

    try:
        url = NSURL.fileURLWithPath_(str(Path(path).resolve()))
        source = Quartz.CGImageSourceCreateWithURL(url, None)
        if source is None:
            return []
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        if image is None:
            return []
        request = Vision.VNDetectFaceRectanglesRequest.alloc().init()
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
        handler.performRequests_error_([request], None)
        results = request.results() or []
    except Exception:
        # PyObjC surfaces a wide range of ObjC failures; none of them should stop a
        # crop from happening.
        return []

    boxes: list[FaceBox] = []
    for obs in results:
        conf = float(obs.confidence())
        if conf < min_confidence:
            continue
        bb = obs.boundingBox()
        # Vision uses a bottom-left origin; images are addressed top-left.
        boxes.append(FaceBox(
            x=float(bb.origin.x),
            y=1.0 - float(bb.origin.y) - float(bb.size.height),
            width=float(bb.size.width),
            height=float(bb.size.height),
            confidence=conf,
        ))
    return sorted(boxes, key=lambda b: b.confidence, reverse=True)


def focus_point(path: str | Path) -> float | None:
    """Vertical point (0.0 top, 1.0 bottom) a crop should be composed around.

    With several faces this is the midpoint of the group rather than the most
    confident single face, so a team photo does not get composed around whichever
    person the detector liked best.
    """
    boxes = detect_faces(path)
    if not boxes:
        return None
    if len(boxes) == 1:
        return boxes[0].centre_y
    top = min(b.y for b in boxes)
    bottom = max(b.y + b.height for b in boxes)
    return (top + bottom) / 2
