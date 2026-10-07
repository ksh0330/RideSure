"""Public transit geometry must never change historical route selection."""

import unittest
from unittest.mock import Mock

from kakao_transit_geometry import BusMatch, KakaoTransitGeometry, select_bus_match, select_bus_path, select_transfer_time
from service import BusPredictionService, boarding_likelihood


def leg(line="1000"):
    return {
        "line_name": line,
        "origin_stop": {"name": "대전역", "lat": 36.1, "lon": 127.1},
        "destination_stop": {"name": "세종터미널", "lat": 36.2, "lon": 127.2},
        "geometry_kind": "STOP_TO_STOP_APPROXIMATION",
        "geometry": [{"lat": 36.1, "lon": 127.1}, {"lat": 36.2, "lon": 127.2}],
    }


def response(line="1000", origin="대전 역", destination="세종터미널"):
    return {"status": "OK", "routes": [{"properties": {"totalTime": 1800}, "steps": [
        {"properties": {"type": "WALKING"}, "path": {"points": [[127.1, 36.1], [127.2, 36.2]]}},
        {"properties": {"type": "BUS", "time": 1200, "vehicles": [{"name": line}],
                        "stops": [{"name": origin}, {"name": destination}]},
         "path": {"points": [[127.1, 36.1], [127.15, 36.15], [127.2, 36.2]]}},
    ]}]}


class KakaoTransitGeometryTests(unittest.TestCase):
    def test_boarding_likelihood_is_categorical_and_never_a_probability(self):
        for level, expected in (("LOW", ("HIGH", "높음")), ("MEDIUM", ("MEDIUM", "보통")),
                                ("HIGH", ("LOW", "낮음")), ("VERY_HIGH", ("VERY_LOW", "매우 낮음"))):
            self.assertEqual(boarding_likelihood(level, "HISTORICAL_OBSERVATION"),
                             (*expected, "HISTORICAL_RELATIVE_CONGESTION"))
        self.assertEqual(boarding_likelihood("UNKNOWN", "HISTORICAL_OBSERVATION"),
                         ("UNKNOWN", "판단 불가", "INSUFFICIENT_HISTORICAL_EVIDENCE"))
        self.assertEqual(boarding_likelihood("HIGH", "REALTIME"),
                         ("UNKNOWN", "판단 불가", "INSUFFICIENT_HISTORICAL_EVIDENCE"))

    def test_direct_duration_requires_validated_bus_step(self):
        payload = response()
        match = select_bus_match(payload, leg())
        self.assertEqual((match.bus_time_seconds, match.total_time_seconds), (1200, 1800))
        self.assertIsNone(select_bus_match(response(line="1000-1"), leg()))
        self.assertIsNone(select_bus_match(response(destination="다른 정류장"), leg()))
        payload["routes"][0]["properties"].pop("totalTime")
        self.assertIsNone(select_bus_match(payload, leg()).total_time_seconds)
        self.assertEqual(len(select_bus_match(payload, leg()).points), 3)
        payload["routes"][0]["steps"][1]["properties"]["time"] = -5
        self.assertIsNone(select_bus_match(payload, leg()).bus_time_seconds)

    def test_direct_eta_rejects_extra_bus_and_prefers_valid_single_bus_route(self):
        extra = response()
        extra["routes"][0]["steps"].append(response("1004")["routes"][0]["steps"][1])
        self.assertIsNone(select_bus_match(extra, leg()).total_time_seconds)
        extra["routes"].append(response()["routes"][0])
        self.assertEqual(select_bus_match(extra, leg()).total_time_seconds, 1800)

    def test_transfer_eta_requires_ordered_lines_and_verified_transfer_identity(self):
        first = leg("1000")
        first["destination_stop"] = {"name": "옛 환승 A", "official_stop_name": "환승점",
                                      "official_stop_id": "physical-1", "lat": 36.2, "lon": 127.2}
        second = leg("1004")
        second["origin_stop"] = {"name": "옛 환승 B", "official_stop_name": "환승점",
                                  "official_stop_id": "physical-1", "lat": 36.2, "lon": 127.2}
        second["destination_stop"] = {"name": "도착", "lat": 36.3, "lon": 127.3}
        first_step = response("1000", destination="환승점")["routes"][0]["steps"][1]
        second_step = response("1004", origin="환승점", destination="도착")["routes"][0]["steps"][1]
        second_step["path"]["points"] = [[127.2, 36.2], [127.25, 36.25], [127.3, 36.3]]
        payload = {"status": "OK", "routes": [{"properties": {"totalTime": 2400},
                    "steps": [first_step, second_step]}]}
        transfer = {"official_stop_id": "physical-1", "official_stop_name": "환승점"}
        self.assertEqual(select_transfer_time(payload, [first, second], transfer), 2400)
        self.assertIsNone(select_transfer_time(payload, [second, first], transfer))
        self.assertIsNone(select_transfer_time(payload, [first, second],
                                                {**transfer, "official_stop_id": "other"}))
        payload["routes"][0]["steps"].reverse()
        self.assertIsNone(select_transfer_time(payload, [first, second], transfer))

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
        self.assertIsNone(route["estimated_duration_seconds"])
        self.assertEqual(route["geometry_kind"], "STOP_TO_STOP_APPROXIMATION")

    def test_each_transfer_leg_checked_independently(self):
        service = BusPredictionService.__new__(BusPredictionService)
        service.geometry_client = Mock()
        service.geometry_client.for_leg_match.side_effect = [
            BusMatch([{"lat": 36.1, "lon": 127.1}, {"lat": 36.2, "lon": 127.2}], 600, 900), None]
        service.geometry_client.for_leg.return_value = []
        first, second = leg("1000"), leg("1004")
        service._enrich_map_geometry(first)
        service._enrich_map_geometry(second)
        self.assertEqual(first["map_geometry_source"], "KAKAO_VERIFIED_BUS_PATH")
        self.assertEqual(second["map_geometry_source"], "UNAVAILABLE")
        self.assertEqual(service.geometry_client.for_leg_match.call_count, 2)
        self.assertEqual(first["estimated_duration_seconds"], 900)
        self.assertIsNone(second["estimated_duration_seconds"])


if __name__ == "__main__":
    unittest.main()
