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

from .core import expand_hourly_to_quarterly, is_hourly_resolution

_LOGGER = logging.getLogger(__name__)

_SCHEMA_VERSION = 2

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS quarter_prices (
    date       TEXT    NOT NULL,
    interval   INTEGER NOT NULL,
    price      REAL    NOT NULL,
    volume     REAL    NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (date, interval)
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
        """Create the schema (and migrate v1 databases) if needed."""
        await self.hass.async_add_executor_job(self._init_sync)

    def _init_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.db_path) as conn:
            conn.execute(_CREATE_META)
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            schema_version = int(version["value"]) if version else None

            if schema_version is None or schema_version < _SCHEMA_VERSION:
                self._migrate_sync(conn)
            else:
                conn.execute(_CREATE_TABLE)

    def _migrate_sync(self, conn: sqlite3.Connection) -> None:
        """Migrate schema v1 (hourly_prices) to v2 (quarter_prices, 15-min).

        v1 stored two shapes in one ``hourly_prices`` table:
          * hourly days   (pre-2025-10-01, intervals 1..25) - expanded x4
          * 15-min days   (since 2025-10-01,  intervals 1..96/100/92) - copied
        Resolution is detected per day by its largest interval number.

        The old table is renamed (not dropped) and kept as ``hourly_prices_v1``
        so the migration is reversible. Everything runs in one transaction.
        """
        has_old = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hourly_prices'"
        ).fetchone()

        try:
            conn.execute(_CREATE_TABLE)
            if has_old:
                conn.execute("DELETE FROM quarter_prices")
                self._copy_v1_rows_sync(conn)

            conn.execute("DROP TABLE IF EXISTS hourly_prices_v1")
            if has_old:
                conn.execute("ALTER TABLE hourly_prices RENAME TO hourly_prices_v1")

            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(_SCHEMA_VERSION),),
            )
        except sqlite3.DatabaseError:
            _LOGGER.exception(
                "Schema migration to v2 failed; old data was left untouched"
            )
            raise

        if has_old:
            _LOGGER.info(
                "Schema migrated to v2: hourly history expanded to 15-minute "
                "rows; old table kept as hourly_prices_v1"
            )
        else:
            _LOGGER.debug("Created schema v2 (15-minute granularity)")

    def _copy_v1_rows_sync(self, conn: sqlite3.Connection) -> None:
        """Copy every v1 day into the 15-minute table (run inside migration)."""
        old_dates = conn.execute("SELECT DISTINCT date FROM hourly_prices").fetchall()
        for date_row in old_dates:
            day = date_row["date"]
            rows = conn.execute(
                "SELECT hour, interval, price, volume, updated_at "
                "FROM hourly_prices WHERE date = ? ORDER BY interval",
                (day,),
            ).fetchall()
            v1_rows = [
                {
                    "hour": r["hour"],
                    "interval": r["interval"],
                    "price": r["price"],
                    "volume": r["volume"],
                }
                for r in rows
            ]

            if is_hourly_resolution(v1_rows):
                converted = expand_hourly_to_quarterly(v1_rows)
            else:
                converted = [
                    {"interval": r["interval"], "price": r["price"], "volume": r["volume"]}
                    for r in v1_rows
                ]

            now = rows[0]["updated_at"] if rows else datetime.now().isoformat(timespec="seconds")
            conn.executemany(
                """
                INSERT OR REPLACE INTO quarter_prices
                    (date, interval, price, volume, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (day, int(r["interval"]), float(r["price"]),
                     float(r.get("volume", 0)), now)
                    for r in converted
                ],
            )

    def _db_exists(self) -> bool:
        return self.db_path.exists() and self.db_path.stat().st_size > 0

    # --------------------------------------------------------------- writes
    async def async_upsert_day(self, date_str: str, rows: list[dict]) -> int:
        """Insert or replace the 15-minute rows for a date."""
        async with self._lock:
            return await self.hass.async_add_executor_job(
                self._upsert_day_sync, date_str, rows
            )

    def _upsert_day_sync(self, date_str: str, rows: list[dict]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with _connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO quarter_prices(date, interval, price, volume, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(date, interval) DO UPDATE SET
                    price      = excluded.price,
                    volume     = excluded.volume,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        date_str,
                        int(r["interval"]),
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
        """Return all 15-minute rows for a date, ordered by interval."""
        async with self._lock:
            return await self.hass.async_add_executor_job(self._get_day_sync, date_str)

    def _get_day_sync(self, date_str: str) -> list[dict]:
        if not self._db_exists():
            return []
        with _connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT interval, price, volume FROM quarter_prices "
                "WHERE date = ? ORDER BY interval",
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
                "SELECT MAX(updated_at) AS u FROM quarter_prices WHERE date = ?",
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
                "FROM quarter_prices"
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
            cur = conn.execute("SELECT DISTINCT date FROM quarter_prices")
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
                "DELETE FROM quarter_prices WHERE date < ?", (cutoff_date_str,)
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