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
from kakao_transit_geometry import KakaoTransitGeometry


logger = logging.getLogger(__name__)

BOARDING_LIKELIHOODS = {
    "LOW": ("HIGH", "높음"),
    "MEDIUM": ("MEDIUM", "보통"),
    "HIGH": ("LOW", "낮음"),
    "VERY_HIGH": ("VERY_LOW", "매우 낮음"),
}
LIKELIHOOD_RANK = {"VERY_LOW": 1, "LOW": 2, "MEDIUM": 3, "HIGH": 4}
CONGESTION_LABELS = {
    "LOW": "여유", "MEDIUM": "보통", "HIGH": "혼잡",
    "VERY_HIGH": "매우 혼잡", "UNKNOWN": "정보 없음",
}
CONGESTION_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "VERY_HIGH": 4}


def boarding_likelihood(level: str, source: str) -> tuple[str, str, str]:
    if source in {"HISTORICAL_OBSERVATION", "HISTORICAL_PROFILE"} and level in BOARDING_LIKELIHOODS:
        code, label = BOARDING_LIKELIHOODS[level]
        return code, label, "HISTORICAL_RELATIVE_CONGESTION"
    return "UNKNOWN", "판단 불가", "INSUFFICIENT_HISTORICAL_EVIDENCE"


def _minutes(seconds: int | None) -> int | None:
    return max(1, round(seconds / 60)) if seconds is not None and seconds > 0 else None


def _present(value: Any) -> bool:
    return value is not None and value != ""


class BusPredictionService:
    """Build a structured result from v2 facts, then ask EXAONE to phrase it."""

    def __init__(self, driver: Any | None = None, repository: Any | None = None,
                 geometry_client: Any | None = None):
        config.validate_required_config(("neo4j_v2", "llm_client"))
        self.driver = driver or GraphDatabase.driver(
            config.NEO4J_V2_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASS),
        )
        self.repository = repository or V2TransitRepository(self.driver)
        self.geometry_client = geometry_client or KakaoTransitGeometry(config.KAKAO_REST_API_KEY)

    def _enrich_map_geometry(self, leg: dict[str, Any]) -> None:
        client = getattr(self, "geometry_client", None)
        has_structured_match = client is not None and hasattr(client, "for_leg_match")
        match = client.for_leg_match(leg) if has_structured_match else None
        points = match.points if match else (client.for_leg(leg) if client is not None and not has_structured_match else [])
        leg["map_geometry"] = points
        leg["map_geometry_source"] = "KAKAO_VERIFIED_BUS_PATH" if points else "UNAVAILABLE"
        leg["bus_duration_seconds"] = match.bus_time_seconds if match else None
        leg["estimated_duration_seconds"] = match.total_time_seconds if match else None
        leg["duration_kind"] = "KAKAO_VERIFIED_ROUTE" if match and match.total_time_seconds else "UNAVAILABLE"
        leg["estimated_travel_time_seconds"] = leg["estimated_duration_seconds"]
        leg["estimated_travel_time_minutes"] = _minutes(leg["estimated_travel_time_seconds"])
        leg["travel_time_source"] = "KAKAO_VERIFIED_TRANSIT_ROUTE" if leg["estimated_travel_time_seconds"] else "UNAVAILABLE"

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

    def search_stops(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.repository.find_stops_by_name(query, limit=limit)

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
        stop_id: str | None = None,
        occurrence_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if occurrence_id is not None:
            if stop_id is None:
                raise ValueError("정류장 occurrence ID에는 stop ID도 필요합니다.")
            selected = self.repository.find_verified_stop_occurrence_by_id(occurrence_id)
            if selected is None or selected.get("stop_id") != stop_id:
                raise ValueError("검증된 정류장 occurrence ID를 찾지 못했습니다.")
            return [{**selected, "name": selected["stop_name"], "_selected": True}]
        if stop_id is not None:
            selected = self.repository.find_stop_by_id(stop_id)
            if selected is None:
                raise ValueError(f"선택한 정류장 ID를 찾지 못했습니다: {stop_id}")
            name = self._candidate_name(selected)
            if not name:
                raise ValueError(f"선택한 정류장에 이름이 없습니다: {stop_id}")
            return [{**selected, "name": name, "_selected": True}]

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
        seen: set[tuple[str | None, str, str | None]] = set()
        for candidate in candidates:
            name = self._candidate_name(candidate)
            if not name:
                continue
            key = (candidate.get("stop_id"), name, candidate.get("occurrence_id"))
            if key in seen:
                continue
            seen.add(key)
            normalized = dict(candidate)
            normalized["name"] = name
            if candidate.get("match_kind") == "VERIFIED_OFFICIAL_ALIAS":
                normalized["_selected"] = True
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
            "official_stop_name": stop.get("official_stop_name"),
            "official_stop_id": stop.get("official_stop_id"),
            "seq": stop.get("seq"),
            "lat": stop.get("lat"),
            "lon": stop.get("lon"),
            "distance_m": stop.get("distance_m"),
        }
        return normalized

    @staticmethod
    def _geometry_from_stops(stops: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
        ordered_stops = list(stops)
        if len(ordered_stops) < 2 or any(
            not _present(stop.get("lat")) or not _present(stop.get("lon"))
            for stop in ordered_stops
        ):
            return []
        return [
            {"lat": float(stop["lat"]), "lon": float(stop["lon"])}
            for stop in ordered_stops
        ]

    def _route_candidates(
        self,
        origin_candidates: list[dict[str, Any]],
        destination_candidates: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for origin in origin_candidates[:8]:
            for destination in destination_candidates[:8]:
                origin_id = origin.get("stop_id") if origin.get("_selected") else None
                destination_id = destination.get("stop_id") if destination.get("_selected") else None
                origin_occurrence_id = origin.get("occurrence_id") if origin_id else None
                destination_occurrence_id = destination.get("occurrence_id") if destination_id else None
                if not origin_id and not destination_id and origin["name"] == destination["name"]:
                    continue
                occurrence_filters = {}
                if origin_occurrence_id:
                    occurrence_filters["origin_occurrence_id"] = origin_occurrence_id
                if destination_occurrence_id:
                    occurrence_filters["destination_occurrence_id"] = destination_occurrence_id
                direct_routes = self.repository.find_direct_routes(
                    origin["name"], destination["name"], limit=20,
                    origin_stop_id=origin_id,
                    destination_stop_id=destination_id,
                    **occurrence_filters,
                )
                for route in direct_routes:
                    if origin_id and route.get("origin_stop_id") != origin_id:
                        continue
                    if destination_id and route.get("destination_stop_id") != destination_id:
                        continue
                    if origin_occurrence_id and route.get("origin_occurrence_id") != origin_occurrence_id:
                        continue
                    if destination_occurrence_id and route.get("destination_occurrence_id") != destination_occurrence_id:
                        continue
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

        # A missing intermediate occurrence must make the entire approximation
        # unavailable; never draw a shortcut across an unmapped stop.
        route_geometry = self._geometry_from_stops(stops)
        geometry = [
            {"lat": float(point["lat"]), "lon": float(point["lon"])}
            for point in route_geometry
            if _present(point.get("lat")) and _present(point.get("lon"))
        ]
        geometry_kind = "STOP_TO_STOP_APPROXIMATION" if geometry else "UNAVAILABLE"
        likelihood, likelihood_label, likelihood_basis = boarding_likelihood(congestion["congestion_level"], congestion["source"])
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
            "boarding_likelihood": likelihood,
            "boarding_likelihood_label": likelihood_label,
            "boarding_likelihood_basis": likelihood_basis,
            "boarding_decision": likelihood_label,
            "boarding_decision_basis": likelihood_basis,
            "evidence_source": congestion["source"],
            "congestion_status": congestion["status"],
            "service_date": congestion.get("service_date") or service_date,
            "hour": congestion.get("hour", hour),
            "sample_size": int(congestion.get("sample_size") or 0),
            "boarding_probability": None,
            "expected_load": None,
            "travel_time": None,
        }

    def _build_transfer_leg(
        self, leg: dict[str, Any], service_date: str, hour: int
    ) -> dict[str, Any]:
        stops = [self._normalize_stop(stop) for stop in leg.get("stops", [])]
        if len(stops) < 2:
            raise ValueError("Neo4j returned an incomplete transfer leg")
        congestion = self.repository.resolve_congestion(
            str(leg["pattern_id"]), str(leg["origin_occurrence_id"]), service_date, hour
        )
        geometry = self._geometry_from_stops(stops)
        likelihood, likelihood_label, likelihood_basis = boarding_likelihood(congestion["congestion_level"], congestion["source"])
        return {
            "line_id": str(leg["line_id"]),
            "line_name": str(leg["line_name"]),
            "pattern_id": str(leg["pattern_id"]),
            "terminal_description": leg.get("terminal_description"),
            "origin_occurrence_id": str(leg["origin_occurrence_id"]),
            "destination_occurrence_id": str(leg["destination_occurrence_id"]),
            "origin_stop": stops[0],
            "destination_stop": stops[-1],
            "stops": stops,
            "geometry": geometry,
            "geometry_kind": "STOP_TO_STOP_APPROXIMATION" if geometry else "UNAVAILABLE",
            "hops": int(leg["hops"]),
            "onboard_count": congestion["onboard_count"],
            "relative_percentile": congestion["relative_percentile"],
            "congestion_level": congestion["congestion_level"],
            "boarding_guidance": congestion["boarding_guidance"],
            "boarding_likelihood": likelihood,
            "boarding_likelihood_label": likelihood_label,
            "boarding_likelihood_basis": likelihood_basis,
            "boarding_decision": likelihood_label,
            "boarding_decision_basis": likelihood_basis,
            "evidence_source": congestion["source"],
            "congestion_status": congestion["status"],
            "service_date": congestion.get("service_date") or service_date,
            "hour": congestion.get("hour", hour),
            "sample_size": int(congestion.get("sample_size") or 0),
            "boarding_probability": None,
            "expected_load": None,
            "travel_time": None,
        }

    def _transfer_candidates(
        self,
        origin_candidates: list[dict[str, Any]],
        destination_candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for origin in origin_candidates[:8]:
            for destination in destination_candidates[:8]:
                origin_id = origin.get("stop_id") if origin.get("_selected") else None
                destination_id = destination.get("stop_id") if destination.get("_selected") else None
                occurrence_filters = {}
                if origin_id and origin.get("occurrence_id"):
                    occurrence_filters["origin_occurrence_id"] = origin["occurrence_id"]
                if destination_id and destination.get("occurrence_id"):
                    occurrence_filters["destination_occurrence_id"] = destination["occurrence_id"]
                if not origin_id and not destination_id and origin["name"] == destination["name"]:
                    continue
                matches = self.repository.find_one_transfer_routes(
                    origin["name"], destination["name"], limit=20,
                    origin_stop_id=origin_id, destination_stop_id=destination_id,
                    **occurrence_filters,
                )
                for itinerary in matches:
                    legs = itinerary.get("legs") or []
                    if len(legs) != 2 or itinerary.get("transfer_count") != 1:
                        continue
                    if legs[0].get("pattern_id") == legs[1].get("pattern_id"):
                        continue
                    if any(int(leg.get("hops") or 0) < 1 for leg in legs):
                        continue
                    transfer = itinerary.get("transfer") or {}
                    physical_id = transfer.get("official_stop_id")
                    first_mapping = transfer.get("mapping_leg1") or {}
                    second_mapping = transfer.get("mapping_leg2") or {}
                    if not physical_id or any(
                        mapping.get("mapping_status") not in {"EXACT", "SEQUENCE_MATCH"}
                        or mapping.get("official_node_id") != physical_id
                        for mapping in (first_mapping, second_mapping)
                    ):
                        continue
                    if int(itinerary.get("total_hops") or 0) != sum(int(leg["hops"]) for leg in legs):
                        continue
                    if origin_id and legs[0]["stops"][0].get("stop_id") != origin_id:
                        continue
                    if destination_id and legs[1]["stops"][-1].get("stop_id") != destination_id:
                        continue
                    if occurrence_filters.get("origin_occurrence_id") is not None and (
                        legs[0].get("origin_occurrence_id") != occurrence_filters["origin_occurrence_id"]
                    ):
                        continue
                    if occurrence_filters.get("destination_occurrence_id") is not None and (
                        legs[1].get("destination_occurrence_id") != occurrence_filters["destination_occurrence_id"]
                    ):
                        continue
                    identity = (
                        legs[0]["pattern_id"], legs[0]["origin_occurrence_id"],
                        physical_id,
                        legs[1]["pattern_id"], legs[1]["destination_occurrence_id"],
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    candidates.append(itinerary)
        candidates.sort(key=lambda item: (
            int(item["total_hops"]), int(item["legs"][0]["hops"]),
            int(item["legs"][1]["hops"]), item["legs"][0]["line_name"],
            item["legs"][1]["line_name"], item["legs"][0]["pattern_id"],
            item["legs"][1]["pattern_id"], item["transfer"]["official_stop_id"],
        ))
        return candidates[:3]

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
    def _transfer_fallback_explanation(itineraries: list[dict[str, Any]]) -> str:
        if not itineraries:
            return "현재 데이터에서 이용 가능한 경로를 찾지 못했습니다."
        itinerary = itineraries[0]
        first, second = itinerary["legs"]
        transfer_name = itinerary["transfer"]["official_stop_name"]
        def evidence(leg: dict[str, Any]) -> str:
            if leg["onboard_count"] is None:
                return "재차인원 데이터 부족"
            return f"{leg['hour']:02d}시 재차인원 {leg['onboard_count']}명, {leg['boarding_guidance']}"
        return (
            f"직접 경로가 없어 1회 환승 경로를 안내합니다. {first['line_name']}을(를) 타고 "
            f"{transfer_name}에서 {second['line_name']}(으)로 갈아타세요. "
            f"각 구간의 과거 근거는 {first['line_name']} {evidence(first)}, "
            f"{second['line_name']} {evidence(second)}입니다. 총 {itinerary['total_hops']}개 구간 연결이며 "
            "이 안내는 이동 시간이나 혼잡 기준 최적 경로를 뜻하지 않습니다."
        )

    @staticmethod
    def _itinerary_likelihood(legs: list[dict[str, Any]]) -> tuple[str, str, str]:
        if any(leg["boarding_likelihood"] == "UNKNOWN" for leg in legs):
            return "UNKNOWN", "판단 불가", "INSUFFICIENT_HISTORICAL_EVIDENCE"
        worst = min(legs, key=lambda leg: LIKELIHOOD_RANK[leg["boarding_likelihood"]])
        return (worst["boarding_likelihood"], worst["boarding_likelihood_label"],
                "HISTORICAL_RELATIVE_CONGESTION")

    @staticmethod
    def _recommended_alternative(
        routes: list[dict[str, Any]], itineraries: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if not routes or routes[0]["boarding_likelihood"] not in {"LOW", "VERY_LOW"}:
            return None
        current_rank = LIKELIHOOD_RANK[routes[0]["boarding_likelihood"]]
        candidates = []
        for index, route in enumerate(routes[1:], 1):
            rank = LIKELIHOOD_RANK.get(route["boarding_likelihood"], 0)
            if rank > current_rank:
                candidates.append((rank, 1, -len(route.get("stops") or []), -index, "DIRECT", index, route))
        for index, itinerary in enumerate(itineraries):
            rank = LIKELIHOOD_RANK.get(itinerary["boarding_likelihood"], 0)
            if rank > current_rank:
                candidates.append((rank, 0, -(int(itinerary.get("total_hops") or 0) + 1),
                                   -index, "ONE_TRANSFER", index, itinerary))
        if not candidates:
            return None
        _, _, _, _, kind, index, chosen = max(candidates)
        return {
            "kind": kind,
            "index": index,
            "boarding_likelihood": chosen["boarding_likelihood"],
            "boarding_likelihood_label": chosen["boarding_likelihood_label"],
        }

    def _enrich_itinerary(self, itinerary: dict[str, Any]) -> None:
        for leg in itinerary["legs"]:
            self._enrich_map_geometry(leg)
        client = getattr(self, "geometry_client", None)
        duration = (client.for_transfer_time(itinerary["legs"], itinerary["transfer"])
                    if client is not None and hasattr(client, "for_transfer_time") else None)
        itinerary["estimated_duration_seconds"] = duration
        itinerary["duration_kind"] = "KAKAO_VERIFIED_ROUTE" if duration else "UNAVAILABLE"
        itinerary["estimated_travel_time_seconds"] = duration
        itinerary["estimated_travel_time_minutes"] = _minutes(duration)
        itinerary["travel_time_source"] = "KAKAO_VERIFIED_TRANSIT_ROUTE" if duration else "UNAVAILABLE"
        bus_times = [leg.get("bus_duration_seconds") for leg in itinerary["legs"]]
        itinerary["bus_duration_sum_seconds"] = sum(bus_times) if all(bus_times) else None

    @staticmethod
    def _option_refs(
        routes: list[dict[str, Any]], itineraries: list[dict[str, Any]],
        recommendation: dict[str, Any] | None,
    ) -> list[tuple[str, int]]:
        primary = ("DIRECT", 0) if routes else ("ONE_TRANSFER", 0)
        refs = [primary]
        if recommendation:
            refs.append((recommendation["kind"], recommendation["index"]))
        others = [("DIRECT", index) for index in range(len(routes))]
        others += [("ONE_TRANSFER", index) for index in range(len(itineraries))]

        def rank(ref: tuple[str, int]) -> tuple[int, int, int, int]:
            kind, index = ref
            item = routes[index] if kind == "DIRECT" else itineraries[index]
            stop_count = (len(item["stops"]) if kind == "DIRECT"
                          else int(item["total_hops"]) + 1)
            return (LIKELIHOOD_RANK.get(item["boarding_likelihood"], 0),
                    1 if kind == "DIRECT" else 0, -stop_count, -index)

        for ref in sorted(others, key=rank, reverse=True):
            if ref not in refs:
                refs.append(ref)
            if len(refs) == 3:
                break
        return refs

    @staticmethod
    def _route_option(kind: str, index: int, item: dict[str, Any],
                      tag: str, reason: str | None) -> dict[str, Any]:
        legs = [item] if kind == "DIRECT" else item["legs"]
        congestion_level = (item["congestion_level"] if kind == "DIRECT" else
                            max((leg["congestion_level"] for leg in legs),
                                key=lambda level: CONGESTION_RANK.get(level, 99)))
        leg_details = [{
            "line_name": leg["line_name"],
            "origin_name": leg["origin_stop"]["name"],
            "destination_name": leg["destination_stop"]["name"],
            "stop_count": len(leg["stops"]),
            "onboard_count": leg["onboard_count"],
            "congestion_level": leg["congestion_level"],
            "congestion_label": CONGESTION_LABELS.get(leg["congestion_level"], "정보 없음"),
            "boarding_likelihood_label": leg["boarding_likelihood_label"],
            "bus_duration_seconds": leg.get("bus_duration_seconds"),
        } for leg in legs]
        full_seconds = item.get("estimated_travel_time_seconds")
        bus_sum = item.get("bus_duration_sum_seconds") if kind == "ONE_TRANSFER" else None
        return {
            "option_type": kind,
            "source_index": index,
            "tag": tag,
            "badges": [],
            "title": " → ".join(leg["line_name"] for leg in legs),
            "legs": leg_details,
            "boarding_likelihood": item["boarding_likelihood"],
            "boarding_likelihood_label": item["boarding_likelihood_label"],
            "boarding_likelihood_basis": item["boarding_likelihood_basis"],
            "congestion_level": congestion_level,
            "congestion_label": CONGESTION_LABELS.get(congestion_level, "정보 없음"),
            "onboard_count": item["onboard_count"] if kind == "DIRECT" else None,
            "estimated_travel_time_seconds": full_seconds,
            "estimated_travel_time_minutes": _minutes(full_seconds),
            "travel_time_source": item.get("travel_time_source", "UNAVAILABLE"),
            "bus_travel_time_sum_seconds": bus_sum,
            "bus_travel_time_sum_minutes": _minutes(bus_sum),
            "stop_count": len(item["stops"]) if kind == "DIRECT" else int(item["total_hops"]) + 1,
            "transfer_count": 0 if kind == "DIRECT" else 1,
            "recommendation_reason": reason,
        }

    @staticmethod
    def _meaningful_comparison(primary: dict[str, Any], other: dict[str, Any]) -> bool:
        primary_rank = LIKELIHOOD_RANK.get(primary["boarding_likelihood"], 0)
        other_rank = LIKELIHOOD_RANK.get(other["boarding_likelihood"], 0)
        if other_rank > primary_rank:
            return True
        if other_rank < primary_rank or other_rank == 0:
            return False
        if other["transfer_count"] < primary["transfer_count"]:
            return True
        if other["stop_count"] < primary["stop_count"]:
            return True
        current_time, other_time = (primary["estimated_travel_time_seconds"],
                                    other["estimated_travel_time_seconds"])
        return bool(current_time and other_time and other_time < current_time)

    @staticmethod
    def _tag_comparable_minima(options: list[dict[str, Any]]) -> None:
        if len(options) < 2:
            return
        min_transfers = min(option["transfer_count"] for option in options)
        if sum(option["transfer_count"] == min_transfers for option in options) == 1:
            next(option for option in options if option["transfer_count"] == min_transfers)["badges"].append("최소 환승")
        if all(option["estimated_travel_time_seconds"] for option in options):
            shortest = min(option["estimated_travel_time_seconds"] for option in options)
            if sum(option["estimated_travel_time_seconds"] == shortest for option in options) == 1:
                next(option for option in options if option["estimated_travel_time_seconds"] == shortest)["badges"].append("최소시간")

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
        origin_stop_id: str | None = None,
        destination_stop_id: str | None = None,
        origin_occurrence_id: str | None = None,
        destination_occurrence_id: str | None = None,
    ) -> dict[str, Any]:
        origin = origin.strip()
        destination = destination.strip()
        if not origin or not destination:
            raise ValueError("origin and destination are required")
        hour, service_date = self._parse_inputs(departure_time, date)
        origin_candidates = self._stop_candidates(
            origin, origin_lat, origin_lon, origin_stop_id, origin_occurrence_id
        )
        destination_candidates = self._stop_candidates(
            destination, destination_lat, destination_lon, destination_stop_id,
            destination_occurrence_id,
        )
        if not origin_candidates:
            raise ValueError(f"출발지와 일치하는 정류장을 찾지 못했습니다: {origin}")
        if not destination_candidates:
            raise ValueError(f"도착지와 일치하는 정류장을 찾지 못했습니다: {destination}")

        direct = self._route_candidates(origin_candidates, destination_candidates)
        routes = [
            self._build_route_result(route, origin_stop, destination_stop, service_date, hour)
            for route, origin_stop, destination_stop in direct
        ]
        transfer_candidates = self._transfer_candidates(origin_candidates, destination_candidates)
        itineraries = []
        for candidate in transfer_candidates:
            legs = [self._build_transfer_leg(leg, service_date, hour) for leg in candidate["legs"]]
            likelihood, label, basis = self._itinerary_likelihood(legs)
            itineraries.append({**candidate, "legs": legs,
                                "boarding_likelihood": likelihood,
                                "boarding_likelihood_label": label,
                                "boarding_likelihood_basis": basis})
        if not routes and not itineraries:
            return {
                "success": True,
                "result_status": "NO_SUPPORTED_ROUTE",
                "origin": origin,
                "destination": destination,
                "routes": [],
                "itineraries": [],
                "reasoning": "현재 데이터 범위에서 경로를 찾지 못했습니다.",
                "explanation": "현재 데이터 범위에서 경로를 찾지 못했습니다.",
                "alternatives": [],
                "recommended_alternative": None,
                "recommendation_reason": None,
                "route_options": [],
                "data_mode": "NEO4J_V2_HISTORICAL",
            }
        recommendation = self._recommended_alternative(routes, itineraries)
        refs = self._option_refs(routes, itineraries, recommendation)
        if routes:
            explanation = self.predict_with_llm(routes, origin, destination)
        else:
            explanation = self._transfer_fallback_explanation(itineraries)
        for kind, index in refs:
            if kind == "DIRECT":
                self._enrich_map_geometry(routes[index])
            else:
                self._enrich_itinerary(itineraries[index])
        options = [self._route_option(kind, index,
                                     routes[index] if kind == "DIRECT" else itineraries[index],
                                     "CURRENT" if position == 0 else "COMPARE", None)
                   for position, (kind, index) in enumerate(refs)]
        # A same-likelihood alternative can only claim a time advantage when
        # both complete itineraries have independently verified Kakao times.
        if not recommendation and routes and routes[0]["boarding_likelihood"] in {"LOW", "VERY_LOW"}:
            current = options[0]
            faster = [option for option in options[1:]
                      if option["boarding_likelihood"] == current["boarding_likelihood"]
                      and current["estimated_travel_time_seconds"]
                      and option["estimated_travel_time_seconds"]
                      and option["estimated_travel_time_seconds"] <= current["estimated_travel_time_seconds"] - 60]
            if faster:
                chosen = min(faster, key=lambda option: (option["estimated_travel_time_seconds"],
                                                         option["transfer_count"], option["stop_count"]))
                recommendation = {"kind": chosen["option_type"], "index": chosen["source_index"],
                                  "boarding_likelihood": chosen["boarding_likelihood"],
                                  "boarding_likelihood_label": chosen["boarding_likelihood_label"]}
        recommendation_reason = None
        if recommendation:
            current = options[0]
            chosen = next(option for option in options
                          if option["option_type"] == recommendation["kind"]
                          and option["source_index"] == recommendation["index"])
            steps = (LIKELIHOOD_RANK.get(chosen["boarding_likelihood"], 0)
                     - LIKELIHOOD_RANK.get(current["boarding_likelihood"], 0))
            if steps > 0:
                recommendation_reason = f"현재 경로보다 과거 상대 혼잡 기준 탑승 가능성이 {steps}단계 높습니다."
            else:
                saved = _minutes(current["estimated_travel_time_seconds"] - chosen["estimated_travel_time_seconds"])
                recommendation_reason = f"검증된 예상 소요시간이 현재 경로보다 약 {saved}분 짧습니다."
            chosen["tag"] = "RECOMMENDED"
            chosen["recommendation_reason"] = recommendation_reason
        options = [option for position, option in enumerate(options)
                   if position == 0 or option["tag"] == "RECOMMENDED"
                   or self._meaningful_comparison(options[0], option)]
        self._tag_comparable_minima(options)
        alternatives = [
            f"{route['line_name']}: {route['origin_stop']['name']} → "
            f"{route['destination_stop']['name']}"
            for route in routes[1:]
        ]
        if not routes and itineraries:
            alternatives = [
                f"{item['legs'][0]['line_name']} → {item['legs'][1]['line_name']} "
                f"(환승: {item['transfer']['official_stop_name']})"
                for item in itineraries[1:]
            ]
        return {
            "success": True,
            "result_status": "ROUTE_FOUND",
            "origin": origin,
            "destination": destination,
            "routes": routes,
            "itineraries": itineraries,
            "reasoning": explanation,
            "explanation": explanation,
            "alternatives": alternatives,
            "recommended_alternative": recommendation,
            "recommendation_reason": recommendation_reason,
            "route_options": options,
            "data_mode": "NEO4J_V2_HISTORICAL",
        }


_service_instance: BusPredictionService | None = None


def get_service() -> BusPredictionService:
    global _service_instance
    if _service_instance is None:
        _service_instance = BusPredictionService()
    return _service_instance
