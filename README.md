# Romania Dynamic Electricity Price Downloader

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/custom-components/hacs)
[![GitHub Release](https://img.shields.io/github/release/tntvlad/romania-dynamic-tariff.svg)](https://github.com/tntvlad/romania-dynamic-tariff/releases)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=tntvlad&repository=https%3A%2F%2Fgithub.com%2Ftntvlad%2Fromania-dynamic-tariff.git&category=Energy)

A Home Assistant integration that downloads and displays electricity prices from the Romanian Electricity Market.

## Features

- Downloads historical and current electricity prices from the Romanian market
- Provides current hour, daily average, and forecast price sensors
- Shows price statistics (peak, off-peak, min, max)
- Automatically fetches next day prices when available
- Displays price data with proper Romanian timezone (EET/EEST)

## Installation

### HACS Installation (Recommended)

1. Make sure [HACS](https://hacs.xyz/) is installed in your Home Assistant instance
2. Add this repository as a custom repository in HACS:
   - Go to HACS > Integrations
   - Click the three dots in the upper right corner
   - Select "Custom repositories"
   - Add the URL `https://github.com/https://github.com/tntvlad/romania-dynamic-tariff`
   - Select category "Integration"
   - Click "Add"
3. Click on "+ Explore & Download Repositories" and search for "Romania Dynamic"
4. Click "Download"
5. Restart Home Assistant

### Manual Installation

1. Download the latest release from GitHub
2. Extract the contents
3. Copy the `custom_components/romania-dynamic-tariff` directory to your Home Assistant `custom_components` directory
4. Restart Home Assistant

## Configuration

This integration can be configured through the Home Assistant UI:

1. Go to **Settings** > **Devices & Services**
2. Click the "+ Add Integration" button
3. Search for "Romania Dynamic"
4. Follow the configuration steps
   - Enter a start date for historical data (format: YYYY-MM-DD)

## Available Sensors

After setup, the following sensors will be available:

- `sensor.dynamic_current_hour_price`: Current hour electricity price
- `sensor.dynamic_daily_average_price`: Daily average price
- `sensor.dynamic_download_status`: Status of data downloads
- `sensor.dynamic_next_hour_forecast`: Price forecast for the next hour

## Attributes

The `sensor.dynamic_current_hour_price` sensor provides several useful attributes:

- `average`: Daily average price
- `peak`: Average peak price (08:00-20:00)
- `off_peak_1`: Average off-peak price (00:00-08:00)
- `off_peak_2`: Average off-peak price (20:00-24:00)
- `min`: Minimum daily price
- `max`: Maximum daily price
- `today`: List of all hourly prices for today
- `tomorrow`: List of all hourly prices for tomorrow (when available)
- `raw_today`: Formatted data with proper timestamps for today
- `raw_tomorrow`: Formatted data with proper timestamps for tomorrow

## Lovelace Card Example

You can create a nice price visualization using ApexCharts Card:

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
