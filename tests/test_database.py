"""Unit tests for the SQLite storage layer (schema v2 + v1 migration).

PriceDatabase needs no Home Assistant objects to run its *sync* methods, so we
drive those directly against temporary database files.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: E402

_bootstrap.bootstrap()

from custom_components.romania_dynamic_tariff import database  # noqa: E402

V1_TABLE = """
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


def _make_db(db_path) -> None:
    """Create a v1-style database with mixed hourly and quarterly days."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(V1_TABLE)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '1')"
        )
        rows = []
        # pre-Oct-2025 hourly day: 24 rows
        for h in range(24):
            rows.append(("2025-09-30", h, h + 1, 100.0 + h, 1.0, "t0"))
        # autumn DST hourly day (2024): 25 rows
        for h in range(25):
            rows.append(("2024-10-27", h, h + 1, 200.0 + h, 2.0, "t0"))
        # spring DST hourly day (2024): 23 rows
        for h in range(23):
            rows.append(("2024-03-31", h, h + 1, 300.0 + h, 3.0, "t0"))
        # post-Oct-2025 quarterly day: 96 rows (interval 1..96, "hour" 0..95)
        for iv in range(1, 97):
            rows.append(("2025-10-01", iv - 1, iv, 500.0 + iv, 4.0, "t1"))
        # autumn 2025 DST quarterly day: 100 intervals
        for iv in range(1, 101):
            rows.append(("2025-10-26", iv - 1, iv, 600.0 + iv, 5.0, "t1"))
        # spring 2026 DST quarterly day: 92 intervals
        for iv in range(1, 93):
            rows.append(("2026-03-29", iv - 1, iv, 700.0 + iv, 6.0, "t2"))
        conn.executemany(
            "INSERT INTO hourly_prices(date, hour, interval, price, volume, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            rows,
        )


class TestSchemaV2(unittest.TestCase):
    def test_brand_new_db_gets_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prices.db")
            db = database.PriceDatabase(None, path)
            db._init_sync()
            self.assertEqual(db._get_meta_sync("schema_version"), "2")
            self.assertEqual(db._stats_sync()["rows"], 0)

    def test_v1_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prices.db")
            _make_db(path)
            db = database.PriceDatabase(None, path)
            db._init_sync()

            # Expected: 24->96, 25->100, 23->92, 96->96, 100->100, 92->92
            with sqlite3.connect(path) as conn:
                rows = conn.execute(
                    "SELECT date, COUNT(*) FROM quarter_prices GROUP BY date"
                ).fetchall()
                by_date = dict(rows)
            self.assertEqual(by_date["2025-09-30"], 96)
            self.assertEqual(by_date["2024-10-27"], 100)
            self.assertEqual(by_date["2024-03-31"], 92)
            self.assertEqual(by_date["2025-10-01"], 96)
            self.assertEqual(by_date["2025-10-26"], 100)
            self.assertEqual(by_date["2026-03-29"], 92)
            self.assertEqual(db._get_meta_sync("schema_version"), "2")

            # Old table kept as backup, not dropped.
            with sqlite3.connect(path) as conn:
                names = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertIn("hourly_prices_v1", names)
            self.assertNotIn("hourly_prices", names)

            # Spot check: hourly expansion copies the price into 4 quarters.
            day = db._get_day_sync("2025-09-30")
            self.assertEqual([r["interval"] for r in day], list(range(1, 97)))
            self.assertEqual(day[0]["price"], 100.0)
            self.assertEqual(day[1]["price"], 100.0)
            self.assertEqual(day[4]["price"], 101.0)
            self.assertEqual(day[0]["volume"], 0.25)  # 1.0 / 4

            # Quarterly days are copied unchanged.
            day = db._get_day_sync("2025-10-01")
            self.assertEqual(len(day), 96)
            self.assertEqual(day[0]["price"], 501.0)
            self.assertEqual(day[95]["price"], 596.0)
            self.assertEqual(day[0]["volume"], 4.0)

            day = db._get_day_sync("2025-10-26")
            self.assertEqual(len(day), 100)
            self.assertEqual(day[99]["interval"], 100)

            day = db._get_day_sync("2026-03-29")
            self.assertEqual(len(day), 92)
            self.assertEqual(day[91]["interval"], 92)

    def test_v1_migration_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prices.db")
            _make_db(path)
            db = database.PriceDatabase(None, path)
            db._init_sync()
            rows_first = db._stats_sync()["rows"]
            db._init_sync()  # run again on a now-v2 database
            self.assertEqual(db._get_meta_sync("schema_version"), "2")
            self.assertEqual(db._stats_sync()["rows"], rows_first)


class TestV2Operations(unittest.TestCase):
    def test_upsert_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prices.db")
            db = database.PriceDatabase(None, path)
            db._init_sync()
            rows = [
                {"interval": 1, "price": 100.0, "volume": 1.0},
                {"interval": 48, "price": 250.0, "volume": 2.5},
                {"interval": 96, "price": 90.0, "volume": 0.0},
            ]
            db._upsert_day_sync("2026-10-12", rows)
            stored = db._get_day_sync("2026-10-12")
            self.assertEqual([r["interval"] for r in stored], [1, 48, 96])
            self.assertEqual(stored[1]["price"], 250.0)
            self.assertEqual(stored[1]["volume"], 2.5)
            # Upsert replaces the whole day's rows for the same intervals.
            db._upsert_day_sync("2026-10-12", [{"interval": 1, "price": 111.0, "volume": 0}])
            stored = db._get_day_sync("2026-10-12")
            self.assertEqual(len(stored), 3)
            self.assertEqual(stored[0]["price"], 111.0)
            self.assertTrue(db._get_day_sync("2026-10-11") == [])

    def test_prune_and_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prices.db")
            db = database.PriceDatabase(None, path)
            db._init_sync()
            db._upsert_day_sync("2026-10-11", [{"interval": 1, "price": 1.0, "volume": 0}])
            db._upsert_day_sync("2026-10-12", [{"interval": 1, "price": 2.0, "volume": 0}])
            deleted = db._prune_sync("2026-10-12")
            self.assertEqual(deleted, 1)
            self.assertEqual(db._present_dates_sync(), {"2026-10-12"})

            db._set_meta_sync("k", "v")
            self.assertEqual(db._get_meta_sync("k"), "v")
            self.assertEqual(db._day_updated_at_sync("2026-10-12") is not None, True)


if __name__ == "__main__":
    unittest.main()