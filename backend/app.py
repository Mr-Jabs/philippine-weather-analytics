"""Read-only weather analytics API. Run: python -m uvicorn backend.app:app."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
import logging
import math
import os
from typing import Annotated, Iterator
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import Connection, Engine, bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from src.load import database_engine
from src.transform import heat_index_c

logger = logging.getLogger(__name__)
MANILA = ZoneInfo("Asia/Manila")
TimestampPH = Annotated[AwareDatetime, AfterValidator(lambda stamp: stamp.astimezone(MANILA))]
Days = Annotated[int, Query(ge=1, le=365)]
CityName = Annotated[str | None, Query(min_length=1, max_length=100)]


class ResponseModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


class City(ResponseModel):
    city_id: int
    city_name: str
    region: str
    latitude: float
    longitude: float


class CitiesResponse(ResponseModel):
    data: list[City]
    count: int


class Weather(ResponseModel):
    city_id: int
    city_name: str
    timestamp_ph: TimestampPH
    temperature_c: float | None
    humidity_pct: float | None
    precipitation_mm: float | None
    wind_speed_kmh: float | None
    heat_index_c: float | None


class WeatherResponse(ResponseModel):
    data: list[Weather]
    count: int
    as_of: TimestampPH


class HistoricalResponse(WeatherResponse):
    start: TimestampPH
    end: TimestampPH


class Comparison(ResponseModel):
    city_id: int
    city_name: str
    region: str
    hourly_records: int
    temperature_hours: int
    humidity_hours: int
    precipitation_hours: int
    wind_hours: int
    avg_temperature_c: float | None
    min_temperature_c: float | None
    max_temperature_c: float | None
    avg_humidity_pct: float | None
    total_precipitation_mm: float | None
    avg_wind_speed_kmh: float | None
    max_wind_speed_kmh: float | None
    latest_timestamp_ph: TimestampPH | None


class ComparisonResponse(ResponseModel):
    data: list[Comparison]
    count: int
    as_of: TimestampPH
    start: TimestampPH
    end: TimestampPH


def get_now() -> datetime:
    """One clock value per request; override for deterministic tests."""
    return datetime.now(timezone.utc)


def get_connection(request: Request) -> Iterator[Connection]:
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(503, "Weather database is not configured")
    with engine.connect() as connection:
        yield connection


DB = Annotated[Connection, Depends(get_connection)]
Clock = Annotated[datetime, Depends(get_now)]


def _city_id(connection: Connection, city: str | None) -> int | None:
    if city is None:
        return None
    city = city.strip()
    if not city:
        raise HTTPException(422, "city must not be blank")
    row = connection.execute(text(
        "SELECT city_id FROM dim_cities WHERE lower(city_name) = lower(:city)"
    ), {"city": city}).first()
    if row is None:
        raise HTTPException(404, "City not found")
    return row.city_id


def _weather(row) -> Weather:
    values = dict(row)
    # PostgreSQL floating types can contain NaN/Infinity; emit JSON null instead.
    for key in ("temperature_c", "humidity_pct", "precipitation_mm", "wind_speed_kmh"):
        value = values[key]
        if value is not None and not math.isfinite(value):
            values[key] = None
    t, rh = values["temperature_c"], values["humidity_pct"]
    index = heat_index_c(t, rh) if t is not None and rh is not None else float("nan")
    values["heat_index_c"] = round(index, 2) if math.isfinite(index) else None
    return Weather(**values)


def _window(now: datetime, days: int, start: date | None, end: date | None) -> tuple[datetime, datetime]:
    """Trailing days, or inclusive Manila calendar dates capped at now."""
    if (start is None) != (end is None):
        raise HTTPException(422, "Provide both start and end dates")
    if start is None:
        return now - timedelta(days=days), now
    if end < start or (end - start).days >= 365 or start > now.astimezone(MANILA).date():
        raise HTTPException(422, "Date range must be ordered, start by today, and span at most 365 days")
    lower = datetime.combine(start, time.min, MANILA).astimezone(timezone.utc)
    if end == date.max:
        raise HTTPException(422, "End date is out of range")
    upper = min(datetime.combine(end + timedelta(days=1), time.min, MANILA), now)
    return lower, upper.astimezone(timezone.utc)


WEATHER_COLUMNS = """
    c.city_id, c.city_name, f.timestamp_ph, f.temperature_c,
    f.humidity_pct, f.precipitation_mm, f.wind_speed_kmh
"""


def create_app(*, engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        owned_engine = None
        if engine is not None:
            application.state.engine = engine
        elif database_url := os.environ.get("DATABASE_URL"):
            try:
                owned_engine = database_engine(database_url)
                application.state.engine = owned_engine
            except (ValueError, SQLAlchemyError):
                logger.error("Invalid database configuration; API data routes will return 503")
        try:
            yield
        finally:
            if owned_engine is not None:
                owned_engine.dispose()

    application = FastAPI(
        title="Philippine Weather Analytics API", version="1.0.0", lifespan=lifespan,
        description="Hourly model weather data in Asia/Manila. Future forecast hours are excluded.",
    )
    application.state.engine = engine
    application.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=False,
        allow_methods=["GET"], allow_headers=["*"],
    )

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        # Avoid exposing SQL, connection strings, and credentials in responses/logs.
        logger.error("Weather database query failed (%s)", type(error).__name__)
        return JSONResponse(status_code=503, content={"detail": "Weather database is unavailable"})

    @application.get("/api/v1/cities", response_model=CitiesResponse, tags=["Cities"])
    def cities(connection: DB):
        rows = connection.execute(text(
            "SELECT city_id, city_name, region, latitude, longitude FROM dim_cities ORDER BY city_name"
        )).mappings().all()
        return CitiesResponse(data=[City(**row) for row in rows], count=len(rows))

    @application.get("/api/v1/weather/latest", response_model=WeatherResponse, tags=["Weather"])
    def latest(connection: DB, now: Clock, city: CityName = None):
        city_id = _city_id(connection, city)
        # Correlated MAX uses the existing (city_id, timestamp_ph) unique index.
        sql = "SELECT " + WEATHER_COLUMNS + """
            FROM dim_cities c JOIN fact_hourly_weather f ON f.city_id = c.city_id
            AND f.timestamp_ph = (
                SELECT MAX(w.timestamp_ph) FROM fact_hourly_weather w
                WHERE w.city_id = c.city_id AND w.timestamp_ph <= :as_of
            )
        """
        params = {"as_of": now}
        if city_id is not None:
            sql += " WHERE c.city_id = :city_id"
            params["city_id"] = city_id
        rows = connection.execute(text(sql + " ORDER BY c.city_name"), params).mappings().all()
        return WeatherResponse(data=[_weather(row) for row in rows], count=len(rows), as_of=now)

    @application.get("/api/v1/weather/historical", response_model=HistoricalResponse, tags=["Weather"])
    def historical(connection: DB, now: Clock, city: CityName = None, days: Days = 7,
                   start: date | None = None, end: date | None = None):
        lower, upper = _window(now, days, start, end)
        city_id = _city_id(connection, city)
        sql = "SELECT " + WEATHER_COLUMNS + """
            FROM fact_hourly_weather f JOIN dim_cities c ON c.city_id = f.city_id
            WHERE f.timestamp_ph >= :start AND f.timestamp_ph < :end
        """
        params = {"start": lower, "end": upper}
        if city_id is not None:
            sql += " AND c.city_id = :city_id"
            params["city_id"] = city_id
        rows = connection.execute(text(sql + " ORDER BY c.city_id, f.timestamp_ph"), params).mappings().all()
        return HistoricalResponse(data=[_weather(row) for row in rows], count=len(rows),
                                  as_of=now, start=lower, end=upper)

    @application.get("/api/v1/weather/comparison", response_model=ComparisonResponse, tags=["Weather"])
    def comparison(connection: DB, now: Clock, days: Days = 7,
                   cities: Annotated[str | None, Query(min_length=1, max_length=1000)] = None):
        lower, upper = _window(now, days, None, None)
        selected = None
        if cities is not None:
            names = [name.strip() for name in cities.split(",")]
            if any(not name for name in names) or len(names) > 50:
                raise HTTPException(422, "Provide 1–50 comma-separated city names")
            selected = sorted({_city_id(connection, name) for name in names})
        sql = """
            SELECT c.city_id, c.city_name, c.region,
                COUNT(f.timestamp_ph) AS hourly_records,
                COUNT(f.temperature_c) AS temperature_hours,
                COUNT(f.humidity_pct) AS humidity_hours,
                COUNT(f.precipitation_mm) AS precipitation_hours,
                COUNT(f.wind_speed_kmh) AS wind_hours,
                AVG(f.temperature_c) AS avg_temperature_c,
                MIN(f.temperature_c) AS min_temperature_c,
                MAX(f.temperature_c) AS max_temperature_c,
                AVG(f.humidity_pct) AS avg_humidity_pct,
                SUM(f.precipitation_mm) AS total_precipitation_mm,
                AVG(f.wind_speed_kmh) AS avg_wind_speed_kmh,
                MAX(f.wind_speed_kmh) AS max_wind_speed_kmh,
                MAX(f.timestamp_ph) AS latest_timestamp_ph
            FROM dim_cities c LEFT JOIN fact_hourly_weather f ON f.city_id = c.city_id
                AND f.timestamp_ph >= :start AND f.timestamp_ph < :end
        """
        if selected is not None:
            sql += " WHERE c.city_id IN :city_ids"
        statement = text(sql + " GROUP BY c.city_id, c.city_name, c.region ORDER BY c.city_name")
        params = {"start": lower, "end": upper}
        if selected is not None:
            statement = statement.bindparams(bindparam("city_ids", expanding=True))
            params["city_ids"] = selected
        rows = connection.execute(statement, params).mappings().all()
        data = []
        for row in rows:
            values = {key: (None if isinstance(value, float) and not math.isfinite(value) else value)
                      for key, value in row.items()}
            data.append(Comparison(**values))
        return ComparisonResponse(data=data, count=len(data), as_of=now, start=lower, end=upper)

    return application


app = create_app()
