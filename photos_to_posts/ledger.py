"""Persistent memory of which assets have already been looked at.

The point of this file is that going back through a camera roll is a repeated
activity. Without a ledger, every pass over "the last six months" re-surfaces the
same rejects, and the same near-identical burst frames, forever.

Two layers of recall:

1. **Exact** - the Photos UUID. Stable across renames, edits and re-exports.
2. **Visual** - a 64-bit perceptual hash. Catches the case where the *same scene*
   shows up again as a different asset: a re-export, a duplicate import, a burst
   frame that was never reviewed, or the same photo shared back from someone else.

Privacy invariant, enforced here rather than by convention: once an asset is
marked private or junk, an automated pass can never undo that. Only an explicit
:meth:`Ledger.set_status` call, which is a deliberate human action, can.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

SEEN = "seen"                    # reviewed, no action taken
SHORTLISTED = "shortlisted"      # picked as a candidate but not published
POSTED = "posted"                # actually went out
EXCLUDED_PRIVATE = "excluded_private"   # deliberately withheld (family, home, sensitive)
EXCLUDED_JUNK = "excluded_junk"         # screenshots, blurry, dark, receipts

ALL_STATUSES = (SEEN, SHORTLISTED, POSTED, EXCLUDED_PRIVATE, EXCLUDED_JUNK)

# Terminal for automated writes. `record()` will never move an asset out of one of
# these; only an explicit `set_status()` can. This is the privacy guarantee: a
# later scan cannot un-exclude a photo you deliberately withheld.
TERMINAL = frozenset({EXCLUDED_PRIVATE, EXCLUDED_JUNK})

# Statuses that mean "do not bring this back to me again by default".
#
# SEEN is deliberately in this set. An asset that was reviewed and not chosen is a
# decision, and re-surfacing it on every subsequent pass over the same months is
# the exact failure this ledger exists to prevent. Pass `resurface_seen=True` to
# take a second look.
# SHORTLISTED is deliberately absent: a pick that was never published is still an
# open decision, so it should come back until it is resolved either way.
SETTLED = (SEEN, POSTED, EXCLUDED_PRIVATE, EXCLUDED_JUNK)
SETTLED_STRICT = (POSTED, EXCLUDED_PRIVATE, EXCLUDED_JUNK)

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    uuid         TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    phash        TEXT,
    status       TEXT NOT NULL,
    note         TEXT,
    destination  TEXT,
    first_seen   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_status  ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_capture ON assets(captured_at);

CREATE TABLE IF NOT EXISTS status_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid       TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    reason     TEXT,
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_uuid ON status_history(uuid);

-- One row per actual publication. `assets.destination` holds only the most recent,
-- so posting the same photo to two platforms used to silently overwrite the first and
-- lose its timestamp. This is the durable record.
CREATE TABLE IF NOT EXISTS publications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid         TEXT NOT NULL,
    destination  TEXT NOT NULL,
    published_at TEXT NOT NULL,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_pub_uuid ON publications(uuid);
CREATE INDEX IF NOT EXISTS idx_pub_dest ON publications(destination);

-- Cache of library metadata. fetch_all_assets re-parses ~50k AppleScript records on
-- every run, which is about a minute of wall clock before anything useful happens.
CREATE TABLE IF NOT EXISTS asset_cache (
    uuid        TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    favorite    INTEGER NOT NULL DEFAULT 0,
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0,
    cached_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    state       TEXT NOT NULL DEFAULT 'running',
    window      TEXT,
    examined    INTEGER NOT NULL DEFAULT 0,
    recorded    INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode_phash(value: int | None) -> str | None:
    """Store hashes as fixed-width hex.

    A dHash is an *unsigned* 64-bit value, but SQLite integers are signed 64-bit
    and Python's sqlite3 adapter raises OverflowError above 2**63 - 1. That would
    reject roughly half of all real hashes, so hex text it is.

    The bounds check keeps the column fixed-width: a value outside 64 bits would
    format to a different length and silently break distance comparisons.
    """
    if value is None:
        return None
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"phash must fit in unsigned 64 bits, got {value}")
    return f"{value:016x}"


def decode_phash(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return int(value, 16)


@dataclass(frozen=True)
class Record:
    uuid: str
    filename: str
    captured_at: str
    kind: str
    phash: int | None
    status: str
    note: str | None
    destination: str | None


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class Ledger:
    """SQLite-backed record of reviewed assets.

    The database file and its directory are created with owner-only permissions:
    it holds filenames, capture times and review decisions about someone's
    personal photographs.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        # Applied unconditionally, not just on creation: a ledger created before
        # this rule existed, or copied in, must still end up owner-only.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # ---- writes -----------------------------------------------------------

    def record(self, *, uuid: str, filename: str, captured_at: datetime | str, kind: str,
               status: str = SEEN, phash: int | None = None, note: str | None = None,
               destination: str | None = None, reason: str | None = None) -> str:
        """Insert or update one asset from an automated pass.

        Returns the status the asset ended up with, which is not always the one
        passed in: an asset already in a :data:`TERMINAL` state keeps it.
        """
        if status not in ALL_STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {ALL_STATUSES}")
        cap = captured_at.isoformat() if isinstance(captured_at, datetime) else str(captured_at)
        now = _now()
        hexed = encode_phash(phash)

        with self.conn:  # single transaction, and commits once
            return self._write(self.conn.cursor(), uuid, filename, cap, kind, status,
                               hexed, note, destination, reason, now)

    def _write(self, cur, uuid, filename, cap, kind, status, hexed, note, destination,
               reason, now) -> str:
        """Row-level write. Caller owns the transaction."""
        row = cur.execute("SELECT status FROM assets WHERE uuid = ?", (uuid,)).fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO assets (uuid, filename, captured_at, kind, phash, status,"
                " note, destination, first_seen, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uuid, filename, cap, kind, hexed, status, note, destination, now, now))
            cur.execute(
                "INSERT INTO status_history (uuid, old_status, new_status, reason, changed_at)"
                " VALUES (?,?,?,?,?)", (uuid, None, status, reason, now))
            return status

        current = row["status"]
        # Terminal states absorb automated writes. This is the privacy rule.
        final = current if current in TERMINAL else status
        cur.execute(
            "UPDATE assets SET filename=?, captured_at=?, kind=?, status=?,"
            " phash=COALESCE(?, phash), note=COALESCE(?, note),"
            " destination=COALESCE(?, destination), updated_at=? WHERE uuid=?",
            (filename, cap, kind, final, hexed, note, destination, now, uuid))
        if final != current:
            cur.execute(
                "INSERT INTO status_history (uuid, old_status, new_status, reason, changed_at)"
                " VALUES (?,?,?,?,?)", (uuid, current, final, reason, now))
        return final

    def record_many(self, rows: Iterable[dict]) -> int:
        """Insert or update many assets in a single transaction.

        One commit for the batch rather than one per row: at camera-roll scale a
        commit per asset is both slow and non-atomic, so a crash mid-batch would
        leave the ledger half-written.
        """
        n = 0
        now = _now()
        with self.conn:
            cur = self.conn.cursor()
            for r in rows:
                status = r.get("status", SEEN)
                if status not in ALL_STATUSES:
                    raise ValueError(f"unknown status {status!r}")
                cap = r["captured_at"]
                cap = cap.isoformat() if isinstance(cap, datetime) else str(cap)
                self._write(cur, r["uuid"], r["filename"], cap, r["kind"], status,
                            encode_phash(r.get("phash")), r.get("note"),
                            r.get("destination"), r.get("reason"), now)
                n += 1
        return n

    def set_status(self, uuid: str, status: str, *, note: str | None = None,
                   destination: str | None = None, reason: str = "explicit") -> bool:
        """Force a status, including out of a terminal state.

        This is the only way to un-exclude something, and it is deliberately not
        reachable from the automated pipeline. Returns False if the uuid is unknown.
        """
        if status not in ALL_STATUSES:
            raise ValueError(f"unknown status {status!r}")
        now = _now()
        with self.conn:
            cur = self.conn.cursor()
            row = cur.execute("SELECT status FROM assets WHERE uuid=?", (uuid,)).fetchone()
            if row is None:
                return False
            cur.execute(
                "UPDATE assets SET status=?, note=COALESCE(?, note),"
                " destination=COALESCE(?, destination), updated_at=? WHERE uuid=?",
                (status, note, destination, now, uuid))
            cur.execute(
                "INSERT INTO status_history (uuid, old_status, new_status, reason, changed_at)"
                " VALUES (?,?,?,?,?)", (uuid, row["status"], status, reason, now))
            if status == POSTED and destination:
                # Appended, never overwritten: the same photo may go to several
                # platforms and each one is a separate event with its own timestamp.
                cur.execute(
                    "INSERT INTO publications (uuid, destination, published_at, note)"
                    " VALUES (?,?,?,?)", (uuid, destination, now, note))
        return True

    def record_publication(self, uuid: str, destination: str,
                           note: str | None = None) -> None:
        """Append a publication event. Multiple destinations per asset are expected."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO publications (uuid, destination, published_at, note)"
                " VALUES (?,?,?,?)", (uuid, destination, _now(), note))

    def publications(self, limit: int = 200) -> list[tuple[str, str, str, str | None]]:
        """(uuid, destination, published_at, filename), most recent first."""
        with closing(self.conn.cursor()) as cur:
            return [(r[0], r[1], r[2], r[3]) for r in cur.execute(
                "SELECT p.uuid, p.destination, p.published_at, a.filename"
                " FROM publications p LEFT JOIN assets a ON a.uuid = p.uuid"
                " ORDER BY p.published_at DESC LIMIT ?", (limit,))]

    # ---- library metadata cache -------------------------------------------

    def cache_assets(self, rows: Iterable[tuple[str, str, str, bool, int, int]]) -> int:
        """Replace the cached library snapshot. Rows are (uuid, filename, captured_at,
        favorite, width, height)."""
        now = _now()
        n = 0
        with self.conn:
            self.conn.execute("DELETE FROM asset_cache")
            for uuid, filename, captured, fav, w, h in rows:
                self.conn.execute(
                    "INSERT OR REPLACE INTO asset_cache"
                    " (uuid, filename, captured_at, favorite, width, height, cached_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (uuid, filename, captured, 1 if fav else 0, w, h, now))
                n += 1
        return n

    def cached_assets(self) -> list[tuple[str, str, str, bool, int, int]]:
        with closing(self.conn.cursor()) as cur:
            return [(r[0], r[1], r[2], bool(r[3]), r[4], r[5]) for r in cur.execute(
                "SELECT uuid, filename, captured_at, favorite, width, height"
                " FROM asset_cache")]

    def cache_size(self) -> int:
        with closing(self.conn.cursor()) as cur:
            return int(cur.execute("SELECT COUNT(*) FROM asset_cache").fetchone()[0])

    def clear_cache(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM asset_cache")

    def start_run(self, window: str | None, note: str | None = None) -> int:
        """Begin a run, first reconciling any that were killed rather than finished.

        A process terminated by SIGKILL, a closed laptop, or a harness timeout cannot
        run its own cleanup, so its row would otherwise sit at 'running' forever and
        quietly corrupt any report built from run state.
        """
        with self.conn:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE runs SET state='interrupted', finished_at=?"
                " WHERE state='running'", (_now(),))
            cur.execute("INSERT INTO runs (started_at, window, note) VALUES (?,?,?)",
                        (_now(), window, note))
            return int(cur.lastrowid)

    def stale_runs(self) -> list[int]:
        with closing(self.conn.cursor()) as cur:
            return [r[0] for r in cur.execute(
                "SELECT id FROM runs WHERE state='running'")]

    def finish_run(self, run_id: int, examined: int, recorded: int,
                   state: str = "completed") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET examined=?, recorded=?, state=?, finished_at=? WHERE id=?",
                (examined, recorded, state, _now(), run_id))

    # ---- reads ------------------------------------------------------------

    def known_uuids(self) -> set[str]:
        with closing(self.conn.cursor()) as cur:
            return {r[0] for r in cur.execute("SELECT uuid FROM assets")}

    def settled_uuids(self, *, include_seen: bool = True) -> set[str]:
        """Assets that should not be resurfaced by default."""
        statuses = SETTLED if include_seen else SETTLED_STRICT
        q = ",".join("?" * len(statuses))
        with closing(self.conn.cursor()) as cur:
            return {r[0] for r in cur.execute(
                f"SELECT uuid FROM assets WHERE status IN ({q})", statuses)}

    def uuids_with_status(self, statuses: Sequence[str]) -> set[str]:
        q = ",".join("?" * len(statuses))
        with closing(self.conn.cursor()) as cur:
            return {r[0] for r in cur.execute(
                f"SELECT uuid FROM assets WHERE status IN ({q})", tuple(statuses))}

    def get(self, uuid: str) -> Record | None:
        with closing(self.conn.cursor()) as cur:
            row = cur.execute(
                "SELECT uuid, filename, captured_at, kind, phash, status, note, destination"
                " FROM assets WHERE uuid=?", (uuid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["phash"] = decode_phash(d["phash"])
        return Record(**d)

    def all_hashes(self, statuses: Sequence[str] = SETTLED) -> list[tuple[str, int, str]]:
        """(uuid, phash, status) for settled assets that have a hash.

        Loaded once per run so the pipeline can screen a whole candidate set
        against history without a query per asset.
        """
        q = ",".join("?" * len(statuses))
        with closing(self.conn.cursor()) as cur:
            rows = cur.execute(
                f"SELECT uuid, phash, status FROM assets"
                f" WHERE phash IS NOT NULL AND status IN ({q})", tuple(statuses)).fetchall()
        return [(r["uuid"], decode_phash(r["phash"]), r["status"]) for r in rows]

    def near_duplicates(self, phash: int, *, max_distance: int = 8,
                        statuses: Sequence[str] = SETTLED) -> list[Record]:
        """Previously-settled assets that look like this one."""
        out: list[Record] = []
        for uuid, stored, _status in self.all_hashes(statuses):
            if hamming(stored, phash) <= max_distance:
                rec = self.get(uuid)
                if rec:
                    out.append(rec)
        return out

    def stats(self) -> dict[str, int]:
        with closing(self.conn.cursor()) as cur:
            rows = cur.execute("SELECT status, COUNT(*) c FROM assets GROUP BY status").fetchall()
            total = cur.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        out = {r["status"]: r["c"] for r in rows}
        out["total"] = total
        return out

    def coverage(self) -> list[tuple[str, int]]:
        """Assets reviewed per month, oldest first. Shows how far back you have got."""
        with closing(self.conn.cursor()) as cur:
            return [(r[0], r[1]) for r in cur.execute(
                "SELECT substr(captured_at,1,7) m, COUNT(*) FROM assets GROUP BY m ORDER BY m")]

    def history(self, uuid: str) -> list[tuple[str | None, str, str | None, str]]:
        with closing(self.conn.cursor()) as cur:
            return [(r["old_status"], r["new_status"], r["reason"], r["changed_at"])
                    for r in cur.execute(
                        "SELECT old_status, new_status, reason, changed_at FROM status_history"
                        " WHERE uuid=? ORDER BY id", (uuid,))]
