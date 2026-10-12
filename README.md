# Romania Dynamic Electricity Price

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub Release](https://img.shields.io/github/release/tntvlad/romania-dynamic-tariff.svg)](https://github.com/tntvlad/romania-dynamic-tariff/releases)

A Home Assistant integration that downloads and displays day-ahead electricity
prices from the Romanian electricity market (OPCOM / PZU-DAM), with local
history, configurable retention and a live progress sensor.

Prices are consumed at **15-minute granularity** (96 intervals per day, 100 on
the autumn DST day and 92 in spring) — the resolution OPCOM has traded since
2025-10-01.

## Features

- Downloads current and next-day day-ahead prices from OPCOM (**15-minute
  intervals**)
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
- Provides current 15-minute, daily average, and next-15-minute forecast
  sensors with rich attributes (peak / off-peak / min / max, raw 15-minute data
  for cards, including **yesterday** so cards can show a rolling last-24 h
  window)
- Correct Romanian timezone handling (EET/EEST), including DST days

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

### 15-minute granularity (v1.3.0)

OPCOM moved to quarter-hourly products on **2025-10-01**; since then a day has
96 prices (100 / 92 on the DST days). This version stores every price keyed by
its OPCOM interval number and exposes the **current 15-minute block** instead
of an hour-wide average.

The database is migrated automatically on first start of v1.3.0:

- History from before 2025-10-01 (which was only ever published hourly) is
  expanded into 15-minute rows (each hour's price repeated for its 4 quarters,
  volume split equally) so every day has the same shape.
- The old table is kept as `hourly_prices_v1` (reversible — nothing is
  deleted).
- Existing entity IDs (`sensor.dynamic_current_hour_price`,
  `sensor.dynamic_next_hour_forecast`, …) are **unchanged**, so dashboards and
  automations keep working. Their *meaning* changed:

| Was (hourly) | Now (15-minute) |
| --- | --- |
| `current hour price` | current 15-minute block price (updates at :00/:15/:30/:45) |
| `next hour forecast` | price of the next 15-minute block |
| `peak` was sliced from array positions | **exact OPCOM band** `33–80` = 08:00–20:00 |
| `off_peak_1/2` sliced positions | **exact OPCOM bands** `1–32` / `81–96` |
| `forecast_prices[*].hour` | `forecast_prices[*].interval` + `start` (15-min) |
| `yesterday/today/tomorrow` | still flat price lists, now 96 entries = true 15-min values |

Cards that consume `raw_yesterday` / `raw_today` / `raw_tomorrow` (the README
examples below) need **no changes** — the slices now simply cover 15 minutes
each.

> **Note:** days with a few intervals missing (OPCOM left the closing price
> empty because nothing traded) keep only the published intervals — the
> current-price sensor falls back to the last available price, and the cards
> simply show a gap.

### Migrating from file storage

On first start, any old per-day `romania_dynamic_data/*.json|.csv` files are
imported into the database once (idempotent — safe across restarts). Enable
**Move legacy files after import** if you want the old folder parked as
`romania_dynamic_data_legacy/`. Nothing is deleted.

## Available sensors

| Entity | Description |
| --- | --- |
| `sensor.dynamic_current_hour_price` | Price of the **current 15-minute block** (entity ID kept from v1.x for dashboard compatibility) |
| `sensor.dynamic_daily_average_price` | Average price for today |
| `sensor.dynamic_next_hour_forecast` | Price of the **next 15-minute block** |
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

- `current_interval`: the OPCOM interval number of the current 15-minute block
  (1–96, 100/92 on DST days)
- `resolution`: `"15m"`
- `average`: Daily average price (mean over today's 15-minute prices)
- `peak`: Average peak price — **exact OPCOM band** intervals `33–80`
  (08:00–20:00)
- `off_peak_1` / `off_peak_2`: Average off-peak price — exact OPCOM bands
  intervals `1–32` (00:00–08:00) / `81–96` (20:00–24:00)
- `off_peak`: Combined off-peak average (everything outside the peak band)
- `min` / `max` / `mean`: Daily price statistics
- `low_price`: `true` when the current price is below the daily average
- `price_percent_to_average`: Current price / daily average (1.0 = equal)
- `unit` / `currency` / `region`: `kWh` / `RON` / `RO`
- `yesterday` / `today` / `tomorrow`: Flat lists of **15-minute** prices
  (96 entries per day; 100/92 on DST days)
- `tomorrow_valid`: Whether next-day data is available yet
- `raw_yesterday` / `raw_today` / `raw_tomorrow`: **15-minute slices** with ISO
  timestamps (`start`, `end`, `value`) for cards. `raw_yesterday` is what makes
  a rolling "last 24 h" view possible across midnight.
- `storage`: Database path, row count and stored date span
- `last_updated`, `source`

`sensor.dynamic_next_hour_forecast` additionally exposes `forecast_prices` — one
entry per published 15-minute interval of tomorrow, each with `price`,
`interval` and `start` (ISO timestamp of the interval start).

## Lovelace card example

### Last 24 h + everything that is still to come

The default README example used to combine `graph_span: 2d` with
`span.start: day`/`offset: '-1d'`, which draws the window *yesterday → tomorrow*
while the series only contained *today → tomorrow*. The result is a misaligned
chart that seems to start and end at odd hours (e.g. only noon → midnight).

The card below shows the **previous 24 hours together with every available
future price**. It anchors the window at the start of *yesterday* and spans three
full days, so the right edge of the chart lands exactly on the **last published
15-minute interval of tomorrow** — nothing is cut off and nothing is faked:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Romania Dynamic Electricity Prices
  show_states: true
  colorize_states: true
graph_span: 3d
span:
  start: day        # window starts at the beginning of…
  offset: '-1d'     # …yesterday (24 h of history)…
now:
  show: true
  label: Now
series:
  - entity: sensor.dynamic_current_hour_price
    name: Price now
    type: column
    color: '#e74c3c'
    show:
      in_header: before_now   # header shows the current 15-min block (e.g. 10:30–10:45)
    data_generator: |
      // yesterday 00:00 → tomorrow 24:00, i.e. past + all published future
      const prices = [
        ...(entity.attributes.raw_yesterday || []),
        ...(entity.attributes.raw_today || []),
        ...(entity.attributes.raw_tomorrow || []),
      ];
      return prices.map((entry) => [new Date(entry.start).getTime(), entry.value]);
```

> **Why three days?** Day-ahead prices are only published one day ahead, so the
> last price that can ever exist is the final 15-minute interval of *tomorrow*.
> Anchoring the graph from *yesterday 00:00* to *tomorrow 24:00* guarantees the
> chart always ends on that last available price, while still showing at least
> the last 24 h. Before ~13:00 the tomorrow columns are simply empty because
> OPCOM has not published them yet.

> **Why `in_header: before_now`?** With `header.show_states: true`, the card
> displays the **last value of the series** by default. Because this series runs
> into the future, that would be tomorrow's final interval — not the price you
> are paying right now. `before_now` pins the header to the value just before
> the current time, i.e. the current 15-minute block (at 10:36 it shows the
> 10:30–10:45 price). Use `after_now` if you would rather see the next one.

### Rolling 24 h + 24 h (always centred on now)

If you prefer a window that is always exactly 24 h behind and 24 h ahead of the
current time, use `span.start: hour` with an offset instead:

```yaml
type: custom:apexcharts-card
header:
  show: true
  show_states: true
  colorize_states: true
graph_span: 48h
span:
  start: hour
  offset: '-24h'
now:
  show: true
  label: Now
series:
  - entity: sensor.dynamic_current_hour_price
    name: Price now
    type: column
    show:
      in_header: before_now
    data_generator: |
      const from = start.getTime(); // left edge of the graph
      return [
        ...(entity.attributes.raw_yesterday || []),
        ...(entity.attributes.raw_today || []),
        ...(entity.attributes.raw_tomorrow || []),
      ]
        .map((entry) => [new Date(entry.start).getTime(), entry.value])
        .filter((point) => point[0] >= from);
```

### Optional: colour cheap vs expensive hours

To highlight cheap hours in green and expensive ones in red, enable the
`color_threshold` experimental feature and tune the cut-offs (values are in
`lei/MWh`, the daily average works well as a middle threshold):

```yaml
type: custom:apexcharts-card
experimental:
  color_threshold: true
graph_span: 3d
span:
  start: day
  offset: '-1d'
now:
  show: true
  label: Now
series:
  - entity: sensor.dynamic_current_hour_price
    name: Price
    type: column
    data_generator: |
      const prices = [
        ...(entity.attributes.raw_yesterday || []),
        ...(entity.attributes.raw_today || []),
        ...(entity.attributes.raw_tomorrow || []),
      ];
      return prices.map((entry) => [new Date(entry.start).getTime(), entry.value]);
    color_threshold:
      - value: 0
        color: '#2ecc71'   # cheap
      - value: 400
        color: '#f1c40f'   # mid
      - value: 800
        color: '#e74c3c'   # expensive
```

> `raw_yesterday` was added in **v1.2.0**. On older versions only `raw_today` and
> `raw_tomorrow` exist, so the left third of the three-day cards will be empty
> and a rolling 24 h view only starts at midnight.
