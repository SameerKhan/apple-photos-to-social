"""Unit tests that never touch a real Photos library.

Everything here runs on synthetic payloads and temp directories, so it works in
CI on any platform. The parts that genuinely need a live machine (osascript,
Photos export, iCloud downloads) are covered by docs/manual-testing.md instead.

Several of these tests exist because a specific bug shipped in an earlier draft
and was caught in review. Those are marked with a REGRESSION comment.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from photos_to_posts import applescript as ps
from photos_to_posts import imaging, ledger as L
from photos_to_posts import review as R
from photos_to_posts.config import Config, Zone, haversine_m, load_config
from photos_to_posts.review import PrivacyRefusal, ReviewResult, apply_filters, select_window


# --------------------------------------------------------------------------
# AppleScript payload parsing
# --------------------------------------------------------------------------

def test_parse_dates_day_first():
    raw = ("date Wednesday, 4 February 2015 at 1:13:09 AM, "
           "date Friday, 24 April 2015 at 12:10:25 PM")
    got = ps._parse_dates(raw)
    assert got == [datetime(2015, 2, 4, 1, 13, 9), datetime(2015, 4, 24, 12, 10, 25)]


def test_parse_dates_month_first():
    raw = ("date Wednesday, February 4, 2015 at 1:13:09 AM, "
           "date Friday, April 24, 2015 at 11:05:00 PM")
    got = ps._parse_dates(raw)
    assert got == [datetime(2015, 2, 4, 1, 13, 9), datetime(2015, 4, 24, 23, 5, 0)]


@pytest.mark.parametrize("text,expected_hour", [
    ("date Monday, 1 June 2026 at 12:00:00 AM", 0),   # midnight is 0, not 12
    ("date Monday, 1 June 2026 at 12:00:00 PM", 12),  # noon stays 12
    ("date Monday, 1 June 2026 at 1:00:00 PM", 13),
])
def test_parse_dates_meridiem_edges(text, expected_hour):
    assert ps._parse_dates(text)[0].hour == expected_hour


def test_asset_kind_and_orientation():
    a = ps.Asset("u/L0/001", "clip.MOV", datetime(2026, 1, 1), False, 1080, 1920)
    assert a.kind == "video" and a.orientation == "vertical"
    b = ps.Asset("u/L0/002", "shot.HEIC", datetime(2026, 1, 1), False, 4032, 2268)
    assert b.kind == "photo" and b.orientation == "horizontal"
    c = ps.Asset("u/L0/003", "grab.PNG", datetime(2026, 1, 1), False, 100, 100)
    assert c.is_screenshot and c.orientation == "square"


def test_quote_escapes_backslash_before_quote():
    # REGRESSION: escaping the quote first would double-escape the backslash.
    assert ps.quote(r'a\b"c') == r'a\\b\"c'


def test_quote_strips_control_characters():
    # A newline could otherwise terminate the AppleScript literal and inject code.
    assert "\n" not in ps.quote('safe"\nset x to do shell script "boom"')


def test_validate_uuid_accepts_real_and_rejects_injection():
    assert ps.validate_uuid("A61761AF-9FE0-4733-8562-C8B8040E8580/L0/001")
    with pytest.raises(ps.PhotosError):
        ps.validate_uuid('x" & (do shell script "id") & "')


def test_uuid_to_dirname_is_filesystem_safe():
    assert "/" not in ps.uuid_to_dirname("ABC-123/L0/001")


# --------------------------------------------------------------------------
# Perceptual hashing and clustering
# --------------------------------------------------------------------------

def _image(tmp: Path, name: str, colour, size=(64, 64)) -> Path:
    p = tmp / name
    Image.new("RGB", size, colour).save(p)
    return p


def test_dhash_is_deterministic_and_discriminates(tmp_path):
    a = _image(tmp_path, "a.jpg", (10, 10, 10))
    b = _image(tmp_path, "b.jpg", (10, 10, 10))
    assert imaging.dhash(a) == imaging.dhash(b)
    grad = tmp_path / "grad.jpg"
    im = Image.new("L", (64, 64))
    im.putdata([(x * 4) % 256 for y in range(64) for x in range(64)])
    im.convert("RGB").save(grad)
    assert imaging.dhash(grad) != imaging.dhash(a)


def test_hamming():
    assert imaging.hamming(0b1011, 0b1001) == 1
    assert imaging.hamming(0, 0xFFFFFFFFFFFFFFFF) == 64


def _frame(uid, phash, when):
    return imaging.Frame(uuid=uid, path=Path("/nonexistent"), filename=f"{uid}.jpg",
                         captured_at=when, phash=phash)


def test_cluster_requires_both_visual_and_time_proximity():
    t = datetime(2026, 1, 1, 12, 0, 0)
    same_scene_far_apart = [
        _frame("a", 0b0000, t),
        _frame("b", 0b0000, t + timedelta(hours=5)),   # identical, hours later
    ]
    assert len(imaging.cluster_bursts(same_scene_far_apart)) == 2

    close_in_time_different_scene = [
        _frame("a", 0x0000000000000000, t),
        _frame("b", 0xFFFFFFFFFFFFFFFF, t + timedelta(seconds=5)),
    ]
    assert len(imaging.cluster_bursts(close_in_time_different_scene)) == 2

    a_real_burst = [
        _frame("a", 0b0000, t),
        _frame("b", 0b0001, t + timedelta(seconds=2)),
        _frame("c", 0b0011, t + timedelta(seconds=4)),
    ]
    assert len(imaging.cluster_bursts(a_real_burst)) == 1


def test_cluster_representative_is_newest():
    t = datetime(2026, 1, 1, 12, 0, 0)
    clusters = imaging.cluster_bursts([
        _frame("old", 0b0000, t),
        _frame("new", 0b0001, t + timedelta(seconds=3)),
    ])
    assert clusters[0][0].uuid == "new"


def test_frames_without_hash_never_merge():
    t = datetime(2026, 1, 1)
    clusters = imaging.cluster_bursts([_frame("a", None, t), _frame("b", None, t)])
    assert len(clusters) == 2


def test_contact_sheet_written(tmp_path):
    imgs = [_image(tmp_path, f"i{i}.jpg", (i * 20 % 255, 40, 60)) for i in range(3)]
    frames = [imaging.Frame(uuid=f"u{i}", path=p, filename=p.name,
                            captured_at=datetime(2026, 1, 1, 0, i), phash=i)
              for i, p in enumerate(imgs)]
    sheets = imaging.contact_sheets(frames, tmp_path / "sheets", columns=2, rows=2)
    assert len(sheets) == 1 and sheets[0].exists()
    with Image.open(sheets[0]) as im:
        assert im.size[0] > 0


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

@pytest.fixture()
def led(tmp_path):
    with L.Ledger(tmp_path / "ledger.db") as l:
        yield l


def _rec(led, uid="a/L0/001", status=L.SEEN, phash=None):
    return led.record(uuid=uid, filename=f"{uid}.jpg", captured_at=datetime(2026, 1, 1),
                      kind="photo", status=status, phash=phash)


def test_high_bit_phash_roundtrip(led):
    # REGRESSION: dHash is unsigned 64-bit; SQLite integers are signed, so storing
    # the raw int raised OverflowError for roughly half of all real hashes.
    for value in (0, 1, 2**63, 2**63 - 1, 0xFFFFFFFFFFFFFFFF):
        uid = f"u{value}/L0/001"
        _rec(led, uid, phash=value)
        assert led.get(uid).phash == value


def test_raw_int_would_have_overflowed():
    # Demonstrates the underlying constraint the hex encoding works around.
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (v INTEGER)")
    with pytest.raises(OverflowError):
        conn.execute("INSERT INTO t VALUES (?)", (0xFFFFFFFFFFFFFFFF,))


def test_excluded_private_is_terminal_against_automated_writes(led):
    # REGRESSION: status ranking let an automated pass upgrade a deliberately
    # withheld photo to shortlisted, silently un-excluding it.
    _rec(led, status=L.EXCLUDED_PRIVATE)
    for attempt in (L.SEEN, L.SHORTLISTED, L.POSTED):
        assert _rec(led, status=attempt) == L.EXCLUDED_PRIVATE
    assert led.get("a/L0/001").status == L.EXCLUDED_PRIVATE


def test_excluded_junk_is_terminal(led):
    _rec(led, status=L.EXCLUDED_JUNK)
    assert _rec(led, status=L.SHORTLISTED) == L.EXCLUDED_JUNK


def test_explicit_set_status_can_override_terminal(led):
    _rec(led, status=L.EXCLUDED_PRIVATE)
    assert led.set_status("a/L0/001", L.SHORTLISTED)
    assert led.get("a/L0/001").status == L.SHORTLISTED


def test_status_changes_are_audited(led):
    _rec(led, status=L.EXCLUDED_PRIVATE)
    led.set_status("a/L0/001", L.POSTED, reason="published")
    hist = led.history("a/L0/001")   # accepts the raw id and digests it internally
    assert hist[0][1] == L.EXCLUDED_PRIVATE
    assert hist[-1][1:3] == (L.POSTED, "published")


def test_seen_counts_as_settled(led):
    # REGRESSION: `seen` was excluded from the settled set, so every reviewed but
    # unchosen asset resurfaced on the next run.
    # The set holds digests, not raw ids: the ledger keeps no readable list of what
    # was merely looked at.
    _rec(led, status=L.SEEN)
    key = led.digest("a/L0/001")
    assert key in led.settled_uuids()
    assert key not in led.settled_uuids(include_seen=False)
    assert "a/L0/001" not in led.settled_uuids(), "a raw id must never be stored"


def test_set_status_unknown_uuid_returns_false(led):
    assert led.set_status("nope/L0/001", L.POSTED) is False


def test_rejects_unknown_status(led):
    with pytest.raises(ValueError):
        _rec(led, status="invented")


def test_near_duplicates_respects_distance(led):
    _rec(led, "seen/L0/001", status=L.POSTED, phash=0b1111_0000)
    assert led.near_duplicates(0b1111_0001, max_distance=2)
    assert not led.near_duplicates(0b0000_1111, max_distance=2)


def test_ledger_file_is_owner_only(tmp_path):
    p = tmp_path / "nested" / "ledger.db"
    with L.Ledger(p):
        pass
    assert oct(p.stat().st_mode)[-3:] == "600"
    assert oct(p.parent.stat().st_mode)[-3:] == "700"


def test_stats_and_coverage(led):
    _rec(led, "a/L0/001", status=L.POSTED)
    _rec(led, "b/L0/001", status=L.SEEN)
    assert led.stats()["total"] == 2
    # Every row keeps a capture date now, seen included, so coverage describes the whole
    # review history rather than only the assets that earned a decision.
    assert led.coverage() == [("2026-01", 2)]


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def test_missing_config_returns_usable_defaults():
    cfg = load_config()
    assert cfg.default_days > 0


def test_privacy_configured_reflects_filters():
    assert not Config().privacy_configured
    assert Config(exclude_albums=["Private"]).privacy_configured
    assert Config(exclude_zones=[Zone("home", 0.0, 0.0)]).privacy_configured


def test_load_config_rejects_bad_coordinates(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[[privacy.exclude_zones]]\nname="x"\nlatitude=200\nlongitude=0\n')
    with pytest.raises(ValueError):
        load_config(p)


def test_load_config_rejects_bad_radius(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[[privacy.exclude_zones]]\nname="x"\nlatitude=0\nlongitude=0\nradius_m=-5\n')
    with pytest.raises(ValueError):
        load_config(p)


def test_load_config_missing_explicit_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_haversine_known_distance():
    # London to Paris, about 344 km.
    d = haversine_m(51.5074, -0.1278, 48.8566, 2.3522)
    assert 340_000 < d < 350_000
    assert haversine_m(10.0, 20.0, 10.0, 20.0) == pytest.approx(0.0)


# --------------------------------------------------------------------------
# Window selection and filtering
# --------------------------------------------------------------------------

def _asset(uid, when, name="IMG.HEIC", w=3000, h=4000):
    return ps.Asset(uid, name, when, False, w, h)


def test_select_window_accepts_aware_datetimes():
    # REGRESSION: comparing an aware `since` against Photos' naive timestamps
    # raised TypeError.
    now = datetime.now()
    assets = [_asset("a/L0/001", now - timedelta(days=1))]
    got = select_window(assets, since=datetime.now(timezone.utc) - timedelta(days=7))
    assert len(got) == 1


def test_select_window_orders_newest_first():
    now = datetime.now()
    assets = [_asset("a/L0/001", now - timedelta(days=5)),
              _asset("b/L0/001", now - timedelta(days=1))]
    assert [a.uuid for a in select_window(assets, days=30)][0] == "b/L0/001"


def test_apply_filters_refuses_without_privacy_config(led):
    cfg = Config()
    with pytest.raises(PrivacyRefusal):
        apply_filters([_asset("a/L0/001", datetime.now())], cfg, led,
                      ReviewResult(30, 1, 1))


def test_apply_filters_allows_when_explicitly_accepted(led):
    cfg = Config()
    out = apply_filters([_asset("a/L0/001", datetime.now())], cfg, led,
                        ReviewResult(30, 1, 1), allow_unfiltered=True)
    assert len(out) == 1


def test_apply_filters_drops_screenshots_and_settled(led):
    # No albums or zones here: resolving either would call AppleScript, which
    # these tests must never do. allow_unfiltered covers the privacy gate.
    cfg = Config(exclude_albums=[], exclude_zones=[])
    _rec(led, "old/L0/001", status=L.POSTED)
    result = ReviewResult(30, 3, 3)
    out = apply_filters([
        _asset("old/L0/001", datetime.now()),
        _asset("shot/L0/001", datetime.now(), name="Screenshot.PNG"),
        _asset("keep/L0/001", datetime.now()),
    ], cfg, led, result, allow_unfiltered=True)
    assert [a.uuid for a in out] == ["keep/L0/001"]
    assert result.skipped_known == 1 and result.skipped_screenshot == 1


def test_apply_filters_defers_videos_by_default(led):
    result = ReviewResult(30, 1, 1)
    out = apply_filters([_asset("v/L0/001", datetime.now(), name="clip.MOV")],
                        Config(), led, result, allow_unfiltered=True)
    assert out == [] and result.videos_deferred == 1


def test_apply_filters_drops_small_images(led):
    result = ReviewResult(30, 1, 1)
    cfg = Config(min_pixels=1_000_000)
    out = apply_filters([_asset("t/L0/001", datetime.now(), w=100, h=100)],
                        cfg, led, result, allow_unfiltered=True)
    assert out == [] and result.skipped_small == 1


# --------------------------------------------------------------------------
# Round-2 regressions
# --------------------------------------------------------------------------

def test_uuid_dirname_is_injective():
    # REGRESSION: replacing "/" with "_" made the ids A/B and A_B collide, so one
    # asset's export would delete the other's.
    assert ps.uuid_to_dirname("A/B") != ps.uuid_to_dirname("A_B")
    assert ps.uuid_to_dirname("A%B") != ps.uuid_to_dirname("A%25B")


def test_pick_export_prefers_the_matching_kind(tmp_path):
    # REGRESSION: Photos writes two files for a Live Photo. Taking iterdir()[0]
    # could hand a .mov back for a still, which Pillow cannot open, so the asset
    # silently vanished from the review.
    (tmp_path / "live.mov").write_bytes(b"x" * 5000)
    (tmp_path / "live.jpeg").write_bytes(b"y" * 100)
    files = list(tmp_path.iterdir())
    assert ps._pick_export(files, want_video=False).suffix == ".jpeg"
    assert ps._pick_export(files, want_video=True).suffix == ".mov"


def test_pick_export_refuses_the_wrong_kind(tmp_path):
    # There is deliberately no cross-kind fallback: handing a .mov back for a
    # still only moves the failure into Pillow, where it looks like a successful
    # export that silently vanishes.
    (tmp_path / "only.mov").write_bytes(b"x")
    files = list(tmp_path.iterdir())
    assert ps._pick_export(files, want_video=False) is None
    assert ps._pick_export(files, want_video=True).suffix == ".mov"
    assert ps._pick_export([], want_video=False) is None


def test_pick_export_prefers_larger_within_a_group(tmp_path):
    (tmp_path / "small.jpeg").write_bytes(b"x" * 10)
    (tmp_path / "big.jpeg").write_bytes(b"x" * 9999)
    assert ps._pick_export(list(tmp_path.iterdir()), want_video=False).name == "big.jpeg"


def test_dry_run_does_not_write_privacy_exclusions(tmp_path, monkeypatch):
    # REGRESSION: apply_filters persisted excluded_private before run_review's
    # dry_run check, so a preview mutated the ledger.
    monkeypatch.setattr(ps, "album_asset_ids",
                        lambda names: ps.AlbumLookup(ids={"fam/L0/001"}, missing=[]))
    cfg = Config(exclude_albums=["Family"])
    with L.Ledger(tmp_path / "l.db") as led:
        asset = _asset("fam/L0/001", datetime.now())
        apply_filters([asset], cfg, led, ReviewResult(30, 1, 1), dry_run=True)
        assert led.get("fam/L0/001") is None, "dry run wrote to the ledger"
        apply_filters([asset], cfg, led, ReviewResult(30, 1, 1), dry_run=False)
        assert led.get("fam/L0/001").status == L.EXCLUDED_PRIVATE


def test_history_screening_records_what_it_drops(tmp_path):
    # REGRESSION: a candidate dropped for matching history was never recorded, so
    # it was re-exported and re-hashed on every subsequent run, forever.
    from photos_to_posts.review import screen_against_history
    with L.Ledger(tmp_path / "l.db") as led:
        led.record(uuid="old/L0/001", filename="old.jpg", captured_at=datetime(2026, 1, 1),
                   kind="photo", status=L.POSTED, phash=0b1111_0000)
        frame = imaging.Frame(uuid="new/L0/001", path=tmp_path / "x.jpg",
                              filename="new.jpg", captured_at=datetime(2026, 2, 1),
                              phash=0b1111_0000)
        assets = {"new/L0/001": _asset("new/L0/001", datetime(2026, 2, 1), name="new.jpg")}
        result = ReviewResult(30, 1, 1)
        kept = screen_against_history([frame], led, Config(), result, assets)
        assert kept == []
        assert result.skipped_seen_before_visually == 1
        rec = led.get("new/L0/001")
        assert rec is not None and rec.status == L.SEEN, "dropped frame was not recorded"


def test_history_screening_keeps_novel_frames(tmp_path):
    from photos_to_posts.review import screen_against_history
    with L.Ledger(tmp_path / "l.db") as led:
        led.record(uuid="old/L0/001", filename="old.jpg", captured_at=datetime(2026, 1, 1),
                   kind="photo", status=L.POSTED, phash=0x0000000000000000)
        frame = imaging.Frame(uuid="new/L0/001", path=tmp_path / "x.jpg", filename="new.jpg",
                              captured_at=datetime(2026, 2, 1), phash=0xFFFFFFFFFFFFFFFF)
        kept = screen_against_history([frame], led, Config(), ReviewResult(30, 1, 1),
                                      {"new/L0/001": _asset("new/L0/001", datetime(2026, 2, 1))})
        assert len(kept) == 1


def test_uuid_validation_rejects_path_traversal():
    # REGRESSION: "." and ".." satisfied the character class, and would make the
    # export directory resolve to the export root or its parent, whose contents
    # are deleted before exporting.
    for bad in (".", "..", "a/../b", "a//b", ""):
        with pytest.raises(ps.PhotosError):
            ps.validate_uuid(bad)


def test_resurface_settled_never_resurfaces_private(tmp_path, monkeypatch):
    # REGRESSION: resurface_settled emptied the settled set entirely, so a photo
    # marked excluded_private kept its status but was exported and shown again.
    monkeypatch.setattr(ps, "album_asset_ids",
                        lambda names: ps.AlbumLookup(ids=set(), missing=[]))
    cfg = Config(exclude_albums=["Whatever"], resurface_settled=True)
    with L.Ledger(tmp_path / "l.db") as led:
        led.record(uuid="p/L0/001", filename="p.jpg", captured_at=datetime(2026, 1, 1),
                   kind="photo", status=L.EXCLUDED_PRIVATE)
        led.record(uuid="s/L0/001", filename="s.jpg", captured_at=datetime(2026, 1, 1),
                   kind="photo", status=L.SEEN)
        out = apply_filters(
            [_asset("p/L0/001", datetime.now()), _asset("s/L0/001", datetime.now())],
            cfg, led, ReviewResult(30, 2, 2))
        ids = {a.uuid for a in out}
        assert "p/L0/001" not in ids, "private asset resurfaced"
        assert "s/L0/001" in ids, "resurface_settled should still resurface seen"


def test_include_albums_is_an_allowlist(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "album_asset_ids",
                        lambda names: ps.AlbumLookup(ids={"ok/L0/001"}, missing=[]))
    cfg = Config(include_albums=["Safe To Post"])
    assert cfg.privacy_configured
    with L.Ledger(tmp_path / "l.db") as led:
        result = ReviewResult(30, 2, 2)
        out = apply_filters(
            [_asset("ok/L0/001", datetime.now()), _asset("no/L0/001", datetime.now())],
            cfg, led, result)
        assert [a.uuid for a in out] == ["ok/L0/001"]
        assert result.skipped_not_allowlisted == 1


def test_missing_include_album_stops_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "album_asset_ids",
                        lambda names: ps.AlbumLookup(ids=set(), missing=["Typo"]))
    with L.Ledger(tmp_path / "l.db") as led:
        with pytest.raises(PrivacyRefusal):
            apply_filters([_asset("a/L0/001", datetime.now())],
                          Config(include_albums=["Typo"]), led, ReviewResult(30, 1, 1))


def test_existing_ledger_permissions_are_repaired(tmp_path):
    p = tmp_path / "l.db"
    p.touch(mode=0o644)
    with L.Ledger(p):
        pass
    assert oct(p.stat().st_mode)[-3:] == "600"


@pytest.mark.parametrize("key,value", [
    ("assumed_mb_per_asset", 0), ("min_free_gb", -1),
])
def test_config_rejects_bad_budget(tmp_path, key, value):
    p = tmp_path / "c.toml"
    p.write_text(f"[export]\n{key} = {value}\n")
    with pytest.raises(ValueError):
        load_config(p)


# --------------------------------------------------------------------------
# tri-review regressions (all four confirmed by execution before fixing)
# --------------------------------------------------------------------------

def test_nan_geofence_radius_is_rejected(tmp_path):
    # REGRESSION: NaN passed `radius <= 0`, and `distance <= nan` is also False,
    # so the zone silently matched nothing while privacy_configured stayed True.
    p = tmp_path / "c.toml"
    p.write_text('[[privacy.exclude_zones]]\nname="h"\nlatitude=1.0\nlongitude=1.0\n'
                 'radius_m=nan\n')
    with pytest.raises(ValueError):
        load_config(p)


def test_non_finite_coordinates_are_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[[privacy.exclude_zones]]\nname="h"\nlatitude=nan\nlongitude=1.0\n')
    with pytest.raises(ValueError):
        load_config(p)


def test_private_album_wins_over_screenshot_filter(tmp_path, monkeypatch):
    # REGRESSION: the screenshot check ran first, so a screenshot inside a private
    # album was dropped as junk and never recorded private, losing that protection
    # if the screenshot filter was later turned off.
    monkeypatch.setattr(ps, "album_asset_ids",
                        lambda names: ps.AlbumLookup(ids={"s/L0/001"}, missing=[]))
    cfg = Config(exclude_albums=["Family"], exclude_screenshots=True)
    with L.Ledger(tmp_path / "l.db") as led:
        shot = _asset("s/L0/001", datetime.now(), name="Screenshot.PNG")
        apply_filters([shot], cfg, led, ReviewResult(30, 1, 1))
        rec = led.get("s/L0/001")
        assert rec is not None and rec.status == L.EXCLUDED_PRIVATE


def test_history_screening_ignores_the_assets_own_row(tmp_path):
    # REGRESSION: with resurface_settled the asset is deliberately a candidate
    # again, and matching it against its own stored hash both dropped it and
    # rewrote posted down to seen.
    from photos_to_posts.review import screen_against_history
    with L.Ledger(tmp_path / "l.db") as led:
        led.record(uuid="p/L0/001", filename="p.jpg", captured_at=datetime(2026, 1, 1),
                   kind="photo", status=L.POSTED, phash=0xABCD)
        frame = imaging.Frame(uuid="p/L0/001", path=tmp_path / "x.jpg", filename="p.jpg",
                              captured_at=datetime(2026, 1, 1), phash=0xABCD)
        kept = screen_against_history([frame], led, Config(resurface_settled=True),
                                      ReviewResult(30, 1, 1),
                                      {"p/L0/001": _asset("p/L0/001", datetime(2026, 1, 1))})
        assert len(kept) == 1, "asset was screened out by its own history row"
        assert led.get("p/L0/001").status == L.POSTED, "posted was demoted to seen"


def test_export_refuses_a_symlinked_target(tmp_path):
    # REGRESSION: mkdir(exist_ok=True) is satisfied by a symlink to a directory
    # elsewhere, and the clearing loop would then empty that directory.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("do not delete me")
    root = tmp_path / "export"
    root.mkdir()
    uid = "abc/L0/001"
    (root / ps.uuid_to_dirname(uid)).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ps.PhotosError):
        ps.export_assets([uid], root)
    assert (outside / "precious.txt").exists(), "files outside the workspace were deleted"


def test_allow_unfiltered_config_key_permits_the_run(tmp_path):
    cfg = Config(allow_unfiltered=True)
    assert not cfg.privacy_configured
    with L.Ledger(tmp_path / "l.db") as led:
        out = apply_filters([_asset("a/L0/001", datetime.now())], cfg, led,
                            ReviewResult(30, 1, 1))
        assert len(out) == 1


def test_allow_unfiltered_defaults_off(tmp_path):
    # A fresh install must still fail closed.
    with L.Ledger(tmp_path / "l.db") as led:
        with pytest.raises(PrivacyRefusal):
            apply_filters([_asset("a/L0/001", datetime.now())], Config(), led,
                          ReviewResult(30, 1, 1))


def test_platform_fit_flags_a_too_tall_phone_photo():
    # REGRESSION: a 1080x1616 studio portrait (0.668) was scheduled to Instagram and
    # rejected with "aspect ratio between 3:4 and 1.91:1". Nothing had checked.
    fit = imaging.platform_fit(1080, 1616)
    assert fit["instagram"] is False
    assert fit["facebook"] is True and fit["x"] is True
    assert imaging.platform_fit(1080, 1350)["instagram"] is True   # 4:5
    assert imaging.platform_fit(1080, 1080)["instagram"] is True   # square
    assert imaging.platform_fit(0, 0)["instagram"] is False


def test_crop_to_ratio_hits_4_5_and_keeps_the_top(tmp_path):
    src = tmp_path / "tall.jpg"
    Image.new("RGB", (1080, 1616), (30, 60, 90)).save(src)
    out = imaging.crop_to_ratio(src, tmp_path / "out.jpg")
    with Image.open(out) as im:
        w, h = im.size
        assert (w, h) == (1080, 1350)
        assert abs(w / h - 0.8) < 0.001
    # an already-wide image is passed through untouched
    wide = tmp_path / "wide.jpg"
    Image.new("RGB", (1600, 900), (0, 0, 0)).save(wide)
    with Image.open(imaging.crop_to_ratio(wide, tmp_path / "w2.jpg")) as im:
        assert im.size == (1600, 900)


def test_faces_module_degrades_without_pyobjc(tmp_path, monkeypatch):
    # The package must work on a machine with no PyObjC, and on photos with no
    # faces in them, which is most of a spearfishing camera roll.
    from photos_to_posts import faces
    img = tmp_path / "x.jpg"
    Image.new("RGB", (400, 600), (120, 120, 120)).save(img)
    assert faces.detect_faces(img) == [] or isinstance(faces.detect_faces(img), list)
    assert faces.focus_point(img) is None or isinstance(faces.focus_point(img), float)
    assert isinstance(faces.available(), bool)


def test_crop_uses_focus_point_when_one_is_found(tmp_path, monkeypatch):
    from photos_to_posts import faces as faces_mod
    src = tmp_path / "tall.jpg"
    Image.new("RGB", (1080, 1616), (10, 20, 30)).save(src)
    # Face low in the frame should pull the crop window down.
    monkeypatch.setattr(faces_mod, "focus_point", lambda p: 0.60)
    out = imaging.crop_to_ratio(src, tmp_path / "low.jpg")
    with Image.open(out) as im:
        assert im.size == (1080, 1350)
    monkeypatch.setattr(faces_mod, "focus_point", lambda p: 0.10)
    imaging.crop_to_ratio(src, tmp_path / "high.jpg")
    # A focus point near the top clamps to 0 rather than going negative.
    monkeypatch.setattr(faces_mod, "focus_point", lambda p: 0.0)
    out3 = imaging.crop_to_ratio(src, tmp_path / "clamp.jpg")
    with Image.open(out3) as im:
        assert im.size == (1080, 1350)


def test_crop_falls_back_when_no_face(tmp_path, monkeypatch):
    from photos_to_posts import faces as faces_mod
    monkeypatch.setattr(faces_mod, "focus_point", lambda p: None)
    src = tmp_path / "t.jpg"
    Image.new("RGB", (1080, 1616), (0, 0, 0)).save(src)
    out = imaging.crop_to_ratio(src, tmp_path / "o.jpg", top_share=0.30)
    with Image.open(out) as im:
        assert im.size == (1080, 1350)


# --------------------------------------------------------------------------
# Quality diagnosis and conservative repair
# --------------------------------------------------------------------------

def _solid(tmp_path, name, rgb, size=(300, 400)):
    p = tmp_path / name
    Image.new("RGB", size, rgb).save(p, quality=95)
    return p


def test_quality_available_is_boolean():
    from photos_to_posts import quality
    assert isinstance(quality.available(), bool)


def test_diagnose_flags_a_blown_image(tmp_path):
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    d = quality.diagnose(_solid(tmp_path, "white.jpg", (254, 254, 254)))
    assert "blown_highlights" in d.faults
    # Detected, but NOT auto-repaired: clipped pixels hold nothing to recover, so the
    # only available "fix" is darkening white to grey, which is worse.
    assert "blown_highlights" not in d.auto_fixable
    assert "blown_highlights" in d.needs_judgement


def test_diagnose_separates_intent_from_defect(tmp_path):
    # THE central rule of this module. A dark frame is ambiguous: it might be a
    # silhouette. It must never be auto-repaired.
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    d = quality.diagnose(_solid(tmp_path, "dark.jpg", (8, 8, 8)))
    assert "underexposed" in d.faults
    assert "underexposed" in d.needs_judgement
    assert "underexposed" not in d.auto_fixable
    assert quality.repair(d, tmp_path / "out.jpg") is None, "a dark frame was auto-edited"


def test_crushed_shadows_are_never_auto_fixed(tmp_path):
    # REGRESSION: a dawn jetty shot with 14.5% crushed shadows was "corrected" into
    # something flatter and worse. Shadow clipping is frequently the intent.
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    import numpy as np
    a = np.zeros((400, 300, 3), dtype=np.uint8)
    a[:60] = 200                      # a bright sky over a black foreground
    p = tmp_path / "lowkey.jpg"
    Image.fromarray(a).save(p, quality=95)
    d = quality.diagnose(p)
    assert "crushed_shadows" in d.faults
    assert "crushed_shadows" not in d.auto_fixable


def test_repair_returns_none_when_nothing_to_do(tmp_path):
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    d = quality.diagnose(_solid(tmp_path, "mid.jpg", (128, 128, 128)))
    assert quality.repair(d, tmp_path / "x.jpg") is None


def test_colour_cast_ignores_a_coloured_subject(tmp_path):
    # REGRESSION: grey-world called an orange takeaway box a 149% red cast. A photo of
    # a red object is not a red cast, and the neutral-pixel mask is what fixes it.
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    import numpy as np
    a = np.full((400, 300, 3), 130, dtype=np.uint8)   # neutral grey surroundings
    a[100:300, 60:240] = (230, 90, 20)                # a big orange object
    p = tmp_path / "orange.jpg"
    Image.fromarray(a).save(p, quality=95)
    d = quality.diagnose(p)
    assert "colour_cast" not in d.faults, f"false cast: {d.cast:.0f}%"


def test_low_detail_is_reported_not_repaired(tmp_path):
    # Renamed from "unfixable blur": Laplacian variance measures DETAIL, and a sharp
    # photo of a plain wall scores as low as a smeared one. The label must not claim
    # more than the measurement supports.
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    d = quality.diagnose(_solid(tmp_path, "flat.jpg", (120, 120, 120)))
    assert d.low_detail
    assert "low_detail" in quality.NEEDS_JUDGEMENT
    assert "low_detail" not in d.auto_fixable


def test_diagnosis_summary_is_human_readable(tmp_path):
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    d = quality.diagnose(_solid(tmp_path, "w.jpg", (254, 254, 254)))
    s = d.summary()
    assert "auto:" in s or "your call:" in s


def test_tiny_images_do_not_produce_nan_sharpness(tmp_path):
    # REGRESSION: a 1x1 or 2x2 image leaves an empty Laplacian interior, and var() on
    # an empty slice is NaN. NaN fails every comparison, so the image was silently
    # classified as perfectly sharp instead of unmeasurable.
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    import math
    for size in ((1, 1), (2, 2), (1, 500)):
        p = tmp_path / f"tiny_{size[0]}x{size[1]}.jpg"
        Image.new("RGB", size, (90, 90, 90)).save(p)
        d = quality.diagnose(p)
        assert d is not None
        assert math.isnan(d.sharpness), "expected the measure to be declined"
        assert "soft" not in d.faults and "unfixable_blur" not in d.faults, \
            "an unmeasurable image must not be classified either way"


@pytest.mark.parametrize("mode,ext", [("L", "png"), ("RGBA", "png"),
                                      ("CMYK", "jpg"), ("P", "png")])
def test_diagnose_handles_other_colour_modes(tmp_path, mode, ext):
    # A camera roll is not all RGB JPEGs: screenshots arrive as RGBA PNGs, scans can be
    # greyscale, and a stray CMYK file should be measured rather than crash the run.
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    p = tmp_path / f"m{mode}.{ext}"
    Image.new("RGB", (200, 200), (90, 100, 110)).convert(mode).save(p)
    d = quality.diagnose(p)
    assert d is not None and d.width == 200



def test_repair_refuses_when_judgement_and_autofix_coexist(tmp_path):
    # REGRESSION, and the most important test here. A low-key silhouette with a small
    # patch of blown sky carries BOTH a judgement fault and a repairable one. The old
    # code repaired it because it only checked whether any auto-fixable fault existed.
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    import numpy as np
    a = np.zeros((400, 300, 3), dtype=np.uint8)
    a[:40] = 255                                  # blown sky
    a[300:, :] = (150, 60, 40)                    # a warm cast in the foreground
    p = tmp_path / "silhouette.jpg"
    Image.fromarray(a).save(p, quality=95)
    d = quality.diagnose(p)
    assert d.needs_judgement, "expected the dark frame to need judgement"
    assert quality.repair(d, tmp_path / "out.jpg") is None, \
        "a photo needing judgement was modified because it also had a fixable fault"
    assert not (tmp_path / "out.jpg").exists()


def test_repair_has_no_override_flag():
    # An escape hatch on a safety guarantee is not a guarantee. sharpen_soft was one.
    import inspect
    from photos_to_posts import quality
    params = inspect.signature(quality.repair).parameters
    assert set(params) == {"diagnosis", "dst"}, f"unexpected parameters: {list(params)}"


def test_blown_highlights_are_reported_not_repaired(tmp_path):
    # Clipped pixels hold no recoverable data, so darkening them to grey is a
    # downgrade, not a repair.
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    assert "blown_highlights" in quality.NEEDS_JUDGEMENT
    assert "blown_highlights" not in quality.AUTO_FIXABLE


def test_repair_preserves_alpha_and_mode(tmp_path):
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy not installed")
    import numpy as np
    a = np.full((200, 200, 4), 120, dtype=np.uint8)
    a[..., 0] = 190          # a red cast on otherwise neutral pixels
    a[..., 3] = 128          # semi transparent
    p = tmp_path / "t.png"
    Image.fromarray(a, "RGBA").save(p)
    d = quality.diagnose(p)
    if not d.auto_fixable or d.needs_judgement:
        pytest.skip("synthetic image did not land in the auto-fix class")
    out = quality.repair(d, tmp_path / "o.png")
    with Image.open(out) as im:
        assert im.mode == "RGBA", "transparency was flattened"


def test_linkedin_ratio_floor_is_four_fifths():
    # REGRESSION: the floor was 0.33, so a 0.668 phone portrait was reported as
    # fitting LinkedIn. LinkedIn documents 4:5 through 3:1 for organic photos.
    assert imaging.PLATFORM_RATIOS["linkedin"][0] == 0.80
    assert imaging.platform_fit(1080, 1616)["linkedin"] is False
    assert imaging.platform_fit(1080, 1350)["linkedin"] is True


def test_face_placement_actually_moves_the_crop(tmp_path, monkeypatch):
    # REGRESSION: the old test asserted only output dimensions, so it passed even if
    # the focus point were ignored entirely. Compare pixels instead.
    from photos_to_posts import faces as faces_mod
    import numpy as np
    a = np.zeros((1616, 1080, 3), dtype=np.uint8)
    for y in range(1616):
        a[y, :, :] = y % 256                      # a vertical gradient, so position shows
    src = tmp_path / "grad.jpg"
    Image.fromarray(a).save(src, quality=95)

    monkeypatch.setattr(faces_mod, "focus_point", lambda p: 0.20)
    high = imaging.crop_to_ratio(src, tmp_path / "high.jpg")
    monkeypatch.setattr(faces_mod, "focus_point", lambda p: 0.75)
    low = imaging.crop_to_ratio(src, tmp_path / "low.jpg")
    with Image.open(high) as a1, Image.open(low) as a2:
        assert np.asarray(a1).mean() != np.asarray(a2).mean(), \
            "focus point did not change which pixels were kept"


# --------------------------------------------------------------------------
# v0.3: publication events, metadata cache, video sizing, mark-sheet
# --------------------------------------------------------------------------

def test_publications_are_appended_not_overwritten(tmp_path):
    # REGRESSION: assets.destination is a single column, so posting the same photo to
    # two platforms overwrote the first and lost its timestamp.
    with L.Ledger(tmp_path / "l.db") as led:
        _rec(led, "a/L0/001", status=L.SEEN)
        led.set_status("a/L0/001", L.POSTED, destination="instagram")
        led.set_status("a/L0/001", L.POSTED, destination="facebook")
        pubs = led.publications()
        assert len(pubs) == 2
        assert {p[1] for p in pubs} == {"instagram", "facebook"}
        assert all(p[2] for p in pubs), "every publication needs a timestamp"


def test_posting_without_destination_logs_no_publication(tmp_path):
    with L.Ledger(tmp_path / "l.db") as led:
        _rec(led, "a/L0/001")
        led.set_status("a/L0/001", L.POSTED)
        assert led.publications() == []


def test_asset_cache_roundtrip_and_replace(tmp_path):
    with L.Ledger(tmp_path / "l.db") as led:
        rows = [("u1/L0/001", "a.jpg", "2026-01-01T00:00:00", True, 100, 200),
                ("u2/L0/001", "b.jpg", "2026-01-02T00:00:00", False, 300, 400)]
        assert led.cache_assets(rows) == 2
        assert led.cache_size() == 2
        got = {r[0]: r for r in led.cached_assets()}
        assert got["u1/L0/001"][3] is True and got["u2/L0/001"][3] is False
        # caching again REPLACES rather than accumulating
        assert led.cache_assets(rows[:1]) == 1
        assert led.cache_size() == 1
        led.clear_cache()
        assert led.cache_size() == 0


def test_video_disk_budget_is_not_costed_as_a_photo(tmp_path):
    # REGRESSION: one figure was used for both. A 4K video derivative is 100-500 MB
    # against an 8 MB photo, so a video run under-estimated by 10x to 50x and would
    # fill the disk mid-export.
    from photos_to_posts.review import _check_disk_budget
    cfg = Config(workspace=tmp_path / "w", min_free_bytes=0,
                 assumed_bytes_per_asset=8_000_000,
                 assumed_bytes_per_video=200_000_000)
    assert cfg.assumed_bytes_per_video > cfg.assumed_bytes_per_asset * 10
    # 10 photos is affordable; 10 videos at 2 GB should trip a tight floor
    cfg.min_free_bytes = 10**15
    with pytest.raises(RuntimeError):
        _check_disk_budget(cfg, 10, videos=10)


def test_config_rejects_bad_video_size(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[export]\nassumed_mb_per_video = 0\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_sample_points_avoid_the_very_start_and_end():
    # Grabbing 1.0s on a dive clip returns the surface of the water.
    assert min(imaging.SAMPLE_POINTS) > 0.0
    assert max(imaging.SAMPLE_POINTS) < 1.0
    assert len(imaging.SAMPLE_POINTS) >= 3


def test_filmstrip_joins_frames_side_by_side(tmp_path):
    frames = []
    for i in range(4):
        p = tmp_path / f"f{i}.jpg"
        Image.new("RGB", (200, 300), (i * 50, 40, 60)).save(p)
        frames.append(p)
    out = imaging.filmstrip(frames, tmp_path / "strip.jpg", height=150)
    assert out is not None
    with Image.open(out) as im:
        assert im.height == 150
        # four 200x300 frames scale to 100x150 each, so ~412px wide with the gaps
        assert im.width > im.height * 2, "expected a wide strip, not a square"
        assert im.width == pytest.approx(4 * 100 + 3 * 4, abs=8)
    assert imaging.filmstrip([], tmp_path / "none.jpg") is None


def test_filmstrip_survives_an_unreadable_frame(tmp_path):
    good = tmp_path / "g.jpg"
    Image.new("RGB", (100, 100), (10, 20, 30)).save(good)
    bad = tmp_path / "b.jpg"
    bad.write_text("not an image")
    assert imaging.filmstrip([good, bad], tmp_path / "s.jpg") is not None


def test_video_duration_returns_none_without_ffprobe(tmp_path, monkeypatch):
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda n: None)
    p = tmp_path / "nope.mov"
    p.write_bytes(b"not a video")
    assert imaging.video_duration(p) is None


def test_a_killed_run_is_reconciled_on_the_next_start(tmp_path):
    # REGRESSION: a real run was killed by a harness timeout and sat at state
    # 'running' forever, because a terminated process cannot run its own cleanup.
    with L.Ledger(tmp_path / "l.db") as led:
        orphan = led.start_run(window="2026-01")
        assert led.stale_runs() == [orphan]
        second = led.start_run(window="2026-02")
        assert led.stale_runs() == [second], "the orphan should have been reconciled"
        import sqlite3
        row = led.conn.execute("SELECT state, finished_at FROM runs WHERE id=?",
                               (orphan,)).fetchone()
        assert row[0] == "interrupted" and row[1] is not None


def test_export_script_raises_the_apple_event_timeout(monkeypatch, tmp_path):
    # REGRESSION: 16 of 60 assets failed to export with AppleScript error -1712,
    # "AppleEvent timed out". Apple Events have their own 120s ceiling independent of
    # the subprocess timeout, and a large video export exceeds it.
    seen = {}

    def fake_run(script, timeout=900):
        seen["script"] = script
        return ""

    monkeypatch.setattr(ps, "run", fake_run)
    ps.export_assets(["ABC-1/L0/001"], tmp_path / "out")
    assert "with timeout of" in seen["script"], "export must raise the Apple Event timeout"
    assert str(ps.APPLE_EVENT_TIMEOUT) in seen["script"]
    assert "end timeout" in seen["script"]



def test_seen_assets_keep_no_readable_identity(tmp_path):
    # The retention rule Sameer chose, as revised 17 Aug 2026: a photo merely looked at
    # leaves no filename and no id, but DOES leave its capture time, so `coverage` can
    # honestly report which months have been reviewed.
    with L.Ledger(tmp_path / "l.db") as led:
        led.record(uuid="secret/L0/001", filename="IMG_PRIVATE.HEIC",
                   captured_at=datetime(2026, 5, 5), kind="photo", status=L.SEEN)
        row = led.conn.execute(
            "SELECT uuid, plain_uuid, filename, captured_at FROM assets").fetchone()
        assert row[0].startswith("sha256:")
        assert row[1] is None and row[2] is None, "seen must not keep uuid or filename"
        assert row[3] == "2026-05-05T00:00:00", "seen keeps capture time, for coverage"
        # and yet the whole point still holds
        assert led.digest("secret/L0/001") in led.settled_uuids()


def test_decisions_keep_their_identity(tmp_path):
    with L.Ledger(tmp_path / "l.db") as led:
        led.record(uuid="keep/L0/001", filename="IMG_KEEP.HEIC",
                   captured_at=datetime(2026, 5, 5), kind="photo", status=L.POSTED,
                   destination="instagram")
        rec = led.get("keep/L0/001")
        assert rec is not None and rec.filename == "IMG_KEEP.HEIC"
        row = led.conn.execute("SELECT plain_uuid FROM assets").fetchone()
        assert row[0] == "keep/L0/001"


def test_promoting_a_seen_asset_does_not_resurrect_discarded_details(tmp_path):
    with L.Ledger(tmp_path / "l.db") as led:
        led.record(uuid="x/L0/001", filename="IMG_X.HEIC",
                   captured_at=datetime(2026, 5, 5), kind="photo", status=L.SEEN)
        led.set_status("x/L0/001", L.POSTED, destination="facebook")
        row = led.conn.execute(
            "SELECT plain_uuid, filename FROM assets").fetchone()
        # the id comes back because the caller supplied it; the filename does not,
        # because it was discarded and is not reconstructed
        assert row[0] == "x/L0/001"
        assert row[1] is None


def test_no_raw_photos_id_reaches_any_table(tmp_path):
    # REGRESSION: set_status wrote the raw id into status_history while every other
    # write used the digest, so the readable id leaked back in through the audit trail.
    with L.Ledger(tmp_path / "l.db") as led:
        raw = "LEAK-ME-1234/L0/001"
        led.record(uuid=raw, filename="f.jpg", captured_at=datetime(2026, 1, 1),
                   kind="photo", status=L.SEEN)
        led.set_status(raw, L.POSTED, destination="instagram", reason="published")
        led.record_publication(raw, "facebook")
        for table, column in (("assets", "uuid"), ("status_history", "uuid"),
                              ("publications", "uuid")):
            vals = [r[0] for r in led.conn.execute(f"SELECT {column} FROM {table}")]
            assert all(v.startswith("sha256:") for v in vals), \
                f"{table}.{column} holds a raw id: {vals}"
        # plain_uuid is the ONE place an id is kept, and only for a decision
        plain = [r[0] for r in led.conn.execute("SELECT plain_uuid FROM assets")]
        assert plain == [raw], "a deliberate decision should keep its id"


def test_migration_from_the_old_not_null_schema(tmp_path):
    # REGRESSION: the pre-0.4 table declared filename and captured_at NOT NULL, and
    # SQLite cannot relax that with ALTER, so the first migration attempt against a
    # real ledger raised IntegrityError.
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE assets (
            uuid TEXT PRIMARY KEY, filename TEXT NOT NULL, captured_at TEXT NOT NULL,
            kind TEXT NOT NULL, phash TEXT, status TEXT NOT NULL, note TEXT,
            destination TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL, old_status TEXT,
            new_status TEXT NOT NULL, reason TEXT, changed_at TEXT NOT NULL);
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            finished_at TEXT, state TEXT NOT NULL DEFAULT 'running', window TEXT,
            examined INTEGER NOT NULL DEFAULT 0, recorded INTEGER NOT NULL DEFAULT 0,
            note TEXT);
    """)
    con.execute("INSERT INTO assets VALUES ('old-seen/L0/001','S.HEIC','2026-01-01',"
                "'photo','abcd','seen',NULL,NULL,'2026-01-01','2026-01-01')")
    con.execute("INSERT INTO assets VALUES ('old-post/L0/001','P.HEIC','2026-01-02',"
                "'photo','beef','posted',NULL,'instagram','2026-01-02','2026-01-02')")
    con.commit(); con.close()

    with L.Ledger(path) as led:
        assert led.stats()["total"] == 2
        rows = {r[0]: r for r in led.conn.execute(
            "SELECT status, uuid, plain_uuid, filename FROM assets")}
        seen, posted = rows["seen"], rows["posted"]
        assert seen[1].startswith("sha256:") and seen[2] is None and seen[3] is None
        assert posted[1].startswith("sha256:") and posted[2] == "old-post/L0/001"
        assert posted[3] == "P.HEIC", "a decision keeps its filename"
        # and dedup survives the migration
        assert led.digest("old-seen/L0/001") in led.settled_uuids()


def test_migration_is_idempotent_from_a_half_migrated_state(tmp_path):
    # REGRESSION: a failed first attempt added plain_uuid and then errored on the
    # NOT NULL columns. The retry then skipped the rebuild, because it tested for the
    # column rather than for the constraint, and failed identically forever.
    import sqlite3
    path = tmp_path / "half.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE assets (
            uuid TEXT PRIMARY KEY, filename TEXT NOT NULL, captured_at TEXT NOT NULL,
            kind TEXT NOT NULL, phash TEXT, status TEXT NOT NULL, note TEXT,
            destination TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
        ALTER TABLE assets ADD COLUMN plain_uuid TEXT;
        CREATE TABLE status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL, old_status TEXT,
            new_status TEXT NOT NULL, reason TEXT, changed_at TEXT NOT NULL);
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            finished_at TEXT, state TEXT NOT NULL DEFAULT 'running', window TEXT,
            examined INTEGER NOT NULL DEFAULT 0, recorded INTEGER NOT NULL DEFAULT 0,
            note TEXT);
    """)
    con.execute("INSERT INTO assets (uuid,filename,captured_at,kind,phash,status,"
                "first_seen,updated_at) VALUES ('h/L0/001','H.HEIC','2026-01-01',"
                "'photo','aa','seen','2026-01-01','2026-01-01')")
    con.commit(); con.close()

    with L.Ledger(path) as led:           # must not raise
        assert led.stats()["total"] == 1
        row = led.conn.execute("SELECT uuid, filename FROM assets").fetchone()
        assert row[0].startswith("sha256:") and row[1] is None
    with L.Ledger(path) as led:           # and opening again must be a no-op
        assert led.stats()["total"] == 1


def test_summary_does_not_report_included_images_as_skipped():
    # REGRESSION: auto_repaired and low_detail were listed in the "skipped:" dict.
    # Neither is a skip. A run printing "skipped: ... very low detail 32" led to the
    # conclusion that 32 photos had been dropped from review, when all 32 were in the
    # contact sheets.
    r = ReviewResult(window_days=30, total_in_library=100, in_window=10,
                     exported=10, moments=10)
    r.auto_repaired = 2
    r.low_detail = 32
    r.skipped_screenshot = 5
    s = r.summary()
    assert "skipped: screenshots 5" in s
    assert "included: auto-repaired 2, flagged low detail 32" in s
    skipped_part = s.split("skipped:")[1].split("|")[0]
    assert "low detail" not in skipped_part and "repaired" not in skipped_part


def test_sharpness_is_stable_across_image_scale(tmp_path):
    # REGRESSION: raw Laplacian variance swung 48x across resolutions on one unchanged
    # photograph (13 at 4032px, 625 at 800px), so "soft"/"low_detail" partly measured
    # megapixels. Because those are judgement faults, the highest-resolution originals
    # were the ones refused a colour-cast repair.
    np = pytest.importorskip("numpy")
    from photos_to_posts import quality
    if not quality.available():
        pytest.skip("numpy unavailable")
    rng = np.random.default_rng(0)
    big = Image.fromarray(rng.integers(0, 255, (2400, 1800, 3)).astype("uint8"))
    scores = []
    for w in (1800, 1200, 600):
        p = tmp_path / f"s{w}.png"
        big.resize((w, int(w * 2400 / 1800)), Image.LANCZOS).save(p)
        scores.append(quality.diagnose(p).sharpness)
    assert max(scores) / max(min(scores), 1e-6) < 2.0, scores


def _rec_id(led, uuid="U/L0/001", status=L.SEEN, **kw):
    return led.record(uuid=uuid, filename=kw.pop("filename", "IMG_1.HEIC"),
                      captured_at="2026-08-01T10:00:00", kind="photo", status=status, **kw)


def test_an_automated_pass_never_downgrades_a_human_decision(tmp_path):
    # REGRESSION: _write protected only TERMINAL statuses, so a routine review pass
    # recorded shortlisted assets back down to `seen` and erased 13 real picks. The
    # same path would have overwritten POSTED, destroying the publication log.
    with L.Ledger(tmp_path / "l.db") as led:
        _rec_id(led, status=L.SEEN)
        led.set_status("U/L0/001", L.SHORTLISTED)
        assert _rec_id(led, status=L.SEEN) == L.SHORTLISTED       # pass must not undo it

        led.set_status("U/L0/001", L.POSTED, destination="instagram")
        assert _rec_id(led, status=L.SEEN) == L.POSTED
        assert _rec_id(led, status=L.SHORTLISTED) == L.POSTED

        # but a genuine upgrade still lands
        led.set_status("V/L0/001", L.SEEN) or _rec_id(led, uuid="V/L0/001")
        assert _rec_id(led, uuid="V/L0/001", status=L.SHORTLISTED) == L.SHORTLISTED


def test_dropping_to_a_non_legible_status_erases_identity(tmp_path):
    # REGRESSION: the UPDATE used COALESCE(?, plain_uuid), so a row falling back to
    # `seen` KEPT the uuid a legible status had attached. 13 rows in the real ledger
    # ended up as `seen` while still carrying identifying uuids, which is exactly the
    # leak the opaque ledger was built to prevent.
    with L.Ledger(tmp_path / "l.db") as led:
        _rec_id(led, status=L.SEEN)
        led.set_status("U/L0/001", L.SHORTLISTED, filename="IMG_1.HEIC")
        row = led.conn.execute("SELECT plain_uuid, filename FROM assets").fetchone()
        assert row[0] == "U/L0/001" and row[1] == "IMG_1.HEIC"

        led.set_status("U/L0/001", L.SEEN)          # explicit demotion
        row = led.conn.execute(
            "SELECT plain_uuid, filename, captured_at FROM assets").fetchone()
        assert row[0] is None and row[1] is None, "identity survived a demotion to seen"
        # Capture time is deliberately retained on every status so `coverage` can answer
        # "which months have I been through". Sameer's explicit choice, 17 Aug 2026.
        assert row[2] == "2026-08-01T10:00:00"


def test_marking_a_decision_restores_the_filename(tmp_path):
    # A `seen` row has no filename by design. Deciding on it from a contact sheet
    # should re-attach one, or the exclusion log cannot name what it excluded.
    with L.Ledger(tmp_path / "l.db") as led:
        _rec_id(led, status=L.SEEN, filename="IMG_9.HEIC")
        assert led.conn.execute("SELECT filename FROM assets").fetchone()[0] is None
        led.set_status("U/L0/001", L.EXCLUDED_PRIVATE, filename="IMG_9.HEIC",
                       captured_at="2026-08-01T10:00:00", note="family")
        r = led.conn.execute("SELECT filename, captured_at FROM assets").fetchone()
        assert r[0] == "IMG_9.HEIC" and r[1] == "2026-08-01T10:00:00"


def test_seen_keeps_capture_time_but_never_the_identifier(tmp_path):
    # Sameer's ruling 17 Aug 2026: a merely-seen asset records WHEN it was shot, so
    # coverage is honest about which months have been reviewed, but still never records
    # WHICH photo it was. Before this, seen rows had no date and `coverage` could only
    # see the 94 of 257 rows that carried a decision.
    with L.Ledger(tmp_path / "l.db") as led:
        _rec_id(led, status=L.SEEN, filename="IMG_7.HEIC")
        r = led.conn.execute(
            "SELECT plain_uuid, filename, captured_at FROM assets").fetchone()
        assert r[0] is None and r[1] is None
        assert r[2] == "2026-08-01T10:00:00"
        assert led.coverage() == [("2026-08", 1)]


# --------------------------------------------------------------------------
# Findings from the /tri-review of 17 Aug 2026
# --------------------------------------------------------------------------

def test_migration_keeps_capture_time_on_seen_rows(tmp_path):
    # [codex+gemini P1] The opacity migration nulled captured_at for every non-legible
    # row, so upgrading a pre-0.4 ledger silently and irreversibly destroyed the review
    # history that `coverage` reads. Both external legs caught this; I did not.
    import sqlite3
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE assets (
            uuid TEXT PRIMARY KEY, filename TEXT NOT NULL, captured_at TEXT NOT NULL,
            kind TEXT NOT NULL, phash TEXT, status TEXT NOT NULL, note TEXT,
            destination TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT NOT NULL, old_status TEXT,
            new_status TEXT NOT NULL, reason TEXT, changed_at TEXT NOT NULL);
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            finished_at TEXT, state TEXT NOT NULL DEFAULT 'running', window TEXT,
            examined INTEGER NOT NULL DEFAULT 0, recorded INTEGER NOT NULL DEFAULT 0,
            note TEXT);
    """)
    con.execute("INSERT INTO assets (uuid,filename,captured_at,kind,phash,status,"
                "first_seen,updated_at) VALUES ('L/L0/001','A.HEIC','2026-03-09T08:00:00',"
                "'photo','aa','seen','2026-03-09','2026-03-09')")
    con.commit(); con.close()

    with L.Ledger(path) as led:
        r = led.conn.execute(
            "SELECT plain_uuid, filename, captured_at FROM assets").fetchone()
        assert r[0] is None and r[1] is None, "identity should be gone"
        assert r[2] == "2026-03-09T08:00:00", "capture time must survive the migration"
        assert led.coverage() == [("2026-03", 1)]


def test_migration_drops_the_plaintext_library_cache(tmp_path):
    # [codex P1] asset_cache is a snapshot of the LIBRARY with raw ids and filenames.
    # Migrating without clearing it left the "opaque" ledger a fully readable photo
    # inventory, defeating the entire retention rule.
    with L.Ledger(tmp_path / "l.db") as led:
        led.cache_assets([("X/L0/001", "SECRET.HEIC", "2026-01-01", False, 10, 10)])
        assert led.cache_size() == 1
        led.conn.execute("UPDATE assets SET uuid='raw/L0/001' WHERE 1=0")
    # force the migration path to run again on the same file
    with L.Ledger(tmp_path / "l.db") as led:
        led.record(uuid="raw/L0/001", filename="B.HEIC", captured_at="2026-01-01T00:00:00",
                   kind="photo", status=L.SEEN)
        led.conn.execute("UPDATE assets SET uuid='raw/L0/001' WHERE uuid!='raw/L0/001'")
        led.conn.commit()
    with L.Ledger(tmp_path / "l.db") as led:
        assert led.cache_size() == 0, "migration must not carry a plaintext inventory"


def test_null_ledger_satisfies_every_call_the_pipeline_makes(tmp_path):
    # [codex P1] review.py calls ledger.digest() per asset. _NullLedger lacked it, so
    # `review --dry-run` on a fresh machine died with AttributeError before printing.
    from photos_to_posts.review import _NullLedger
    import inspect
    n = _NullLedger()
    assert n.digest("a/L0/001") == "a/L0/001"
    src = inspect.getsource(R)
    called = {m for m in ("digest", "settled_uuids", "uuids_with_status", "all_hashes",
                          "record", "start_run") if f"ledger.{m}(" in src}
    missing = [m for m in called if not hasattr(n, m)]
    assert not missing, f"_NullLedger is missing {missing}"


def test_an_automated_exclusion_outranks_posted_deliberately(tmp_path):
    # [gemini] posted + an automated excluded_private lands on excluded_private. That is
    # INTENDED: a privacy filter must be able to hide something already published. The
    # publication event survives in its own table, so the history is not lost. Pinned so
    # the choice is deliberate rather than accidental.
    with L.Ledger(tmp_path / "l.db") as led:
        _rec_id(led, status=L.SEEN)
        led.set_status("U/L0/001", L.POSTED, destination="instagram",
                       filename="IMG_1.HEIC")
        assert _rec_id(led, status=L.EXCLUDED_PRIVATE) == L.EXCLUDED_PRIVATE
        pubs = led.publications()
        assert len(pubs) == 1 and pubs[0][1] == "instagram", "history must survive"


def test_report_shows_the_retained_identity_not_the_digest(tmp_path):
    # [codex P2] publications() returned the digest even when plain_uuid was retained,
    # so `report` printed an opaque hash for an asset whose identity was kept on purpose.
    with L.Ledger(tmp_path / "l.db") as led:
        _rec_id(led, status=L.SEEN)
        led.set_status("U/L0/001", L.POSTED, destination="facebook",
                       filename="IMG_1.HEIC")
        uuid, dest, _when, fname = led.publications()[0]
        assert uuid == "U/L0/001", f"report would print {uuid}"
        assert fname == "IMG_1.HEIC"


def test_setting_status_by_digest_does_not_erase_identity(tmp_path):
    # [codex P2] A digest identifies the row but cannot rebuild the identity, so
    # treating it as "not legible" wiped an audit trail that could not be restored.
    with L.Ledger(tmp_path / "l.db") as led:
        _rec_id(led, status=L.SEEN)
        led.set_status("U/L0/001", L.POSTED, filename="IMG_1.HEIC")
        d = led.digest("U/L0/001")
        led.set_status(d, L.EXCLUDED_PRIVATE, note="second thoughts")
        r = led.conn.execute(
            "SELECT status, plain_uuid, filename FROM assets").fetchone()
        assert r[0] == L.EXCLUDED_PRIVATE
        assert r[1] == "U/L0/001" and r[2] == "IMG_1.HEIC", "identity was erased"


def test_identity_contract_holds_for_every_status(tmp_path):
    # My own gap: mutating _write's identity assignment back to COALESCE did not fail
    # any test, because STATUS_RANK already prevents the demotion that would expose it.
    # The two fixes overlap, so pin the _identity contract directly instead.
    with L.Ledger(tmp_path / "l.db") as led:
        for status in (L.SEEN, L.SHORTLISTED, L.POSTED, L.EXCLUDED_JUNK,
                       L.EXCLUDED_PRIVATE):
            _key, plain, fname, cap = led._identity(
                "Z/L0/001", "Z.HEIC", "2026-04-04T00:00:00", status)
            assert cap == "2026-04-04T00:00:00", f"{status} must keep capture time"
            if status in L.LEGIBLE_STATUSES:
                assert plain == "Z/L0/001" and fname == "Z.HEIC", status
            else:
                assert plain is None and fname is None, f"{status} leaked identity"


def test_a_published_photo_suppresses_its_near_neighbours(tmp_path):
    # REGRESSION, and Sameer caught it: two frames from one studio session sat 7 apart.
    # history_max_distance was 6, so the second was offered as a fresh candidate AFTER
    # the first had already been published. Meanwhile burst_max_distance was 12, so the
    # very same pair WOULD have collapsed into one moment inside a single run. The two
    # thresholds disagreed and the photo fell through the gap.
    cfg = Config()
    assert cfg.published_max_distance >= cfg.burst_max_distance, (
        "a pair that collapses into one moment within a run must also match across runs")

    led = L.Ledger(tmp_path / "l.db")
    a, b = 0x0000000000000000, 0x00000000000000FF     # distance 8: >6, <12
    assert L.hamming(a, b) == 8
    led.record(uuid="posted/L0/001", filename="A.HEIC", captured_at="2026-07-28T20:41:00",
               kind="photo", status=L.SEEN, phash=a)
    led.set_status("posted/L0/001", L.POSTED, destination="instagram", filename="A.HEIC")

    frame = imaging.Frame(uuid="new/L0/001", path=tmp_path / "b.jpg", filename="B.HEIC",
                          phash=b, captured_at=None)
    res = R.ReviewResult(window_days=30, total_in_library=1, in_window=1)
    asset = ps.Asset(uuid="new/L0/001", filename="B.HEIC",
                     captured_at=datetime(2026, 7, 28, 20, 42),
                     favorite=False, width=1080, height=1616)
    kept = R.screen_against_history([frame], led, cfg, res, {"new/L0/001": asset})
    assert kept == [], "a near-neighbour of a PUBLISHED photo was offered again"
    assert res.skipped_seen_before_visually == 1
    led.close()


def test_a_merely_seen_photo_keeps_the_tighter_radius(tmp_path):
    # The wider radius must apply to decisions only. Something glanced at should not
    # blanket-suppress everything within 12 of it, or real candidates go missing.
    cfg = Config()
    led = L.Ledger(tmp_path / "l.db")
    a, b = 0x0000000000000000, 0x00000000000000FF     # distance 8
    led.record(uuid="seen/L0/001", filename="A.HEIC", captured_at="2026-07-28T20:41:00",
               kind="photo", status=L.SEEN, phash=a)
    frame = imaging.Frame(uuid="new/L0/001", path=tmp_path / "b.jpg", filename="B.HEIC",
                          phash=b, captured_at=None)
    res = R.ReviewResult(window_days=30, total_in_library=1, in_window=1)
    asset = ps.Asset(uuid="new/L0/001", filename="B.HEIC",
                     captured_at=datetime(2026, 7, 28, 20, 42),
                     favorite=False, width=1080, height=1616)
    kept = R.screen_against_history([frame], led, cfg, res, {"new/L0/001": asset})
    assert len(kept) == 1, "distance 8 from a merely-seen photo should still surface"
    led.close()


def test_a_padded_cover_is_not_grid_safe(tmp_path):
    # REGRESSION, and the first version of BOTH the check and this test was wrong.
    #
    # Sameer spotted on his own grid that a landscape selfie padded into 4:5 keeps the
    # whole frame in the POST and puts bars through the middle of the SQUARE grid tile.
    # 44% of the cover that shipped was padding.
    #
    # The first check measured colour spread per row, and this test padded with SOLID
    # grey. It passed. On the real file, whose padding is a BLURRED copy of the photo
    # and therefore full of colour, the check reported 0% and called it grid-safe.
    # So the padding here MUST be blurred, not flat, or the test lies again.
    pytest.importorskip("numpy")
    from PIL import Image, ImageFilter
    import numpy as np
    rng = np.random.default_rng(1)
    photo = Image.fromarray(rng.integers(0, 255, (600, 1080, 3)).astype("uint8"))
    blurred_bg = photo.resize((1080, 1350), Image.LANCZOS).filter(ImageFilter.GaussianBlur(28))
    padded = blurred_bg.copy()
    padded.paste(photo, (0, (1350 - 600) // 2))
    p = tmp_path / "padded.jpg"; padded.save(p, quality=95)

    frac = imaging.cover_padding_fraction(p)
    assert frac > 0.25, f"blurred letterboxing not detected, got {frac:.2f}"
    assert not imaging.is_grid_safe(p)

    full = Image.fromarray(rng.integers(0, 255, (1350, 1080, 3)).astype("uint8"))
    q = tmp_path / "full.jpg"; full.save(q, quality=95)
    assert imaging.cover_padding_fraction(q) < 0.05
    assert imaging.is_grid_safe(q), "a full-bleed cover should pass"


def test_a_smooth_background_is_not_mistaken_for_padding(tmp_path):
    # [codex+gemini] A global low-detail threshold condemns any photograph with a plain
    # wall or a clear sky: the textured rows set the median and every smooth row counts
    # as a bar. Measured at 0.35 on a wall that occupies a third of the frame.
    # Letterboxing is distinguished by being contiguous AND anchored to BOTH edges.
    pytest.importorskip("numpy")
    from PIL import Image
    import numpy as np
    rng = np.random.default_rng(2)
    # The subject must be PHOTOGRAPH-like, not white noise. Pure noise against a smooth
    # wall creates a detail ratio no real photo has (37 vs 1), which makes any relative
    # threshold fire. On the real studio portrait the wall reads 1.99 against a median
    # of 6.13, a ratio of 0.32, comfortably above the 0.25 line.
    from PIL import ImageFilter
    wall = np.random.default_rng(3).normal(226, 6.0, (1350, 1080, 3))
    img = np.clip(wall, 0, 255).astype("uint8")
    subject = Image.fromarray(rng.integers(0, 255, (700, 700, 3)).astype("uint8"))
    subject = subject.filter(ImageFilter.GaussianBlur(2))          # photo-like, not noise
    img[500:1200, 200:900] = np.asarray(subject)
    p = tmp_path / "portrait.jpg"; Image.fromarray(img).save(p, quality=95)
    assert imaging.is_grid_safe(p), (
        f"a plain backdrop was called padding: {imaging.cover_padding_fraction(p):.2f}")

    # a sky along the TOP only is not letterboxing either
    sky = rng.integers(0, 255, (1350, 1080, 3)).astype("uint8")
    sky[:400] = np.clip(np.random.default_rng(4).normal(180, 2, (400, 1080, 3)),
                        0, 255).astype("uint8")
    q = tmp_path / "sky.jpg"; Image.fromarray(sky).save(q, quality=95)
    assert imaging.is_grid_safe(q), (
        f"a sky was called padding: {imaging.cover_padding_fraction(q):.2f}")


def test_grid_tile_is_the_centre_square(tmp_path):
    # [codex+gemini] The first version only cropped vertically, so a LANDSCAPE image came
    # back unchanged and every measurement taken from it described the whole frame rather
    # than the tile. The portrait-only assertion passed throughout.
    from PIL import Image
    for size in ((1080, 1350), (1600, 900), (900, 900)):
        t = imaging.grid_tile(Image.new("RGB", size, "white"))
        side = min(size)
        assert t.size == (side, side), f"{size} -> {t.size}"


def test_review_announces_when_face_detection_is_off(monkeypatch, tmp_path):
    # Silent degradation is what let "face-aware" be false for months. The run must say so.
    from photos_to_posts import faces as F
    monkeypatch.setattr(F, "available", lambda: False)
    said: list[str] = []
    monkeypatch.setattr(R.ps, "is_available", lambda: (False, "stub"))
    try:
        R.run_review(Config(), days=1, log=said.append)
    except Exception:
        pass
    # is_available False short-circuits before the notice, so assert the ordering holds
    monkeypatch.setattr(R.ps, "is_available", lambda: (True, ""))
    monkeypatch.setattr(R.ps, "fetch_all_assets", lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    said.clear()
    try:
        R.run_review(Config(), days=1, log=said.append)
    except RuntimeError:
        pass
    assert any("face detection unavailable" in m for m in said), said


def test_purge_keeps_manifests_by_default(tmp_path, monkeypatch, capsys):
    # REGRESSION: purge did rmtree on the whole workspace. Used for disk space on
    # 17 Aug 2026 it destroyed the manifests holding the filenames of a POSTED and a
    # SHORTLISTED asset. Pixels rebuild; a manifest does not.
    from photos_to_posts import cli
    ws = tmp_path / "work"
    run = ws / "run_00001"
    (run / "export" / "a").mkdir(parents=True)
    (run / "export" / "a" / "p.jpeg").write_bytes(b"x" * 2048)
    (run / "thumbs").mkdir(); (run / "thumbs" / "t.jpg").write_bytes(b"y" * 512)
    (run / "sheets").mkdir(); (run / "sheets" / "s.jpg").write_bytes(b"z" * 512)
    (run / "manifest.csv").write_text("index,uuid,filename\n1,A/L0/001,IMG_1.HEIC\n")

    cfg = Config(); cfg.workspace = ws
    monkeypatch.setattr(cli, "load_config", lambda _p=None: cfg)
    rc = cli.cmd_purge(argparse.Namespace(config=None, yes=True, all=False))
    assert rc == 0
    assert (run / "manifest.csv").exists(), "the manifest was destroyed"
    assert not (run / "export").exists() and not (run / "thumbs").exists()

    rc = cli.cmd_purge(argparse.Namespace(config=None, yes=True, all=True))
    assert rc == 0 and not ws.exists(), "--all should remove everything"


def test_published_radius_follows_history_when_not_set(tmp_path):
    # [gemini] Raising history_max_distance alone used to make load_config RAISE, because
    # published defaulted to 12 and was then below it. A perfectly valid config rejected.
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("[review]\nhistory_max_distance = 15\n")
    cfg = load_config(str(cfg_file))
    assert cfg.history_max_distance == 15
    assert cfg.published_max_distance >= 15, "published must follow history when unset"

    cfg_file.write_text("[review]\nhistory_max_distance = 8\npublished_max_distance = 4\n")
    with pytest.raises(ValueError, match="published_max_distance"):
        load_config(str(cfg_file))


def test_purge_does_not_follow_a_symlink(tmp_path, monkeypatch):
    # [gemini] shutil.rmtree refuses a symlink outright, so purge crashed on one. Worse,
    # following it would delete outside the workspace entirely.
    from photos_to_posts import cli
    outside = tmp_path / "precious"; outside.mkdir()
    (outside / "keep.txt").write_text("do not delete me")
    ws = tmp_path / "work"; run = ws / "run_00001"; run.mkdir(parents=True)
    (run / "manifest.csv").write_text("index,uuid\n")
    (run / "export").symlink_to(outside, target_is_directory=True)

    cfg = Config(); cfg.workspace = ws
    monkeypatch.setattr(cli, "load_config", lambda _p=None: cfg)
    rc = cli.cmd_purge(argparse.Namespace(config=None, yes=True, all=False))
    assert rc == 0
    assert outside.exists() and (outside / "keep.txt").exists(), "purge followed a symlink"
    assert not (run / "export").exists(), "the symlink itself should be gone"
    assert (run / "manifest.csv").exists()
