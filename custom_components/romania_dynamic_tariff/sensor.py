"""Romania Dynamic Price Integration."""
import asyncio
import aiohttp
import csv
import io
import logging
from datetime import datetime, timedelta, date
import os
import json
import re
import statistics
import pytz  # Import pytz for timezone handling

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

DOMAIN = "romania_dynamic_tariff"
ROMANIAN_TIMEZONE = pytz.timezone('Europe/Bucharest')  # Romanian timezone

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Romania Dynamic sensors."""
    coordinator = DynamicDataCoordinator(hass, config_entry)
    await coordinator.async_config_entry_first_refresh()
    
    sensors = [
        DynamicCurrentPriceSensor(coordinator),
        DynamicAveragePriceSensor(coordinator),
        DynamicDownloadStatusSensor(coordinator),
        DynamicForecastSensor(coordinator),
    ]
    
    async_add_entities(sensors)

class DynamicDataCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Romania Dynamic data via CSV only."""
    
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        """Initialize."""
        self.hass = hass
        self.config_entry = config_entry
        self.start_date = datetime.strptime(config_entry.data.get("start_date", "2023-12-14"), "%Y-%m-%d").date()
        self.data_dir = os.path.join(hass.config.config_dir, "romania_dynamic_data")
        self.current_download_date = self.start_date
        self.download_complete = True  # Skip historical downloads
        
        # Create data directory
        os.makedirs(self.data_dir, exist_ok=True)
        
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=15),  # Check more frequently
        )
    
    async def _async_update_data(self):
        """Fetch data from Romanian Electricity Market using CSV method only."""
        try:
            now = datetime.now()
            _LOGGER.info(f"=== ROMANIA DYNAMIC CSV UPDATE START === Current time: {now}")
        
            # Download today's data if missing
            today = date.today()
            today_file = os.path.join(self.data_dir, f"{today.strftime('%Y-%m-%d')}.json")
            today_csv = os.path.join(self.data_dir, f"{today.strftime('%Y-%m-%d')}.csv")
            
            # Check if we need to reprocess an existing CSV file
            if os.path.exists(today_csv) and (not os.path.exists(today_file) or 
                                          os.path.getmtime(today_csv) > os.path.getmtime(today_file)):
                _LOGGER.info(f"🔄 Found CSV file newer than JSON, reprocessing: {today_csv}")
                await self._reprocess_csv_file(today.strftime('%Y-%m-%d'))
            elif not os.path.exists(today_file):
                _LOGGER.info(f"📥 Downloading today's data: {today}")
                await self._download_daily_data_csv(today)
        
            # Download tomorrow's data
            tomorrow = today + timedelta(days=1)
            tomorrow_file = os.path.join(self.data_dir, f"{tomorrow.strftime('%Y-%m-%d')}.json")
            tomorrow_csv = os.path.join(self.data_dir, f"{tomorrow.strftime('%Y-%m-%d')}.csv")
            
            # Check if we need to reprocess an existing tomorrow CSV file
            if os.path.exists(tomorrow_csv) and (not os.path.exists(tomorrow_file) or 
                                             os.path.getmtime(tomorrow_csv) > os.path.getmtime(tomorrow_file)):
                _LOGGER.info(f"🔄 Found tomorrow's CSV file newer than JSON, reprocessing: {tomorrow_csv}")
                await self._reprocess_csv_file(tomorrow.strftime('%Y-%m-%d'))
                
            should_download_tomorrow = False
            
            if not os.path.exists(tomorrow_file):
                should_download_tomorrow = True
                _LOGGER.info(f"🔮 Tomorrow's file doesn't exist, will try CSV download")
            else:
                file_time = datetime.fromtimestamp(os.path.getmtime(tomorrow_file))
                hours_since_update = (datetime.now() - file_time).total_seconds() / 3600
                
                # Try updating tomorrow's data after 12:00 PM if file is older than 2 hours
                if hours_since_update > 2 and now.hour >= 12:
                    should_download_tomorrow = True
                    _LOGGER.info(f"🔄 Tomorrow's file is {hours_since_update:.1f} hours old, trying CSV update")
            
            if should_download_tomorrow:
                _LOGGER.info(f"📊 ATTEMPTING CSV DOWNLOAD FOR: {tomorrow}")
                success = await self._download_daily_data_csv(tomorrow)
                if success:
                    _LOGGER.info(f"✅ Successfully downloaded tomorrow's data via CSV")
                else:
                    _LOGGER.info(f"⏳ Tomorrow's data not yet available (normal if before 13:00)")
        
            # Load and return current price data
            result = await self._load_current_data()
            _LOGGER.info(f"=== ROMANIA DYNAMIC CSV UPDATE END === Data loaded successfully")
            return result
            
        except Exception as err:
            _LOGGER.error(f"❌ Error in _async_update_data: {err}")
            raise UpdateFailed(f"Error communicating with Romanian Electricity Market: {err}")
    
    async def _reprocess_csv_file(self, date_str):
        """Reprocess a CSV file to update the JSON data."""
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            csv_file = os.path.join(self.data_dir, f"{date_str}.csv")
            json_file = os.path.join(self.data_dir, f"{date_str}.json")
            
            if not os.path.exists(csv_file):
                _LOGGER.error(f"❌ CSV file not found: {csv_file}")
                return False
            
            _LOGGER.info(f"🔄 Reprocessing CSV file: {csv_file}")
            
            with open(csv_file, 'r', encoding='utf-8') as file:
                csv_content = file.read()
            
            success = await self._parse_dynamic_csv_data(csv_content, target_date, json_file)
            if success:
                _LOGGER.info(f"✅ Successfully reprocessed CSV data for {date_str}")
                return True
            else:
                _LOGGER.error(f"❌ Failed to reprocess CSV data for {date_str}")
                return False
                
        except Exception as e:
            _LOGGER.error(f"❌ Error reprocessing CSV file: {e}")
            return False
    
    async def _download_daily_data_csv(self, target_date):
        """Download daily data via CSV export only - handles Romanian format."""
        try:
            json_file = os.path.join(self.data_dir, f"{target_date.strftime('%Y-%m-%d')}.json")
            csv_file = os.path.join(self.data_dir, f"{target_date.strftime('%Y-%m-%d')}.csv")
            
            _LOGGER.info(f"📊 CSV DOWNLOAD for {target_date}")
            
            # Skip if file is very recent (less than 1 hour old)
            if os.path.exists(json_file):
                file_time = datetime.fromtimestamp(os.path.getmtime(json_file))
                age_hours = (datetime.now() - file_time).total_seconds() / 3600
                if age_hours < 1:
                    _LOGGER.info(f"⏭️ File is only {age_hours:.1f} hours old, skipping")
                    return True
            
            # CSV URL format: https://www.opcom.ro/rapoarte-pzu-raportPIP-export-csv/DD/MM/YYYY/ro
            csv_url = f"https://www.opcom.ro/rapoarte-pzu-raportPIP-export-csv/{target_date.strftime('%d/%m/%Y')}/ro"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/csv,application/csv,text/plain,*/*',
                'Accept-Language': 'ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Referer': 'https://www.opcom.ro/pp/DAM/DAM_PZU.php',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'same-origin'
            }
            
            _LOGGER.info(f"🌐 CSV Request: {csv_url}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(csv_url, headers=headers, timeout=30) as response:
                    _LOGGER.info(f"📡 CSV Response Status: {response.status}")
                    
                    if response.status == 200:
                        # Try different encodings for Romanian characters
                        try:
                            csv_content = await response.text(encoding='utf-8')
                        except UnicodeDecodeError:
                            try:
                                csv_content = await response.text(encoding='iso-8859-2')  # Romanian encoding
                            except UnicodeDecodeError:
                                csv_content = await response.text(encoding='cp1250')  # Windows Romanian
                        
                        _LOGGER.info(f"📄 CSV content length: {len(csv_content)} characters")
                        
                        # Save raw CSV content for debugging
                        with open(csv_file, 'w', encoding='utf-8') as file:
                            file.write(csv_content)
                        _LOGGER.info(f"💾 Saved raw CSV to {csv_file}")
                        
                        # Log first few lines for debugging
                        lines = csv_content.split('\n')[:10]
                        for i, line in enumerate(lines):
                            _LOGGER.info(f"📋 CSV Line {i}: {line[:150]}...")
                        
                        if len(csv_content) > 100:  # Basic validation
                            success = await self._parse_dynamic_csv_data(csv_content, target_date, json_file)
                            if success:
                                _LOGGER.info(f"✅ CSV download and parse successful for {target_date}")
                                return True
                            else:
                                _LOGGER.warning(f"⚠️ CSV parse failed for {target_date}")
                                return False
                        else:
                            _LOGGER.warning(f"⚠️ CSV content too short: {len(csv_content)} chars")
                            return False
                    else:
                        _LOGGER.warning(f"⚠️ CSV request failed with status {response.status}")
                        return False
                        
        except Exception as e:
            _LOGGER.error(f"❌ CSV download error for {target_date}: {e}")
            return False
    
    async def _parse_dynamic_csv_data(self, csv_content, target_date, json_file):
        """Parse Romanian CSV content and save to JSON file."""
        try:
            # Clean up CSV content
            csv_content = csv_content.strip()
            
            # Split into lines
            lines = csv_content.split('\n')
            _LOGGER.info(f"📊 CSV has {len(lines)} lines")
            
            hourly_data = []
            
            # Find the hourly data section
            hourly_section_start = -1
            for i, line in enumerate(lines):
                if "Interval" in line and "Pret de Inchidere" in line:
                    hourly_section_start = i + 1
                    break
            
            if hourly_section_start == -1:
                _LOGGER.warning("⚠️ Could not find hourly data section in CSV")
                return False
            
            _LOGGER.info(f"📊 Found hourly data starting at line {hourly_section_start}")
            
            # Parse hourly data
            for i in range(hourly_section_start, len(lines)):
                line = lines[i].strip()
                if not line:
                    continue
                    
                # Try to parse as CSV row
                try:
                    # Use different delimiters
                    for delimiter in [',', ';', '\t']:
                        try:
                            row = list(csv.reader([line], delimiter=delimiter))[0]
                            if len(row) >= 3:  # We need at least zone, interval, and price
                                break
                        except:
                            continue
                    else:
                        continue
                    
                    # Clean up row data
                    row = [cell.strip('"').strip() for cell in row]
                    
                    if len(row) < 3:
                        continue
                        
                    # Check if this is the hourly data format
                    # Format: "Romania","1","443.76","1145.0",...
                    if row[0].lower() == "romania":
                        interval_str = row[1]
                        price_str = row[2]
                        volume_str = row[3] if len(row) > 3 else "0"
                        
                        try:
                            # Parse interval (should be 1-24)
                            interval = int(interval_str)
                            
                            # Convert to 0-23 hour format
                            hour = interval - 1
                            
                            # Parse price (handle Romanian decimal format)
                            price_clean = price_str.replace(',', '.').replace(' ', '')
                            price = float(price_clean)
                            
                            # Parse volume
                            volume_clean = volume_str.replace(',', '.').replace(' ', '')
                            volume = float(volume_clean) if volume_clean else 0
                            
                            # Create datetime with Romanian timezone
                            naive_dt = datetime.combine(target_date, datetime.min.time()) + timedelta(hours=hour)
                            # Localize to Romanian timezone
                            dt = ROMANIAN_TIMEZONE.localize(naive_dt)
                            
                            hourly_data.append({
                                'interval': interval,
                                'hour': hour,
                                'price': price,
                                'volume': volume,
                                'datetime': dt.strftime('%Y-%m-%d %H:%M:%S')
                            })
                            
                            if len(hourly_data) <= 5 or len(hourly_data) >= 20:
                                _LOGGER.info(f"📊 Parsed Hour {interval}: Price {price} lei/MWh, Volume {volume} MWh")
                                
                        except (ValueError, IndexError) as e:
                            _LOGGER.warning(f"⚠️ Error parsing row {row}: {e}")
                            continue
                except Exception as e:
                    _LOGGER.warning(f"⚠️ Error processing line: {e}")
                    continue
            
            # If we didn't find hourly data, try to extract from summary data
            if not hourly_data:
                _LOGGER.warning(f"⚠️ No hourly data found, trying to extract from summary")
                
                # Look for average price and create 24-hour estimate
                avg_price = 0
                for line in lines:
                    if 'ROPEX_DAM_Base' in line and '1-24' in line:
                        try:
                            parts = line.split(',')
                            if len(parts) >= 2:
                                price_str = parts[1].strip('"').replace(',', '.')
                                avg_price = float(price_str)
                                break
                        except Exception as e:
                            _LOGGER.warning(f"⚠️ Error parsing average price: {e}")
                            continue
                
                if avg_price > 0:
                    _LOGGER.info(f"📊 Using average price {avg_price} lei/MWh for all hours")
                    for hour in range(24):
                        naive_dt = datetime.combine(target_date, datetime.min.time()) + timedelta(hours=hour)
                        # Localize to Romanian timezone
                        dt = ROMANIAN_TIMEZONE.localize(naive_dt)
                        
                        hourly_data.append({
                            'interval': hour + 1,
                            'hour': hour,
                            'price': avg_price,
                            'volume': 0,
                            'datetime': dt.strftime('%Y-%m-%d %H:%M:%S')
                        })
            
            if not hourly_data:
                _LOGGER.warning(f"⚠️ No valid data parsed from CSV")
                return False
            
            if len(hourly_data) < 20:
                _LOGGER.warning(f"⚠️ Only {len(hourly_data)} hours parsed, expected ~24")
            
            # Sort by hour to ensure correct order
            hourly_data.sort(key=lambda x: x['hour'])
            
            # Save to JSON
            output_data = {
                'date': target_date.strftime('%Y-%m-%d'),
                'hourly_data': hourly_data,
                'downloaded_at': datetime.now().isoformat(),
                'source': 'Romanian Electricity Market CSV Export',
                'method': 'CSV',
                'total_hours': len(hourly_data)
            }
            
            with open(json_file, 'w', encoding='utf-8') as file:
                json.dump(output_data, file, indent=2, ensure_ascii=False)
            
            _LOGGER.info(f"✅ Saved {len(hourly_data)} CSV records to {json_file}")
            return True
            
        except Exception as e:
            _LOGGER.error(f"❌ Error parsing CSV data: {e}")
            return False
    
    async def _load_current_data(self):
        """Load current price data from JSON files."""
        try:
            now = datetime.now()
            today = now.date()
            tomorrow = today + timedelta(days=1)
            current_hour = now.hour
            
            # Load today's data
            current_price = 0
            daily_prices = []
            today_file = os.path.join(self.data_dir, f"{today.strftime('%Y-%m-%d')}.json")
            
            raw_today_data = []
            
            if os.path.exists(today_file):
                with open(today_file, 'r', encoding='utf-8') as file:
                    data = json.load(file)
                    
                if 'hourly_data' in data:
                    for entry in data['hourly_data']:
                        hour = int(entry['hour'])
                        price = float(entry['price'])
                        daily_prices.append(price)
                        
                        # Create raw_today entry with start/end times with proper Romanian timezone
                        naive_start_time = datetime.combine(today, datetime.min.time()) + timedelta(hours=hour)
                        start_time = ROMANIAN_TIMEZONE.localize(naive_start_time)
                        end_time = start_time + timedelta(hours=1)
                        
                        # Format with ISO 8601 including timezone offset
                        raw_today_data.append({
                            'start': start_time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                            'end': end_time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                            'value': price
                        })
                        
                        if hour == current_hour:
                            current_price = price
            
            # Load forecast data (tomorrow)
            forecast_prices = []
            raw_tomorrow_data = []
            tomorrow_valid = False
            tomorrow_prices = []
            tomorrow_file = os.path.join(self.data_dir, f"{tomorrow.strftime('%Y-%m-%d')}.json")
            
            if os.path.exists(tomorrow_file):
                with open(tomorrow_file, 'r', encoding='utf-8') as file:
                    data = json.load(file)
                    
                if 'hourly_data' in data:
                    tomorrow_valid = True
                    
                    for entry in data['hourly_data']:
                        hour = int(entry['hour'])
                        price = float(entry['price'])
                        dt_str = entry['datetime']
                        
                        tomorrow_prices.append(price)
                        
                        forecast_prices.append({
                            'timestamp': dt_str,
                            'price': price,
                            'hour': hour
                        })
                        
                        # Create raw_tomorrow entry with start/end times with proper Romanian timezone
                        naive_start_time = datetime.combine(tomorrow, datetime.min.time()) + timedelta(hours=hour)
                        start_time = ROMANIAN_TIMEZONE.localize(naive_start_time)
                        end_time = start_time + timedelta(hours=1)
                        
                        # Format with ISO 8601 including timezone offset
                        raw_tomorrow_data.append({
                            'start': start_time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                            'end': end_time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                            'value': price
                        })
            
            # Calculate next hour price
            next_hour_price = 0
            if current_hour < 23 and daily_prices and len(daily_prices) > current_hour + 1:
                next_hour_price = daily_prices[current_hour + 1]
            elif forecast_prices:
                next_hour_price = forecast_prices[0]['price']
            
            # Calculate statistics for enhanced attributes
            stats = {}
            if daily_prices:
                # Calculate average
                stats['average'] = sum(daily_prices) / len(daily_prices)
                
                # Calculate off-peak and peak prices
                # Off-peak 1: 00:00-08:00
                off_peak_1 = daily_prices[:8] if len(daily_prices) >= 8 else []
                stats['off_peak_1'] = sum(off_peak_1) / len(off_peak_1) if off_peak_1 else 0
                
                # Off-peak 2: 20:00-24:00
                off_peak_2 = daily_prices[20:] if len(daily_prices) >= 20 else []
                stats['off_peak_2'] = sum(off_peak_2) / len(off_peak_2) if off_peak_2 else 0
                
                # Peak: 08:00-20:00
                peak = daily_prices[8:20] if len(daily_prices) >= 20 else []
                stats['peak'] = sum(peak) / len(peak) if peak else 0
                
                # Min, max, mean
                stats['min'] = min(daily_prices) if daily_prices else 0
                stats['max'] = max(daily_prices) if daily_prices else 0
                stats['mean'] = statistics.mean(daily_prices) if daily_prices else 0
                
                # Low price indicator (if current price is below average)
                stats['low_price'] = current_price < stats['average']
                
                # Price percent to average
                if stats['average'] > 0:
                    stats['price_percent_to_average'] = current_price / stats['average']
                else:
                    stats['price_percent_to_average'] = 1.0
            
            # Determine status
            status = "CSV Data Available" if forecast_prices else "Forecast Pending"
            
            _LOGGER.info(f"📊 Loaded: current={current_price}, avg={stats.get('average', 0):.2f}, forecast_count={len(forecast_prices)}, status={status}")
            
            return {
                "current_price": current_price,
                "daily_average": stats.get('average', 0),
                "next_hour_price": next_hour_price,
                "daily_prices": daily_prices,
                "forecast_prices": forecast_prices,
                "download_status": status,
                "last_updated": now.isoformat(),
                "progress": 100,
                "stats": stats,
                "today": daily_prices,
                "tomorrow": tomorrow_prices,
                "tomorrow_valid": tomorrow_valid,
                "raw_today": raw_today_data,
                "raw_tomorrow": raw_tomorrow_data,
                "region": "RO",
                "currency": "RON",
                "unit": "kWh"
            }
            
        except Exception as e:
            _LOGGER.error(f"❌ Error loading current data: {e}")
            return {
                "current_price": 0,
                "daily_average": 0,
                "next_hour_price": 0,
                "daily_prices": [],
                "forecast_prices": [],
                "download_status": f"Error: {str(e)}",
                "last_updated": datetime.now().isoformat(),
                "progress": 0,
                "region": "RO",
                "currency": "RON",
                "unit": "kWh"
            }

# Sensor classes (renamed from OPCOM to Dynamic)
class DynamicBaseSensor(SensorEntity):
    """Base class for Romania Dynamic sensors."""

    def __init__(self, coordinator, sensor_type):
        """Initialize the sensor."""
        self.coordinator = coordinator
        self._sensor_type = sensor_type
        self._attr_device_class = SensorDeviceClass.MONETARY
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
        
        # Format attributes similar to Current1-price.txt
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
            "today": data.get("today", []),
            "tomorrow": data.get("tomorrow", []),
            "tomorrow_valid": data.get("tomorrow_valid", False),
            "raw_today": data.get("raw_today", []),
            "raw_tomorrow": data.get("raw_tomorrow", []),
            "last_updated": data.get("last_updated"),
            "source": "Romanian Electricity Market CSV Export"
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
    """Download status sensor."""

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator, "Download Status")
        self._attr_device_class = None
        self._attr_state_class = None

    @property
    def native_value(self):
        """Return the download status."""
        if not self.coordinator.data:
            return "Unknown"
        return self.coordinator.data.get("download_status", "Unknown")

    @property
    def icon(self):
        """Return the icon."""
        if not self.coordinator.data:
            return "mdi:help-circle"
        
        status = self.coordinator.data.get("download_status", "Unknown")
        if "Available" in status:
            return "mdi:check-circle"
        elif "Error" in status:
            return "mdi:alert-circle"
        elif "Pending" in status:
            return "mdi:clock-outline"
        else:
            return "mdi:help-circle"


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
            "source": "Romanian Electricity Market CSV Forecast"
        }