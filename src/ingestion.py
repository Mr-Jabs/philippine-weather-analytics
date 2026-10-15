"""Fetch city forecasts from Open-Meteo and retain raw JSON before normalization.

Run with ``python -m src.ingestion`` after applying sql/schema.sql and setting
DATABASE_URL. This extraction stage does not load fact_hourly_weather.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import time
from typing import Any
from uuid import uuid4

import pandas as pd
import psycopg
from psycopg.rows import dict_row
import requests

API_URL = "https://api.open-meteo.com/v1/forecast"
PH_TIMEZONE = "Asia/Manila"
RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"
HOURLY_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
)
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class City:
    city_id: int
    city_name: str
    region: str
    latitude: float
    longitude: float


def load_cities(database_url: str) -> list[City]:
    """Read the configurable target list directly from the city dimension."""
    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT city_id, city_name, region, latitude, longitude "
                "FROM dim_cities ORDER BY city_id"
            )
            cities = [City(**{**row, "latitude": float(row["latitude"]),
                             "longitude": float(row["longitude"])})
                      for row in cursor.fetchall()]
    if not cities:
        raise ValueError("dim_cities is empty; apply sql/schema.sql or add cities first")
    return cities


def retry_after_seconds(value: str | None) -> float:
    """Support both Retry-After seconds and HTTP dates."""
    if not value:
        return 0.0
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            return max(0.0, deadline.timestamp() - datetime.now(timezone.utc).timestamp())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def fetch_forecast(
    session: requests.Session,
    city: City,
    *,
    forecast_days: int = 7,
    max_attempts: int = 5,
    backoff_seconds: float = 1.0,
    timeout: tuple[float, float] = (10.0, 60.0),
) -> dict[str, Any]:
    """Retry connection errors, timeouts, throttling, and transient server errors.

    Permanent HTTP errors (such as 400) and malformed JSON fail immediately.
    Retry-After is honored when it exceeds the exponential delay plus jitter.
    """
    if not 1 <= forecast_days <= 16:
        raise ValueError("forecast_days must be between 1 and 16")
    if max_attempts < 1 or not math.isfinite(backoff_seconds) or backoff_seconds <= 0:
        raise ValueError("max_attempts and backoff_seconds must be positive")
    params = {
        "latitude": city.latitude,
        "longitude": city.longitude,
        "hourly": ",".join(HOURLY_FIELDS),
        "timezone": PH_TIMEZONE,
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
        "forecast_days": forecast_days,
    }
    for attempt in range(max_attempts):
        retry_after = 0.0
        try:
            with session.get(API_URL, params=params, timeout=timeout) as response:
                if response.status_code in RETRYABLE_STATUSES:
                    retry_after = retry_after_seconds(response.headers.get("Retry-After"))
                    response.raise_for_status()
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("error"):
                    raise ValueError("Open-Meteo returned an invalid forecast object")
                return payload
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as error:
            if isinstance(error, requests.HTTPError):
                if error.response is None or error.response.status_code not in RETRYABLE_STATUSES:
                    raise
            if attempt + 1 == max_attempts:
                raise
            delay = max(retry_after, backoff_seconds * 2**attempt + random.uniform(0, 0.25))
            logger.warning("Retrying %s after attempt %d/%d in %.2fs",
                           city.city_name, attempt + 1, max_attempts, delay)
            time.sleep(delay)
    raise RuntimeError("Unreachable retry state")


def save_raw_response(payload: dict[str, Any], city: City, raw_dir: Path = RAW_DIR) -> Path:
    """Write the untransformed API payload to a unique file for replay/audit."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", city.city_name.lower()).strip("_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = raw_dir / f"{city.city_id}_{slug}_{stamp}_{uuid4().hex}.json"
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    with path.open("x", encoding="utf-8") as output:
        output.write(serialized)
        output.write("\n")
    return path


def hourly_dataframe(payload: dict[str, Any], city: City) -> pd.DataFrame:
    """Return raw hourly fields with city ID and aware Manila timestamps.

    Keep native API measurement names; cleaning and fact loading belong to ETL.
    """
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise ValueError("Missing hourly data")
    fields = ("time", *HOURLY_FIELDS)
    if any(not isinstance(hourly.get(field), list) for field in fields):
        raise ValueError("Missing or invalid hourly arrays")
    if len({len(hourly[field]) for field in fields}) != 1 or not hourly["time"]:
        raise ValueError("Hourly arrays must be nonempty and have equal lengths")
    if payload.get("timezone") != PH_TIMEZONE:
        raise ValueError("Expected Asia/Manila timestamps")
    frame = pd.DataFrame({field: hourly[field] for field in fields})
    timestamps = pd.to_datetime(frame.pop("time"), errors="raise")
    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(PH_TIMEZONE)
    else:
        timestamps = timestamps.dt.tz_convert(PH_TIMEZONE)
    frame.insert(0, "timestamp_ph", timestamps)
    frame.insert(0, "city_id", city.city_id)
    return frame


def ingest_cities(
    cities: list[City], *, raw_dir: Path = RAW_DIR, forecast_days: int = 7,
) -> tuple[list[Path], list[int]]:
    """Continue other cities after failures; return saved paths and failed city IDs."""
    paths: list[Path] = []
    failures: list[int] = []
    with requests.Session() as session:
        session.headers.update({"User-Agent": "PhilippineWeatherAnalytics/0.1"})
        for city in cities:
            try:
                payload = fetch_forecast(session, city, forecast_days=forecast_days)
                path = save_raw_response(payload, city, raw_dir)
                paths.append(path)
                # Persist raw JSON first, including payloads that fail normalization.
                frame = hourly_dataframe(payload, city)
                logger.info("Saved %s: %d hourly rows -> %s", city.city_name, len(frame), path)
            except (requests.RequestException, ValueError, OSError):
                failures.append(city.city_id)
                logger.error("Ingestion failed for %s (city_id=%s)", city.city_name, city.city_id)
    return paths, failures


def ingest(database_url: str, *, raw_dir: Path = RAW_DIR, forecast_days: int = 7) -> list[Path]:
    """Extract one complete city batch, returning only files from this run.

    Preserve successful raw responses on partial failure, but stop the pipeline
    before transformation/loading so an incomplete city batch is not published.
    """
    paths, failures = ingest_cities(
        load_cities(database_url), raw_dir=raw_dir, forecast_days=forecast_days,
    )
    if failures:
        raise RuntimeError(f"Ingestion failed for city IDs: {failures}; raw successes retained")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--forecast-days", type=int, choices=range(1, 17), default=7)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("Set DATABASE_URL to the PostgreSQL connection string")
    try:
        cities = load_cities(database_url)
    except (psycopg.Error, ValueError):
        logger.error("Could not load cities; check DATABASE_URL and apply sql/schema.sql")
        return 1
    paths, failures = ingest_cities(cities, raw_dir=args.raw_dir, forecast_days=args.forecast_days)
    logger.info("Saved %d raw responses; %d cities failed", len(paths), len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
