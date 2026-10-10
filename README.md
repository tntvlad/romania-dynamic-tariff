# Romania Dynamic Electricity Price

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub Release](https://img.shields.io/github/release/tntvlad/romania-dynamic-tariff.svg)](https://github.com/tntvlad/romania-dynamic-tariff/releases)

A Home Assistant integration that downloads and displays day-ahead electricity
prices from the Romanian electricity market (OPCOM / PZU-DAM), with local
history, configurable retention and a live progress sensor.

## Features

- Downloads current and next-day day-ahead prices from OPCOM
- **Stores price history in a local SQLite database** (instead of thousands of
  per-day CSV/JSON files)
- **Configurable data retention** — keep everything, or automatically prune to
  the last 30 / 90 / 365 days (or a custom window)
- **Background history backfill** from your start date, with a live **0–100 %**
  progress sensor
- **Self-tuning next-day fetch schedule** so it asks for tomorrow's prices right
  after OPCOM publishes them, instead of all night
- One-time, automatic import of any existing `romania_dynamic_data/*.json|.csv`
  files into the database
- Provides current hour, daily average, and next-hour forecast sensors with rich
  attributes (peak / off-peak / min / max, raw hourly data for cards)
- Correct Romanian timezone handling (EET/EEST)

## Requirements

- Home Assistant **2024.8** or newer (2026.3+ to see the bundled brand icon)
- No extra Python dependencies

## Installation

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=tntvlad&repository=romania-dynamic-tariff&category=integration)

Or

### HACS

1. Make sure [HACS](https://hacs.xyz/) is installed.
2. Add this repository as a custom repository in HACS:
   - **HACS → Integrations → ⋮ → Custom repositories**
   - URL: `https://github.com/tntvlad/romania-dynamic-tariff`
   - Category: **Integration** → **Add**
3. Search for **Romania Dynamic** and click **Download**.
4. Restart Home Assistant.

### Manual

1. Download the latest release from GitHub.
2. Copy the `custom_components/romania_dynamic_tariff` directory into your Home
   Assistant `config/custom_components/` directory.
3. Restart Home Assistant.

## Updating

- **HACS:** **HACS → Integrations → Romania Dynamic → Update**, then restart.
- **Manual:** overwrite `config/custom_components/romania_dynamic_tariff/` with
  the new version, delete its `__pycache__` folder, then restart.

## Configuration

1. Go to **Settings → Devices & Services**.
2. Click **+ Add Integration** and search for **Romania Dynamic**.
3. Enter a **start date** for the historical data (`YYYY-MM-DD`).

### Options

Open the integration and click **Configure** to change these; saving reloads the
integration automatically.

| Option | Values | Description |
| --- | --- | --- |
| **Data retention** | `all` (default), `30`, `90`, `365`, `custom` | How much price history to keep. `all` never deletes anything. Anything else prunes rows older than the chosen window each time new data is downloaded. |
| **Custom retention (days)** | number | Used only when retention is set to `custom`. |
| **Keep raw CSV files** | on / off (default off) | When off, the downloaded OPCOM CSV is parsed and discarded. Turn on to also keep the raw CSV under `romania_dynamic_data/` for debugging. |
| **Backfill history** | on / off (default on) | On setup (and after a restart) the integration downloads every day from your start date up to today in the background and stores it in the database. The **Download Status** sensor shows this as a live 0–100 %. Turn off to skip the historical import. |
| **Move legacy files after import** | on / off (default off) | After the one-time import of old per-day JSON/CSV files, move `romania_dynamic_data/` to `romania_dynamic_data_legacy/` so it stops cluttering your config folder. Reversible (a move, not a delete). |

When retention is a limited window, the backfill is also capped to that window
so it never downloads history it would immediately prune.

## Next-day price scheduling

OPCOM publishes the next day's day-ahead prices at roughly **13:00** local time.
The integration polls every 15 minutes for *today's* data, but only starts asking
for **tomorrow** at its self-tuning *first-try* time, which then adjusts itself:

- starts at **13:00**
- if the first try **fails** (not published yet) → the next day's start moves
  **+15 min** (e.g. 13:15)
- if the first try **succeeds** → the next day's start moves **−1 min**
  (e.g. 13:14)

So it converges to just after the real publication time and never hammers OPCOM
overnight. The time is clamped to **12:00–22:00** and is persisted in the
database, so it survives restarts. After a failed first try it keeps retrying
every 15 minutes until the data appears (or the day rolls over).

## Data storage

Price history is stored in a single SQLite database (WAL mode):

```
<config>/romania_dynamic_tariff/prices.db
```

`sensor.dynamic_current_hour_price` exposes a `storage` attribute with the
database path, row count, and first/last stored dates, so you can confirm at a
glance how much history you are keeping.

### Migrating from file storage

On first start, any old per-day `romania_dynamic_data/*.json|.csv` files are
imported into the database once (idempotent — safe across restarts). Enable
**Move legacy files after import** if you want the old folder parked as
`romania_dynamic_data_legacy/`. Nothing is deleted.

## Available sensors

| Entity | Description |
| --- | --- |
| `sensor.dynamic_current_hour_price` | Current hour price |
| `sensor.dynamic_daily_average_price` | Average price for today |
| `sensor.dynamic_next_hour_forecast` | Price for the next hour |
| `sensor.dynamic_download_status` | History / backfill progress (0–100 %) |

All price sensors use the unit **`lei/MWh`** (RON per MWh) and the
`measurement` state class (long-term statistics work).

### Download Status sensor

The state is a percentage (unit `%`):

- `0 %` — no history stored yet (backfill just starting)
- `50 %` — half of the configured window is stored
- `100 %` — the full `start_date → today` window is in the database

Extra attributes: `status` (text, e.g. `Backfilling (42%)` / `Backfill complete`),
`forecast_status`, `progress`, `completed_days`, `total_days`, `missing_days`,
`history_start`, `current_date`, `backfill_enabled`, `backfill_running`,
`last_updated` and `storage`.

> **Upgrading note:** in v1.0.0 this sensor's state was text
> (`CSV Data Available`). It is now a number. The old text is still available in
> the `status` / `forecast_status` attributes, but any automation or card that
> tested the old state must be updated.

## Attributes

`sensor.dynamic_current_hour_price` provides:

- `average`: Daily average price
- `peak`: Average peak price (08:00–20:00)
- `off_peak_1`: Average off-peak price (00:00–08:00)
- `off_peak_2`: Average off-peak price (20:00–24:00)
- `min` / `max` / `mean`: Daily price statistics
- `low_price`: `true` when the current price is below the daily average
- `price_percent_to_average`: Current price / daily average (1.0 = equal)
- `unit` / `currency` / `region`: `kWh` / `RON` / `RO`
- `today` / `tomorrow`: Flat lists of hourly prices
- `tomorrow_valid`: Whether next-day data is available yet
- `raw_today` / `raw_tomorrow`: Hourly slices with ISO timestamps (`start`,
  `end`, `value`) for cards
- `storage`: Database path, row count and stored date span
- `last_updated`, `source`

`sensor.dynamic_next_hour_forecast` additionally exposes `forecast_prices`.

## Lovelace card example

A price column chart using [ApexCharts Card](https://github.com/RomRider/apexcharts-card):

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Romania Dynamic Electricity Prices
  show_states: true
  colorize_states: true
graph_span: 2d
span:
  start: day
  offset: '-1d'
series:
  - entity: sensor.dynamic_current_hour_price
    type: column
    data_generator: |
      return entity.attributes.raw_today.concat(entity.attributes.raw_tomorrow).map((entry) => {
        return [new Date(entry.start).getTime(), entry.value];
      });
    name: Price
    color: '#e74c3c'
```
