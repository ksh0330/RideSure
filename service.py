"""Neo4j v2 routing/congestion service with EXAONE explanation fallback."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Iterable

import requests
from neo4j import GraphDatabase

import config
from prediction_v2 import V2TransitRepository


logger = logging.getLogger(__name__)


def _present(value: Any) -> bool:
    return value is not None and value != ""


class BusPredictionService:
    """Build a structured result from v2 facts, then ask EXAONE to phrase it."""

    def __init__(self, driver: Any | None = None, repository: Any | None = None):
        config.validate_required_config(("neo4j_v2", "llm_client"))
        self.driver = driver or GraphDatabase.driver(
            config.NEO4J_V2_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASS),
        )
        self.repository = repository or V2TransitRepository(self.driver)

    def close(self) -> None:
        self.driver.close()

    def check_connection(self) -> bool:
        try:
            with self.driver.session() as session:
                record = session.run(
                    "MATCH (n:LoadObservation) RETURN count(n) AS count"
                ).single(strict=True)
            return int(record["count"]) > 0
        except Exception as exc:
            logger.warning("Neo4j v2 connection check failed: %s", exc)
            return False

    @staticmethod
    def _parse_inputs(departure_time: str | None, service_date: str) -> tuple[int, str]:
        if departure_time:
            try:
                hour = datetime.strptime(departure_time, "%H:%M").hour
            except ValueError as exc:
                raise ValueError("departure_time must use HH:MM") from exc
        else:
            hour = 9
        try:
            parsed_date = datetime.strptime(service_date, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ValueError("date must use YYYY-MM-DD") from exc
        return hour, parsed_date

    @staticmethod
    def _candidate_name(candidate: dict[str, Any]) -> str | None:
        value = candidate.get("name") or candidate.get("stop_name")
        return str(value).strip() if value else None

    def _stop_candidates(
        self,
        text: str,
        latitude: float | None,
        longitude: float | None,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        if latitude is not None and longitude is not None:
            candidates.extend(
                self.repository.find_nearby_stops(
                    latitude,
                    longitude,
                    radius_m=2_000,
                    max_results=8,
                )
            )
        # Historical topology is name-only until official mappings are verified.
        # Text candidates keep the historical fallback usable when official data
        # or coordinates are not configured.
        candidates.extend(self.repository.find_stops_by_name(text, limit=8))

        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[str | None, str]] = set()
        for candidate in candidates:
            name = self._candidate_name(candidate)
            if not name:
                continue
            key = (candidate.get("stop_id"), name)
            if key in seen:
                continue
            seen.add(key)
            normalized = dict(candidate)
            normalized["name"] = name
            deduplicated.append(normalized)
        return deduplicated

    @staticmethod
    def _normalize_stop(stop: dict[str, Any]) -> dict[str, Any]:
        name = stop.get("name") or stop.get("stop_name")
        if not name:
            raise ValueError("Route contains a stop without a name")
        normalized = {
            "stop_id": stop.get("stop_id"),
            "occurrence_id": stop.get("occurrence_id"),
            "name": str(name),
            "seq": stop.get("seq"),
            "lat": stop.get("lat"),
            "lon": stop.get("lon"),
            "distance_m": stop.get("distance_m"),
        }
        return normalized

    @staticmethod
    def _geometry_from_stops(stops: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
        geometry = [
            {"lat": float(stop["lat"]), "lon": float(stop["lon"])}
            for stop in stops
            if _present(stop.get("lat")) and _present(stop.get("lon"))
        ]
        return geometry if len(geometry) >= 2 else []

    def _route_candidates(
        self,
        origin_candidates: list[dict[str, Any]],
        destination_candidates: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for origin in origin_candidates[:8]:
            for destination in destination_candidates[:8]:
                if origin["name"] == destination["name"]:
                    continue
                direct_routes = self.repository.find_direct_routes(
                    origin["name"], destination["name"], limit=20
                )
                for route in direct_routes:
                    candidates.append((route, origin, destination))

        def rank(item: tuple[dict[str, Any], dict[str, Any], dict[str, Any]]) -> tuple:
            route, origin, destination = item
            distance = float(origin.get("distance_m") or 0) + float(
                destination.get("distance_m") or 0
            )
            coordinate_rank = 0 if origin.get("distance_m") is not None else 1
            return (coordinate_rank, distance, int(route.get("hops") or 0))

        candidates.sort(key=rank)
        # A repeated stop can create several paths within one pattern. Keep the
        # shortest/best candidate for each pattern rather than presenting a loop
        # around the same line as an alternative route.
        result: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        seen_patterns: set[str] = set()
        for item in candidates:
            pattern_id = str(item[0]["pattern_id"])
            if pattern_id in seen_patterns:
                continue
            seen_patterns.add(pattern_id)
            result.append(item)
            if len(result) == 3:
                break
        return result

    def _build_route_result(
        self,
        route: dict[str, Any],
        origin_candidate: dict[str, Any],
        destination_candidate: dict[str, Any],
        service_date: str,
        hour: int,
    ) -> dict[str, Any]:
        congestion = self.repository.resolve_congestion(
            str(route["pattern_id"]),
            str(route["origin_occurrence_id"]),
            service_date,
            hour,
        )
        stops = [self._normalize_stop(stop) for stop in route.get("stops", [])]
        if not stops:
            raise ValueError("Neo4j returned an empty direct route")
        if origin_candidate.get("distance_m") is not None:
            stops[0]["distance_m"] = origin_candidate["distance_m"]
        if destination_candidate.get("distance_m") is not None:
            stops[-1]["distance_m"] = destination_candidate["distance_m"]

        route_geometry = route.get("geometry") or self._geometry_from_stops(stops)
        geometry = [
            {"lat": float(point["lat"]), "lon": float(point["lon"])}
            for point in route_geometry
            if _present(point.get("lat")) and _present(point.get("lon"))
        ]
        geometry_kind = route.get("geometry_kind") or (
            "STOP_TO_STOP_APPROXIMATION" if geometry else "UNAVAILABLE"
        )
        return {
            "line_id": str(route["line_id"]),
            "line_name": str(route["line_name"]),
            "pattern_id": str(route["pattern_id"]),
            "terminal_description": route.get("terminal_description"),
            "origin_stop": stops[0],
            "destination_stop": stops[-1],
            "stops": stops,
            "geometry": geometry,
            "geometry_kind": geometry_kind,
            "onboard_count": congestion["onboard_count"],
            "relative_percentile": congestion["relative_percentile"],
            "congestion_level": congestion["congestion_level"],
            "boarding_guidance": congestion["boarding_guidance"],
            "evidence_source": congestion["source"],
            "congestion_status": congestion["status"],
            "service_date": congestion.get("service_date") or service_date,
            "hour": congestion.get("hour", hour),
            "sample_size": int(congestion.get("sample_size") or 0),
            "boarding_probability": None,
            "expected_load": None,
            "travel_time": None,
        }

    @staticmethod
    def _fallback_explanation(routes: list[dict[str, Any]]) -> str:
        if not routes:
            return "현재 데이터에서 이용 가능한 직접 노선을 찾지 못했습니다."
        route = routes[0]
        origin = route["origin_stop"]["name"]
        destination = route["destination_stop"]["name"]
        if route["evidence_source"] == "HISTORICAL_OBSERVATION":
            percentile = route.get("relative_percentile")
            percentile_text = (
                f", 동일 패턴 기준 상대 percentile은 {percentile:.1f}입니다"
                if percentile is not None
                else ""
            )
            return (
                f"{route['line_name']} 직행 경로({origin} → {destination})를 안내합니다. "
                f"{route['hour']:02d}시 historical 재차인원은 "
                f"{route['onboard_count']}명이고{percentile_text}. "
                f"상대 혼잡 안내는 '{route['boarding_guidance']}'이며 탑승 확률을 뜻하지 않습니다."
            )
        return (
            f"{route['line_name']} 직행 경로({origin} → {destination})를 안내합니다. "
            "선택한 날짜와 시간의 재차인원 근거가 없어 혼잡 안내는 데이터 부족입니다."
        )

    @staticmethod
    def _llm_output_is_grounded(text: str, facts: dict[str, Any]) -> bool:
        if not text or len(text) > 500 or "%" in text or "탑승 확률" in text:
            return False
        best = facts["recommended_route"]
        if best["line_name"] not in text or best["boarding_guidance"] not in text:
            return False
        allowed_numbers = set(
            re.findall(r"\d+(?:\.\d+)?", json.dumps(facts, ensure_ascii=False))
        )
        output_numbers = set(re.findall(r"\d+(?:\.\d+)?", text))
        return output_numbers.issubset(allowed_numbers)

    def predict_with_llm(
        self,
        routes: list[dict[str, Any]],
        origin: str,
        destination: str,
    ) -> str:
        fallback = self._fallback_explanation(routes)
        best = routes[0]
        facts = {
            "origin_input": origin,
            "destination_input": destination,
            "recommended_route": {
                "line_name": best["line_name"],
                "origin_stop": best["origin_stop"]["name"],
                "destination_stop": best["destination_stop"]["name"],
                "service_date": best["service_date"],
                "hour": best["hour"],
                "onboard_count": best["onboard_count"],
                "relative_percentile": best["relative_percentile"],
                "congestion_level": best["congestion_level"],
                "boarding_guidance": best["boarding_guidance"],
                "evidence_source": best["evidence_source"],
            },
            "alternative_lines": [route["line_name"] for route in routes[1:]],
        }
        prompt = (
            "다음 JSON의 사실만 사용해 200자 이내 한국어 버스 안내를 작성하세요. "
            "노선, 정류장, 수치, 좌표를 추가하거나 추측하지 마세요. "
            "재차인원과 상대 혼잡 안내를 탑승 확률 또는 정원 대비 혼잡률로 표현하지 마세요. "
            "추천 노선명과 boarding_guidance 표현을 반드시 그대로 포함하세요.\n"
            + json.dumps(facts, ensure_ascii=False, sort_keys=True)
        )
        try:
            response = requests.post(
                f"{config.LLM_BASE_URL}/generate",
                json={
                    "prompt": prompt,
                    "max_new_tokens": min(config.MAX_NEW_TOKENS, 160),
                    "temperature": 0.2,
                    "top_p": 0.9,
                },
                timeout=120,
            )
            response.raise_for_status()
            text = str(response.json().get("result") or "").strip()
            if self._llm_output_is_grounded(text, facts):
                return text
            logger.warning("EXAONE output failed structured grounding checks; using fallback")
        except Exception as exc:
            logger.warning("EXAONE explanation failed; using fallback: %s", exc)
        return fallback

    def predict_boarding(
        self,
        origin: str,
        destination: str,
        departure_time: str | None = None,
        date: str = "2025-11-08",
        origin_lat: float | None = None,
        origin_lon: float | None = None,
        destination_lat: float | None = None,
        destination_lon: float | None = None,
    ) -> dict[str, Any]:
        origin = origin.strip()
        destination = destination.strip()
        if not origin or not destination:
            raise ValueError("origin and destination are required")
        hour, service_date = self._parse_inputs(departure_time, date)
        origin_candidates = self._stop_candidates(origin, origin_lat, origin_lon)
        destination_candidates = self._stop_candidates(
            destination, destination_lat, destination_lon
        )
        if not origin_candidates:
            raise ValueError(f"출발지와 일치하는 정류장을 찾지 못했습니다: {origin}")
        if not destination_candidates:
            raise ValueError(f"도착지와 일치하는 정류장을 찾지 못했습니다: {destination}")

        direct = self._route_candidates(origin_candidates, destination_candidates)
        if not direct:
            raise ValueError("현재 Neo4j v2 데이터에서 이용 가능한 직접 노선을 찾지 못했습니다.")
        routes = [
            self._build_route_result(route, origin_stop, destination_stop, service_date, hour)
            for route, origin_stop, destination_stop in direct
        ]
        explanation = self.predict_with_llm(routes, origin, destination)
        alternatives = [
            f"{route['line_name']}: {route['origin_stop']['name']} → "
            f"{route['destination_stop']['name']}"
            for route in routes[1:]
        ]
        return {
            "success": True,
            "origin": origin,
            "destination": destination,
            "routes": routes,
            "reasoning": explanation,
            "explanation": explanation,
            "alternatives": alternatives,
            "data_mode": "NEO4J_V2_HISTORICAL",
        }


_service_instance: BusPredictionService | None = None


def get_service() -> BusPredictionService:
    global _service_instance
    if _service_instance is None:
        _service_instance = BusPredictionService()
    return _service_instance
