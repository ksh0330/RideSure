from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import pandas as pd
from fastapi.testclient import TestClient

import app as app_module
import config
import data_insert
from service import BusPredictionService


def v2_route() -> dict:
    origin = {
        "stop_id": "s-origin",
        "occurrence_id": "o-origin",
        "name": "대전역",
        "seq": 2,
        "lat": None,
        "lon": None,
    }
    destination = {
        "stop_id": "s-destination",
        "occurrence_id": "o-destination",
        "name": "세종시청.교육청.시의회",
        "seq": 13,
        "lat": None,
        "lon": None,
    }
    return {
        "line_id": "hist-line-b1",
        "line_name": "B1",
        "pattern_id": "hist-pattern-b1",
        "terminal_description": "대전역동광장 - 대전역동광장",
        "origin_stop": origin,
        "destination_stop": destination,
        "stops": [origin, destination],
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


class ConfigTests(unittest.TestCase):
    def test_missing_secret_names_the_setting_without_a_value(self) -> None:
        with patch.object(config, "NEO4J_PASS", ""):
            with self.assertRaisesRegex(RuntimeError, "NEO4J_PASS"):
                config.validate_required_config(("neo4j",))

    def test_configured_groups_validate(self) -> None:
        configured = {
            "NEO4J_URI": "bolt://127.0.0.1:7687",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASS": "test-only-password",
            "LLM_BASE_URL": "http://127.0.0.1:8001",
            "MODEL_ID": "test-only-model",
            "HF_HOME": ".cache/test-only",
            "MODEL_PATH": ".cache/test-only/model",
        }
        with patch.multiple(config, **configured):
            config.validate_required_config(("neo4j", "llm_client", "model"))


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.app)

    def test_frontend_config_success(self) -> None:
        with patch.object(config, "KAKAO_MAP_JAVASCRIPT_KEY", "unit-test-key"):
            response = self.client.get("/api/frontend-config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kakao_map_javascript_key"], "unit-test-key")

    def test_frontend_config_missing(self) -> None:
        with patch.object(config, "KAKAO_MAP_JAVASCRIPT_KEY", ""):
            response = self.client.get("/api/frontend-config")
        self.assertEqual(response.status_code, 503)
        self.assertIn("KAKAO_MAP_JAVASCRIPT_KEY", response.json()["detail"])

    def test_health_reports_both_dependencies(self) -> None:
        fake_service = Mock()
        fake_service.check_connection.return_value = True
        fake_response = Mock(status_code=200)
        with patch.object(app_module, "get_service", return_value=fake_service), patch(
            "requests.get", return_value=fake_response
        ):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "healthy")

    def test_prediction_endpoint_smoke_with_service_stub(self) -> None:
        fixed = {
            "success": True,
            "origin": "대전역",
            "destination": "세종시청",
            "routes": [v2_route()],
            "reasoning": "unit test",
            "explanation": "unit test",
            "alternatives": [],
            "data_mode": "NEO4J_V2_HISTORICAL",
        }
        fake_service = Mock()
        fake_service.predict_boarding.return_value = fixed
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = self.client.post(
                "/api/predict",
                json={
                    "origin": "대전역",
                    "destination": "세종시청",
                    "departure_time": "08:00",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertIsNone(response.json()["routes"][0]["boarding_probability"])

    def test_prediction_coordinates_are_range_checked_and_paired(self) -> None:
        out_of_range = self.client.post(
            "/api/predict",
            json={
                "origin": "A",
                "destination": "B",
                "origin_lat": 91,
                "origin_lon": 127,
            },
        )
        self.assertEqual(out_of_range.status_code, 422)
        missing_pair = self.client.post(
            "/api/predict",
            json={"origin": "A", "destination": "B", "origin_lat": 36.3},
        )
        self.assertEqual(missing_pair.status_code, 422)


class PredictionV2ServiceTests(unittest.TestCase):
    def test_user_input_drives_v2_route_and_historical_evidence(self) -> None:
        repository = Mock()
        repository.find_stops_by_name.side_effect = [
            [{"stop_id": "s-origin", "stop_name": "대전역"}],
            [{"stop_id": "s-destination", "stop_name": "세종시청.교육청.시의회"}],
        ]
        repository.find_direct_routes.return_value = [
            {
                "line_id": "hist-line-b1",
                "line_name": "B1",
                "pattern_id": "hist-pattern-b1",
                "terminal_description": "대전역동광장 - 대전역동광장",
                "origin_occurrence_id": "o-origin",
                "destination_occurrence_id": "o-destination",
                "hops": 11,
                "stops": [
                    {
                        "stop_id": "s-origin",
                        "occurrence_id": "o-origin",
                        "stop_name": "대전역",
                        "seq": 2,
                        "lat": None,
                        "lon": None,
                    },
                    {
                        "stop_id": "s-destination",
                        "occurrence_id": "o-destination",
                        "stop_name": "세종시청.교육청.시의회",
                        "seq": 13,
                        "lat": None,
                        "lon": None,
                    },
                ],
                "geometry_kind": "UNAVAILABLE",
            }
        ]
        repository.resolve_congestion.return_value = {
            "source": "HISTORICAL_OBSERVATION",
            "status": "AVAILABLE",
            "onboard_count": 17,
            "relative_percentile": 56.6,
            "congestion_level": "MEDIUM",
            "boarding_guidance": "보통",
            "service_date": "2025-11-08",
            "hour": 8,
            "sample_size": 53,
        }
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = repository
        service.predict_with_llm = Mock(return_value="structured explanation")
        result = service.predict_boarding(
            "대전역", "세종시청", "08:00", "2025-11-08"
        )
        self.assertEqual(result["origin"], "대전역")
        self.assertEqual(result["destination"], "세종시청")
        self.assertEqual(result["routes"][0]["line_name"], "B1")
        self.assertEqual(result["routes"][0]["onboard_count"], 17)
        self.assertEqual(result["routes"][0]["boarding_guidance"], "보통")
        self.assertIsNone(result["routes"][0]["boarding_probability"])
        repository.resolve_congestion.assert_called_once_with(
            "hist-pattern-b1", "o-origin", "2025-11-08", 8
        )

    def test_llm_failure_keeps_deterministic_structured_explanation(self) -> None:
        service = BusPredictionService.__new__(BusPredictionService)
        route = v2_route()
        with patch("service.requests.post", side_effect=RuntimeError("offline")):
            explanation = service.predict_with_llm([route], "대전역", "세종시청")
        self.assertIn("B1", explanation)
        self.assertIn("17명", explanation)
        self.assertIn("탑승 확률을 뜻하지 않습니다", explanation)


class DataLoaderTests(unittest.TestCase):
    def test_required_csv_schema_is_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "required column"):
            data_insert.melt_hourly_data(pd.DataFrame({"노선": ["B1"]}), "2025-11-08")

    def test_upsert_uses_stable_merge_keys(self) -> None:
        tx = Mock()
        result = Mock()
        tx.run.return_value = result
        data_insert.upsert_batch(tx, [{"line_id": "B1"}])
        query = tx.run.call_args.args[0]
        self.assertIn("MERGE (ld:Load {id:", query)
        self.assertIn("MERGE (ld)-[:AT]->(s)", query)
        self.assertIn("MERGE (ld)-[:AFFECTS]->(l)", query)
        result.consume.assert_called_once()

    def test_expected_recovered_dataset_counts_are_explicit(self) -> None:
        self.assertEqual(data_insert.EXPECTED_COUNTS["Line"], 148)
        self.assertEqual(data_insert.EXPECTED_COUNTS["Stop"], 2_047)
        self.assertEqual(data_insert.EXPECTED_COUNTS["Load"], 172_872)


if __name__ == "__main__":
    unittest.main()
