from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import app as app_module


def _v2_result() -> dict:
    origin_stop = {
        "stop_id": "stop-origin",
        "occurrence_id": "occ-origin",
        "name": "대전역",
        "seq": 2,
        "lat": None,
        "lon": None,
        "distance_m": None,
    }
    destination_stop = {
        "stop_id": "stop-destination",
        "occurrence_id": "occ-destination",
        "name": "세종시청",
        "seq": 13,
        "lat": None,
        "lon": None,
        "distance_m": None,
    }
    explanation = (
        "B1 직행 경로를 안내합니다. 08시 historical 재차인원은 17명이며 "
        "상대 혼잡 안내는 '보통'입니다."
    )
    return {
        "success": True,
        "origin": "대전역",
        "destination": "세종시청",
        "routes": [
            {
                "line_id": "line-b1",
                "line_name": "B1",
                "pattern_id": "pattern-b1",
                "terminal_description": "synthetic service stub",
                "origin_stop": origin_stop,
                "destination_stop": destination_stop,
                "stops": [origin_stop, destination_stop],
                "geometry": [],
                "geometry_kind": "UNAVAILABLE",
                "onboard_count": 17,
                "relative_percentile": 56.6,
                "congestion_level": "MEDIUM",
                "boarding_guidance": "보통",
                "evidence_source": "HISTORICAL_OBSERVATION",
                "congestion_status": "AVAILABLE",
                "service_date": "2025-11-08",
                "hour": 8,
                "sample_size": 53,
                "boarding_probability": None,
                "expected_load": None,
                "travel_time": None,
            }
        ],
        "reasoning": explanation,
        "explanation": explanation,
        "alternatives": [],
        "data_mode": "NEO4J_V2_HISTORICAL",
    }


class PredictionApiV2ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.app)

    def test_structured_historical_result_keeps_probability_nullable(self) -> None:
        fake_service = Mock()
        fake_service.predict_boarding.return_value = _v2_result()
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = self.client.post(
                "/api/predict",
                json={
                    "origin": "대전역",
                    "destination": "세종시청",
                    "departure_time": "08:00",
                    "date": "2025-11-08",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        route = payload["routes"][0]
        self.assertEqual(payload["data_mode"], "NEO4J_V2_HISTORICAL")
        self.assertEqual(route["onboard_count"], 17)
        self.assertEqual(route["congestion_level"], "MEDIUM")
        self.assertEqual(route["evidence_source"], "HISTORICAL_OBSERVATION")
        self.assertIsNone(route["boarding_probability"])
        self.assertIsNone(route["expected_load"])
        self.assertIsNone(route["travel_time"])
        self.assertEqual(route["geometry_kind"], "UNAVAILABLE")
        self.assertEqual(route["geometry"], [])
        self.assertEqual(payload["itineraries"], [])

    def test_verified_duration_and_likelihood_survive_predict_response_model(self) -> None:
        result = _v2_result()
        route = result["routes"][0]
        route.update({
            "boarding_likelihood": "MEDIUM",
            "boarding_likelihood_label": "보통",
            "boarding_likelihood_basis": "HISTORICAL_RELATIVE_CONGESTION",
            "estimated_travel_time_seconds": 2533,
            "estimated_travel_time_minutes": 42,
            "travel_time_source": "KAKAO_VERIFIED_TRANSIT_ROUTE",
        })
        fake_service = Mock()
        fake_service.predict_boarding.return_value = result
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = self.client.post("/api/predict", json={"origin": "대전역", "destination": "세종시청"})
        self.assertEqual(response.status_code, 200)
        api_route = response.json()["routes"][0]
        self.assertEqual(api_route["boarding_likelihood_label"], "보통")
        self.assertEqual(api_route["estimated_travel_time_seconds"], 2533)
        self.assertEqual(api_route["estimated_travel_time_minutes"], 42)
        self.assertEqual(api_route["travel_time_source"], "KAKAO_VERIFIED_TRANSIT_ROUTE")
        self.assertIsNone(api_route["boarding_probability"])

    def test_recommendation_survives_predict_response_model(self) -> None:
        result = _v2_result()
        result["routes"][0]["boarding_likelihood"] = "LOW"
        result["routes"][0]["boarding_likelihood_label"] = "낮음"
        better = {**result["routes"][0], "line_name": "1000",
                  "boarding_likelihood": "MEDIUM", "boarding_likelihood_label": "보통"}
        result["routes"].append(better)
        result["recommended_alternative"] = {
            "kind": "DIRECT", "index": 1,
            "boarding_likelihood": "MEDIUM", "boarding_likelihood_label": "보통",
        }
        result["recommendation_reason"] = "과거 재차인원 기반 탑승 여유가 높습니다."
        option_leg = {
            "line_name": "B1", "origin_name": "대전역", "destination_name": "세종시청",
            "stop_count": 2, "onboard_count": 17, "congestion_level": "HIGH",
            "congestion_label": "혼잡", "boarding_likelihood_label": "낮음",
        }
        result["route_options"] = [
            {
                "option_type": "DIRECT", "source_index": 0, "tag": "CURRENT",
                "title": "B1", "legs": [option_leg], "boarding_likelihood": "LOW",
                "boarding_likelihood_label": "낮음", "boarding_likelihood_basis": "HISTORICAL_RELATIVE_CONGESTION",
                "congestion_level": "HIGH", "congestion_label": "혼잡", "onboard_count": 17,
                "stop_count": 2, "transfer_count": 0,
            },
            {
                "option_type": "DIRECT", "source_index": 1, "tag": "RECOMMENDED",
                "title": "1000", "legs": [{**option_leg, "line_name": "1000"}],
                "boarding_likelihood": "MEDIUM", "boarding_likelihood_label": "보통",
                "boarding_likelihood_basis": "HISTORICAL_RELATIVE_CONGESTION",
                "congestion_level": "MEDIUM", "congestion_label": "보통", "onboard_count": 7,
                "stop_count": 2, "transfer_count": 0,
                "recommendation_reason": result["recommendation_reason"],
            },
        ]
        fake_service = Mock()
        fake_service.predict_boarding.return_value = result
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = self.client.post("/api/predict", json={"origin": "대전역", "destination": "세종시청"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recommended_alternative"]["index"], 1)
        self.assertIn("탑승 여유", response.json()["recommendation_reason"])
        self.assertEqual([option["tag"] for option in response.json()["route_options"]],
                         ["CURRENT", "RECOMMENDED"])

    def test_one_transfer_response_is_additive_and_validates_exactly_two_legs(self) -> None:
        first, second = _v2_result()["routes"][0], _v2_result()["routes"][0].copy()
        first.update({"line_name": "1000", "pattern_id": "p1", "hops": 2,
                      "origin_occurrence_id": "origin-occ", "destination_occurrence_id": "transfer-a"})
        second.update({"line_name": "1004", "pattern_id": "p2", "hops": 3,
                       "origin_occurrence_id": "transfer-b", "destination_occurrence_id": "dest-occ"})
        transfer_result = _v2_result()
        transfer_result["routes"] = []
        transfer_result["itineraries"] = [{
            "transfer_count": 1,
            "total_hops": 5,
            "transfer": {
                "official_stop_id": "SJB123", "official_stop_name": "공식 정류장",
                "historical_name_leg1": "과거 표기 A", "historical_name_leg2": "과거 표기 B",
                "leg1_occurrence_id": "o1", "leg2_occurrence_id": "o2",
                "leg1_seq": 4, "leg2_seq": 8,
                "mapping_leg1": {"mapping_status": "SEQUENCE_MATCH"},
                "mapping_leg2": {"mapping_status": "EXACT"},
            },
            "legs": [first, second],
        }]
        fake_service = Mock()
        fake_service.predict_boarding.return_value = transfer_result
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = TestClient(app_module.app).post(
                "/api/predict", json={"origin": "출발", "destination": "도착"}
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["routes"], [])
        self.assertEqual(payload["itineraries"][0]["transfer_count"], 1)
        self.assertEqual(len(payload["itineraries"][0]["legs"]), 2)
        self.assertEqual(payload["itineraries"][0]["transfer"]["official_stop_id"], "SJB123")

    def test_optional_coordinates_are_forwarded_as_pairs(self) -> None:
        fake_service = Mock()
        fake_service.predict_boarding.return_value = _v2_result()
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = self.client.post(
                "/api/predict",
                json={
                    "origin": "대전역",
                    "destination": "세종시청",
                    "origin_lat": 36.332,
                    "origin_lon": 127.434,
                    "destination_lat": 36.48,
                    "destination_lon": 127.289,
                },
            )

        self.assertEqual(response.status_code, 200)
        kwargs = fake_service.predict_boarding.call_args.kwargs
        self.assertEqual(kwargs["origin_lat"], 36.332)
        self.assertEqual(kwargs["destination_lon"], 127.289)

    def test_selected_stop_ids_are_forwarded_with_original_text(self) -> None:
        fake_service = Mock()
        fake_service.predict_boarding.return_value = _v2_result()
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = self.client.post(
                "/api/predict",
                json={
                    "origin": "사용자가 입력한 출발지",
                    "destination": "사용자가 입력한 도착지",
                    "origin_stop_id": " stop-origin ",
                    "destination_stop_id": "stop-destination",
                    "destination_occurrence_id": " occ-destination ",
                },
            )

        self.assertEqual(response.status_code, 200)
        kwargs = fake_service.predict_boarding.call_args.kwargs
        self.assertEqual(kwargs["origin"], "사용자가 입력한 출발지")
        self.assertEqual(kwargs["destination"], "사용자가 입력한 도착지")
        self.assertEqual(kwargs["origin_stop_id"], "stop-origin")
        self.assertEqual(kwargs["destination_stop_id"], "stop-destination")
        self.assertEqual(kwargs["destination_occurrence_id"], "occ-destination")

    def test_blank_stop_id_is_rejected(self) -> None:
        response = self.client.post(
            "/api/predict",
            json={"origin": "대전역", "destination": "세종시청", "origin_stop_id": "   "},
        )
        self.assertEqual(response.status_code, 422)

    def test_occurrence_id_requires_a_stop_id(self) -> None:
        response = self.client.post(
            "/api/predict",
            json={"origin": "대전역", "destination": "도착", "origin_occurrence_id": "occ-1"},
        )
        self.assertEqual(response.status_code, 422)

    def test_unpaired_or_out_of_range_coordinates_are_rejected(self) -> None:
        unpaired = self.client.post(
            "/api/predict",
            json={
                "origin": "대전역",
                "destination": "세종시청",
                "origin_lat": 36.332,
            },
        )
        out_of_range = self.client.post(
            "/api/predict",
            json={
                "origin": "대전역",
                "destination": "세종시청",
                "origin_lat": 91,
                "origin_lon": 127.434,
            },
        )
        self.assertEqual(unpaired.status_code, 422)
        self.assertEqual(out_of_range.status_code, 422)

    def test_invalid_stop_lookup_remains_a_client_error(self) -> None:
        fake_service = Mock()
        fake_service.predict_boarding.side_effect = ValueError(
            "현재 Neo4j v2 데이터에서 이용 가능한 직접 노선을 찾지 못했습니다."
        )
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = self.client.post(
                "/api/predict",
                json={"origin": "없는 출발지", "destination": "없는 도착지"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("직접 노선", response.json()["detail"])

    def test_valid_stop_pair_without_supported_route_returns_empty_result(self) -> None:
        fake_service = Mock()
        result = _v2_result()
        result.update({"result_status": "NO_SUPPORTED_ROUTE", "routes": [], "itineraries": [],
                       "reasoning": "현재 데이터 범위에서 경로를 찾지 못했습니다.",
                       "explanation": "현재 데이터 범위에서 경로를 찾지 못했습니다."})
        fake_service.predict_boarding.return_value = result
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = self.client.post(
                "/api/predict", json={"origin": "출발", "destination": "도착"}
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["result_status"], "NO_SUPPORTED_ROUTE")
        self.assertEqual(payload["routes"], [])
        self.assertEqual(payload["itineraries"], [])


class StopSearchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.app)

    def test_search_keeps_distinct_ids_and_real_coordinates(self) -> None:
        fake_service = Mock()
        fake_service.search_stops.return_value = [
            {
                "stop_id": "exact-a",
                "stop_name": "세종시청",
                "line_names": ["B1", "1001"],
                "occurrence_count": 3,
                "exact_match": True,
                "lat": None,
                "lon": None,
            },
            {
                "stop_id": "exact-b",
                "stop_name": "세종시청",
                "line_names": ["2001"],
                "occurrence_count": 1,
                "exact_match": True,
                "lat": 36.48,
                "lon": 127.29,
            },
        ]
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = self.client.get("/api/stops/search", params={"q": " 세종시청 ", "limit": 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["q"], "세종시청")
        stops = response.json()["stops"]
        self.assertEqual([stop["stop_id"] for stop in stops], ["exact-a", "exact-b"])
        self.assertEqual(stops[0]["line_names"], ["B1", "1001"])
        self.assertIsNone(stops[0]["lat"])
        self.assertEqual(stops[1]["lat"], 36.48)
        fake_service.search_stops.assert_called_once_with("세종시청", 2)

    def test_search_rejects_empty_or_unbounded_queries(self) -> None:
        requests = [
            {},
            {"q": "   "},
            {"q": "x" * 101},
            {"q": "대전", "limit": 0},
            {"q": "대전", "limit": 21},
        ]
        with patch.object(app_module, "get_service") as get_service:
            for params in requests:
                with self.subTest(params=params):
                    response = self.client.get("/api/stops/search", params=params)
                    self.assertEqual(response.status_code, 422)
        get_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
