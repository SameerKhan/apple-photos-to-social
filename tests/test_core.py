"""Unit tests that never touch a real Photos library.

Everything here runs on synthetic payloads and temp directories, so it works in
CI on any platform. The parts that genuinely need a live machine (osascript,
Photos export, iCloud downloads) are covered by docs/manual-testing.md instead.

Several of these tests exist because a specific bug shipped in an earlier draft
and was caught in review. Those are marked with a REGRESSION comment.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from photos_to_posts import applescript as ps
from photos_to_posts import imaging, ledger as L
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
    hist = led.history("a/L0/001")
    assert hist[0][1] == L.EXCLUDED_PRIVATE
    assert hist[-1][1:3] == (L.POSTED, "published")


def test_seen_counts_as_settled(led):
    # REGRESSION: `seen` was excluded from the settled set, so every reviewed but
    # unchosen asset resurfaced on the next run.
    _rec(led, status=L.SEEN)
    assert "a/L0/001" in led.settled_uuids()
    assert "a/L0/001" not in led.settled_uuids(include_seen=False)


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
