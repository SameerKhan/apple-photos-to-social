"""Command line interface.

Commands:

  doctor    check the machine can actually run this
  scan      metadata only: what is in a window, what is new, nothing exported
  review    the full pass: export, dedupe, contact sheets, manifest, ledger
  mark      record a decision against an asset by id\n  mark-sheet same, but by the number printed on the contact sheet
  stats     ledger totals
  coverage  how far back the library has been reviewed\n  report    what was published, where and when\n  diagnose  report what a photo actually needs, and optionally fix it
  purge     delete exported pixels, keeping the ledger

``scan`` exists so the expensive step is always opt-in: you can see the shape of
a window, and how much of it is already known, before anything touches disk.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import load_config
from .ledger import ALL_STATUSES, Ledger


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="path to a config TOML (default: search standard paths)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="photos2social", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check prerequisites")
    _add_common(doctor)

    scan = sub.add_parser("scan", help="metadata only, exports nothing")
    _add_common(scan)
    scan.add_argument("--days", type=int, help="window size in days")
    scan.add_argument("--since", help="ISO date, overrides --days")
    scan.add_argument("--until", help="ISO date")

    review = sub.add_parser("review", help="full pass with export and contact sheets")
    _add_common(review)
    review.add_argument("--days", type=int)
    review.add_argument("--since")
    review.add_argument("--until")
    review.add_argument("--limit", type=int, help="cap the number of assets exported")
    review.add_argument("--include-videos", action="store_true",
                        help="also review videos (needs ffmpeg; slower)")
    review.add_argument("--dry-run", action="store_true",
                        help="filter only, export nothing, write nothing")
    review.add_argument(
        "--allow-unfiltered", action="store_true",
        help="proceed even though no privacy filters are configured. Every photo in "
             "the window, including family, becomes a candidate.")

    mark = sub.add_parser("mark", help="record a decision against an asset")
    _add_common(mark)
    mark.add_argument("uuid")
    mark.add_argument("status", choices=list(ALL_STATUSES))
    mark.add_argument("--note")
    mark.add_argument("--destination", help="where it was published, for posted assets")

    marksheet = sub.add_parser(
        "mark-sheet", help="mark by contact-sheet number instead of asset id")
    _add_common(marksheet)
    marksheet.add_argument("run", help="a run directory, or its number, or 'latest'")
    marksheet.add_argument("index", type=int, help="the number printed on the tile")
    marksheet.add_argument("status", choices=list(ALL_STATUSES))
    marksheet.add_argument("--note")
    marksheet.add_argument("--destination", help="where it was published")

    report = sub.add_parser("report", help="what was published, where and when")
    _add_common(report)
    report.add_argument("--limit", type=int, default=50)

    stats = sub.add_parser("stats", help="ledger totals")
    _add_common(stats)

    coverage = sub.add_parser("coverage", help="assets reviewed per month")
    _add_common(coverage)

    diag = sub.add_parser("diagnose", help="report what a photo or folder actually needs")
    _add_common(diag)
    diag.add_argument("path", help="an image file, or a directory of them")
    diag.add_argument("--fix", action="store_true",
                      help="also repair the unambiguous faults, writing *_fixed files")

    purge = sub.add_parser("purge", help="delete exported pixels, keep the ledger")
    _add_common(purge)
    purge.add_argument("--yes", action="store_true", help="do not prompt")
    return p


def _iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    print(f"config          : {cfg.source_path or 'defaults (no file found)'}")
    print(f"ledger          : {cfg.ledger_path}")
    print(f"workspace       : {cfg.workspace}")

    # Imported lazily so a missing Pillow does not stop doctor from diagnosing it.
    try:
        from PIL import Image  # noqa: F401
        print("Pillow          : installed")
    except ImportError:
        print("Pillow          : MISSING (pip install Pillow)")

    print(f"ffmpeg          : {'found' if shutil.which('ffmpeg') else 'not found (videos disabled)'}")

    from . import applescript as ps
    ok, why = ps.is_available()
    print(f"Photos app      : {'reachable' if ok else 'UNAVAILABLE - ' + why}")
    if ok:
        try:
            print(f"library size    : {ps.count_media_items():,} items")
        except ps.PhotosError as exc:
            print(f"library size    : error - {exc}")

    usage = shutil.disk_usage(Path.home())
    print(f"free disk       : {usage.free / 1e9:.1f} GB")

    if cfg.privacy_configured:
        print(f"privacy filters : {len(cfg.exclude_albums)} album(s), "
              f"{len(cfg.exclude_zones)} zone(s)")
    else:
        print("privacy filters : NONE CONFIGURED")
        if cfg.allow_unfiltered:
            print("                  allow_unfiltered = true in config, so review WILL run")
            print("                  and every photo in a window becomes a candidate.")
        else:
            print("                  Every photo in a window would become a candidate,")
            print("                  including photos of family. See config.example.toml.")
    return 0 if ok else 1


def cmd_scan(args) -> int:
    from . import applescript as ps
    from .review import select_window

    cfg = load_config(args.config)
    ok, why = ps.is_available()
    if not ok:
        print(f"Cannot reach the Photos app: {why}", file=sys.stderr)
        return 1

    assets = ps.fetch_all_assets()
    window = select_window(assets, days=args.days or cfg.default_days,
                           since=_iso(args.since), until=_iso(args.until))
    # Read-only command: if no ledger exists yet, do not bring one into being
    # just to answer a question.
    if Path(cfg.ledger_path).expanduser().exists():
        with Ledger(cfg.ledger_path) as ledger:
            settled = ledger.settled_uuids(include_seen=not cfg.resurface_seen)
    else:
        settled = set()

    fresh = [a for a in window if a.uuid not in settled]
    photos = sum(1 for a in fresh if a.kind == "photo")
    videos = sum(1 for a in fresh if a.kind == "video")
    shots = sum(1 for a in fresh if a.is_screenshot)

    print(f"library      : {len(assets):,}")
    print(f"in window    : {len(window):,}")
    print(f"already known: {len(window) - len(fresh):,}")
    print(f"new to review: {len(fresh):,}  ({photos} photos, {videos} videos, "
          f"{shots} screenshots)")
    if fresh:
        print(f"date range   : {min(a.captured_at for a in fresh):%Y-%m-%d} to "
              f"{max(a.captured_at for a in fresh):%Y-%m-%d}")
    return 0


def cmd_review(args) -> int:
    from .review import PrivacyRefusal, run_review
    from . import applescript as ps

    cfg = load_config(args.config)
    try:
        result = run_review(
            cfg, days=args.days, since=_iso(args.since), until=_iso(args.until),
            include_videos=args.include_videos, limit=args.limit,
            allow_unfiltered=args.allow_unfiltered, dry_run=args.dry_run,
            log=lambda m: print(m, flush=True))
    except PrivacyRefusal as exc:
        print(f"\nStopped for privacy: {exc}", file=sys.stderr)
        return 2
    except (ps.PhotosError, RuntimeError) as exc:
        print(f"\nStopped: {exc}", file=sys.stderr)
        return 1

    print("\n" + result.summary())
    if args.dry_run:
        print("dry run, nothing exported")
        return 0
    for sheet in result.sheets:
        print(f"sheet     : {sheet}")
    if result.manifest_path:
        print(f"manifest  : {result.manifest_path}")
    return 0


def cmd_mark(args) -> int:
    cfg = load_config(args.config)
    with Ledger(cfg.ledger_path) as ledger:
        if ledger.set_status(args.uuid, args.status, note=args.note,
                             destination=args.destination):
            print(f"{args.uuid} -> {args.status}")
            return 0
    print(f"unknown asset: {args.uuid}", file=sys.stderr)
    return 1


def _resolve_run(cfg, spec: str) -> Path | None:
    """Accept a path, a bare run number, or 'latest'."""
    p = Path(spec).expanduser()
    if p.is_dir():
        return p
    runs = sorted(cfg.workspace.glob("run_*"))
    if spec == "latest":
        return runs[-1] if runs else None
    if spec.isdigit():
        target = cfg.workspace / f"run_{int(spec):05d}"
        return target if target.is_dir() else None
    return None


def cmd_mark_sheet(args) -> int:
    """Mark an asset using the number printed on the contact sheet.

    This exists because copying a 36-character id out of a CSV is enough friction that
    the marking step simply does not happen, which leaves the ledger empty and the
    whole no-repeat guarantee useless.
    """
    import csv
    cfg = load_config(args.config)
    run_dir = _resolve_run(cfg, args.run)
    if run_dir is None:
        print(f"no such run: {args.run}. Try 'latest' or a run number.", file=sys.stderr)
        return 1
    manifest = run_dir / "manifest.csv"
    if not manifest.exists():
        print(f"no manifest in {run_dir}", file=sys.stderr)
        return 1

    with manifest.open() as fh:
        rows = {int(r["index"]): r for r in csv.DictReader(fh)}
    row = rows.get(args.index)
    if row is None:
        print(f"#{args.index} is not in {manifest.name} "
              f"(it has 1 to {max(rows) if rows else 0})", file=sys.stderr)
        return 1

    with Ledger(cfg.ledger_path) as ledger:
        ok = ledger.set_status(row["uuid"], args.status, note=args.note,
                               destination=args.destination)
    if not ok:
        print(f"#{args.index} ({row['filename']}) is not in the ledger yet",
              file=sys.stderr)
        return 1
    where = f" -> {args.destination}" if args.destination else ""
    print(f"#{args.index}  {row['filename']}  {args.status}{where}")
    return 0


def cmd_report(args) -> int:
    cfg = load_config(args.config)
    with Ledger(cfg.ledger_path) as ledger:
        rows = ledger.publications(limit=args.limit)
        stats = ledger.stats()
    if not rows:
        print("nothing published through the ledger yet.")
        print(f"(the ledger holds {stats.get('total', 0)} assets, "
              f"{stats.get('posted', 0)} marked posted)")
        return 0
    by_dest: dict[str, int] = {}
    print(f"{'when':20s} {'where':14s} file")
    for uuid, dest, when, filename in rows:
        by_dest[dest] = by_dest.get(dest, 0) + 1
        print(f"{when[:19]:20s} {dest:14s} {filename or uuid[:18]}")
    print("\nby destination: " + ", ".join(f"{k} {v}" for k, v in
                                            sorted(by_dest.items(), key=lambda x: -x[1])))
    return 0


def cmd_stats(args) -> int:
    cfg = load_config(args.config)
    with Ledger(cfg.ledger_path) as ledger:
        for key, value in sorted(ledger.stats().items()):
            print(f"{key:20s} {value:,}")
    return 0


def cmd_coverage(args) -> int:
    cfg = load_config(args.config)
    with Ledger(cfg.ledger_path) as ledger:
        rows = ledger.coverage()
    if not rows:
        print("nothing reviewed yet")
        return 0
    widest = max(c for _m, c in rows)
    for month, count in rows:
        bar = "#" * max(1, round(40 * count / widest))
        print(f"{month}  {count:5,}  {bar}")
    return 0


def cmd_diagnose(args) -> int:
    from . import quality
    if not quality.available():
        print("numpy is required: pip install numpy", file=sys.stderr)
        return 1
    root = Path(args.path).expanduser()
    files = sorted(p for p in (root.rglob("*") if root.is_dir() else [root])
                   if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff"})
    if not files:
        print(f"no images found at {root}", file=sys.stderr)
        return 1

    from . import faces
    ok = fixed = 0
    for f in files:
        box = None
        found = faces.detect_faces(f)
        if found:
            b = found[0]
            box = (b.x, b.y, b.width, b.height)
        d = quality.diagnose(f, face_box=box)
        if d is None:
            continue
        if not d.faults:
            ok += 1
        print(f"{f.name:34s} {d.summary()}")
        if args.fix and d.auto_fixable:
            out = quality.repair(d, f.with_name(f.stem + "_fixed" + f.suffix))
            if out:
                fixed += 1
                print(f"{'':34s}   -> wrote {out.name}")
    print(f"\n{len(files)} images, {ok} need nothing, {len(files)-ok} flagged"
          + (f", {fixed} repaired" if args.fix else ""))
    return 0


def cmd_purge(args) -> int:
    cfg = load_config(args.config)
    if not cfg.workspace.exists():
        print("nothing to purge")
        return 0
    size = sum(f.stat().st_size for f in cfg.workspace.rglob("*") if f.is_file())
    if not args.yes:
        reply = input(f"Delete {cfg.workspace} ({size / 1e6:.0f} MB of exported "
                      f"photos)? The ledger is kept. [y/N] ")
        if reply.strip().lower() not in {"y", "yes"}:
            print("cancelled")
            return 1
    shutil.rmtree(cfg.workspace)
    print(f"purged {size / 1e6:.0f} MB from {cfg.workspace}")
    return 0


COMMANDS = {
    "doctor": cmd_doctor, "scan": cmd_scan, "review": cmd_review,
    "mark": cmd_mark, "stats": cmd_stats, "coverage": cmd_coverage,
    "diagnose": cmd_diagnose, "purge": cmd_purge,
    "mark-sheet": cmd_mark_sheet, "report": cmd_report,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
