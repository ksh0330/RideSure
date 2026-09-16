from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock

import pandas as pd

import config
import data_insert_v2
from prediction_v2 import congestion_labels, relative_percentile
from public_data import load_tago_fixture, normalize_national_stop_rows, upsert_official_stop_batch


def fixture_frame() -> pd.DataFrame:
    rows = [
        {"노선": "B1", "기종점": "순환", "정류장순번": 1, "정류장명": "반복"},
        {"노선": "B1", "기종점": "순환", "정류장순번": 2, "정류장명": "중간"},
        {"노선": "B1", "기종점": "순환", "정류장순번": 3, "정류장명": "반복"},
    ]
    for row in rows:
        row.update({name: 0 for name in config.HOUR_COLS})
    rows[0]["01시"] = None
    rows[0]["02시"] = "not-a-number"
    return pd.DataFrame(rows)


class HistoricalV2TransformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = data_insert_v2.SourceFrame(
            path=Path("fixture_20251108.csv"),
            file_hash="a" * 64,
            service_date="2025-11-08",
            frame=fixture_frame(),
        )

    def test_repeated_stop_has_distinct_occurrences(self) -> None:
        rows = data_insert_v2.build_topology_rows([self.source])
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row["stop_id"] for row in rows}), 2)
        self.assertEqual(len({row["occurrence_id"] for row in rows}), 3)
        self.assertEqual(sum(row["previous_occurrence_id"] is not None for row in rows), 2)

    def test_zero_missing_and_invalid_are_distinct(self) -> None:
        topology = data_insert_v2.build_topology_rows([self.source])
        rows = list(
            data_insert_v2.iter_observation_rows(
                self.source, data_insert_v2.topology_lookup(topology), "test-time"
            )
        )
        first = [row for row in rows if row["source_row"] == 2]
        self.assertEqual(first[0]["parse_status"], "VALID")
        self.assertEqual(first[0]["onboard_count"], 0)
        self.assertEqual(first[1]["parse_status"], "MISSING")
        self.assertIsNone(first[1]["onboard_count"])
        self.assertEqual(first[2]["parse_status"], "INVALID")
        self.assertIsNone(first[2]["onboard_count"])

    def test_ids_are_deterministic_and_include_provenance(self) -> None:
        topology = data_insert_v2.build_topology_rows([self.source])
        lookup = data_insert_v2.topology_lookup(topology)
        first = list(data_insert_v2.iter_observation_rows(self.source, lookup, "one"))[0]
        replay = list(data_insert_v2.iter_observation_rows(self.source, lookup, "two"))[0]
        self.assertEqual(first["observation_id"], replay["observation_id"])
        self.assertEqual(first["source_file"], "fixture_20251108.csv")
        self.assertEqual(first["source_row"], 2)
        self.assertEqual(first["source_hour_column"], "00시")

    def test_upsert_queries_use_required_v2_entities_and_relationships(self) -> None:
        tx = Mock()
        tx.run.return_value.consume.return_value = None
        data_insert_v2.upsert_topology_batch(tx, [])
        topology_query = tx.run.call_args.args[0]
        for token in ("RoutePattern", "StopOccurrence", "HAS_PATTERN", "HAS_OCCURRENCE", "AT_STOP"):
            self.assertIn(token, topology_query)
        data_insert_v2.upsert_observation_batch(tx, [])
        observation_query = tx.run.call_args.args[0]
        for token in ("LoadObservation", "OBSERVED_AT", "ON_LINE", "IMPORTED_IN"):
            self.assertIn(token, observation_query)

    def test_current_csv_profile_is_lossless(self) -> None:
        profile = data_insert_v2.source_profile(data_insert_v2.load_sources(config.INPUT_FILES))
        self.assertEqual(profile["raw_rows"], 11_375)
        self.assertEqual(profile["observations"], 273_000)
        self.assertEqual(profile["route_patterns"], 154)
        self.assertEqual(profile["source_local_stops"], 2_049)
        self.assertEqual(profile["parse_status"], {"VALID": 273_000, "MISSING": 0, "INVALID": 0})


class CongestionTests(unittest.TestCase):
    def test_relative_level_is_not_a_boarding_probability(self) -> None:
        percentile = relative_percentile(20, [0, 10, 20, 30, 40])
        self.assertEqual(percentile, 50.0)
        self.assertEqual(congestion_labels(percentile), ("MEDIUM", "보통"))
        self.assertEqual(relative_percentile(0, [0, 0, 0]), 50.0)


class PublicDataAdapterTests(unittest.TestCase):
    def test_tago_fixture_preserves_missing_direction_as_unknown(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "tago_route_stops.json"
        records = load_tago_fixture(fixture)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].official_node_id, "TEST_NODE_001")
        self.assertEqual(records[0].direction_code, "TEST_DIRECTION")
        self.assertIsNone(records[1].direction_code)

    def test_national_stop_coordinates_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "out-of-range GPS_LATI"):
            normalize_national_stop_rows(
                [{"NODE_ID": "N1", "NODE_NM": "Test", "GPS_LATI": 91, "GPS_LONG": 127}]
            )

    def test_official_import_uses_official_id_and_spatial_point(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "tago_route_stops.json"
        tx = Mock()
        tx.run.return_value.consume.return_value = None
        upsert_official_stop_batch(tx, load_tago_fixture(fixture))
        query = tx.run.call_args.args[0]
        self.assertIn("official_node_id", query)
        self.assertIn("point({latitude:", query)
        self.assertIn("RouteStopStaging", query)
        self.assertIn("node_order", query)


if __name__ == "__main__":
    unittest.main()
