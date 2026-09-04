from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import DB_PATH

logger = logging.getLogger(__name__)

_DUP_WINDOW_DAYS = 60
_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_listings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword    TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    status     TEXT NOT NULL,
    pdf_path   TEXT,
    tags       TEXT
);
"""


class ListingDatabase:
    """SQLite-backed tracker for previously processed Etsy listings."""

    def __init__(self, db_path: str | Path = DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def is_keyword_processed(self, keyword: str) -> bool:
        """Return True if *keyword* was targeted within the last 60 days.

        Prevents creating duplicate Etsy listings for the same topic.
        """
        normalized = keyword.strip().lower()
        if not normalized:
            return False

        cutoff = int(time.time()) - (_DUP_WINDOW_DAYS * 24 * 60 * 60)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_listings "
                "WHERE LOWER(keyword) = ? AND created_at >= ? "
                "LIMIT 1",
                (normalized, cutoff),
            ).fetchone()

        return row is not None

    def log_listing(
        self,
        keyword: str,
        listing_id: str,
        status: str,
        pdf_path: str,
        tags: list[str],
    ) -> int:
        """Record a successfully created draft listing. Returns the row id."""
        created_at = int(time.time())
        tags_json = json.dumps(tags, ensure_ascii=False)

        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO processed_listings "
                "(keyword, listing_id, created_at, status, pdf_path, tags) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    keyword.strip().lower(),
                    str(listing_id),
                    created_at,
                    status,
                    str(pdf_path),
                    tags_json,
                ),
            )
            conn.commit()
            row_id = int(cursor.lastrowid)

        logger.info("Logged listing id=%s keyword=%r as row=%d", listing_id, keyword, row_id)
        return row_id

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent processed listings for inspection/debugging."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, keyword, listing_id, created_at, status, pdf_path, tags "
                "FROM processed_listings ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["tags"] = json.loads(item.get("tags") or "[]")
            except (json.JSONDecodeError, TypeError):
                item["tags"] = []
            result.append(item)
        return result


_db: ListingDatabase | None = None


def get_db() -> ListingDatabase:
    """Return a process-wide, lazily-initialised database singleton."""
    global _db
    if _db is None:
        _db = ListingDatabase()
    return _db


# Convenience module-level wrappers -------------------------------------------------

def is_keyword_processed(keyword: str) -> bool:
    return get_db().is_keyword_processed(keyword)


def log_listing(
    keyword: str,
    listing_id: str,
    status: str,
    pdf_path: str,
    tags: list[str],
) -> int:
    return get_db().log_listing(keyword, listing_id, status, pdf_path, tags)
