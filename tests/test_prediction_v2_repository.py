from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from prediction_v2 import V2TransitRepository


class FakeResult:
    def __init__(self, rows: list[dict[str, object]] | None = None):
        self.rows = rows or []

    def data(self) -> list[dict[str, object]]:
        return self.rows

    def single(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, driver: "FakeDriver"):
        self.driver = driver

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def run(self, cypher: str, **parameters: object) -> FakeResult:
        self.driver.calls.append((cypher, parameters))
        rows = self.driver.results.pop(0) if self.driver.results else []
        return FakeResult(rows)


class FakeDriver:
    def __init__(self, *results: list[dict[str, object]]):
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def session(self) -> FakeSession:
        return FakeSession(self)


class StopLookupTests(unittest.TestCase):
    def test_nearby_stops_returns_empty_when_graph_has_no_coordinates(self) -> None:
        driver = FakeDriver([])
        repository = V2TransitRepository(driver)

        self.assertEqual(repository.find_nearby_stops(36.5, 127.3, 750, 5), [])
        query, parameters = driver.calls[0]
        self.assertIn("properties(stop)['location'] IS NOT NULL", query)
        self.assertIn("properties(stop)['lat'] IS NOT NULL", query)
        self.assertIn("point.distance", query)
        self.assertIn("StopOccurrence", query)
        self.assertEqual(parameters["radius_m"], 750.0)
        self.assertEqual(parameters["limit"], 5)

    def test_nearby_stops_rejects_invalid_coordinates(self) -> None:
        repository = V2TransitRepository(FakeDriver())
        invalid = (
            (91, 127, 100),
            (36, 181, 100),
            (36, 127, 0),
            (float("nan"), 127, 100),
        )
        for lat, lon, radius in invalid:
            with self.subTest(lat=lat, lon=lon, radius=radius):
                with self.assertRaises(ValueError):
                    repository.find_nearby_stops(lat, lon, radius)

    def test_name_lookup_orders_raw_normalized_prefix_and_partial(self) -> None:
        candidates = [
            {"stop_id": "partial", "stop_name": "동대전역", "line_names": ["2"]},
            {"stop_id": "prefix", "stop_name": "대전역네거리", "line_names": ["3"]},
            {"stop_id": "normalized", "stop_name": "대전 역", "line_names": ["B1"]},
            {"stop_id": "exact", "stop_name": "대전역", "line_names": ["B1", "2"]},
        ]
        driver = FakeDriver(candidates, [])
        repository = V2TransitRepository(driver)

        found = repository.find_stops_by_name(" 대전역 ")
        self.assertEqual([item["stop_id"] for item in found],
                         ["exact", "normalized", "prefix", "partial"])
        self.assertEqual([item["match_kind"] for item in found],
                         ["RAW_EXACT", "NORMALIZED_EXACT", "NORMALIZED_PREFIX", "PARTIAL"])
        self.assertTrue(found[0]["exact_match"])
        self.assertFalse(found[1]["exact_match"])
        self.assertEqual(found[0]["line_names"], ["B1", "2"])
        self.assertIn("StopOccurrence", driver.calls[0][0])
        self.assertIn("VERIFIED_OFFICIAL_STOP", driver.calls[1][0])

    def test_punctuation_only_variation_and_verified_official_alias(self) -> None:
        historical = [{"stop_id": "historical", "stop_name": "법원.검찰청", "line_names": ["B1"]}]
        alias = [{"stop_id": "historical", "stop_name": "법원.검찰청",
                  "occurrence_id": "verified-occ", "official_stop_name": "법원,검찰청",
                  "official_stop_id": "official-1", "line_name": "B1", "pattern_id": "p1",
                  "lat": 36.4, "lon": 127.3}]
        driver = FakeDriver(historical, alias)
        found = V2TransitRepository(driver).find_stops_by_name("법원,검찰청")
        self.assertEqual([row["match_kind"] for row in found],
                         ["NORMALIZED_EXACT", "VERIFIED_OFFICIAL_ALIAS"])
        self.assertEqual(found[1]["occurrence_id"], "verified-occ")
        self.assertEqual(found[1]["official_stop_name"], "법원,검찰청")
        self.assertEqual(found[1]["lat"], 36.4)
        query = driver.calls[1][0]
        self.assertIn("verified.mapping_status IN ['EXACT', 'SEQUENCE_MATCH']", query)
        self.assertIn("verified.official_node_id = official.official_node_id", query)

    def test_unverified_name_similarity_does_not_create_alias(self) -> None:
        driver = FakeDriver([], [])
        self.assertEqual(V2TransitRepository(driver).find_stops_by_name("현재 공식명"), [])
        selected = FakeDriver([])
        self.assertIsNone(V2TransitRepository(selected).find_verified_stop_occurrence_by_id("unverified"))
        self.assertIn("VERIFIED_OFFICIAL_STOP", selected.calls[0][0])

    def test_selected_stop_lookup_uses_id_and_requires_routeable_occurrence(self) -> None:
        selected = {"stop_id": "chosen", "stop_name": "동명", "line_names": ["1001"]}
        driver = FakeDriver([selected])
        repository = V2TransitRepository(driver)

        self.assertEqual(repository.find_stop_by_id(" chosen "), selected)
        query, parameters = driver.calls[0]
        self.assertIn("stop.stop_id AS stop_id", query)
        self.assertIn("StopOccurrence", query)
        self.assertIn("line.name", query)
        self.assertEqual(parameters["stop_id"], "chosen")
        self.assertIsNone(V2TransitRepository(FakeDriver([])).find_stop_by_id("missing"))


class DirectRouteTests(unittest.TestCase):
    def test_selected_occurrence_filters_both_routing_queries(self) -> None:
        driver = FakeDriver([], [])
        repository = V2TransitRepository(driver)
        repository.find_direct_routes(
            "과거 출발", "과거 도착", origin_stop_id="start-stop",
            destination_stop_id="end-stop", origin_occurrence_id="start-occ",
            destination_occurrence_id="end-occ",
        )
        repository.find_one_transfer_routes(
            "과거 출발", "과거 도착", origin_stop_id="start-stop",
            destination_stop_id="end-stop", origin_occurrence_id="start-occ",
            destination_occurrence_id="end-occ",
        )
        for query, parameters in driver.calls:
            self.assertIn("origin_occurrence.occurrence_id = $origin_occurrence_id", query)
            self.assertIn("destination_occurrence.occurrence_id = $destination_occurrence_id", query)
            self.assertEqual(parameters["origin_occurrence_id"], "start-occ")
            self.assertEqual(parameters["destination_occurrence_id"], "end-occ")

    def test_direct_route_keeps_repeated_occurrences_and_real_stop_fields(self) -> None:
        route = {
            "pattern_id": "p1",
            "origin_stop_id": "repeat",
            "destination_stop_id": "repeat",
            "stops": [
                {
                    "occurrence_id": "occ-1",
                    "seq": 1,
                    "stop_id": "repeat",
                    "stop_name": "반복",
                    "lat": None,
                    "lon": None,
                },
                {
                    "occurrence_id": "occ-3",
                    "seq": 3,
                    "stop_id": "repeat",
                    "stop_name": "반복",
                    "lat": None,
                    "lon": None,
                },
            ],
        }
        driver = FakeDriver([route])
        repository = V2TransitRepository(driver)

        self.assertEqual(
            repository.find_direct_routes_by_stop_ids("repeat", "repeat"), [route]
        )
        query, parameters = driver.calls[0]
        self.assertIn("NEXT*1..500", query)
        self.assertIn("occurrence.pattern_id = pattern.pattern_id", query)
        self.assertIn("endNode(next_relationship).seq", query)
        self.assertIn("stop_id: stop.stop_id", query)
        self.assertIn("stop_name: stop.name", query)
        self.assertIn("OPTIONAL MATCH (occurrence)-[verified:VERIFIED_OFFICIAL_STOP]->(official:Stop)", query)
        self.assertIn("lat: official.lat", query)
        self.assertIn("lon: official.lon", query)
        self.assertNotIn("lat: properties(stop)['lat']", query)
        self.assertEqual(parameters["origin_stop_id"], "repeat")
        self.assertEqual(parameters["destination_stop_id"], "repeat")

    def test_route_stop_slice_uses_occurrence_ids_not_stop_names(self) -> None:
        stops = [
            {"occurrence_id": "o1", "seq": 1, "stop_id": "repeat"},
            {"occurrence_id": "o2", "seq": 2, "stop_id": "middle"},
            {"occurrence_id": "o3", "seq": 3, "stop_id": "repeat"},
        ]
        repository = V2TransitRepository(FakeDriver(stops))

        self.assertEqual(repository.get_route_stops("p1", "o2", "o3"), stops[1:])

    def test_occurrence_coordinates_do_not_change_historical_names_or_collapse_duplicates(self) -> None:
        stops = [
            {"occurrence_id": "o27", "seq": 27, "stop_id": "historical-osong",
             "stop_name": "오송역2.3.4", "lat": 36.6, "lon": 127.3},
            {"occurrence_id": "o28", "seq": 28, "stop_id": "historical-osong",
             "stop_name": "오송역2.3.4", "lat": 36.61, "lon": 127.31},
            {"occurrence_id": "o29", "seq": 29, "stop_id": "historical-other",
             "stop_name": "누리리", "lat": None, "lon": None},
        ]
        driver = FakeDriver(stops)
        repository = V2TransitRepository(driver)

        self.assertEqual(repository.get_route_stops("p1"), stops)
        query, _ = driver.calls[0]
        self.assertIn("OPTIONAL MATCH (occurrence)-[verified:VERIFIED_OFFICIAL_STOP]->(official:Stop)", query)
        self.assertIn("stop.name AS stop_name", query)
        self.assertIn("official.lat AS lat", query)
        self.assertIn("official.lon AS lon", query)
        self.assertNotIn("properties(stop)['lat'] AS lat", query)
        self.assertEqual(stops[0]["stop_id"], stops[1]["stop_id"])
        self.assertNotEqual(stops[0]["lat"], stops[1]["lat"])
        self.assertIsNone(stops[2]["lat"])


class OneTransferRouteTests(unittest.TestCase):
    def candidate(self) -> dict[str, object]:
        return {
            "line1_id": "line-1", "line1_name": "1000", "pattern1_id": "pattern-1",
            "pattern1_terminal": "start-end", "origin_stop_id": "origin",
            "origin_stop_name": "과거 출발명", "origin_occurrence_id": "origin-occ",
            "transfer1_occurrence_id": "transfer-a", "transfer1_name": "역사 이름 A",
            "transfer1_seq": 2, "leg1_hops": 1,
            "transfer1_mapping": {
                "mapping_status": "SEQUENCE_MATCH", "mapping_method": "GLOBAL",
                "official_node_id": "physical-1", "official_route_id": "route-1",
                "created_at": object(),
            },
            "line2_id": "line-2", "line2_name": "1004", "pattern2_id": "pattern-2",
            "pattern2_terminal": "start-end", "transfer2_occurrence_id": "transfer-b",
            "transfer2_name": "역사 이름 B", "transfer2_seq": 8,
            "destination_stop_id": "destination", "destination_stop_name": "과거 도착명",
            "destination_occurrence_id": "destination-occ", "destination_seq": 9,
            "leg2_hops": 1, "transfer_official_stop_id": "physical-1",
            "transfer_official_stop_name": "공식 물리 정류장",
            "transfer2_mapping": {
                "mapping_status": "EXACT", "official_node_id": "physical-1",
                "official_route_id": "route-2",
            },
        }

    def test_verified_shared_stop_returns_two_ordered_legs_and_provenance(self) -> None:
        candidate = self.candidate()
        leg1_stops = [
            {"occurrence_id": "origin-occ", "seq": 1, "stop_id": "origin", "stop_name": "과거 출발명", "lat": 36.1, "lon": 127.1},
            {"occurrence_id": "transfer-a", "seq": 2, "stop_id": "transfer-a-stop", "stop_name": "역사 이름 A", "lat": 36.2, "lon": 127.2},
        ]
        leg2_stops = [
            {"occurrence_id": "transfer-b", "seq": 8, "stop_id": "transfer-b-stop", "stop_name": "역사 이름 B", "lat": 36.2, "lon": 127.2},
            {"occurrence_id": "destination-occ", "seq": 9, "stop_id": "destination", "stop_name": "과거 도착명", "lat": 36.3, "lon": 127.3},
        ]
        driver = FakeDriver([candidate], leg1_stops, leg2_stops)
        repository = V2TransitRepository(driver)

        itineraries = repository.find_one_transfer_routes("출발", "도착")

        self.assertEqual(len(itineraries), 1)
        itinerary = itineraries[0]
        self.assertEqual(itinerary["transfer_count"], 1)
        self.assertEqual(itinerary["total_hops"], 2)
        self.assertEqual(itinerary["transfer"]["official_stop_id"], "physical-1")
        self.assertEqual(itinerary["transfer"]["historical_name_leg1"], "역사 이름 A")
        self.assertEqual(itinerary["transfer"]["historical_name_leg2"], "역사 이름 B")
        self.assertEqual(len(itinerary["legs"]), 2)
        self.assertEqual(itinerary["legs"][0]["stops"], leg1_stops)
        self.assertNotIn("created_at", itinerary["transfer"]["mapping_leg1"])
        query, parameters = driver.calls[0]
        self.assertIn("NEXT*1..500", query)
        self.assertIn("endNode(rel).seq = startNode(rel).seq + 1", query)
        self.assertIn("pattern2.pattern_id <> pattern1.pattern_id", query)
        self.assertIn("VERIFIED_OFFICIAL_STOP", query)
        self.assertIn("mapping1.official_node_id = physical.official_node_id", query)
        self.assertIn("mapping2.official_node_id = physical.official_node_id", query)
        self.assertNotIn("<-[:NEXT", query)
        self.assertEqual(parameters["origin_name"], "출발")

    def test_missing_official_identity_returns_no_transfer(self) -> None:
        driver = FakeDriver([])
        repository = V2TransitRepository(driver)
        self.assertEqual(repository.find_one_transfer_routes("같은 이름", "도착"), [])
        query = driver.calls[0][0]
        self.assertIn("VERIFIED_OFFICIAL_STOP", query)
        self.assertIn("mapping2.official_node_id = physical.official_node_id", query)

    def test_transfer_results_are_bounded(self) -> None:
        repository = V2TransitRepository(FakeDriver())
        with self.assertRaises(ValueError):
            repository.find_one_transfer_routes("출발", "도착", limit=51)


class RouteGeometryTests(unittest.TestCase):
    def test_geometry_is_unavailable_if_any_selected_stop_lacks_coordinates(self) -> None:
        stops = [
            {"occurrence_id": "o1", "lat": 36.1, "lon": 127.1},
            {"occurrence_id": "o2", "lat": None, "lon": None},
        ]
        repository = V2TransitRepository(FakeDriver(stops))

        geometry = repository.get_route_geometry("p1")
        self.assertEqual(geometry["geometry_kind"], "UNAVAILABLE")
        self.assertEqual(geometry["coordinates"], [])
        self.assertEqual(geometry["missing_coordinates"], 1)

    def test_geometry_preserves_stop_order_when_all_coordinates_exist(self) -> None:
        stops = [
            {"occurrence_id": "o1", "lat": 36.1, "lon": 127.1},
            {"occurrence_id": "o2", "lat": 36.2, "lon": 127.2},
        ]
        repository = V2TransitRepository(FakeDriver(stops))

        geometry = repository.get_route_geometry("p1")
        self.assertEqual(geometry["geometry_kind"], "STOP_TO_STOP_APPROXIMATION")
        self.assertEqual(
            geometry["coordinates"],
            [{"lat": 36.1, "lon": 127.1}, {"lat": 36.2, "lon": 127.2}],
        )


class CongestionFallbackTests(unittest.TestCase):
    def test_realtime_then_historical_then_profile_precedence_is_preserved(self) -> None:
        repository = V2TransitRepository(FakeDriver())
        now = datetime(2025, 11, 8, 0, tzinfo=timezone.utc)
        historical = {
            "observation_id": "historical",
            "onboard_count": 17,
            "population": [10, 17, 20],
        }
        with (
            patch.object(repository, "_realtime_observation", return_value=None),
            patch.object(repository, "_historical_observation", return_value=historical),
            patch.object(repository, "_historical_profile") as profile,
        ):
            result = repository.resolve_congestion(
                "p1", "o1", "2025-11-08", 8, now=now,
                realtime_max_age=timedelta(minutes=10),
            )

        self.assertEqual(result["source"], "HISTORICAL_OBSERVATION")
        self.assertEqual(result["onboard_count"], 17)
        profile.assert_not_called()
        self.assertNotIn("boarding_probability", result)


if __name__ == "__main__":
    unittest.main()
