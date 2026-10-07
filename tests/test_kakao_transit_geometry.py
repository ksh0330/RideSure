"""Public transit geometry must never change historical route selection."""

import unittest
from unittest.mock import Mock

from kakao_transit_geometry import KakaoTransitGeometry, select_bus_path
from service import BusPredictionService


def leg(line="1000"):
    return {
        "line_name": line,
        "origin_stop": {"name": "대전역", "lat": 36.1, "lon": 127.1},
        "destination_stop": {"name": "세종터미널", "lat": 36.2, "lon": 127.2},
        "geometry_kind": "STOP_TO_STOP_APPROXIMATION",
        "geometry": [{"lat": 36.1, "lon": 127.1}, {"lat": 36.2, "lon": 127.2}],
    }


def response(line="1000", origin="대전 역", destination="세종터미널"):
    return {"status": "OK", "routes": [{"steps": [
        {"properties": {"type": "WALKING"}, "path": {"points": [[127.1, 36.1], [127.2, 36.2]]}},
        {"properties": {"type": "BUS", "vehicles": [{"name": line}],
                        "stops": [{"name": origin}, {"name": destination}]},
         "path": {"points": [[127.1, 36.1], [127.15, 36.15], [127.2, 36.2]]}},
    ]}]}


class KakaoTransitGeometryTests(unittest.TestCase):
    def test_missing_key_never_calls_api_or_draws_approximation(self):
        session = Mock()
        self.assertEqual(KakaoTransitGeometry("", session).for_leg(leg()), [])
        session.get.assert_not_called()

    def test_exact_line_and_endpoint_context_accept_bus_step_only(self):
        session = Mock()
        session.get.return_value.json.return_value = response()
        points = KakaoTransitGeometry("unit-key", session).for_leg(leg())
        self.assertEqual(len(points), 3)
        self.assertEqual(points[1], {"lat": 36.15, "lon": 127.15})
        self.assertEqual(session.get.call_args.kwargs["params"]["start_x"], 127.1)
        self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], "KakaoAK unit-key")

    def test_wrong_line_endpoint_or_distant_path_rejected(self):
        for payload in (response("1000-1"), response(origin="다른 정류장"),
                        response(destination="다른 정류장")):
            self.assertEqual(select_bus_path(payload, leg()), [])
        distant = response()
        distant["routes"][0]["steps"][1]["path"]["points"][0] = [128.0, 37.0]
        self.assertEqual(select_bus_path(distant, leg()), [])

    def test_reviewed_b1_official_endpoint_name_still_requires_b1_vehicle(self):
        ride = leg("B1")
        ride["destination_stop"]["name"] = "세종시청.교육청.시의회"
        ride["destination_stop"]["official_stop_name"] = "세종시청,시의회,교육청"
        payload = response("B1", destination="세종시청,시의회,교육청")
        self.assertEqual(len(select_bus_path(payload, ride)), 3)
        self.assertEqual(ride["destination_stop"]["name"], "세종시청.교육청.시의회")
        payload["routes"][0]["steps"][1]["properties"]["vehicles"] = [{"name": "B4"}]
        self.assertEqual(select_bus_path(payload, ride), [])
        ride["destination_stop"]["official_stop_name"] = None
        payload["routes"][0]["steps"][1]["properties"]["vehicles"] = [{"name": "B1"}]
        self.assertEqual(select_bus_path(payload, ride), [], "unverified name similarity cannot validate a BUS path")

    def test_api_failure_leaves_map_geometry_unavailable(self):
        session = Mock()
        session.get.side_effect = ValueError("bad response")
        route = leg()
        service = BusPredictionService.__new__(BusPredictionService)
        service.geometry_client = KakaoTransitGeometry("unit-key", session)
        service._enrich_map_geometry(route)
        self.assertEqual(route["map_geometry_source"], "UNAVAILABLE")
        self.assertEqual(route["map_geometry"], [])
        self.assertEqual(route["geometry_kind"], "STOP_TO_STOP_APPROXIMATION")

    def test_each_transfer_leg_checked_independently(self):
        service = BusPredictionService.__new__(BusPredictionService)
        service.geometry_client = Mock()
        service.geometry_client.for_leg.side_effect = [
            [{"lat": 36.1, "lon": 127.1}, {"lat": 36.2, "lon": 127.2}], []]
        first, second = leg("1000"), leg("1004")
        service._enrich_map_geometry(first)
        service._enrich_map_geometry(second)
        self.assertEqual(first["map_geometry_source"], "KAKAO_VERIFIED_BUS_PATH")
        self.assertEqual(second["map_geometry_source"], "UNAVAILABLE")
        self.assertEqual(service.geometry_client.for_leg.call_count, 2)


if __name__ == "__main__":
    unittest.main()
