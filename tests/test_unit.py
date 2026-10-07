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
    def test_geometry_requires_every_historical_occurrence_coordinate(self) -> None:
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = Mock()
        service.repository.resolve_congestion.return_value = {
            "source": "UNKNOWN", "status": "INSUFFICIENT_DATA",
            "onboard_count": None, "relative_percentile": None,
            "congestion_level": "UNKNOWN", "boarding_guidance": "데이터 부족",
        }
        route = {
            "line_id": "hist-line-b1", "line_name": "B1", "pattern_id": "hist-pattern-b1",
            "origin_occurrence_id": "o1", "destination_occurrence_id": "o3",
            "stops": [
                {"occurrence_id": "o1", "stop_name": "역사적 출발", "lat": 36.3, "lon": 127.3},
                {"occurrence_id": "o2", "stop_name": "역사적 중간", "lat": None, "lon": None},
                {"occurrence_id": "o3", "stop_name": "역사적 도착", "lat": 36.4, "lon": 127.4},
            ],
            "geometry": [{"lat": 36.3, "lon": 127.3}, {"lat": 36.4, "lon": 127.4}],
            "geometry_kind": "STOP_TO_STOP_APPROXIMATION",
        }
        result = service._build_route_result(route, {}, {}, "2025-11-08", 8)
        self.assertEqual(result["geometry_kind"], "UNAVAILABLE")
        self.assertEqual(result["geometry"], [])
        self.assertEqual([stop["name"] for stop in result["stops"]],
                         ["역사적 출발", "역사적 중간", "역사적 도착"])

        route["stops"][1]["lat"] = 36.35
        route["stops"][1]["lon"] = 127.35
        complete = service._build_route_result(route, {}, {}, "2025-11-08", 8)
        self.assertEqual(complete["geometry_kind"], "STOP_TO_STOP_APPROXIMATION")
        self.assertEqual(len(complete["geometry"]), 3)

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
        repository.find_direct_routes.assert_called_once_with(
            "대전역", "세종시청.교육청.시의회", limit=20,
            origin_stop_id=None, destination_stop_id=None,
        )

    def test_selected_stop_ids_drive_route_without_name_reresolution(self) -> None:
        repository = Mock()
        repository.find_stop_by_id.side_effect = [
            {"stop_id": "s-origin", "stop_name": "동명"},
            {"stop_id": "s-destination", "stop_name": "도착"},
        ]
        route = v2_route()
        route.update(
            origin_stop_id="s-origin",
            destination_stop_id="s-destination",
            origin_occurrence_id="o-origin",
            destination_occurrence_id="o-destination",
            hops=1,
        )
        repository.find_direct_routes.return_value = [route]
        repository.resolve_congestion.return_value = {
            "source": "UNKNOWN", "status": "INSUFFICIENT_DATA",
            "onboard_count": None, "relative_percentile": None,
            "congestion_level": "UNKNOWN", "boarding_guidance": "데이터 부족",
            "sample_size": 0,
        }
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = repository
        service.predict_with_llm = Mock(return_value="structured explanation")

        result = service.predict_boarding(
            "입력한 출발지", "입력한 도착지",
            origin_lat=36.3, origin_lon=127.4,
            origin_stop_id="s-origin", destination_stop_id="s-destination",
        )

        self.assertEqual(result["origin"], "입력한 출발지")
        self.assertEqual(result["destination"], "입력한 도착지")
        self.assertEqual(result["routes"][0]["line_name"], "B1")
        repository.find_stops_by_name.assert_not_called()
        repository.find_nearby_stops.assert_not_called()
        repository.find_direct_routes.assert_called_once_with(
            "동명", "도착", limit=20,
            origin_stop_id="s-origin", destination_stop_id="s-destination",
        )

    def test_verified_alias_selection_uses_its_occurrence(self) -> None:
        repository = Mock()
        repository.find_verified_stop_occurrence_by_id.return_value = {
            "stop_id": "s-origin", "stop_name": "과거 명칭", "occurrence_id": "verified-occ",
            "official_stop_name": "현재 명칭", "official_stop_id": "official-1",
        }
        repository.find_stop_by_id.return_value = {"stop_id": "s-destination", "stop_name": "도착"}
        route = v2_route()
        route.update(origin_stop_id="s-origin", destination_stop_id="s-destination",
                     origin_occurrence_id="verified-occ", destination_occurrence_id="o-destination")
        repository.find_direct_routes.return_value = [route]
        repository.resolve_congestion.return_value = {
            "source": "UNKNOWN", "status": "INSUFFICIENT_DATA", "onboard_count": None,
            "relative_percentile": None, "congestion_level": "UNKNOWN",
            "boarding_guidance": "데이터 부족", "sample_size": 0,
        }
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = repository
        service.predict_with_llm = Mock(return_value="설명")

        result = service.predict_boarding(
            "현재 명칭", "도착", origin_stop_id="s-origin",
            origin_occurrence_id="verified-occ", destination_stop_id="s-destination",
        )

        self.assertEqual(result["routes"][0]["line_name"], "B1")
        repository.find_verified_stop_occurrence_by_id.assert_called_once_with("verified-occ")
        repository.find_stops_by_name.assert_not_called()
        repository.find_direct_routes.assert_called_once_with(
            "과거 명칭", "도착", limit=20, origin_stop_id="s-origin",
            destination_stop_id="s-destination", origin_occurrence_id="verified-occ",
        )

    def test_unverified_or_mismatched_alias_id_is_rejected(self) -> None:
        repository = Mock()
        repository.find_verified_stop_occurrence_by_id.return_value = {
            "stop_id": "another-stop", "stop_name": "동명",
        }
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = repository
        with self.assertRaisesRegex(ValueError, "검증된 정류장 occurrence ID"):
            service.predict_boarding("현재 명칭", "도착", origin_stop_id="chosen",
                                     origin_occurrence_id="unverified")
        repository.find_direct_routes.assert_not_called()

    def test_selected_ambiguous_stop_is_not_replaced_by_same_named_stop(self) -> None:
        repository = Mock()
        repository.find_stop_by_id.return_value = {"stop_id": "chosen", "stop_name": "동명"}
        repository.find_stops_by_name.return_value = [
            {"stop_id": "destination", "stop_name": "도착"}
        ]
        repository.find_direct_routes.return_value = [
            {"pattern_id": "wrong", "origin_stop_id": "other", "destination_stop_id": "destination"}
        ]
        repository.find_one_transfer_routes.return_value = []
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = repository

        result = service.predict_boarding("동명", "도착", origin_stop_id="chosen")
        self.assertEqual(result["result_status"], "NO_SUPPORTED_ROUTE")
        self.assertEqual(result["routes"], [])
        self.assertEqual(result["itineraries"], [])

        repository.find_direct_routes.assert_called_once_with(
            "동명", "도착", limit=20,
            origin_stop_id="chosen", destination_stop_id=None,
        )

    def test_unknown_selected_stop_id_does_not_fall_back_to_text(self) -> None:
        repository = Mock()
        repository.find_stop_by_id.return_value = None
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = repository

        with self.assertRaisesRegex(ValueError, "선택한 정류장 ID"):
            service.predict_boarding("동명", "도착", origin_stop_id="missing")
        repository.find_stops_by_name.assert_not_called()

    def test_llm_failure_keeps_deterministic_structured_explanation(self) -> None:
        service = BusPredictionService.__new__(BusPredictionService)
        route = v2_route()
        with patch("service.requests.post", side_effect=RuntimeError("offline")):
            explanation = service.predict_with_llm([route], "대전역", "세종시청")
        self.assertIn("B1", explanation)
        self.assertIn("17명", explanation)
        self.assertIn("탑승 확률을 뜻하지 않습니다", explanation)

    def transfer_candidate(self, total_hops: int = 2, *, pattern_suffix: str = "a") -> dict:
        first = {
            "line_id": "line-1000", "line_name": "1000", "pattern_id": f"p1-{pattern_suffix}",
            "origin_occurrence_id": "origin-occ", "destination_occurrence_id": "transfer-a",
            "terminal_description": None, "hops": 1,
            "stops": [
                {"stop_id": "origin", "occurrence_id": "origin-occ", "seq": 1, "stop_name": "출발 과거명", "lat": None, "lon": None},
                {"stop_id": "physical-a", "occurrence_id": "transfer-a", "seq": 2, "stop_name": "환승 과거명 A", "lat": 36.1, "lon": 127.1},
            ],
        }
        second = {
            "line_id": "line-1004", "line_name": "1004", "pattern_id": f"p2-{pattern_suffix}",
            "origin_occurrence_id": "transfer-b", "destination_occurrence_id": "dest-occ",
            "terminal_description": None, "hops": total_hops - 1,
            "stops": [
                {"stop_id": "physical-b", "occurrence_id": "transfer-b", "seq": 8, "stop_name": "환승 과거명 B", "lat": 36.1, "lon": 127.1},
                {"stop_id": "destination", "occurrence_id": "dest-occ", "seq": 9, "stop_name": "도착 과거명", "lat": None, "lon": None},
            ],
        }
        return {
            "transfer_count": 1, "total_hops": total_hops,
            "transfer": {
                "official_stop_id": "official-1", "official_stop_name": "공식 환승 정류장",
                "historical_name_leg1": "환승 과거명 A", "historical_name_leg2": "환승 과거명 B",
                "leg1_occurrence_id": "transfer-a", "leg2_occurrence_id": "transfer-b",
                "leg1_seq": 2, "leg2_seq": 8,
                "mapping_leg1": {"mapping_status": "SEQUENCE_MATCH", "official_node_id": "official-1"},
                "mapping_leg2": {"mapping_status": "EXACT", "official_node_id": "official-1"},
            },
            "legs": [first, second],
        }

    def test_direct_route_remains_preferred_over_transfer(self) -> None:
        repository = Mock()
        repository.find_stops_by_name.side_effect = [
            [{"stop_id": "origin", "stop_name": "출발"}],
            [{"stop_id": "destination", "stop_name": "도착"}],
        ]
        direct = v2_route()
        direct.update(origin_occurrence_id="o-origin", destination_occurrence_id="o-destination")
        repository.find_direct_routes.return_value = [direct]
        repository.resolve_congestion.return_value = {
            "source": "UNKNOWN", "status": "INSUFFICIENT_DATA", "onboard_count": None,
            "relative_percentile": None, "congestion_level": "UNKNOWN",
            "boarding_guidance": "데이터 부족", "sample_size": 0,
        }
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = repository
        service.predict_with_llm = Mock(return_value="direct explanation")

        result = service.predict_boarding("출발", "도착")

        self.assertEqual(len(result["routes"]), 1)
        self.assertEqual(result["itineraries"], [])
        repository.find_one_transfer_routes.assert_not_called()

    def test_transfer_service_keeps_leg_congestion_separate_and_historical_names(self) -> None:
        repository = Mock()
        repository.find_stops_by_name.side_effect = [
            [{"stop_id": "origin", "stop_name": "출발"}],
            [{"stop_id": "destination", "stop_name": "도착"}],
        ]
        candidate = self.transfer_candidate()
        repository.find_one_transfer_routes.return_value = [candidate]
        repository.find_direct_routes.return_value = []
        repository.resolve_congestion.side_effect = [
            {"source": "HISTORICAL_OBSERVATION", "status": "AVAILABLE", "onboard_count": 21,
             "relative_percentile": 60.0, "congestion_level": "MEDIUM", "boarding_guidance": "보통",
             "service_date": "2025-11-08", "hour": 8, "sample_size": 34},
            {"source": "HISTORICAL_PROFILE", "status": "FALLBACK", "onboard_count": 7,
             "relative_percentile": 20.0, "congestion_level": "LOW", "boarding_guidance": "여유",
             "hour": 8, "sample_size": 5},
        ]
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = repository
        service.predict_with_llm = Mock()

        result = service.predict_boarding("출발", "도착", "08:00", "2025-11-08")
        itinerary = result["itineraries"][0]

        self.assertEqual(result["routes"], [])
        self.assertEqual(itinerary["transfer_count"], 1)
        self.assertEqual(len(itinerary["legs"]), 2)
        self.assertEqual(itinerary["legs"][0]["onboard_count"], 21)
        self.assertEqual(itinerary["legs"][1]["onboard_count"], 7)
        self.assertEqual(itinerary["legs"][0]["evidence_source"], "HISTORICAL_OBSERVATION")
        self.assertEqual(itinerary["legs"][1]["evidence_source"], "HISTORICAL_PROFILE")
        self.assertEqual(itinerary["transfer"]["historical_name_leg1"], "환승 과거명 A")
        self.assertEqual(itinerary["transfer"]["historical_name_leg2"], "환승 과거명 B")
        self.assertIsNone(itinerary["legs"][0]["boarding_probability"])
        service.predict_with_llm.assert_not_called()

    def test_transfer_candidates_use_deterministic_hop_ranking(self) -> None:
        repository = Mock()
        repository.find_one_transfer_routes.return_value = [
            self.transfer_candidate(5, pattern_suffix="long"),
            self.transfer_candidate(3, pattern_suffix="short"),
        ]
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = repository

        ranked = service._transfer_candidates([{"name": "출발"}], [{"name": "도착"}])

        self.assertEqual([route["total_hops"] for route in ranked], [3, 5])

    def test_same_historical_name_without_verified_identity_is_rejected(self) -> None:
        repository = Mock()
        repository.find_stops_by_name.side_effect = [
            [{"stop_id": "origin", "stop_name": "출발"}],
            [{"stop_id": "destination", "stop_name": "도착"}],
        ]
        candidate = self.transfer_candidate()
        candidate["transfer"]["historical_name_leg2"] = candidate["transfer"]["historical_name_leg1"]
        candidate["transfer"]["mapping_leg2"] = {"mapping_status": "UNMATCHED", "official_node_id": "official-1"}
        repository.find_one_transfer_routes.return_value = [candidate]
        repository.find_direct_routes.return_value = []
        service = BusPredictionService.__new__(BusPredictionService)
        service.repository = repository

        result = service.predict_boarding("출발", "도착")
        self.assertEqual(result["result_status"], "NO_SUPPORTED_ROUTE")
        self.assertEqual(result["itineraries"], [])


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
