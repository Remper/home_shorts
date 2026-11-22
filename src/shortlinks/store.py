"""SQLite link store.

Two tables: `links` (one row per code, ever) and `link_history` (one row per
change). A deleted link keeps its row with `deleted_at` set, so a code that
was once printed or shared is never handed out again with another target.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import sqlite3
import threading
import urllib.parse
from collections.abc import Iterable

SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE links (
            code        TEXT PRIMARY KEY,
            target      TEXT NOT NULL,
            created_by  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            deleted_at  TEXT
        )""",
        "CREATE INDEX links_live_target ON links (target) WHERE deleted_at IS NULL",
        """CREATE TABLE link_history (
            id      INTEGER PRIMARY KEY,
            code    TEXT NOT NULL,
            action  TEXT NOT NULL,
            target  TEXT NOT NULL,
            actor   TEXT NOT NULL,
            at      TEXT NOT NULL
        )""",
        "CREATE INDEX link_history_code ON link_history (code)",
    ],
}


@dataclasses.dataclass(frozen=True)
class Link:
    code: str
    target: str
    created_by: str
    created_at: str
    updated_at: str
    deleted_at: str | None = None

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None

    def to_export(self) -> dict:
        return {
            "code": self.code,
            "target": self.target,
            "createdBy": self.created_by,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "deletedAt": self.deleted_at,
        }

    @classmethod
    def from_export(cls, d: dict) -> Link:
        return cls(
            code=d["code"],
            target=d["target"],
            created_by=d["createdBy"],
            created_at=d["createdAt"],
            updated_at=d["updatedAt"],
            deleted_at=d.get("deletedAt"),
        )


@dataclasses.dataclass(frozen=True)
class HistoryEntry:
    action: str
    target: str
    actor: str
    at: str


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """Thread-safe wrapper around one SQLite connection.

    Traffic is a household's worth of scans; one connection behind a lock is
    simpler than a pool and never contends in practice.
    """

    def __init__(self, path: str, readonly: bool = False):
        """Open the store. `readonly` never creates or migrates anything,
        so an export run by another user (root, say) cannot leave files
        behind that the service then cannot write."""
        self._lock = threading.Lock()
        if readonly:
            uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro"
            self._db = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
        else:
            self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        if readonly:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"database schema is {version}, this program reads {SCHEMA_VERSION}")
            return
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._lock, self._tx():
            # Read the version inside the write transaction, so two processes
            # opening a new database at once cannot both run a migration.
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than this program ({SCHEMA_VERSION})"
                )
            for v in range(version + 1, SCHEMA_VERSION + 1):
                # One statement per list item rather than executescript(),
                # which commits first and would split the migration from its
                # version bump.
                for stmt in _MIGRATIONS[v]:
                    self._db.execute(stmt)
                self._db.execute(f"PRAGMA user_version={v}")

    # --- reads ---------------------------------------------------------

    def get(self, code: str) -> Link | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
        return _link(row) if row else None

    def resolve(self, code: str) -> str | None:
        link = self.get(code)
        if link is None or link.deleted:
            return None
        return link.target

    def list(self, include_deleted: bool = False) -> list[Link]:
        sql = "SELECT * FROM links"
        if not include_deleted:
            sql += " WHERE deleted_at IS NULL"
        sql += " ORDER BY code"
        with self._lock:
            return [_link(r) for r in self._db.execute(sql)]

    def history(self, code: str) -> list[HistoryEntry]:
        with self._lock:
            rows = self._db.execute(
                "SELECT action, target, actor, at FROM link_history WHERE code = ? ORDER BY id",
                (code,),
            ).fetchall()
        return [HistoryEntry(**dict(r)) for r in rows]

    # --- writes --------------------------------------------------------

    def insert(self, code: str, target: str, actor: str) -> Link:
        """Insert a new code. Raises Conflict if the code was ever used."""
        ts = now()
        with self._lock, self._tx():
            try:
                self._db.execute(
                    "INSERT INTO links (code, target, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (code, target, actor, ts, ts),
                )
            except sqlite3.IntegrityError:
                raise Conflict(f"code {code} is already taken") from None
            self._log(code, "create", target, actor, ts)
        return Link(code, target, actor, ts, ts)

    def insert_for_target(self, target: str, actor: str, candidates: Iterable[str]) -> tuple[Link, bool]:
        """Return the live link for `target`, or create one with the first
        free code from `candidates`. Returns (link, created).

        The lookup and the insert are one transaction, so concurrent requests
        for the same target agree on one code.
        """
        ts = now()
        with self._lock, self._tx():
            row = self._db.execute(
                "SELECT * FROM links WHERE target = ? AND deleted_at IS NULL ORDER BY created_at, code LIMIT 1",
                (target,),
            ).fetchone()
            if row is not None:
                return _link(row), False
            for code in candidates:
                taken = self._db.execute("SELECT 1 FROM links WHERE code = ?", (code,)).fetchone()
                if taken:
                    continue
                self._db.execute(
                    "INSERT INTO links (code, target, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (code, target, actor, ts, ts),
                )
                self._log(code, "create", target, actor, ts)
                return Link(code, target, actor, ts, ts), True
        raise Conflict("no free code among the candidates")

    def update(self, code: str, target: str, actor: str) -> Link:
        ts = now()
        with self._lock, self._tx():
            row = self._db.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
            if row is None or row["deleted_at"] is not None:
                raise NotFound(code)
            if row["target"] != target:
                self._db.execute(
                    "UPDATE links SET target = ?, updated_at = ? WHERE code = ?", (target, ts, code)
                )
                self._log(code, "update", target, actor, ts)
            row = self._db.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
        return _link(row)

    def delete(self, code: str, actor: str) -> Link:
        ts = now()
        with self._lock, self._tx():
            row = self._db.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
            if row is None or row["deleted_at"] is not None:
                raise NotFound(code)
            self._db.execute(
                "UPDATE links SET deleted_at = ?, updated_at = ? WHERE code = ?", (ts, ts, code)
            )
            self._log(code, "delete", row["target"], actor, ts)
            row = self._db.execute("SELECT * FROM links WHERE code = ?", (code,)).fetchone()
        return _link(row)

    def restore(self, links: list[Link], actor: str) -> int:
        """Make the store hold exactly these rows for their codes.

        Used to restore from an export. Rows for codes not in `links` are left
        alone. Returns the number of rows inserted or changed.
        """
        changed = 0
        ts = now()
        with self._lock, self._tx():
            for link in links:
                row = self._db.execute("SELECT * FROM links WHERE code = ?", (link.code,)).fetchone()
                if row is not None and _link(row) == link:
                    continue
                self._db.execute(
                    "INSERT OR REPLACE INTO links (code, target, created_by, created_at, updated_at, deleted_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (link.code, link.target, link.created_by, link.created_at, link.updated_at, link.deleted_at),
                )
                self._log(link.code, "import", link.target, actor, ts)
                changed += 1
        return changed

    def _log(self, code: str, action: str, target: str, actor: str, ts: str) -> None:
        self._db.execute(
            "INSERT INTO link_history (code, action, target, actor, at) VALUES (?, ?, ?, ?, ?)",
            (code, action, target, actor, ts),
        )

    def _tx(self) -> _Transaction:
        return _Transaction(self._db)


class _Transaction:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def __enter__(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, exc, tb) -> None:
        self._db.execute("ROLLBACK" if exc_type else "COMMIT")


def _link(row: sqlite3.Row) -> Link:
    return Link(
        code=row["code"],
        target=row["target"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
    )
