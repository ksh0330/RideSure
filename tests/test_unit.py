from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import pandas as pd
from fastapi.testclient import TestClient

import app as app_module
import config
import data_insert
from service import BusPredictionService


class ConfigTests(unittest.TestCase):
    def test_missing_secret_names_the_setting_without_a_value(self) -> None:
        with patch.object(config, "NEO4J_PASS", ""):
            with self.assertRaisesRegex(RuntimeError, "NEO4J_PASS"):
                config.validate_required_config(("neo4j",))

    def test_configured_groups_validate(self) -> None:
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
            "destination": "세종시청,시의회,교육청",
            "routes": [
                {
                    "line_id": "B1",
                    "line_name": "B1",
                    "boarding_probability": 70.0,
                    "expected_load": 20,
                    "stops": ["대전역", "세종시청"],
                    "travel_time": 41,
                }
            ],
            "reasoning": "unit test",
            "alternatives": None,
        }
        fake_service = Mock()
        fake_service.predict_boarding.return_value = fixed
        with patch.object(app_module, "get_service", return_value=fake_service):
            response = self.client.post(
                "/api/predict",
                json={"origin": "A", "destination": "B", "departure_time": "09:00"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])


class DemoBoundaryTests(unittest.TestCase):
    def test_user_locations_and_date_do_not_change_recovered_demo_result(self) -> None:
        service = BusPredictionService.__new__(BusPredictionService)
        service.predict_with_llm = Mock(
            return_value={"reasoning": "stub", "alternatives": None}
        )
        result = service.predict_boarding(
            "임의 출발지", "임의 도착지", "17:45", "2099-01-01"
        )
        self.assertEqual(result["origin"], "대전역")
        self.assertEqual(result["destination"], "세종시청,시의회,교육청")
        self.assertEqual([route["line_id"] for route in result["routes"]], ["B1", "202/613+1002"])


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
