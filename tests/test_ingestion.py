"""Behavior checks for retries, raw preservation, and timezone handling."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from src import ingestion


CITY = ingestion.City(1, "Manila", "National Capital Region", 14.5995, 120.9842)


def payload():
    return {
        "timezone": "Asia/Manila",
        "hourly": {
            "time": ["2026-10-15T00:00", "2026-10-15T01:00"],
            "temperature_2m": [27.5, None],
            "relative_humidity_2m": [80, 83],
            "precipitation": [0, 1.2],
            "wind_speed_10m": [6.5, 7.0],
        },
    }


def response(status=200, data=None, headers=None):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status
    result.headers = headers or {}
    result.json.return_value = data if data is not None else payload()
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    return result


class IngestionTests(unittest.TestCase):
    @patch("src.ingestion.random.uniform", return_value=0)
    @patch("src.ingestion.time.sleep")
    def test_retries_network_and_rate_limit_with_correct_parameters(self, sleep, jitter):
        session = MagicMock()
        session.get.side_effect = [requests.Timeout(), response(429, headers={"Retry-After": "4"}), response()]
        self.assertEqual(ingestion.fetch_forecast(session, CITY), payload())
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 4])
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["timezone"], "Asia/Manila")
        self.assertEqual(params["hourly"].split(","), list(ingestion.HOURLY_FIELDS))
        self.assertEqual(params["wind_speed_unit"], "kmh")
        self.assertEqual(params["latitude"], CITY.latitude)

    @patch("src.ingestion.time.sleep")
    def test_permanent_http_failure_does_not_retry(self, sleep):
        session = MagicMock()
        session.get.return_value = response(400)
        with self.assertRaises(requests.HTTPError):
            ingestion.fetch_forecast(session, CITY)
        self.assertEqual(session.get.call_count, 1)
        sleep.assert_not_called()

    @patch("src.ingestion.time.sleep")
    @patch("src.ingestion.random.uniform", return_value=0)
    def test_retry_exhaustion_is_bounded_and_exponential(self, jitter, sleep):
        session = MagicMock()
        session.get.return_value = response(503)
        with self.assertRaises(requests.HTTPError):
            ingestion.fetch_forecast(session, CITY)
        self.assertEqual(session.get.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8])

    def test_raw_files_preserve_payload_and_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            first = ingestion.save_raw_response(payload(), CITY, Path(folder))
            second = ingestion.save_raw_response(payload(), CITY, Path(folder))
            self.assertNotEqual(first, second)
            self.assertEqual(json.loads(first.read_text(encoding="utf-8")), payload())
            self.assertTrue(first.name.startswith("1_manila_"))

    def test_frame_has_aware_manila_timestamps_and_preserves_missing_values(self):
        frame = ingestion.hourly_dataframe(payload(), CITY)
        self.assertEqual(len(frame), 2)
        self.assertEqual(str(frame.timestamp_ph.dt.tz), "Asia/Manila")
        self.assertEqual(frame.timestamp_ph.iloc[0].isoformat(), "2026-10-15T00:00:00+08:00")
        self.assertEqual(frame.city_id.tolist(), [1, 1])
        self.assertTrue(frame.temperature_2m.isna().iloc[1])

    def test_misaligned_arrays_are_rejected(self):
        data = payload()
        data["hourly"]["precipitation"] = []
        with self.assertRaisesRegex(ValueError, "equal lengths"):
            ingestion.hourly_dataframe(data, CITY)

    @patch("src.ingestion.psycopg.connect")
    def test_cities_are_read_from_database(self, connect):
        cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [CITY.__dict__]
        self.assertEqual(ingestion.load_cities("postgresql://test"), [CITY])
        self.assertIn("FROM dim_cities", cursor.execute.call_args.args[0])

    @patch("src.ingestion.fetch_forecast")
    def test_one_city_failure_does_not_stop_other_cities(self, fetch):
        second_city = ingestion.City(2, "Quezon City", CITY.region, 14.676, 121.0437)
        fetch.side_effect = [requests.Timeout(), payload()]
        with tempfile.TemporaryDirectory() as folder:
            paths, failures = ingestion.ingest_cities([CITY, second_city], raw_dir=Path(folder))
            self.assertEqual(len(paths), 1)
            self.assertEqual(failures, [1])
            self.assertTrue(paths[0].name.startswith("2_quezon_city_"))

    @patch("src.ingestion.fetch_forecast")
    def test_raw_data_survives_normalization_failure(self, fetch):
        fetch.return_value = {"timezone": "Asia/Manila", "hourly": {}}
        with tempfile.TemporaryDirectory() as folder:
            paths, failures = ingestion.ingest_cities([CITY], raw_dir=Path(folder))
            self.assertEqual(failures, [1])
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].exists())


if __name__ == "__main__":
    unittest.main()
