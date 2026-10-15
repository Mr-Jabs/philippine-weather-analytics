"""Run ingestion -> transformation -> PostgreSQL loading, in that order."""

import argparse
import logging
import os
from pathlib import Path

import psycopg
import requests
from sqlalchemy.exc import SQLAlchemyError

from src.ingestion import RAW_DIR, ingest
from src.load import load
from src.transform import PROCESSED_DIR, transform

logger = logging.getLogger(__name__)


def run_pipeline(database_url: str, *, raw_dir: Path = RAW_DIR,
                 processed_dir: Path = PROCESSED_DIR, forecast_days: int = 7) -> int:
    paths = ingest(database_url, raw_dir=raw_dir, forecast_days=forecast_days)
    result = transform(paths=paths, processed_dir=processed_dir)
    count = load(result.hourly, database_url)
    logger.info("Pipeline complete: upserted %d hourly records; processed files: %s",
                count, result.output_dir)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--processed-dir", type=Path, default=PROCESSED_DIR)
    parser.add_argument("--forecast-days", type=int, choices=range(1, 17), default=7)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("Set DATABASE_URL to the PostgreSQL connection string")
    try:
        run_pipeline(database_url, raw_dir=args.raw_dir,
                     processed_dir=args.processed_dir, forecast_days=args.forecast_days)
    except (psycopg.Error, SQLAlchemyError, requests.RequestException, ValueError, OSError, RuntimeError) as error:
        # DB exception strings can include connection details/credentials.
        logger.error("Pipeline stopped (%s); check stage inputs and database configuration",
                     type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
