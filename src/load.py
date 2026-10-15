"""Transactional PostgreSQL bulk UPSERT using SQLAlchemy and psycopg 3."""

from __future__ import annotations

import math

import pandas as pd
from sqlalchemy import Column, DateTime, Double, Integer, MetaData, Table, create_engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine, make_url

from src.transform import FACT_COLUMNS

WEATHER = Table(
    "fact_hourly_weather", MetaData(),
    Column("city_id", Integer, nullable=False),
    Column("timestamp_ph", DateTime(timezone=True), nullable=False),
    *(Column(name, Double) for name in FACT_COLUMNS[2:]),
)


def database_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must reference PostgreSQL")
    return create_engine(url.set(drivername="postgresql+psycopg"),
                         connect_args={"connect_timeout": 10}, pool_pre_ping=True)


def _records(frame: pd.DataFrame) -> list[dict]:
    missing = set(FACT_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing fact columns: {sorted(missing)}")
    if not isinstance(frame.timestamp_ph.dtype, pd.DatetimeTZDtype):
        raise ValueError("timestamp_ph must contain timezone-aware timestamps")
    records = []
    for row in frame[FACT_COLUMNS].itertuples(index=False, name=None):
        city, stamp, *metrics = row
        if pd.isna(city) or isinstance(city, bool) or not 0 < city <= 2_147_483_647 or int(city) != city:
            raise ValueError("city_id must be a positive PostgreSQL integer")
        if pd.isna(stamp) or stamp != stamp.floor("h"):
            raise ValueError("timestamp_ph must be non-null and hourly")
        values = [None if pd.isna(value) else float(value) for value in metrics]
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("Fact metrics must be finite or NULL")
        _, humidity, rain, wind = values
        if ((humidity is not None and not 0 <= humidity <= 100)
                or (rain is not None and rain < 0) or (wind is not None and wind < 0)):
            raise ValueError("Fact metrics violate schema constraints")
        records.append(dict(zip(FACT_COLUMNS, [int(city), stamp.to_pydatetime(), *values])))
    # Guard against PostgreSQL's 'cannot affect row a second time' bulk error.
    return list({(row["city_id"], row["timestamp_ph"]): row for row in records}.values())


def upsert_statement(records: list[dict]):
    statement = insert(WEATHER).values(records)
    return statement.on_conflict_do_update(
        index_elements=[WEATHER.c.city_id, WEATHER.c.timestamp_ph],
        set_={name: statement.excluded[name] for name in FACT_COLUMNS[2:]},
    )


def load(
    frame: pd.DataFrame, database_url: str | None = None, *,
    engine: Engine | None = None, batch_size: int = 1000,
) -> int:
    """UPSERT all batches in one transaction; return distinct submitted rows.

    The database generates IDs. Derived CSV columns are excluded. Any error
    rolls back the whole run, including previously submitted batches.
    """
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 5000:
        raise ValueError("batch_size must be an integer between 1 and 5000")
    records = _records(frame)
    if not records:
        return 0
    owns_engine = engine is None
    if engine is None:
        if not database_url:
            raise ValueError("Provide database_url or engine")
        engine = database_engine(database_url)
    if engine.dialect.name != "postgresql":
        raise ValueError("The loader requires PostgreSQL")
    try:
        with engine.begin() as connection:
            for start in range(0, len(records), batch_size):
                connection.execute(upsert_statement(records[start:start + batch_size]))
    finally:
        if owns_engine:
            engine.dispose()
    return len(records)
