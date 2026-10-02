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

    def test_name_lookup_returns_all_candidates_and_orders_exact_first_in_query(self) -> None:
        candidates = [
            {
                "stop_id": "exact",
                "stop_name": "세종시청",
                "exact_match": True,
            },
            {
                "stop_id": "expanded",
                "stop_name": "세종시청.교육청.시의회",
                "exact_match": False,
            },
        ]
        driver = FakeDriver(candidates)
        repository = V2TransitRepository(driver)

        self.assertEqual(repository.find_stops_by_name(" 세종시청 "), candidates)
        query, parameters = driver.calls[0]
        self.assertIn("CONTAINS", query)
        self.assertIn("ORDER BY exact_match DESC, prefix_match DESC", query)
        self.assertIn("stop.stop_id AS stop_id", query)
        self.assertEqual(parameters["name_query"], "세종시청")

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
        self.assertIn("lat: properties(stop)['lat']", query)
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
