"""Constants for the Romania Dynamic Tariff integration."""
from __future__ import annotations

DOMAIN = "romania_dynamic_tariff"

CONF_START_DATE = "start_date"

# Options ---------------------------------------------------------------
OPT_RETENTION_MODE = "data_retention_mode"
OPT_RETENTION_CUSTOM_DAYS = "data_retention_custom_days"
OPT_KEEP_RAW_CSV = "keep_raw_csv"
OPT_DELETE_LEGACY_FILES = "delete_legacy_files"
OPT_BACKFILL_HISTORY = "backfill_history"

# Retention modes ---------------------------------------------------------
RETENTION_ALL = "all"
RETENTION_30 = "30"
RETENTION_90 = "90"
RETENTION_365 = "365"
RETENTION_CUSTOM = "custom"

RETENTION_MODES = [
    RETENTION_ALL,
    RETENTION_30,
    RETENTION_90,
    RETENTION_365,
    RETENTION_CUSTOM,
]

DEFAULT_RETENTION_MODE = RETENTION_ALL   # never prune by default (existing behaviour)
DEFAULT_RETENTION_CUSTOM_DAYS = 30

# The number of days each preset mode keeps (None = keep everything).
RETENTION_DAYS = {
    RETENTION_ALL: None,
    RETENTION_30: 30,
    RETENTION_90: 90,
    RETENTION_365: 365,
    RETENTION_CUSTOM: None,  # resolved from OPT_RETENTION_CUSTOM_DAYS
}

# Meta keys in the database -----------------------------------------------
META_LEGACY_MIGRATED = "legacy_migrated"
META_NEXT_DAY_FIRST_TRY = "next_day_first_try"
META_NEXT_DAY_LAST_TRY = "next_day_last_try_date"

# Data folders -------------------------------------------------------------
DATA_FOLDER = "romania_dynamic_data"
LEGACY_FOLDER = "romania_dynamic_data_legacy"
DB_FOLDER = "romania_dynamic_tariff"
DB_FILENAME = "prices.db"

# Coordinates ---------------------------------------------------------------
ROMANIA_TZ = "Europe/Bucharest"

# Download cadence ------------------------------------------------------------
UPDATE_INTERVAL_MINUTES = 15

# Next-day fetch scheduling ---------------------------------------------------
# OPCOM normally publishes the next day's DAM prices around 13:00. The
# integration starts asking for "tomorrow" at this time and self-tunes it:
#   * first try fails (not published yet) -> push the start 15 min later
#   * first try succeeds                  -> pull the start 1 min earlier
# so it settles just after the real publication time.
NEXT_DAY_FIRST_TRY_DEFAULT = "13:00"
NEXT_DAY_RETRY_MINUTES = 15      # retry step after a failed first try
NEXT_DAY_BACKOFF_MINUTES = 1     # step earlier after a successful first try
NEXT_DAY_FIRST_TRY_MIN = "12:00"  # never start before this
NEXT_DAY_FIRST_TRY_MAX = "22:00"  # never start after this


# Historical backfill ----------------------------------------------------------
# How many days to fetch concurrently when filling history into the database.
BACKFILL_CONCURRENCY = 3
# How many past days to request before pushing a progress update to the sensors.
BACKFILL_CHUNK = 12
# Small politeness pause between chunks (seconds).
BACKFILL_PAUSE_SECONDS = 0.4
# Rounds of retries for days that failed to download before giving up for now.
BACKFILL_MAX_ROUNDS = 5