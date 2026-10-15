"""Exercise HTTP responses and actual analytical SELECTs using in-memory SQL.

SQLite fixtures store UTC timestamp strings with offsets; production uses
PostgreSQL TIMESTAMPTZ. These tests do not claim PostgreSQL integration coverage.
"""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from backend.app import create_app, get_connection, get_now
from src.transform import heat_index_c

NOW = datetime(2026, 10, 16, 4, 30, tzinfo=timezone.utc)
CITY_NAMES = ["Manila", "Quezon City", "Davao", "Cebu", "Baguio", "Cagayan de Oro"]


class APITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        with self.engine.begin() as connection:
            connection.execute(text("""CREATE TABLE dim_cities (
                city_id INTEGER PRIMARY KEY, city_name TEXT, region TEXT,
                latitude REAL, longitude REAL)"""))
            connection.execute(text("""CREATE TABLE fact_hourly_weather (
                id INTEGER PRIMARY KEY, city_id INTEGER, timestamp_ph TEXT,
                temperature_c REAL, humidity_pct REAL,
                precipitation_mm REAL, wind_speed_kmh REAL)"""))
            connection.execute(text("INSERT INTO dim_cities VALUES (:id, :name, :region, :lat, :lon)"),
                               [{"id": i, "name": name, "region": "Test region", "lat": 14.5, "lon": 121}
                                for i, name in enumerate(CITY_NAMES, 1)])
        self.add_weather(1, NOW - timedelta(days=8), 20, 60, 99, 3)
        self.add_weather(1, NOW - timedelta(hours=2), 30, 70, 1.5, 10)
        self.add_weather(1, NOW - timedelta(hours=1), 32, None, None, 12)
        self.add_weather(1, NOW + timedelta(hours=2), 50, 90, 100, 100)
        self.add_weather(4, NOW - timedelta(hours=3), 28, 80, 4, 8)
        self.add_weather(5, NOW + timedelta(hours=1), 15, 80, 2, 6)
        self.application = create_app(engine=self.engine)
        self.application.dependency_overrides[get_now] = lambda: NOW
        self.client = TestClient(self.application)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.engine.dispose()

    def add_weather(self, city_id, stamp, temperature, humidity, rain, wind):
        with self.engine.begin() as connection:
            connection.execute(text("""INSERT INTO fact_hourly_weather
                (city_id, timestamp_ph, temperature_c, humidity_pct, precipitation_mm, wind_speed_kmh)
                VALUES (:city, :stamp, :temp, :humidity, :rain, :wind)"""),
                               {"city": city_id, "stamp": str(stamp), "temp": temperature,
                                "humidity": humidity, "rain": rain, "wind": wind})

    def test_cities_returns_six_sorted_cities_with_coordinates(self):
        response = self.client.get("/api/v1/cities")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 6)
        self.assertEqual([row["city_name"] for row in body["data"]], sorted(CITY_NAMES))
        self.assertIsInstance(body["data"][0]["latitude"], float)

    def test_latest_selects_one_past_row_per_city_and_preserves_nulls(self):
        response = self.client.get("/api/v1/weather/latest")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        rows = {row["city_name"]: row for row in body["data"]}
        self.assertEqual(rows["Manila"]["temperature_c"], 32)
        self.assertIsNone(rows["Manila"]["humidity_pct"])
        self.assertIsNone(rows["Manila"]["heat_index_c"])
        self.assertIsNone(rows["Manila"]["precipitation_mm"])
        self.assertAlmostEqual(rows["Cebu"]["heat_index_c"], round(heat_index_c(28, 80), 2))
        self.assertTrue(rows["Cebu"]["timestamp_ph"].endswith("+08:00"))
        self.assertEqual(body["as_of"], "2026-10-16T12:30:00+08:00")

    def test_latest_city_filter_is_trimmed_and_case_insensitive(self):
        response = self.client.get("/api/v1/weather/latest", params={"city": "  cEbU  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["data"][0]["city_name"], "Cebu")

    def test_known_city_with_only_future_or_no_data_returns_empty(self):
        for name in ("Baguio", "Davao"):
            with self.subTest(city=name):
                response = self.client.get("/api/v1/weather/latest", params={"city": name})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"], [])

    def test_unknown_cities_and_sql_injection_input_are_404(self):
        for path in ("latest", "historical"):
            response = self.client.get(f"/api/v1/weather/{path}", params={"city": "Manila' OR 1=1 --"})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/api/v1/cities").json()["count"], 6)

    def test_historical_filters_window_city_and_orders_timestamps(self):
        response = self.client.get("/api/v1/weather/historical", params={"city": "Manila", "days": 7})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual([row["temperature_c"] for row in body["data"]], [30, 32])
        self.assertEqual(body["start"], "2026-10-09T12:30:00+08:00")
        self.assertEqual(body["end"], body["as_of"])

    def test_historical_all_cities_and_no_matches(self):
        self.assertEqual(self.client.get("/api/v1/weather/historical").json()["count"], 3)
        body = self.client.get("/api/v1/weather/historical", params={"city": "Davao"}).json()
        self.assertEqual(body["data"], [])

    def test_date_window_uses_inclusive_manila_dates_exclusive_next_midnight(self):
        self.add_weather(1, datetime(2026, 10, 14, 16, tzinfo=timezone.utc), 25, 80, 1, 5)
        self.add_weather(1, datetime(2026, 10, 15, 16, tzinfo=timezone.utc), 26, 80, 2, 5)
        response = self.client.get("/api/v1/weather/historical", params={
            "city": "Manila", "start": "2026-10-15", "end": "2026-10-15"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["data"][0]["timestamp_ph"], "2026-10-15T00:00:00+08:00")
        self.assertEqual(body["end"], "2026-10-16T00:00:00+08:00")

    def test_invalid_windows_and_blank_city_return_422(self):
        for params in ({"days": 0}, {"days": 366}, {"days": "abc"}, {"city": "   "},
                       {"start": "2026-10-15"}, {"start": "bad", "end": "2026-10-15"},
                       {"start": "2026-10-16", "end": "2026-10-15"},
                       {"start": "2026-10-17", "end": "2026-10-18"}):
            with self.subTest(params=params):
                response = self.client.get("/api/v1/weather/historical", params=params)
                self.assertEqual(response.status_code, 422)

    def test_comparison_aggregates_window_and_retains_empty_cities(self):
        response = self.client.get("/api/v1/weather/comparison")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 6)
        rows = {row["city_name"]: row for row in body["data"]}
        manila = rows["Manila"]
        self.assertEqual(manila["avg_temperature_c"], 31)
        self.assertEqual(manila["min_temperature_c"], 30)
        self.assertEqual(manila["max_temperature_c"], 32)
        self.assertEqual(manila["total_precipitation_mm"], 1.5)
        self.assertEqual(manila["avg_wind_speed_kmh"], 11)
        self.assertEqual(manila["precipitation_hours"], 1)
        self.assertEqual(manila["temperature_hours"], 2)
        self.assertEqual(rows["Baguio"]["hourly_records"], 0)
        self.assertIsNone(rows["Baguio"]["total_precipitation_mm"])
        self.assertIsNone(rows["Baguio"]["latest_timestamp_ph"])

    def test_comparison_optional_city_subset_and_validation(self):
        response = self.client.get("/api/v1/weather/comparison", params={"cities": "Manila, Cebu,Manila"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["city_name"] for row in response.json()["data"]], ["Cebu", "Manila"])
        self.assertEqual(self.client.get("/api/v1/weather/comparison", params={"cities": "Unknown"}).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/weather/comparison", params={"cities": "Manila,"}).status_code, 422)

    def test_cors_preflight_allows_frontend_gets(self):
        response = self.client.options("/api/v1/cities", headers={
            "Origin": "http://localhost:4173", "Access-Control-Request-Method": "GET"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertNotIn("access-control-allow-credentials", response.headers)

    def test_database_errors_are_503_with_cors_and_no_sensitive_details(self):
        def failed_connection():
            raise OperationalError("SECRET SQL", {}, RuntimeError("password=secret"))
            yield
        self.application.dependency_overrides[get_connection] = failed_connection
        response = self.client.get("/api/v1/cities", headers={"Origin": "http://localhost:4173"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Weather database is unavailable"})
        self.assertEqual(response.headers["access-control-allow-origin"], "*")

    def test_openapi_documents_all_four_routes(self):
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        for path in ("/api/v1/cities", "/api/v1/weather/latest", "/api/v1/weather/historical",
                     "/api/v1/weather/comparison"):
            self.assertIn(path, response.json()["paths"])


class UnconfiguredAPITests(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_missing_configuration_still_allows_docs_but_data_returns_503(self):
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/docs").status_code, 200)
            response = client.get("/api/v1/cities")
            self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
