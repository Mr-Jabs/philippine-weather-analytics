BEGIN;

CREATE TABLE IF NOT EXISTS dim_cities (
    city_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_name TEXT NOT NULL UNIQUE,
    region TEXT NOT NULL,
    latitude NUMERIC(8, 5) NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude NUMERIC(8, 5) NOT NULL CHECK (longitude BETWEEN -180 AND 180)
);

INSERT INTO dim_cities (city_name, region, latitude, longitude)
VALUES
    ('Manila', 'National Capital Region', 14.59950, 120.98420),
    ('Quezon City', 'National Capital Region', 14.67600, 121.04370),
    ('Davao', 'Davao Region', 7.19070, 125.45530),
    ('Cebu', 'Central Visayas', 10.31570, 123.88540),
    ('Baguio', 'Cordillera Administrative Region', 16.40230, 120.59600),
    ('Cagayan de Oro', 'Northern Mindanao', 8.45420, 124.63190)
ON CONFLICT (city_name) DO UPDATE SET
    region = EXCLUDED.region,
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude;

CREATE TABLE IF NOT EXISTS fact_hourly_weather (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id INTEGER NOT NULL REFERENCES dim_cities(city_id),
    timestamp_ph TIMESTAMPTZ NOT NULL,
    temperature_c DOUBLE PRECISION,
    humidity_pct DOUBLE PRECISION CHECK (humidity_pct BETWEEN 0 AND 100),
    precipitation_mm DOUBLE PRECISION CHECK (precipitation_mm >= 0),
    wind_speed_kmh DOUBLE PRECISION CHECK (wind_speed_kmh >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_hourly_weather_city_timestamp
    ON fact_hourly_weather (city_id, timestamp_ph);

COMMENT ON COLUMN fact_hourly_weather.timestamp_ph IS
    'Absolute observation time. Supply timezone-aware values; display with SET TIME ZONE ''Asia/Manila''.';
COMMENT ON TABLE fact_hourly_weather IS
    'Hourly weather facts; nullable measurements preserve missing API values. Supports ON CONFLICT (city_id, timestamp_ph) DO UPDATE.';

COMMIT;
