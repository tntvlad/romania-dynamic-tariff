"""Romania Dynamic Price Integration (database-backed)."""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import shutil
import statistics
from datetime import datetime, timedelta, date, time as dt_time

import aiohttp
import pytz  # Import pytz for timezone handling

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BACKFILL_CHUNK,
    BACKFILL_CONCURRENCY,
    BACKFILL_MAX_ROUNDS,
    BACKFILL_PAUSE_SECONDS,
    CONF_START_DATE,
    DATA_FOLDER,
    DB_FILENAME,
    DB_FOLDER,
    DOMAIN,
    LEGACY_FOLDER,
    META_LEGACY_MIGRATED,
    META_NEXT_DAY_FIRST_TRY,
    META_NEXT_DAY_LAST_TRY,
    NEXT_DAY_BACKOFF_MINUTES,
    NEXT_DAY_FIRST_TRY_DEFAULT,
    NEXT_DAY_FIRST_TRY_MAX,
    NEXT_DAY_FIRST_TRY_MIN,
    NEXT_DAY_RETRY_MINUTES,
    OPT_BACKFILL_HISTORY,
    OPT_DELETE_LEGACY_FILES,
    OPT_KEEP_RAW_CSV,
    OPT_RETENTION_CUSTOM_DAYS,
    OPT_RETENTION_MODE,
    RETENTION_ALL,
    RETENTION_CUSTOM,
    RETENTION_DAYS,
    ROMANIA_TZ,
    UPDATE_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

ROMANIAN_TIMEZONE = pytz.timezone(ROMANIA_TZ)  # Romanian timezone


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Romania Dynamic sensors."""
    database = hass.data[DOMAIN][config_entry.entry_id]["database"]
    coordinator = DynamicDataCoordinator(hass, config_entry, database)
    hass.data[DOMAIN][config_entry.entry_id]["coordinator"] = coordinator
    await coordinator.async_config_entry_first_refresh()

    sensors = [
        DynamicCurrentPriceSensor(coordinator),
        DynamicAveragePriceSensor(coordinator),
        DynamicDownloadStatusSensor(coordinator),
        DynamicForecastSensor(coordinator),
    ]

    async_add_entities(sensors)


class DynamicDataCoordinator(DataUpdateCoordinator):
    """Fetch Romania Dynamic price data and store it in the database."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, database):
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.config_entry = config_entry
        self.database = database
        self.start_date = datetime.strptime(
            config_entry.data.get(CONF_START_DATE, "2023-12-14"), "%Y-%m-%d"
        ).date()
        self.legacy_dir = os.path.join(hass.config.config_dir, DATA_FOLDER)
        self.options = dict(config_entry.options)

        # Next-day fetch schedule (self-tuning, persisted in the database meta).
        self._next_day_first_try = self._parse_hm(
            NEXT_DAY_FIRST_TRY_DEFAULT, (13, 0)
        )
        self._first_try_done_date: date | None = None
        self._schedule_loaded = False

        # Historical backfill state
        self._backfill_task: asyncio.Task | None = None
        self._backfill_started = False
        self._backfill_current: str | None = None

        # Background sync (migration + today/tomorrow downloads) so that
        # platform setup never blocks on slow I/O or the network.
        self._sync_task: asyncio.Task | None = None

    # ------------------------------------------------------------ helpers
    def _opt(self, key: str, default=None):
        return self.options.get(key, default)

    def _retention_days(self) -> int | None:
        """Return configured retention in days, or None to keep everything."""
        mode = self._opt(OPT_RETENTION_MODE, RETENTION_ALL)
        days = RETENTION_DAYS.get(mode)
        if days is None and mode == RETENTION_CUSTOM:
            days = int(self._opt(OPT_RETENTION_CUSTOM_DAYS, 30) or 30)
        return days

    def _target_start_date(self) -> date:
        """Earliest date we should hold data for.

        Normally this is the configured start date, but when retention is a
        limited window we never backfill beyond it (it would just be pruned).
        """
        days = self._retention_days()
        if days is None:
            return self.start_date
        return max(self.start_date, date.today() - timedelta(days=days))

    def _backfill_enabled(self) -> bool:
        return bool(self._opt(OPT_BACKFILL_HISTORY, True))

    # ------------------------------------------------------------ migration
    async def _migrate_legacy_if_needed(self) -> None:
        """Import legacy per-day JSON/CSV files into the database once."""
        if await self.database.async_get_meta(META_LEGACY_MIGRATED):
            return

        if not await self.hass.async_add_executor_job(os.path.isdir, self.legacy_dir):
            await self.database.async_set_meta(META_LEGACY_MIGRATED, "1")
            return

        # Read every file off the event loop, then upsert on the loop.
        parsed = await self.hass.async_add_executor_job(self._read_legacy_files_sync)

        imported = 0
        for day, rows in parsed:
            await self.database.async_upsert_day(day, rows)
            imported += 1

        await self.database.async_set_meta(META_LEGACY_MIGRATED, "1")
        _LOGGER.info("Legacy migration complete: imported %d day(s) into the database", imported)

        # Optionally get the file spam out of the config folder (reversible).
        if self._opt(OPT_DELETE_LEGACY_FILES, False):
            await self.hass.async_add_executor_job(self._move_legacy_folder_sync)

    def _read_legacy_files_sync(self) -> list[tuple[str, list[dict]]]:
        """Read every legacy JSON/CSV file (executor: never blocks the loop)."""
        try:
            files = sorted(os.listdir(self.legacy_dir))
        except OSError as err:
            _LOGGER.warning("Cannot read legacy data folder %s: %s", self.legacy_dir, err)
            return []

        parsed: list[tuple[str, list[dict]]] = []
        for fname in files:
            if not self._is_date_file(fname):
                continue
            day = fname[:10]
            json_path = os.path.join(self.legacy_dir, f"{day}.json")
            csv_path = os.path.join(self.legacy_dir, f"{day}.csv")
            try:
                if os.path.exists(json_path):
                    with open(json_path, "r", encoding="utf-8") as fh:
                        payload = json.load(fh)
                    rows = [
                        {
                            "hour": int(e["hour"]),
                            "interval": int(e.get("interval", int(e["hour"]) + 1)),
                            "price": float(e["price"]),
                            "volume": float(e.get("volume", 0)),
                        }
                        for e in payload.get("hourly_data", [])
                    ]
                elif os.path.exists(csv_path):
                    with open(csv_path, "r", encoding="utf-8") as fh:
                        content = fh.read()
                    rows = self._parse_csv_content(
                        content, datetime.strptime(day, "%Y-%m-%d").date()
                    )
                else:
                    continue

                if rows:
                    parsed.append((day, rows))
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as err:
                _LOGGER.warning("Skipping legacy file %s: %s", fname, err)
        return parsed

    def _move_legacy_folder_sync(self) -> None:
        """Move the legacy data folder aside (executor: reversible, not deleted)."""
        if not os.path.isdir(self.legacy_dir):
            return
        target = os.path.join(self.hass.config.config_dir, LEGACY_FOLDER)
        if os.path.exists(target):
            target = f"{target}_{int(datetime.now().timestamp())}"
        try:
            shutil.move(self.legacy_dir, target)
            _LOGGER.info("Moved legacy data folder to %s", target)
        except OSError as err:
            _LOGGER.warning("Could not move legacy folder: %s", err)

    def _write_raw_csv_sync(self, day_str: str, content: str) -> None:
        """Persist the raw OPCOM CSV for debugging (executor)."""
        raw_path = os.path.join(self.legacy_dir, f"{day_str}.csv")
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        with open(raw_path, "w", encoding="utf-8") as fh:
            fh.write(content)

    @staticmethod
    def _is_date_file(fname: str) -> bool:
        """Return True if the file name is YYYY-MM-DD.json/.csv."""
        if len(fname) < 10:
            return False
        try:
            datetime.strptime(fname[:10], "%Y-%m-%d")
        except ValueError:
            return False
        return True

    # ------------------------------------------------------------ retention
    async def _apply_retention(self) -> None:
        """Delete rows older than the configured retention window."""
        days = self._retention_days()
        if days is None:
            return
        cutoff = date.today() - timedelta(days=days)
        deleted = await self.database.async_prune(cutoff.strftime("%Y-%m-%d"))
        if deleted:
            _LOGGER.info("Retention pruned %d rows (older than %s)", deleted, cutoff)

    # ------------------------------------------------------------ backfill
    def _start_backfill(self) -> None:
        """Kick off the historical backfill task once per setup."""
        if self._backfill_started:
            return
        self._backfill_started = True
        if not self._backfill_enabled():
            return
        self._backfill_task = self.hass.async_create_task(self._async_backfill())

    def _backfill_window(self) -> list[date]:
        """Every date we want history for: start date .. yesterday."""
        start = self._target_start_date()
        today = date.today()
        total = (today - start).days
        return [start + timedelta(days=i) for i in range(total)]

    async def _async_backfill(self) -> None:
        """Fill missing history from the start date into the database.

        Runs in the background so HA setup stays fast. Downloads a few days at
        a time (bounded concurrency) and pushes progress to the sensors after
        each chunk, so the Download Status sensor shows a live 0-100%.
        """
        try:
            window = self._backfill_window()
            if not window:
                _LOGGER.info("Backfill: nothing to do (start date is today)")
                return

            for round_no in range(1, BACKFILL_MAX_ROUNDS + 1):
                present = await self.database.async_present_dates()
                missing = [d for d in window if d.strftime("%Y-%m-%d") not in present]
                if not missing:
                    break

                _LOGGER.info(
                    "Backfill round %d: %d day(s) missing", round_no, len(missing)
                )
                sem = asyncio.Semaphore(BACKFILL_CONCURRENCY)

                for i in range(0, len(missing), BACKFILL_CHUNK):
                    chunk = missing[i : i + BACKFILL_CHUNK]
                    await asyncio.gather(*(self._backfill_one(d, sem) for d in chunk))
                    self._backfill_current = None
                    if self._backfill_task is not None:
                        self.async_set_updated_data(await self._load_current_data())
                    await asyncio.sleep(BACKFILL_PAUSE_SECONDS)

            present = await self.database.async_present_dates()
            remaining = [d for d in window if d.strftime("%Y-%m-%d") not in present]
            if remaining:
                _LOGGER.warning(
                    "Backfill finished with %d day(s) still missing (will retry on next restart)",
                    len(remaining),
                )
            else:
                _LOGGER.info("Backfill complete: %d day(s) of history stored", len(window))
            self.async_set_updated_data(await self._load_current_data())

        except asyncio.CancelledError:
            _LOGGER.info("Backfill task cancelled")
            raise
        except Exception as err:  # noqa: BLE001 - keep the task from dying silently
            _LOGGER.warning("Backfill task stopped: %s", err)

    async def _backfill_one(self, target_date: date, sem: asyncio.Semaphore) -> None:
        """Download a single historical day under the concurrency limit."""
        async with sem:
            self._backfill_current = target_date.strftime("%Y-%m-%d")
            ok = await self._download_and_store(target_date)
            if not ok:
                _LOGGER.debug("Backfill: no data for %s", target_date)
            await asyncio.sleep(0.05)

    async def async_shutdown(self) -> None:
        """Cancel background tasks and stop the coordinator cleanly."""
        for task in (self._backfill_task, self._sync_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._backfill_task = None
        self._sync_task = None
        await super().async_shutdown()

    # ------------------------------------------------- next-day scheduling
    @staticmethod
    def _parse_hm(value: str, fallback: tuple[int, int]) -> dt_time:
        """Parse 'HH:MM' into a time, falling back to (hour, minute)."""
        try:
            hh, mm = str(value).split(":")
            return dt_time(int(hh), int(mm))
        except (ValueError, AttributeError):
            return dt_time(*fallback)

    def _clamp_first_try(self, when: dt_time) -> dt_time:
        """Keep the first-try time inside sane bounds."""
        lo = self._parse_hm(NEXT_DAY_FIRST_TRY_MIN, (12, 0))
        hi = self._parse_hm(NEXT_DAY_FIRST_TRY_MAX, (22, 0))
        return min(max(when, lo), hi)

    def _shift_first_try(self, minutes: int) -> None:
        """Move tomorrow's first-try time by ``minutes`` (clamped)."""
        base = datetime.combine(date.today(), self._next_day_first_try)
        shifted = self._clamp_first_try((base + timedelta(minutes=minutes)).time())
        self._next_day_first_try = shifted.replace(second=0, microsecond=0)

    async def _load_schedule(self) -> None:
        """Load the persisted first-try time and last-try date once."""
        if self._schedule_loaded:
            return
        self._schedule_loaded = True

        raw = await self.database.async_get_meta(META_NEXT_DAY_FIRST_TRY)
        if raw:
            self._next_day_first_try = self._parse_hm(raw, (13, 0))

        last = await self.database.async_get_meta(META_NEXT_DAY_LAST_TRY)
        if last:
            try:
                self._first_try_done_date = datetime.strptime(last, "%Y-%m-%d").date()
            except ValueError:
                self._first_try_done_date = None

    async def _remember_schedule(self) -> None:
        """Persist the tuned first-try time."""
        await self.database.async_set_meta(
            META_NEXT_DAY_FIRST_TRY, self._next_day_first_try.strftime("%H:%M")
        )
        if self._first_try_done_date is not None:
            await self.database.async_set_meta(
                META_NEXT_DAY_LAST_TRY, self._first_try_done_date.strftime("%Y-%m-%d")
            )

    def _align_next_refresh(self, now: datetime, first_try_dt: datetime) -> None:
        """Poll every 15 min, but land exactly on the next first-try time."""
        default = timedelta(minutes=UPDATE_INTERVAL_MINUTES)
        target = first_try_dt if now < first_try_dt else first_try_dt + timedelta(days=1)
        remaining = (target - now).total_seconds()
        if 0 < remaining < default.total_seconds():
            self.update_interval = timedelta(seconds=max(1.0, remaining))
        else:
            self.update_interval = default

    # ------------------------------------------------------------ main loop
    async def _async_update_data(self):
        """Return current data quickly; heavy work runs in the background.

        Platform setup awaits the first refresh, so this must stay fast: it
        only reads the database. Migration, retention and today/tomorrow
        downloads are handled by ``_async_sync`` in a background task.
        """
        try:
            await self._load_schedule()

            # Poll every 15 min but land exactly on the next first-try time.
            first_try_dt = datetime.combine(date.today(), self._next_day_first_try)
            self._align_next_refresh(datetime.now(), first_try_dt)

            self._start_sync()
            return await self._load_current_data()

        except Exception as err:
            _LOGGER.error("Error updating Romania Dynamic data: %s", err)
            raise UpdateFailed(f"Error communicating with Romanian Electricity Market: {err}") from err

    def _start_sync(self) -> None:
        """Start the background sync unless one is already running."""
        if self._sync_task is not None and not self._sync_task.done():
            return
        self._sync_task = self.hass.async_create_task(self._async_sync())

    async def _async_sync(self) -> None:
        """Background: migrate legacy files, prune, then refresh today/tomorrow."""
        try:
            now = datetime.now()
            today = date.today()
            tomorrow = today + timedelta(days=1)

            await self._migrate_legacy_if_needed()
            await self._apply_retention()

            # Kick off / continue the historical backfill (non-blocking).
            self._start_backfill()

            # Make sure we have rows for today.
            if not await self.database.async_has_day(today.strftime("%Y-%m-%d")):
                _LOGGER.info("Downloading today's data: %s", today)
                await self._download_and_store(today)

            # Tomorrow's prices are published around 13:00. Only start asking
            # for them at the (self-tuning) first-try time; after that, keep
            # refreshing normally until they are stored.
            first_try_dt = datetime.combine(today, self._next_day_first_try)
            if now >= first_try_dt:
                if self._first_try_done_date != today:
                    self._first_try_done_date = today
                    _LOGGER.info(
                        "First attempt for tomorrow's data (%s) at %s",
                        tomorrow,
                        self._next_day_first_try.strftime("%H:%M"),
                    )
                    ok = await self._download_and_store(tomorrow)
                    if ok:
                        self._shift_first_try(-NEXT_DAY_BACKOFF_MINUTES)
                    else:
                        self._shift_first_try(NEXT_DAY_RETRY_MINUTES)
                    _LOGGER.info(
                        "Next-day first try scheduled for %s from tomorrow",
                        self._next_day_first_try.strftime("%H:%M"),
                    )
                    await self._remember_schedule()
                elif await self._should_update_day(tomorrow):
                    _LOGGER.info("Refreshing tomorrow's data: %s", tomorrow)
                    await self._download_and_store(tomorrow)

            # Push the freshly-loaded data to the entities.
            self.async_set_updated_data(await self._load_current_data())

        except asyncio.CancelledError:
            _LOGGER.debug("Sync task cancelled")
            raise
        except Exception as err:  # noqa: BLE001 - keep the task from dying silently
            _LOGGER.warning("Sync task stopped: %s", err)

    async def _should_update_day(self, target_date: date) -> bool:
        """True when a day should be (re)downloaded.

        Downloads when missing, or refreshes tomorrow's data after 12:00 if the
        stored copy is more than 2 hours old (OPCOM can republish corrections).
        """
        day_str = target_date.strftime("%Y-%m-%d")
        updated = await self.database.async_day_updated_at(day_str)
        if updated is None:
            return True
        updated_dt = datetime.fromisoformat(updated)
        age_hours = (datetime.now() - updated_dt).total_seconds() / 3600
        return age_hours > 2 and datetime.now().hour >= 12

    # ------------------------------------------------------------ download
    async def _download_and_store(self, target_date: date) -> bool:
        """Download the OPCOM CSV for a day, parse it and store it in the DB."""
        try:
            day_str = target_date.strftime("%Y-%m-%d")
            url = (
                "https://www.opcom.ro/rapoarte-pzu-raportPIP-export-csv/"
                f"{target_date.strftime('%d/%m/%Y')}/ro"
            )
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/csv,application/csv,text/plain,*/*",
                "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Referer": "https://www.opcom.ro/pp/DAM/DAM_PZU.php",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
            }

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        _LOGGER.warning(
                            "OPCOM returned status %s for %s", response.status, day_str
                        )
                        return False
                    try:
                        content = await response.text(encoding="utf-8")
                    except UnicodeDecodeError:
                        content = await response.text(encoding="iso-8859-2")

            if len(content) < 100:
                _LOGGER.info("OPCOM has no data yet for %s (content too short)", day_str)
                return False

            rows = self._parse_csv_content(content, target_date)
            if not rows:
                _LOGGER.warning("No valid hourly data parsed for %s", day_str)
                return False

            await self.database.async_upsert_day(day_str, rows)

            # Optionally keep the raw CSV for debugging / later reprocess.
            if self._opt(OPT_KEEP_RAW_CSV, False):
                await self.hass.async_add_executor_job(
                    self._write_raw_csv_sync, day_str, content
                )

            _LOGGER.info(
                "Stored %d hourly rows for %s in database", len(rows), day_str
            )
            return True

        except (aiohttp.ClientError, OSError, ValueError) as err:
            _LOGGER.warning("Download failed for %s: %s", target_date, err)
            return False

    # --------------------------------------------------------------- parse
    def _parse_csv_content(self, csv_content: str, target_date: date) -> list[dict]:
        """Parse the OPCOM CSV into hourly rows (no file writes)."""
        lines = csv_content.strip().split("\n")

        hourly_section_start = -1
        for i, line in enumerate(lines):
            if "Interval" in line and "Pret de Inchidere" in line:
                hourly_section_start = i + 1
                break

        if hourly_section_start == -1:
            return []

        hourly_data: list[dict] = []

        for i in range(hourly_section_start, len(lines)):
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

                try:
                    interval = int(row[1])
                    price_clean = row[2].replace(",", ".").replace(" ", "")
                    price = float(price_clean)
                    volume_clean = (row[3].replace(",", ".").replace(" ", "")
                                    if len(row) > 3 else "0")
                    volume = float(volume_clean) if volume_clean else 0
                    hour = interval - 1
                except (ValueError, IndexError):
                    continue

                hourly_data.append(
                    {
                        "interval": interval,
                        "hour": hour,
                        "price": price,
                        "volume": volume,
                    }
                )
            except (ValueError, IndexError):
                continue

        # If no hourly rows were found, fall back to the daily average.
        if not hourly_data:
            avg_price = 0.0
            for line in lines:
                if "ROPEX_DAM_Base" in line and "1-24" in line:
                    try:
                        parts = line.split(",")
                        if len(parts) >= 2:
                            avg_price = float(parts[1].strip('"').replace(",", "."))
                            break
                    except ValueError:
                        continue
            if avg_price > 0:
                hourly_data = [
                    {"interval": h + 1, "hour": h, "price": avg_price, "volume": 0}
                    for h in range(24)
                ]

        hourly_data.sort(key=lambda x: x["hour"])
        return hourly_data

    # ---------------------------------------------------------------- read
    async def _load_current_data(self):
        """Build the public data dict from the database rows."""
        now = datetime.now()
        today = now.date()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)
        current_hour = now.hour

        yesterday_rows = await self.database.async_get_day(yesterday.strftime("%Y-%m-%d"))
        today_rows = await self.database.async_get_day(today.strftime("%Y-%m-%d"))
        tomorrow_rows = await self.database.async_get_day(tomorrow.strftime("%Y-%m-%d"))

        daily_prices = [float(r["price"]) for r in today_rows]
        current_price = 0.0
        for r in today_rows:
            if int(r["hour"]) == current_hour:
                current_price = float(r["price"])

        # Yesterday is exposed so cards can show a rolling "last 24h" window
        # (part of the previous day) together with today and tomorrow.
        raw_yesterday, raw_today, raw_tomorrow = [], [], []
        for r in yesterday_rows:
            raw_yesterday.append(self._row_to_slice(yesterday, r))
        for r in today_rows:
            raw_today.append(self._row_to_slice(today, r))
        for r in tomorrow_rows:
            raw_tomorrow.append(self._row_to_slice(tomorrow, r))
        yesterday_prices = [float(r["price"]) for r in yesterday_rows]

        forecast_prices = [
            {
                "timestamp": self._row_datetime(tomorrow, r).strftime("%Y-%m-%d %H:%M:%S"),
                "price": float(r["price"]),
                "hour": int(r["hour"]),
            }
            for r in tomorrow_rows
        ]
        tomorrow_prices = [float(r["price"]) for r in tomorrow_rows]

        # Next hour price
        if current_hour < 23 and len(daily_prices) > current_hour + 1:
            next_hour_price = daily_prices[current_hour + 1]
        elif forecast_prices:
            next_hour_price = forecast_prices[0]["price"]
        else:
            next_hour_price = 0.0

        # Statistics for today
        stats = {}
        if daily_prices:
            stats["average"] = sum(daily_prices) / len(daily_prices)
            off_peak_1 = daily_prices[:8] if len(daily_prices) >= 8 else []
            stats["off_peak_1"] = sum(off_peak_1) / len(off_peak_1) if off_peak_1 else 0
            off_peak_2 = daily_prices[20:] if len(daily_prices) >= 20 else []
            stats["off_peak_2"] = sum(off_peak_2) / len(off_peak_2) if off_peak_2 else 0
            peak = daily_prices[8:20] if len(daily_prices) >= 20 else []
            stats["peak"] = sum(peak) / len(peak) if peak else 0
            stats["min"] = min(daily_prices) if daily_prices else 0
            stats["max"] = max(daily_prices) if daily_prices else 0
            stats["mean"] = statistics.mean(daily_prices) if daily_prices else 0
            stats["low_price"] = current_price < stats["average"]
            stats["price_percent_to_average"] = (
                current_price / stats["average"] if stats["average"] > 0 else 1.0
            )

        db_stats = await self.database.async_stats()
        forecast_status = "CSV Data Available" if forecast_prices else "Forecast Pending"
        backfill = await self._backfill_status()
        status = (
            "Backfill complete"
            if backfill["complete"]
            else f"Backfilling ({backfill['progress']:.0f}%)"
        )

        return {
            "current_price": current_price,
            "daily_average": stats.get("average", 0),
            "next_hour_price": next_hour_price,
            "daily_prices": daily_prices,
            "forecast_prices": forecast_prices,
            "download_status": status,
            "forecast_status": forecast_status,
            "last_updated": now.isoformat(),
            "progress": backfill["progress"],
            "backfill": backfill,
            "stats": stats,
            "yesterday": yesterday_prices,
            "today": daily_prices,
            "tomorrow": tomorrow_prices,
            "tomorrow_valid": bool(tomorrow_rows),
            "raw_yesterday": raw_yesterday,
            "raw_today": raw_today,
            "raw_tomorrow": raw_tomorrow,
            "region": "RO",
            "currency": "RON",
            "unit": "kWh",
            "storage": {
                "database": os.path.join(DB_FOLDER, DB_FILENAME),
                "rows": db_stats.get("rows", 0),
                "first_date": db_stats.get("first_date"),
                "last_date": db_stats.get("last_date"),
            },
        }

    async def _backfill_status(self) -> dict:
        """Summarise how complete the stored history is (0-100%)."""
        start = self._target_start_date()
        today = date.today()
        span = (today - start).days
        window = [start + timedelta(days=i) for i in range(span + 1)]
        total = max(len(window), 1)
        present = await self.database.async_present_dates()
        completed = sum(1 for d in window if d.strftime("%Y-%m-%d") in present)
        progress = round(min(completed / total, 1.0) * 100, 1)
        running = self._backfill_task is not None and not self._backfill_task.done()
        return {
            "progress": progress,
            "completed_days": completed,
            "total_days": total,
            "missing_days": total - completed,
            "running": running,
            "complete": completed >= total,
            "enabled": self._backfill_enabled(),
            "start_date": start.strftime("%Y-%m-%d"),
            "current_date": self._backfill_current,
        }

    @staticmethod
    def _row_to_slice(day: date, row: dict) -> dict:
        start = DynamicDataCoordinator._row_datetime(day, row)
        end = start + timedelta(hours=1)
        return {
            "start": start.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "value": float(row["price"]),
        }

    @staticmethod
    def _row_datetime(day: date, row: dict) -> datetime:
        naive = datetime.combine(day, datetime.min.time()) + timedelta(
            hours=int(row["hour"])
        )
        return ROMANIAN_TIMEZONE.localize(naive)


# Sensor classes (kept identical to v1.x so upgrades don't break dashboards)
class DynamicBaseSensor(SensorEntity):
    """Base class for Romania Dynamic sensors."""

    def __init__(self, coordinator, sensor_type):
        """Initialize the sensor."""
        self.coordinator = coordinator
        self._sensor_type = sensor_type
        # A price (lei/MWh) is a *measurement*, not a cumulative monetary
        # total. HA rejects MONETARY + MEASUREMENT (MONETARY would require
        # state_class "total"), so leave the device class unset and keep the
        # measurement state class so long-term statistics still work.
        self._attr_device_class = None
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def name(self):
        """Return the name of the sensor."""
        return f"Dynamic {self._sensor_type}"

    @property
    def unique_id(self):
        """Return a unique ID."""
        return f"dynamic_{self._sensor_type.lower().replace(' ', '_')}"

    @property
    def available(self):
        """Return if entity is available."""
        return self.coordinator.last_update_success

    async def async_update(self):
        """Update the entity."""
        await self.coordinator.async_request_refresh()


class DynamicCurrentPriceSensor(DynamicBaseSensor):
    """Current hour price sensor."""

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator, "Current Hour Price")
        self._attr_native_unit_of_measurement = "lei/MWh"

    @property
    def native_value(self):
        """Return the current price."""
        if not self.coordinator.data:
            return 0
        return self.coordinator.data.get("current_price", 0)

    @property
    def extra_state_attributes(self):
        """Return additional attributes."""
        if not self.coordinator.data:
            return {}

        data = self.coordinator.data
        stats = data.get("stats", {})

        return {
            "state_class": "measurement",
            "average": stats.get("average", 0),
            "off_peak_1": stats.get("off_peak_1", 0),
            "off_peak_2": stats.get("off_peak_2", 0),
            "peak": stats.get("peak", 0),
            "min": stats.get("min", 0),
            "max": stats.get("max", 0),
            "mean": stats.get("mean", 0),
            "unit": data.get("unit", "kWh"),
            "currency": data.get("currency", "RON"),
            "region": data.get("region", "RO"),
            "low_price": stats.get("low_price", False),
            "price_percent_to_average": stats.get("price_percent_to_average", 1.0),
            "yesterday": data.get("yesterday", []),
            "today": data.get("today", []),
            "tomorrow": data.get("tomorrow", []),
            "tomorrow_valid": data.get("tomorrow_valid", False),
            "raw_yesterday": data.get("raw_yesterday", []),
            "raw_today": data.get("raw_today", []),
            "raw_tomorrow": data.get("raw_tomorrow", []),
            "last_updated": data.get("last_updated"),
            "storage": data.get("storage"),
            "source": "Romanian Electricity Market CSV Export",
        }


class DynamicAveragePriceSensor(DynamicBaseSensor):
    """Daily average price sensor."""

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator, "Daily Average Price")
        self._attr_native_unit_of_measurement = "lei/MWh"

    @property
    def native_value(self):
        """Return the daily average price."""
        if not self.coordinator.data:
            return 0
        return round(self.coordinator.data.get("daily_average", 0), 2)


class DynamicDownloadStatusSensor(DynamicBaseSensor):
    """History/backfill progress sensor (0-100%).

    The state is the percentage of the configured window
    (start date .. today) that is already stored in the database. It reaches
    100% when the full history has been backfilled.
    """

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator, "Download Status")
        self._attr_device_class = None
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self):
        """Return backfill progress as a percentage (0-100)."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("progress")

    @property
    def icon(self):
        """Return the icon."""
        if not self.coordinator.data:
            return "mdi:help-circle"

        backfill = self.coordinator.data.get("backfill", {})
        if backfill.get("complete"):
            return "mdi:check-circle"
        if backfill.get("running"):
            return "mdi:progress-download"
        return "mdi:clock-outline"

    @property
    def extra_state_attributes(self):
        """Return status details."""
        if not self.coordinator.data:
            return {}

        data = self.coordinator.data
        backfill = data.get("backfill", {})
        return {
            "status": data.get("download_status"),
            "forecast_status": data.get("forecast_status"),
            "progress": data.get("progress"),
            "completed_days": backfill.get("completed_days"),
            "total_days": backfill.get("total_days"),
            "missing_days": backfill.get("missing_days"),
            "history_start": backfill.get("start_date"),
            "current_date": backfill.get("current_date"),
            "backfill_enabled": backfill.get("enabled"),
            "backfill_running": backfill.get("running"),
            "last_updated": data.get("last_updated"),
            "storage": data.get("storage"),
            "source": "Romanian Electricity Market CSV Export",
        }


class DynamicForecastSensor(DynamicBaseSensor):
    """Next hour forecast sensor."""

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator, "Next Hour Forecast")
        self._attr_native_unit_of_measurement = "lei/MWh"

    @property
    def native_value(self):
        """Return the next hour forecast price."""
        if not self.coordinator.data:
            return 0
        return self.coordinator.data.get("next_hour_price", 0)

    @property
    def extra_state_attributes(self):
        """Return additional attributes."""
        if not self.coordinator.data:
            return {}

        data = self.coordinator.data
        return {
            "forecast_prices": data.get("forecast_prices", []),
            "unit_of_measurement": "lei/MWh",
            "source": "Romanian Electricity Market CSV Forecast",
        }