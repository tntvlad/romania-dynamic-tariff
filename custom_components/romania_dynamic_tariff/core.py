"""Pure Romania dynamic-tariff logic (no Home Assistant imports).

Everything here is free of ``homeassistant`` / ``aiohttp`` dependencies so it
can be unit-tested with the standard library alone.

OPCOM publishes day-ahead prices at 15-minute granularity ("PT15M" rows). A
day has 96 intervals normally, 100 on the autumn DST day (25 real hours) and
92 on the spring DST day (23 real hours). The integration stores prices keyed
by the *1-based interval number* OPCOM itself uses, which keeps the mapping to
real time trivial: interval ``N`` covers ``[00:00 + (N-1)*15min,
00:00 + N*15min)`` in Romanian local time.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta

# Market structure ---------------------------------------------------------
SLOTS_PER_DAY = 96
MINUTES_PER_INTERVAL = 15
INTERVAL_RESOLUTION = "15m"

# Official OPCOM bands (from the export header/banner itself):
#   ROPEX_DAM_Peak     (33-80)  -> 08:00-20:00
#   ROPEX_DAM_Off_Peak (1-32) & (81-96)
PEAK_FROM = 33
PEAK_TO = 80
OFF_PEAK_1_FROM = 1
OFF_PEAK_1_TO = 32
OFF_PEAK_2_FROM = 81

# Hourly products existed until 2025-09-30; largest possible hourly interval
# is 25 (the autumn DST day has 25 hourly blocks).
MAX_HOURLY_INTERVAL = 25


# -------------------------------------------------------------------- parse
def parse_opcom_csv(csv_content: str) -> list[dict]:
    """Parse the OPCOM export into ``{interval, price, volume}`` rows.

    Only the market rows for "Romania" are kept. Rows whose closing price is
    missing (intervals with no transactions) are skipped, so a day may return
    fewer than 96 rows - consumers must tolerate sparse data.
    """
    lines = csv_content.strip().split("\n")

    section_start = -1
    for i, line in enumerate(lines):
        if "Interval" in line and "Pret de Inchidere" in line:
            section_start = i + 1
            break

    if section_start == -1:
        return []

    rows: list[dict] = []
    for i in range(section_start, len(lines)):
        line = lines[i].strip()
        if not line:
            continue
        try:
            row = None
            for delimiter in (",", ";", "\t"):
                parsed = list(csv.reader([line], delimiter=delimiter))[0]
                if len(parsed) >= 3:
                    row = parsed
                    break
            if row is None:
                continue

            row = [cell.strip('"').strip() for cell in row]
            if len(row) < 3 or row[0].lower() != "romania":
                continue

            interval = int(row[1])
            price = float(row[2].replace(",", ".").replace(" ", ""))
            volume = 0.0
            if len(row) > 3 and row[3].strip():
                try:
                    volume = float(row[3].replace(",", ".").replace(" ", ""))
                except ValueError:
                    volume = 0.0

            rows.append({"interval": interval, "price": price, "volume": volume})
        except (ValueError, IndexError):
            continue

    rows.sort(key=lambda r: r["interval"])
    return rows


def opcom_daily_average(csv_content: str) -> float | None:
    """Fallback: the average market price for the delivery day.

    OPCOM only prints this banner line (``ROPEX_DAM_Base (1-24)``) when the
    per-interval rows could not be used.
    """
    for line in csv_content.strip().split("\n"):
        if "ROPEX_DAM_Base" in line and "1-24" in line:
            parts = line.split(",")
            if len(parts) >= 2:
                try:
                    return float(parts[1].strip('"').strip().replace(",", "."))
                except ValueError:
                    return None
    return None


# --------------------------------------------------------------- resolution
def max_interval(rows: list[dict]) -> int:
    """Largest interval number present in a set of rows."""
    return max((int(r["interval"]) for r in rows), default=0)


def is_hourly_resolution(rows: list[dict]) -> bool:
    """True when the rows carry OPCOM's old hourly numbering (1..25)."""
    return 0 < max_interval(rows) <= MAX_HOURLY_INTERVAL


def expand_hourly_to_quarterly(rows: list[dict]) -> list[dict]:
    """Spread hourly rows into 15-minute rows (4 equal quarters).

    Used for the schema migration of pre-2025-10-01 history and for legacy
    JSON imports. Volume is split equally across the 4 quarters; the price is
    copied unchanged.
    """
    out: list[dict] = []
    for r in rows:
        price = float(r["price"])
        volume = float(r.get("volume", 0) or 0) / 4.0
        if r.get("interval"):
            hour_index = int(r["interval"]) - 1
        else:
            hour_index = int(r["hour"])
        for k in range(4):
            out.append(
                {
                    "interval": hour_index * 4 + k + 1,
                    "price": price,
                    "volume": volume,
                }
            )
    out.sort(key=lambda r: r["interval"])
    return out


# ----------------------------------------------------------------- intervals
def interval_from_local_time(
    hour: int, minute: int, max_interval: int = SLOTS_PER_DAY
) -> int:
    """1-based OPCOM interval covering a wall-clock time.

    Clamped to ``[1, max_interval]`` so sparse days behave. On DST transition
    days wall-clock minutes do not line up with OPCOM's chronological interval
    numbering - prefer :func:`interval_for_local_now` with a tz-aware clock.
    """
    idx = (hour * 60 + minute) // MINUTES_PER_INTERVAL
    return min(max(idx + 1, 1), max(max_interval, 1))


def interval_for_local_now(now, tz, max_interval: int = SLOTS_PER_DAY) -> int:
    """1-based OPCOM interval covering ``now`` (a tz-aware local datetime).

    OPCOM numbers intervals chronologically through the day, so the right
    lookup is elapsed time since local midnight, not the wall clock. That keeps
    the DST days correct too (100 intervals in autumn, 92 in spring).

    If ``now`` is naive it falls back to the wall-clock approximation.
    """
    if now.tzinfo is None:
        return interval_from_local_time(now.hour, now.minute, max_interval)
    midnight = tz.localize(datetime.combine(now.date(), datetime.min.time()))
    elapsed_minutes = (now - midnight).total_seconds() // 60
    idx = int(elapsed_minutes // MINUTES_PER_INTERVAL)
    return min(max(idx + 1, 1), max(max_interval, 1))


def interval_start_naive(day: date, interval: int) -> datetime:
    """Naive local datetime at which a 15-minute interval starts."""
    return datetime.combine(day, datetime.min.time()) + timedelta(
        minutes=(interval - 1) * MINUTES_PER_INTERVAL
    )


# ------------------------------------------------------------------- stats
def compute_stats(rows: list[dict]) -> dict:
    """Statistics over a day's 15-minute prices, using the official bands.

    ``rows`` are ``{interval, price}`` dicts (may be sparse). Returns an empty
    dict when there are no usable prices. The ``peak`` / ``off_peak_*`` bands
    are evaluated on the OPCOM interval ranges so they are correct regardless
    of which intervals have data.
    """
    if not rows:
        return {}

    prices = {int(r["interval"]): float(r["price"]) for r in rows}
    all_values = list(prices.values())
    count = len(all_values)

    def band(lo: int, hi: int) -> float:
        values = [p for i, p in prices.items() if lo <= i <= hi]
        return sum(values) / len(values) if values else 0.0

    high = max(max(prices), OFF_PEAK_2_FROM)
    peak = band(PEAK_FROM, PEAK_TO)
    off_peak_1 = band(OFF_PEAK_1_FROM, OFF_PEAK_1_TO)
    off_peak_2 = band(OFF_PEAK_2_FROM, high)

    # Combined off-peak: average of every price outside the peak band.
    off_peak_values = [
        p for i, p in prices.items() if not (PEAK_FROM <= i <= PEAK_TO)
    ]
    off_peak = sum(off_peak_values) / len(off_peak_values) if off_peak_values else 0.0

    return {
        "average": sum(all_values) / count,
        "off_peak_1": off_peak_1,
        "off_peak_2": off_peak_2,
        "off_peak": off_peak,
        "peak": peak,
        "min": min(all_values),
        "max": max(all_values),
        "mean": sum(all_values) / count,
        "slots": count,
        "resolution": INTERVAL_RESOLUTION,
    }


# ---------------------------------------------------------------- slices
def row_to_slice(day: date, interval: int, price: float, tz) -> dict:
    """A 15-minute card slice with ISO-localized start/end timestamps."""
    start_local = tz.localize(interval_start_naive(day, interval))
    end_local = start_local + timedelta(minutes=MINUTES_PER_INTERVAL)
    return {
        "start": start_local.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "end": end_local.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "value": float(price),
    }


def forecast_entry(day: date, interval: int, price: float, tz) -> dict:
    """A forecast entry for one 15-minute interval of the next day."""
    start_local = tz.localize(interval_start_naive(day, interval))
    return {
        "timestamp": start_local.strftime("%Y-%m-%d %H:%M:%S"),
        "start": start_local.isoformat(),
        "interval": int(interval),
        "price": float(price),
    }