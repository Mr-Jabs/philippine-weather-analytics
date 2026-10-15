"""ETL checks with real raw-file IO and PostgreSQL SQL compilation."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
from sqlalchemy.dialects import postgresql

from src.ingestion import ingest
from src.load import _records, load, upsert_statement
from src.main import run_pipeline
from src.transform import FACT_COLUMNS, clean_hourly, daily_rainfall, heat_index_c, read_raw, transform


def raw_payload(times=None, timezone="UTC"):
    times = times or ["2026-10-14T16:00", "2026-10-14T17:00"]
    count = len(times)
    return {"timezone": timezone, "hourly": {
        "time": times, "temperature_2m": [30] * count,
        "relative_humidity_2m": [70] * count,
        "precipitation": [1.5] * count, "wind_speed_10m": [10] * count,
    }}


def write_raw(folder, name="1_manila.json", payload=None):
    path = Path(folder) / name
    path.write_text(json.dumps(payload or raw_payload()), encoding="utf-8")
    return path


def hourly_frame(count=2):
    return pd.DataFrame({
        "city_id": [1] * count,
        "timestamp_ph": pd.date_range("2026-10-15", periods=count, freq="h", tz="Asia/Manila"),
        "temperature_c": [30.0] * count, "humidity_pct": [70.0] * count,
        "precipitation_mm": [1.5] * count, "wind_speed_kmh": [10.0] * count,
    })


class TransformTests(unittest.TestCase):
    def test_utc_manila_and_explicit_offsets_represent_same_instant(self):
        with tempfile.TemporaryDirectory() as folder:
            utc = read_raw(write_raw(folder))
            local = read_raw(write_raw(folder, payload=raw_payload(
                ["2026-10-15T00:00", "2026-10-15T01:00"], "Asia/Manila")))
            aware = read_raw(write_raw(folder, payload=raw_payload(
                ["2026-10-14T16:00Z", "2026-10-14T17:00+00:00"], "Asia/Manila")))
            epoch = read_raw(write_raw(folder, payload=raw_payload(
                [1791993600, 1791997200])))
        self.assertEqual(utc.timestamp_ph.tolist(), local.timestamp_ph.tolist())
        self.assertEqual(utc.timestamp_ph.tolist(), aware.timestamp_ph.tolist())
        self.assertEqual(utc.timestamp_ph.tolist(), epoch.timestamp_ph.tolist())
        self.assertEqual(utc.timestamp_ph.iloc[0].isoformat(), "2026-10-15T00:00:00+08:00")

    def test_cleaning_preserves_partial_nulls_and_cool_baguio_values(self):
        frame = hourly_frame(4).astype({"temperature_c": "object", "humidity_pct": "object"})
        frame.loc[0, "temperature_c"] = "12.5"
        frame.loc[1, "humidity_pct"] = 101
        frame.loc[2, "temperature_c"] = "bad"
        frame.loc[2, "precipitation_mm"] = -1
        frame.loc[3, list(FACT_COLUMNS[2:])] = None
        result = clean_hourly(frame)
        self.assertEqual(len(result), 3)
        self.assertEqual(result.temperature_c.iloc[0], 12.5)
        self.assertTrue(pd.isna(result.humidity_pct.iloc[1]))
        self.assertTrue(pd.isna(result.heat_index_c.iloc[1]))
        self.assertTrue(pd.isna(result.precipitation_mm.iloc[2]))
        self.assertEqual(str(result.city_id.dtype), "int64")
        self.assertEqual(str(result.temperature_c.dtype), "float64")

    def test_heat_index_matches_known_nws_hot_and_cool_cases(self):
        self.assertAlmostEqual(heat_index_c((90 - 32) / 1.8, 70), (105.9220206 - 32) / 1.8, places=5)
        t = 68
        simple_average = (0.5 * (t + 61 + (t - 68) * 1.2 + 50 * 0.094) + t) / 2
        self.assertAlmostEqual(heat_index_c(20, 50), (simple_average - 32) / 1.8)

    def test_daily_totals_respect_local_day_and_completeness(self):
        daily = daily_rainfall(clean_hourly(hourly_frame(25)))
        self.assertEqual(daily.rainfall_hours.tolist(), [24, 1])
        self.assertEqual(daily.daily_rainfall_mm.iloc[0], 36)
        self.assertTrue(pd.isna(daily.daily_rainfall_mm.iloc[1]))
        self.assertEqual(daily.rainfall_available_mm.iloc[1], 1.5)

    def test_latest_retrieval_wins_and_processed_files_are_written(self):
        with tempfile.TemporaryDirectory() as folder:
            old = write_raw(folder, "1_manila_20261015T010000000000Z_old.json")
            newer_data = raw_payload()
            newer_data["hourly"]["precipitation"] = [4, 5]
            new = write_raw(folder, "1_manila_20261015T020000000000Z_new.json", newer_data)
            result = transform(paths=[new, old], processed_dir=Path(folder) / "processed")
            self.assertEqual(result.hourly.precipitation_mm.tolist(), [4, 5])
            self.assertEqual(result.daily.rainfall_available_mm.iloc[0], 9)
            self.assertTrue((result.output_dir / "hourly_weather.csv").exists())
            self.assertTrue((result.output_dir / "daily_weather.csv").exists())

    def test_bad_timestamps_are_dropped_but_bad_units_fail_file(self):
        data = raw_payload(["invalid", "2026-10-14T17:00"])
        with tempfile.TemporaryDirectory() as folder:
            frame = clean_hourly(read_raw(write_raw(folder, payload=data)))
            self.assertEqual(len(frame), 1)
            data["hourly_units"] = {"temperature_2m": "°F"}
            with self.assertRaisesRegex(ValueError, "units"):
                read_raw(write_raw(folder, payload=data))


class LoaderTests(unittest.TestCase):
    def test_sql_has_conflict_update_and_null_parameters(self):
        frame = clean_hourly(hourly_frame())
        frame.loc[0, "humidity_pct"] = float("nan")
        records = _records(frame)
        compiled = upsert_statement(records).compile(dialect=postgresql.dialect())
        sql = str(compiled)
        self.assertIn("ON CONFLICT (city_id, timestamp_ph) DO UPDATE SET", sql)
        self.assertIn("precipitation_mm = excluded.precipitation_mm", sql)
        self.assertNotIn("heat_index_c", sql)
        self.assertNotIn("id, city_id", sql)
        self.assertIsNone(records[0]["humidity_pct"])
        self.assertIsNotNone(records[0]["timestamp_ph"].tzinfo)

    def test_batches_share_one_transaction_and_deduplicate(self):
        frame = pd.concat([hourly_frame(3), hourly_frame(3)], ignore_index=True)
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        self.assertEqual(load(frame, engine=engine, batch_size=2), 3)
        engine.begin.assert_called_once()
        connection = engine.begin.return_value.__enter__.return_value
        self.assertEqual(connection.execute.call_count, 2)
        engine.dispose.assert_not_called()

    def test_later_batch_failure_exits_transaction_with_error(self):
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        context = engine.begin.return_value
        context.__exit__.return_value = False
        context.__enter__.return_value.execute.side_effect = [None, RuntimeError("failed")]
        with self.assertRaises(RuntimeError):
            load(hourly_frame(3), engine=engine, batch_size=2)
        self.assertIs(context.__exit__.call_args.args[0], RuntimeError)

    def test_naive_timestamps_rejected_before_database_write(self):
        frame = hourly_frame()
        frame["timestamp_ph"] = frame.timestamp_ph.dt.tz_localize(None)
        engine = MagicMock()
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            load(frame, engine=engine)
        engine.begin.assert_not_called()


class OrchestrationTests(unittest.TestCase):
    @patch("src.main.load")
    @patch("src.main.transform")
    @patch("src.main.ingest")
    def test_sequential_stages_use_only_current_batch(self, extract, transform_stage, load_stage):
        order = MagicMock()
        order.attach_mock(extract, "ingest")
        order.attach_mock(transform_stage, "transform")
        order.attach_mock(load_stage, "load")
        extract.return_value = [Path("new.json")]
        transform_stage.return_value.hourly = hourly_frame()
        load_stage.return_value = 2
        self.assertEqual(run_pipeline("postgresql://test"), 2)
        self.assertEqual([call[0] for call in order.mock_calls if call[0] in {"ingest", "transform", "load"}],
                         ["ingest", "transform", "load"])
        self.assertEqual(transform_stage.call_args.kwargs["paths"], [Path("new.json")])

    @patch("src.main.transform")
    @patch("src.main.ingest", side_effect=RuntimeError("partial failure"))
    def test_failed_ingestion_prevents_downstream_stages(self, extract, transform_stage):
        with self.assertRaises(RuntimeError):
            run_pipeline("postgresql://test")
        transform_stage.assert_not_called()

    @patch("src.ingestion.load_cities", return_value=[])
    @patch("src.ingestion.ingest_cities", return_value=([Path("success.json")], [2]))
    def test_ingest_wrapper_stops_incomplete_city_batches(self, ingest_cities, load_cities):
        with self.assertRaisesRegex(RuntimeError, "city IDs"):
            ingest("postgresql://test")


if __name__ == "__main__":
    unittest.main()
