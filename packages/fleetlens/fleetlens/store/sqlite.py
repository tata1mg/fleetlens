"""SQLite store — the zero-infra default (a single file, or ':memory:').

Implements all three abstractions over one connection. No vector column: the deterministic
core doesn't need embeddings, so the default store stays dependency-free (stdlib sqlite3).
Semantic search is a separate, opt-in backend added later.
"""
from __future__ import annotations

import json
import math
import sqlite3
import struct
from typing import Optional

from .base import ContextStore, KnowledgeStore, RelationshipStore, SemanticStore
from .models import STUB_SOURCE, KnowledgeObject, Relationship, make_stub_object

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_objects (
    id                  TEXT PRIMARY KEY,
    object_type         TEXT NOT NULL,
    name                TEXT NOT NULL,
    summary             TEXT,
    version             TEXT NOT NULL DEFAULT 'unknown',
    source              TEXT NOT NULL,
    generation_strategy TEXT NOT NULL,
    semantic_indexed    INTEGER NOT NULL DEFAULT 0,
    last_generated_at   TEXT,
    payload             TEXT NOT NULL DEFAULT '{}',
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ko_type ON knowledge_objects(object_type);

CREATE TABLE IF NOT EXISTS relationships (
    from_id      TEXT NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
    relationship TEXT NOT NULL,
    to_id        TEXT NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
    source       TEXT NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (from_id, relationship, to_id, source)
);
CREATE INDEX IF NOT EXISTS idx_rel_from ON relationships(from_id, relationship);
CREATE INDEX IF NOT EXISTS idx_rel_to   ON relationships(to_id, relationship);

-- Optional enrichment layer (LLM summary + embedding). No FK to knowledge_objects on
-- purpose, so it survives a full-snapshot re-index; gating is by content_hash.
CREATE TABLE IF NOT EXISTS enrichments (
    object_id    TEXT NOT NULL,
    object_type  TEXT NOT NULL,
    model        TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    summary      TEXT,
    content_hash TEXT NOT NULL,
    vector       BLOB NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (object_id, model)
);
CREATE INDEX IF NOT EXISTS idx_enrich_type ON enrichments(object_type, model);
"""


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)

_COLS = ("id, object_type, name, summary, version, source, generation_strategy, "
         "semantic_indexed, last_generated_at, payload")


def _row_to_object(row) -> KnowledgeObject:
    (id_, otype, name, summary, version, source, strategy, sem, ts, payload) = row
    object_id = id_.split(":", 1)[1] if ":" in id_ else id_
    return KnowledgeObject(
        object_type=otype, object_id=object_id, name=name, summary=summary, version=version,
        source=source, generation_strategy=strategy, last_generated_at=ts,
        embed_text=None, payload=json.loads(payload) if payload else {},
    )


class SqliteStore(ContextStore, KnowledgeStore, RelationshipStore, SemanticStore):
    """One store, all roles. Use path=':memory:' for tests, a file for real use."""

    def __init__(self, path: str = ":memory:", read_only: bool = False):
        """`read_only` opens the file through a SQLite URI with mode=ro, so the connection
        cannot write and never creates the file. A shared server should use it: the index
        is built elsewhere and swapped in, and a served store that cannot be written is one
        less thing to reason about when a tool misbehaves."""
        if path != ":memory:":
            # sqlite3 creates the file but not its parent dir; expand ~ and mkdir so
            # `--db ~/fleetlens/fleet.db` just works.
            from pathlib import Path
            p = Path(path).expanduser()
            if read_only and not p.exists():
                raise FileNotFoundError(f"no index at {p}")
            if not read_only:
                p.parent.mkdir(parents=True, exist_ok=True)
            path = str(p)
        elif read_only:
            raise ValueError("read_only is meaningless for an in-memory store")

        self._path = None if path == ":memory:" else path
        self._sig = None
        if read_only:
            import os
            self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            st = os.stat(path)
            self._sig = (st.st_ino, st.st_size, st.st_mtime_ns)
            self.read_only = True
            return
        self._conn = sqlite3.connect(path)
        self.read_only = False
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def reload_if_changed(self) -> bool:
        """Reopen the connection if the file on disk is no longer the one we opened.

        A served index is refreshed by building a new file and renaming it into place. The
        rename leaves this connection holding a descriptor to the now-unlinked old inode,
        so without this the server would keep answering from the previous index forever
        and nothing would look wrong. Comparing the inode catches exactly that, and a size
        or mtime change catches a file rewritten in place.

        Returns True if it reopened. A failed reopen keeps the current connection: a
        momentarily unreadable file should not take the server down.
        """
        if self._path is None:  # in-memory
            return False
        import os
        try:
            st = os.stat(self._path)
        except OSError:
            return False
        sig = (st.st_ino, st.st_size, st.st_mtime_ns)
        if sig == self._sig:
            return False
        try:
            conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            conn.execute("SELECT 1 FROM knowledge_objects LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        old = self._conn
        self._conn, self._sig = conn, sig
        old.close()
        return True

    def index_info(self) -> dict:
        """When this index was last written and how much is in it.

        A shared server answers questions about code that has moved on since indexing, so
        the age of the index is part of every answer's trustworthiness. Cheap enough to
        expose on an unauthenticated health check.
        """
        cur = self._conn.execute(
            "SELECT object_type, COUNT(*) FROM knowledge_objects GROUP BY object_type")
        counts = {t: n for t, n in cur.fetchall()}
        built = self._conn.execute(
            "SELECT MAX(updated_at) FROM knowledge_objects").fetchone()[0]
        edges = self._conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        return {"indexed_at": built, "services": counts.get("service", 0),
                "interfaces": counts.get("interface", 0),
                "symbols": counts.get("symbol", 0), "relationships": edges}

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- ContextStore ------------------------------------------------------
    def upsert_object(self, obj: KnowledgeObject) -> None:
        self._conn.execute(
            f"INSERT INTO knowledge_objects ({_COLS}, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now')) "
            "ON CONFLICT(id) DO UPDATE SET object_type=excluded.object_type, "
            "name=excluded.name, summary=excluded.summary, version=excluded.version, "
            "source=excluded.source, generation_strategy=excluded.generation_strategy, "
            "semantic_indexed=excluded.semantic_indexed, "
            "last_generated_at=excluded.last_generated_at, payload=excluded.payload, "
            "updated_at=datetime('now');",
            (obj.id, obj.object_type, obj.name, obj.summary, obj.version, obj.source,
             obj.generation_strategy, int(obj.semantic_indexed), obj.last_generated_at,
             json.dumps(obj.payload)),
        )

    def delete_objects_by_id_prefix(self, prefix: str) -> int:
        # substr() comparison avoids LIKE/GLOB wildcard semantics (ids contain '_', ':').
        cur = self._conn.execute(
            "DELETE FROM knowledge_objects WHERE substr(id, 1, ?) = ?;",
            (len(prefix), prefix),
        )
        return cur.rowcount

    # --- KnowledgeStore ----------------------------------------------------
    def get(self, object_id: str) -> Optional[KnowledgeObject]:
        row = self._conn.execute(
            f"SELECT {_COLS} FROM knowledge_objects WHERE id = ?;", (object_id,)
        ).fetchone()
        return _row_to_object(row) if row else None

    def get_many(self, object_ids: list[str]) -> list[KnowledgeObject]:
        if not object_ids:
            return []
        marks = ",".join("?" * len(object_ids))
        rows = self._conn.execute(
            f"SELECT {_COLS} FROM knowledge_objects WHERE id IN ({marks});", object_ids
        ).fetchall()
        found = {r[0]: _row_to_object(r) for r in rows}
        return [found[i] for i in object_ids if i in found]

    def find_ids(self, substring: str, object_type: str, limit: int = 20) -> list[str]:
        if not substring:
            return []
        rows = self._conn.execute(
            "SELECT id FROM knowledge_objects WHERE object_type = ? "
            "AND instr(lower(id), lower(?)) > 0 ORDER BY id LIMIT ?;",
            (object_type, substring, int(limit)),
        ).fetchall()
        return [r[0] for r in rows]

    def list_objects(self, object_type: str, include_stubs: bool = False,
                     limit: Optional[int] = None, offset: int = 0) -> list[KnowledgeObject]:
        sql = f"SELECT {_COLS} FROM knowledge_objects WHERE object_type = ?"
        params: list = [object_type]
        if not include_stubs:
            sql += " AND source <> ?"
            params.append(STUB_SOURCE)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [int(limit), int(offset)]
        return [_row_to_object(r) for r in self._conn.execute(sql, params).fetchall()]

    # --- RelationshipStore -------------------------------------------------
    def edges_of(self, object_id: str, direction: str = "both") -> list[Relationship]:
        if direction == "out":
            where, params = "from_id = ?", (object_id,)
        elif direction == "in":
            where, params = "to_id = ?", (object_id,)
        else:
            where, params = "from_id = ? OR to_id = ?", (object_id, object_id)
        return self._edges(where, params)

    def edges_batch(self, ids: list[str], relationship: Optional[str] = None,
                    direction: str = "out") -> list[Relationship]:
        if not ids:
            return []
        col = "to_id" if direction == "in" else "from_id"
        marks = ",".join("?" * len(ids))
        where = f"{col} IN ({marks})"
        params = list(ids)
        if relationship is not None:
            where += " AND relationship = ?"
            params.append(relationship)
        return self._edges(where, params)

    def _edges(self, where: str, params) -> list[Relationship]:
        rows = self._conn.execute(
            f"SELECT from_id, relationship, to_id, source, metadata "
            f"FROM relationships WHERE {where};", params
        ).fetchall()
        return [Relationship(f, rel, t, src, json.loads(m) if m else {})
                for f, rel, t, src, m in rows]

    def known_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT id FROM knowledge_objects WHERE id IN ({marks});", ids
        ).fetchall()
        return {r[0] for r in rows}

    def ensure_stubs(self, ids: list[str]) -> set[str]:
        for gid in ids:
            obj = make_stub_object(gid)
            self._conn.execute(
                f"INSERT OR IGNORE INTO knowledge_objects ({_COLS}, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'));",
                (obj.id, obj.object_type, obj.name, obj.summary, obj.version, obj.source,
                 obj.generation_strategy, 0, obj.last_generated_at, json.dumps(obj.payload)),
            )
        return set(ids)

    # --- SemanticStore -----------------------------------------------------
    def upsert_enrichment(self, object_id, object_type, model, dim, summary,
                          content_hash, vector) -> None:
        self._conn.execute(
            "INSERT INTO enrichments (object_id, object_type, model, dim, summary, "
            "content_hash, vector, updated_at) VALUES (?,?,?,?,?,?,?, datetime('now')) "
            "ON CONFLICT(object_id, model) DO UPDATE SET object_type=excluded.object_type, "
            "dim=excluded.dim, summary=excluded.summary, content_hash=excluded.content_hash, "
            "vector=excluded.vector, updated_at=datetime('now');",
            (object_id, object_type, model, int(dim), summary, content_hash, _pack(vector)),
        )

    def enrichment_hashes(self, object_type: str, model: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT object_id, content_hash FROM enrichments WHERE object_type=? AND model=?;",
            (object_type, model)).fetchall()
        return {r[0]: r[1] for r in rows}

    def search(self, vector, object_type, model, limit=5) -> list[tuple[str, float]]:
        rows = self._conn.execute(
            "SELECT object_id, vector FROM enrichments WHERE object_type=? AND model=?;",
            (object_type, model)).fetchall()
        scored = [(oid, _cosine(vector, _unpack(blob))) for oid, blob in rows]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:limit]

    def summary_of(self, object_id: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT summary FROM enrichments WHERE object_id=? LIMIT 1;", (object_id,)).fetchone()
        return row[0] if row else None

    # --- RelationshipStore -------------------------------------------------
    def replace_edges(self, source: str, from_scope: list[str],
                      edges: list[Relationship]) -> None:
        if from_scope:
            marks = ",".join("?" * len(from_scope))
            self._conn.execute(
                f"DELETE FROM relationships WHERE source = ? AND from_id IN ({marks});",
                [source, *from_scope],
            )
        for e in edges:
            self._conn.execute(
                "INSERT INTO relationships (from_id, relationship, to_id, source, metadata, "
                "updated_at) VALUES (?,?,?,?,?, datetime('now')) "
                "ON CONFLICT(from_id, relationship, to_id, source) "
                "DO UPDATE SET metadata=excluded.metadata, updated_at=datetime('now');",
                (e.from_id, e.relationship, e.to_id, e.source, json.dumps(e.metadata)),
            )
