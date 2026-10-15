"""Clean Open-Meteo raw files and create hourly and daily processed datasets."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
from typing import Iterable
from uuid import uuid4

import pandas as pd

from src.ingestion import PH_TIMEZONE, RAW_DIR

PROCESSED_DIR = Path(__file__).resolve().parents[1] / "data" / "processed"
METRIC_MAP = {
    "temperature_2m": "temperature_c",
    "relative_humidity_2m": "humidity_pct",
    "precipitation": "precipitation_mm",
    "wind_speed_10m": "wind_speed_kmh",
}
FACT_COLUMNS = ["city_id", "timestamp_ph", *METRIC_MAP.values()]
logger = logging.getLogger(__name__)


@dataclass
class TransformResult:
    hourly: pd.DataFrame
    daily: pd.DataFrame
    output_dir: Path


def heat_index_c(temperature_c: float, humidity_pct: float) -> float:
    """NWS simple/Rothfusz heat index with humidity adjustments, in Celsius.

    Source: https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml
    Missing input produces NaN. This is a shade/light-wind estimate.
    """
    if not math.isfinite(temperature_c) or not math.isfinite(humidity_pct):
        return float("nan")
    if not 0 <= humidity_pct <= 100:
        return float("nan")
    t, rh = temperature_c * 1.8 + 32, humidity_pct
    simple = 0.5 * (t + 61 + (t - 68) * 1.2 + rh * 0.094)
    hi = (simple + t) / 2
    if hi >= 80:
        hi = (-42.379 + 2.04901523*t + 10.14333127*rh - 0.22475541*t*rh
              - 0.00683783*t*t - 0.05481717*rh*rh + 0.00122874*t*t*rh
              + 0.00085282*t*rh*rh - 0.00000199*t*t*rh*rh)
        if rh < 13 and 80 <= t <= 112:
            hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
        elif rh > 85 and 80 <= t <= 87:
            hi += ((rh - 85) / 10) * ((87 - t) / 5)
    return (hi - 32) / 1.8


def _timestamp(value: object, source_timezone: str) -> pd.Timestamp:
    """Respect embedded offsets; localize only naive values, then convert."""
    if value is None or isinstance(value, bool):
        return pd.NaT
    try:
        if isinstance(value, (int, float)):
            return pd.to_datetime(value, unit="s", utc=True).tz_convert(PH_TIMEZONE)
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return pd.NaT
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(source_timezone)
        return stamp.tz_convert(PH_TIMEZONE)
    except (ValueError, TypeError, OverflowError):
        return pd.NaT


def _file_order(path: Path) -> tuple[float, str]:
    """Prefer retrieval time embedded by ingestion over file copy/mtime order."""
    match = re.search(r"_(\d{8}T\d{12}Z)_", path.name)
    stamp = (datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ")
             .replace(tzinfo=timezone.utc).timestamp() if match else path.stat().st_mtime)
    return stamp, path.name


def read_raw(path: Path) -> pd.DataFrame:
    """Read one payload; ingestion filenames supply the original city ID."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError(f"Invalid forecast object: {path.name}")
    match = re.match(r"([1-9]\d*)_", path.name)
    if not match or int(match[1]) > 2_147_483_647:
        raise ValueError(f"Raw filename must begin with a PostgreSQL city ID: {path.name}")
    source_timezone = payload.get("timezone") or "UTC"
    # Validate metadata once: unsupported zones must not silently erase all rows.
    try:
        ZoneInfo(source_timezone)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid timezone metadata: {path.name}") from error
    hourly = payload.get("hourly")
    fields = ["time", *METRIC_MAP]
    if not isinstance(hourly, dict) or any(not isinstance(hourly.get(key), list) for key in fields):
        raise ValueError(f"Missing hourly arrays: {path.name}")
    if len({len(hourly[key]) for key in fields}) != 1:
        raise ValueError(f"Misaligned hourly arrays: {path.name}")
    expected_units = {"temperature_2m": "°C", "relative_humidity_2m": "%",
                      "precipitation": "mm", "wind_speed_10m": "km/h"}
    units = payload.get("hourly_units", {})
    if any(key in units and units[key] != unit for key, unit in expected_units.items()):
        raise ValueError(f"Unexpected measurement units: {path.name}")
    frame = pd.DataFrame({key: hourly[key] for key in fields}).rename(columns=METRIC_MAP)
    stamps = [_timestamp(value, source_timezone) for value in frame.pop("time")]
    frame.insert(0, "timestamp_ph", pd.Series(stamps, dtype=f"datetime64[ns, {PH_TIMEZONE}]"))
    frame.insert(0, "city_id", int(match[1]))
    return frame


def clean_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast metrics, null invalid values, drop unusable/duplicate records.

    Preserve partially missing rows because the fact schema permits SQL NULLs.
    Temperature limits are deliberately broad enough for cool Baguio nights.
    """
    frame = frame[FACT_COLUMNS].copy()
    frame["city_id"] = frame["city_id"].astype("int64")
    limits = {"temperature_c": (-10, 55), "humidity_pct": (0, 100),
              "precipitation_mm": (0, float("inf")), "wind_speed_kmh": (0, float("inf"))}
    for field, (low, high) in limits.items():
        raw_values = frame[field].mask(frame[field].map(lambda value: isinstance(value, bool)))
        values = pd.to_numeric(raw_values, errors="coerce").astype("float64")
        valid = values.between(low, high) & values.map(math.isfinite)
        frame[field] = values.where(valid)
    frame = frame.dropna(subset=["timestamp_ph"])
    frame = frame[frame.timestamp_ph == frame.timestamp_ph.dt.floor("h")]
    # Select newest forecast as a whole row, including explicit missing values.
    frame = frame.drop_duplicates(["city_id", "timestamp_ph"], keep="last")
    frame = frame.dropna(subset=list(METRIC_MAP.values()), how="all")
    frame = frame.sort_values(["city_id", "timestamp_ph"]).reset_index(drop=True)
    frame["heat_index_c"] = pd.Series(
        [heat_index_c(t, rh) for t, rh in zip(frame.temperature_c, frame.humidity_pct)],
        dtype="float64",
    )
    return frame


def daily_rainfall(hourly: pd.DataFrame) -> pd.DataFrame:
    """Aggregate deduplicated rainfall by Manila calendar day, with coverage.

    daily_rainfall_mm is NULL unless all 24 hours have valid precipitation;
    rainfall_available_mm retains the subtotal for partial days.
    """
    frame = hourly.assign(date_ph=hourly.timestamp_ph.dt.date)
    daily = frame.groupby(["city_id", "date_ph"], as_index=False).agg(
        rainfall_available_mm=("precipitation_mm", lambda values: values.sum(min_count=1)),
        rainfall_hours=("precipitation_mm", "count"),
    )
    daily["rainfall_hours"] = daily.rainfall_hours.astype("int64")
    daily["is_complete_day"] = daily.rainfall_hours.eq(24)
    daily["daily_rainfall_mm"] = daily.rainfall_available_mm.where(daily.is_complete_day).astype("float64")
    return daily


def transform(
    raw_dir: Path = RAW_DIR, *, paths: Iterable[Path] | None = None,
    processed_dir: Path = PROCESSED_DIR,
) -> TransformResult:
    """Transform a selected ingestion batch or replay all raw JSON files.

    On overlaps the newest retrieval wins. Fail closed on malformed files.
    Each successful transform creates a new processed run directory.
    """
    selected = [Path(path) for path in paths] if paths is not None else list(Path(raw_dir).glob("*.json"))
    if not selected:
        raise ValueError("No raw JSON files found")
    frames = [read_raw(path) for path in sorted(selected, key=_file_order)]
    hourly = clean_hourly(pd.concat(frames, ignore_index=True))
    if hourly.empty:
        raise ValueError("No usable hourly records after cleaning")
    daily = daily_rainfall(hourly)
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid4().hex[:8]
    output_dir = Path(processed_dir) / run
    output_dir.mkdir(parents=True, exist_ok=False)
    hourly.to_csv(output_dir / "hourly_weather.csv", index=False)
    daily.to_csv(output_dir / "daily_weather.csv", index=False)
    logger.info("Transformed %d files to %d hourly rows and %d city-days: %s",
                len(selected), len(hourly), len(daily), output_dir)
    return TransformResult(hourly, daily, output_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--processed-dir", type=Path, default=PROCESSED_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    transform(args.raw_dir, processed_dir=args.processed_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
