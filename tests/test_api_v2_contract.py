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

    def test_route_lookup_failure_is_a_client_error_not_a_fake_route(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
