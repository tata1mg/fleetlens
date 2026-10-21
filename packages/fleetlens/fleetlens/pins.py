"""Pinned snapshots of the index, so an agent evaluation can be repeated against the same data.

The live index is rebuilt and swapped on a schedule. An evaluation that runs for days, or
that has to be compared with a later run, needs the data to hold still. A pin is a named,
expiring, consistent copy of the index taken at one moment.

Storage is a single directory. Each pin is `<alias>.db` (the snapshot) and `<alias>.json`
(a sidecar saying what it is and when it expires). There is no central registry file, so
there is nothing to corrupt, and the expiry collector is a plain directory scan.

Expiry is controlled by the server, never by the caller. A pin that its creator could keep
forever would turn a convenience into unbounded disk use, so creation takes no duration and
an extension is capped per call. This module is stdlib only: it is unit-testable without
starlette, and the HTTP layer in server/admin.py is a thin wrapper over it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

#: Lowercase, URL-safe, and it cannot start with a dot or contain a separator. The alias is
#: used as a file name and as a URL path segment, so this is what makes escaping the pins
#: directory impossible rather than merely checked for.
#: Always used with fullmatch: `$` also matches before a trailing newline, which would let
#: "abc\n" through and create a file with a newline in its name.
ALIAS_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")

DEFAULT_RETAIN_DAYS = 30
DEFAULT_EXTEND_MAX_DAYS = 10

#: An orphan snapshot with no sidecar may be a create still in flight, so it is only
#: collected once it is clearly abandoned.
_ORPHAN_GRACE_SECONDS = 3600

# One create at a time. Two concurrent creates of the same alias would otherwise both pass
# the "does it exist" check; and VACUUM INTO of a large index is heavy enough that running
# several at once is not something to encourage.
_CREATE_LOCK = threading.Lock()


class PinError(Exception):
    """A refusal the caller should see, with the HTTP status that describes it."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise SystemExit(f"fl serve: ${name} must be a positive whole number, got {raw!r}.")
    return value


@dataclass
class PinPolicy:
    pins_dir: Path
    retain_days: int = DEFAULT_RETAIN_DAYS
    extend_max_days: int = DEFAULT_EXTEND_MAX_DAYS

    @classmethod
    def from_env(cls, pins_dir) -> PinPolicy:
        """Read the two retention settings, failing at start rather than on first use."""
        return cls(Path(pins_dir),
                   _positive_int_env("FLEETLENS_RETAIN_PINNED_DAYS", DEFAULT_RETAIN_DAYS),
                   _positive_int_env("FLEETLENS_PIN_EXTEND_MAX_DAYS", DEFAULT_EXTEND_MAX_DAYS))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _parse(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


class PinStore:
    """Create, list, extend, delete and collect pins under one directory.

    `clock` returns seconds since the epoch and exists so tests can move time without
    sleeping.
    """

    def __init__(self, policy: PinPolicy, clock=time.time, live_db=None):
        self.policy = policy
        self._clock = clock
        # The collector must never touch the live index, so the store is told where it is
        # rather than trusting the caller to keep the two directories apart.
        self._live_db = Path(live_db).resolve() if live_db else None

    # --- paths ------------------------------------------------------------------
    def _db(self, alias: str) -> Path:
        return self.policy.pins_dir / f"{alias}.db"

    def _meta(self, alias: str) -> Path:
        return self.policy.pins_dir / f"{alias}.json"

    @staticmethod
    def _check_alias(alias) -> str:
        if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
            raise PinError(400, "alias must be 1 to 63 characters of lowercase letters, "
                                "digits, '.', '_' or '-', starting with a letter or digit")
        return alias

    def _read(self, alias: str) -> dict:
        try:
            return json.loads(self._meta(alias).read_text())
        except FileNotFoundError:
            raise PinError(404, f"no pin named {alias!r}") from None
        except ValueError:
            raise PinError(500, f"pin {alias!r} has an unreadable record") from None

    def _read_valid(self, alias: str) -> dict:
        record = self._read(alias)
        if not isinstance(record, dict) or not isinstance(record.get("expires_at"), str):
            raise PinError(500, f"pin {alias!r} has an unreadable record")
        return record

    def _write(self, alias: str, record: dict) -> None:
        # temp file then rename, so a reader never sees half a sidecar
        tmp = self._meta(alias).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2))
        os.replace(tmp, self._meta(alias))

    def _with_days_left(self, record: dict) -> dict:
        left = (_parse(record["expires_at"]) - self._clock()) / 86400
        return {**record, "days_left": max(0, int(left + 0.999999))}

    # --- operations -------------------------------------------------------------
    def create(self, source_db, alias, reason: str = "", owner: str = "") -> dict:
        """Snapshot the live index under `alias`.

        The snapshot is taken with `VACUUM INTO`, not by copying the file and not by a
        hardlink. A copy of a database that a build is writing can capture a torn state.
        VACUUM INTO reads through SQLite, so it sees one consistent transaction, writes a
        compacted file, and needs only read access to the source. A hardlink is worse
        than a copy: `fl index` and `fl enrich` write the database in place, so the "pin"
        would silently absorb every later edit and stop being a snapshot at all.
        """
        self._check_alias(alias)
        if sqlite3.sqlite_version_info < (3, 27, 0):
            raise PinError(501, "pinning needs SQLite 3.27 or newer for VACUUM INTO; this "
                                f"server has {sqlite3.sqlite_version}")
        self.policy.pins_dir.mkdir(parents=True, exist_ok=True)
        final, tmp = self._db(alias), self._db(alias).with_suffix(".db.tmp")
        with _CREATE_LOCK:
            if final.exists() or self._meta(alias).exists():
                raise PinError(409, f"a pin named {alias!r} already exists")
            # as_uri() escapes ?, # and % in the path; interpolating the raw path would not
            uri = Path(source_db).resolve().as_uri() + "?mode=ro"
            src = sqlite3.connect(uri, uri=True)
            try:
                try:
                    row = src.execute("SELECT value FROM meta WHERE key = 'build_id'").fetchone()
                except sqlite3.OperationalError:
                    row = None
                if not row:
                    raise PinError(409, "the live index has no build id, so a pin could not "
                                        "say what it holds. Reindex with this version of "
                                        "fleetlens, then pin again.")
                build_id = row[0]
                tmp.unlink(missing_ok=True)
                src.execute("VACUUM INTO ?", (str(tmp),))
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            finally:
                src.close()
            now = self._clock()
            record = {"alias": alias, "build_id": build_id,
                      "reason": (reason or "")[:500], "owner": (owner or "")[:100],
                      "created_at": _iso(now),
                      "expires_at": _iso(now + self.policy.retain_days * 86400)}
            try:
                # Snapshot first, sidecar second: a crash in between leaves an orphan
                # snapshot the collector removes, never a record pointing at nothing.
                os.replace(tmp, final)
                self._write(alias, record)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        return self._with_days_left(record)

    def list(self) -> list:
        pins = []
        for path in sorted(self.policy.pins_dir.glob("*.json")):
            # One bad sidecar must not take the listing down for every caller, so all of
            # the validation and the sort key happen inside the try.
            try:
                record = json.loads(path.read_text())
                if not isinstance(record, dict):
                    raise ValueError("record is not an object")
                if not all(isinstance(record.get(k), str) for k in ("created_at", "expires_at")):
                    raise ValueError("record lacks created_at or expires_at")
                pins.append((record["created_at"], self._with_days_left(record)))
            except (ValueError, KeyError, TypeError, OSError):
                log.warning("skipping unreadable pin record %s", path)
        return [r for _, r in sorted(pins, key=lambda t: t[0])]

    def extend(self, alias, days) -> dict:
        """Add `days` to the pin's current expiry. The cap applies to each call."""
        self._check_alias(alias)
        cap = self.policy.extend_max_days
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= cap:
            raise PinError(422, f"days must be a whole number from 1 to {cap}")
        record = self._read_valid(alias)
        try:
            expires = datetime.fromisoformat(record["expires_at"]) + timedelta(days=days)
        except ValueError:
            raise PinError(500, f"pin {alias!r} has an unreadable record") from None
        record["expires_at"] = expires.isoformat(timespec="seconds")
        self._write(alias, record)
        return self._with_days_left(record)

    def delete(self, alias) -> None:
        self._check_alias(alias)
        # Existence of the sidecar is enough: a pin whose record is corrupt must still be
        # removable through the API, otherwise only a person with shell access can clear it.
        if not self._meta(alias).exists():
            raise PinError(404, f"no pin named {alias!r}")
        self._db(alias).unlink(missing_ok=True)
        self._meta(alias).unlink(missing_ok=True)

    def collect(self) -> list:
        """Remove expired pins and abandoned partial files. Returns the aliases removed.

        It deletes only what it can positively identify as expired or abandoned. A sidecar
        it cannot parse is left alone with a warning: guessing that unreadable means
        expired would let a corrupted file destroy a snapshot someone is relying on.
        """
        d = self.policy.pins_dir
        if not d.is_dir():
            return []
        now, removed = self._clock(), []
        for meta in sorted(d.glob("*.json")):
            try:
                record = json.loads(meta.read_text())
                expires = _parse(record["expires_at"])
            except (ValueError, KeyError, TypeError, OSError):
                log.warning("pin record %s is unreadable; leaving it and its snapshot", meta)
                continue
            if expires > now:
                continue
            alias = meta.stem
            (d / f"{alias}.db").unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            log.info("removed expired pin %s (build %s, expired %s)",
                     alias, record.get("build_id"), record["expires_at"])
            removed.append(alias)
        for stray in [*d.glob("*.db"), *d.glob("*.tmp")]:
            # Only remove what can be attributed to a pin. The directory may be shared with
            # something else, and the live index must never be a candidate.
            if self._live_db is not None and stray.resolve() == self._live_db:
                continue
            # stem, not a split on the first dot: aliases may contain dots
            if stray.suffix == ".db":
                if not ALIAS_RE.fullmatch(stray.stem) or (d / f"{stray.stem}.json").exists():
                    continue
            elif (not stray.name.endswith((".db.tmp", ".json.tmp"))
                  or not ALIAS_RE.fullmatch(Path(stray.stem).stem)):
                continue    # a .tmp this module did not create (`<alias>.db.tmp`, `.json.tmp`)
            try:
                age = now - stray.stat().st_mtime
            except OSError:
                continue
            if age > _ORPHAN_GRACE_SECONDS:
                stray.unlink(missing_ok=True)
                log.info("removed orphan snapshot file %s", stray.name)
                removed.append(stray.name)
        return removed


def check_pins_dir(db_path, pins_dir) -> None:
    """Refuse a pins directory that holds the live index.

    The collector scans the whole directory, so sharing it with the live database puts that
    database in reach of the sweep. Failing at start is cheaper than losing the index.
    """
    db, d = Path(db_path).resolve(), Path(pins_dir).resolve()
    if d == db.parent or d in db.parents:
        raise SystemExit(f"fl serve: --pins-dir {d} contains the live index {db}. "
                         "Use a separate directory for pins.")
