"""Unit tests for the pure tariff logic (no Home Assistant required)."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: E402

_bootstrap.bootstrap()

from custom_components.romania_dynamic_tariff import core  # noqa: E402

# A realistic OPCOM export header + a handful of rows (festival of real data).
OPCOM_SNIPPET = '''"PIP si volum tranzactionat pentru ziua de livrare: 11/10/2026"

"","Pret mediu [lei/MWh]","Volum [MWh]","Rezolutie"
"ROPEX_DAM_Base (1-24)","940.42","35533.3","PT15M"

"Zona de tranzactionare","Interval","Pret de Inchidere a Pietei [lei/MWh]","Volum Tranzactionat [MW]","Volum Tranzactionat pe cumparare [MW]","Volum Tranzactionat pe vanzare [MW]","Rezolutie"
"Romania","1","1309.32","864.5","864.5","782.0","PT15M"
"Romania","2","1224.23","845.1","845.1","795.0","PT15M"
"Romania","3","1287.56","836.7","836.7","802.4","PT15M"
"Romania","4","","827.6","827.6","815.3","PT15M"
"Romania","5","1264.51","886.7","886.7","824.9","PT15M"
"Romania","95","1159.05","1226.3","943.1","1226.3","PT15M"
"Romania","96","1042.85","1209.9","936.5","1209.9","PT15M"
'''

OPCOM_SEMICOLON = (
    "Interval;Pret de Inchidere a Pietei [lei/MWh];Volum Tranzactionat [MW]\n"
    'Romania;1;"111.11";222.2\n'
    'Romania;2;"222.22";333.3\n'
)


class _FakeTz:
    """Deterministic fixed-offset timezone stand-in for pytz.localize."""

    def __init__(self, offset=timedelta(hours=3)):
        self._offset = offset

    def localize(self, dt):
        return dt.replace(tzinfo=timezone(self._offset))


class TestParse(unittest.TestCase):
    def test_parses_quarterly_rows_and_skips_missing_price(self):
        rows = core.parse_opcom_csv(OPCOM_SNIPPET)
        self.assertEqual([r["interval"] for r in rows], [1, 2, 3, 5, 95, 96])
        self.assertEqual(rows[0]["price"], 1309.32)
        self.assertEqual(rows[0]["volume"], 864.5)
        # interval 4 has an empty closing price -> skipped
        self.assertNotIn(4, [r["interval"] for r in rows])

    def test_semicolon_delimiter(self):
        rows = core.parse_opcom_csv(OPCOM_SEMICOLON)
        self.assertEqual([r["interval"] for r in rows], [1, 2])
        self.assertEqual(rows[1]["price"], 222.22)

    def test_no_header_returns_empty(self):
        self.assertEqual(core.parse_opcom_csv("no data here\n"), [])

    def test_daily_average_fallback(self):
        self.assertEqual(core.opcom_daily_average(OPCOM_SNIPPET), 940.42)
        self.assertIsNone(core.opcom_daily_average("nothing\n"))

    def test_max_interval_and_resolution(self):
        self.assertEqual(core.max_interval(core.parse_opcom_csv(OPCOM_SNIPPET)), 96)
        hourly = [{"interval": i, "price": 1.0, "volume": 2.0} for i in range(1, 25)]
        self.assertTrue(core.is_hourly_resolution(hourly))
        quarterly = [{"interval": i, "price": 1.0, "volume": 2.0} for i in range(1, 97)]
        self.assertFalse(core.is_hourly_resolution(quarterly))


class TestExpansion(unittest.TestCase):
    def test_expand_hourly_to_quarterly(self):
        rows = [
            {"interval": 1, "price": 10.0, "volume": 40.0},
            {"interval": 2, "price": 20.0, "volume": 8.0},
        ]
        out = core.expand_hourly_to_quarterly(rows)
        self.assertEqual(len(out), 8)
        self.assertEqual([r["interval"] for r in out], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertTrue(all(r["price"] == 10.0 for r in out[:4]))
        self.assertTrue(all(r["price"] == 20.0 for r in out[4:]))
        self.assertEqual(out[0]["volume"], 10.0)  # 40 / 4
        self.assertEqual(out[4]["volume"], 2.0)   # 8 / 4

    def test_expand_accepts_hour_key_too(self):
        rows = [{"hour": 0, "price": 5.0, "volume": 0}]
        out = core.expand_hourly_to_quarterly(rows)
        self.assertEqual([r["interval"] for r in out], [1, 2, 3, 4])


class TestIntervals(unittest.TestCase):
    def test_interval_from_local_time(self):
        self.assertEqual(core.interval_from_local_time(0, 0), 1)
        self.assertEqual(core.interval_from_local_time(0, 14), 1)
        self.assertEqual(core.interval_from_local_time(0, 15), 2)
        self.assertEqual(core.interval_from_local_time(8, 0), 33)   # peak start
        self.assertEqual(core.interval_from_local_time(20, 0), 81)  # off-peak 2
        self.assertEqual(core.interval_from_local_time(23, 45), 96)

    def test_interval_clamped_on_sparse_days(self):
        self.assertEqual(core.interval_from_local_time(23, 45, max_interval=92), 92)
        self.assertEqual(core.interval_from_local_time(23, 59, max_interval=100), 96)

    def test_interval_for_local_now(self):
        import pytz

        tz = pytz.timezone("Europe/Bucharest")

        # Normal day: 08:00 -> interval 33 (peak start).
        normal = tz.localize(datetime(2026, 10, 12, 8, 0))
        self.assertEqual(core.interval_for_local_now(normal, tz), 33)

        # Autumn DST day (2025-10-26, 25 real hours; Romania repeats
        # 03:00-04:00 EEST -> EET):
        #   first 03:30 EEST -> 210 min elapsed -> interval 15
        #   second 03:30 EET -> 270 min elapsed -> interval 19
        first = tz.localize(datetime(2025, 10, 26, 3, 30), is_dst=True)
        second = tz.localize(datetime(2025, 10, 26, 3, 30), is_dst=False)
        self.assertEqual(first.utcoffset().total_seconds(), 3 * 3600)
        self.assertEqual(second.utcoffset().total_seconds(), 2 * 3600)
        self.assertEqual(core.interval_for_local_now(first, tz), 15)
        self.assertEqual(core.interval_for_local_now(second, tz), 19)
        self.assertEqual(core.interval_for_local_now(second, tz, max_interval=100), 19)

        # Spring DST day (2026-03-29, 23 real hours; 03:00 EET -> 04:00 EEST).
        # Wall 04:30 = 210 min elapsed = interval 15 (03:00-04:00 never exists).
        spring = tz.localize(datetime(2026, 3, 29, 4, 30))
        self.assertEqual(core.interval_for_local_now(spring, tz), 15)
        self.assertEqual(core.interval_for_local_now(spring, tz, max_interval=92), 15)

    def test_interval_for_local_now_naive_falls_back(self):
        self.assertEqual(
            core.interval_for_local_now(datetime(2026, 10, 12, 8, 0), None), 33
        )

    def test_interval_start_naive(self):
        start = core.interval_start_naive(date(2026, 10, 11), 96)
        self.assertEqual(start, datetime(2026, 10, 11, 23, 45))


class TestStats(unittest.TestCase):
    def test_uniform_day(self):
        rows = [{"interval": i, "price": 100.0, "volume": 0} for i in range(1, 97)]
        stats = core.compute_stats(rows)
        self.assertEqual(stats["average"], 100.0)
        self.assertEqual(stats["peak"], 100.0)
        self.assertEqual(stats["off_peak_1"], 100.0)
        self.assertEqual(stats["off_peak_2"], 100.0)
        self.assertEqual(stats["slots"], 96)
        self.assertEqual(stats["resolution"], "15m")

    def test_peak_and_off_peak_bands(self):
        rows = []
        for i in range(1, 97):
            price = 150.0 if 33 <= i <= 80 else 50.0
            rows.append({"interval": i, "price": price, "volume": 0})
        stats = core.compute_stats(rows)
        self.assertEqual(stats["peak"], 150.0)
        self.assertEqual(stats["off_peak_1"], 50.0)
        self.assertEqual(stats["off_peak_2"], 50.0)
        self.assertEqual(stats["off_peak"], 50.0)
        self.assertEqual(stats["average"], 100.0)  # (48*150 + 48*50) / 96
        self.assertEqual(stats["min"], 50.0)
        self.assertEqual(stats["max"], 150.0)

    def test_sparse_stats(self):
        rows = [
            {"interval": 1, "price": 100.0, "volume": 0},
            {"interval": 40, "price": 200.0, "volume": 0},
            {"interval": 90, "price": 50.0, "volume": 0},
        ]
        stats = core.compute_stats(rows)
        self.assertAlmostEqual(stats["average"], (100 + 200 + 50) / 3)
        self.assertEqual(stats["peak"], 200.0)   # only interval 40 in band
        self.assertEqual(stats["off_peak_1"], 100.0)
        self.assertEqual(stats["off_peak_2"], 50.0)

    def test_empty_rows(self):
        self.assertEqual(core.compute_stats([]), {})


class TestSlices(unittest.TestCase):
    def test_row_to_slice_15_minutes(self):
        tz = _FakeTz()
        slice_ = core.row_to_slice(date(2026, 10, 11), 1, 1234.5, tz)
        self.assertEqual(slice_["start"], "2026-10-11T00:00:00+0300")
        self.assertEqual(slice_["end"], "2026-10-11T00:15:00+0300")
        self.assertEqual(slice_["value"], 1234.5)

        last = core.row_to_slice(date(2026, 10, 11), 96, 1.0, tz)
        self.assertEqual(last["start"], "2026-10-11T23:45:00+0300")

    def test_forecast_entry_shape(self):
        tz = _FakeTz()
        entry = core.forecast_entry(date(2026, 10, 12), 33, 700.0, tz)
        self.assertEqual(entry["interval"], 33)
        self.assertEqual(entry["price"], 700.0)
        self.assertEqual(entry["timestamp"], "2026-10-12 08:00:00")
        self.assertIn("start", entry)


if __name__ == "__main__":
    unittest.main()