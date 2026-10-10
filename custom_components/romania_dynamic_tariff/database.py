"""SQLite storage for Romania Dynamic Tariff price history.

Replaces the per-day CSV/JSON file storage with a single database file so the
integration keeps only what the user configured (retention) instead of an ever
growing folder of small files.

All SQLite calls run in the executor (never block the event loop).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS hourly_prices (
    date       TEXT    NOT NULL,
    hour       INTEGER NOT NULL,
    interval   INTEGER NOT NULL,
    price      REAL    NOT NULL,
    volume     REAL    NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (date, hour)
)
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with WAL for resilience on a Raspberry Pi."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class PriceDatabase:
    """Async-friendly wrapper around the price database."""

    def __init__(self, hass, db_path: Path) -> None:
        self.hass = hass
        self.db_path = Path(db_path)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ init
    async def async_init(self) -> None:
        """Create the schema if needed."""
        await self.hass.async_add_executor_job(self._init_sync)

    def _init_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_META)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )

    def _db_exists(self) -> bool:
        return self.db_path.exists() and self.db_path.stat().st_size > 0

    # --------------------------------------------------------------- writes
    async def async_upsert_day(self, date_str: str, rows: list[dict]) -> int:
        """Insert or replace one hour row per entry for a date."""
        async with self._lock:
            return await self.hass.async_add_executor_job(
                self._upsert_day_sync, date_str, rows
            )

    def _upsert_day_sync(self, date_str: str, rows: list[dict]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with _connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO hourly_prices(date, hour, interval, price, volume, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, hour) DO UPDATE SET
                    interval = excluded.interval,
                    price    = excluded.price,
                    volume   = excluded.volume,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        date_str,
                        int(r["hour"]),
                        int(r.get("interval", r["hour"] + 1)),
                        float(r["price"]),
                        float(r.get("volume", 0)),
                        now,
                    )
                    for r in rows
                ],
            )
        _LOGGER.debug("Upserted %d rows for %s", len(rows), date_str)
        return len(rows)

    # ---------------------------------------------------------------- reads
    async def async_get_day(self, date_str: str) -> list[dict]:
        """Return all hourly rows for a date, ordered by hour."""
        async with self._lock:
            return await self.hass.async_add_executor_job(self._get_day_sync, date_str)

    def _get_day_sync(self, date_str: str) -> list[dict]:
        if not self._db_exists():
            return []
        with _connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT hour, interval, price, volume FROM hourly_prices "
                "WHERE date = ? ORDER BY hour",
                (date_str,),
            )
            return [dict(row) for row in cur.fetchall()]

    async def async_has_day(self, date_str: str) -> bool:
        """True if any row exists for the date."""
        rows = await self.async_get_day(date_str)
        return bool(rows)

    async def async_day_updated_at(self, date_str: str) -> str | None:
        """Return the most recent update timestamp for a date."""
        async with self._lock:
            return await self.hass.async_add_executor_job(
                self._day_updated_at_sync, date_str
            )

    def _day_updated_at_sync(self, date_str: str) -> str | None:
        if not self._db_exists():
            return None
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(updated_at) AS u FROM hourly_prices WHERE date = ?",
                (date_str,),
            ).fetchone()
            return row["u"] if row else None

    async def async_stats(self) -> dict:
        """Row count and date span of stored data."""
        async with self._lock:
            return await self.hass.async_add_executor_job(self._stats_sync)

    def _stats_sync(self) -> dict:
        if not self._db_exists():
            return {"rows": 0, "first_date": None, "last_date": None}
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c, MIN(date) AS mind, MAX(date) AS maxd "
                "FROM hourly_prices"
            ).fetchone()
            return {"rows": row["c"], "first_date": row["mind"], "last_date": row["maxd"]}

    async def async_present_dates(self) -> set[str]:
        """Return the set of dates (YYYY-MM-DD) that have any stored rows."""
        async with self._lock:
            return await self.hass.async_add_executor_job(self._present_dates_sync)

    def _present_dates_sync(self) -> set[str]:
        if not self._db_exists():
            return set()
        with _connect(self.db_path) as conn:
            cur = conn.execute("SELECT DISTINCT date FROM hourly_prices")
            return {row["date"] for row in cur.fetchall()}

    # ------------------------------------------------------------ retention
    async def async_prune(self, cutoff_date_str: str) -> int:
        """Delete rows strictly older than cutoff_date_str. Returns deleted count."""
        async with self._lock:
            return await self.hass.async_add_executor_job(
                self._prune_sync, cutoff_date_str
            )

    def _prune_sync(self, cutoff_date_str: str) -> int:
        with _connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM hourly_prices WHERE date < ?", (cutoff_date_str,)
            )
        count = cur.rowcount
        if count:
            _LOGGER.info(
                "Pruned %d price rows older than %s", count, cutoff_date_str
            )
        return count

    # ---------------------------------------------------------- meta helpers
    async def async_get_meta(self, key: str) -> str | None:
        async with self._lock:
            return await self.hass.async_add_executor_job(self._get_meta_sync, key)

    def _get_meta_sync(self, key: str) -> str | None:
        if not self._db_exists():
            return None
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    async def async_set_meta(self, key: str, value: str) -> None:
        async with self._lock:
            await self.hass.async_add_executor_job(self._set_meta_sync, key, value)

    def _set_meta_sync(self, key: str, value: str) -> None:
        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )