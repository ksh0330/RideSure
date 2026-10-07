"""Optional Kakao BUS-step geometry for a route already selected by RideSure."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import requests

from official_stop_mapping import normalize_presentation_name


LOG = logging.getLogger(__name__)
URL = "https://dapi.kakao.com/v2/routing/publictraffic"


def _coordinate(stop: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lat, lon = float(stop["lat"]), float(stop["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180 else None


def _distance_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, first)
    lat2, lon2 = map(math.radians, second)
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 12_742_000 * math.asin(min(1, math.sqrt(a)))


def _same_name(first: Any, second: Any) -> bool:
    return bool(first and second and normalize_presentation_name(str(first)) == normalize_presentation_name(str(second)))


@dataclass(frozen=True)
class BusMatch:
    points: list[dict[str, float]]
    bus_time_seconds: int | None
    total_time_seconds: int | None


def _duration(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _match_step(step: dict[str, Any], leg: dict[str, Any]) -> list[dict[str, float]]:
    origin = _coordinate(leg.get("origin_stop") or {})
    destination = _coordinate(leg.get("destination_stop") or {})
    if not origin or not destination:
        return []
    properties = step.get("properties") or {}
    if properties.get("type") != "BUS" or not any(
        _same_name(vehicle.get("name"), leg.get("line_name"))
        for vehicle in properties.get("vehicles") or [] if isinstance(vehicle, dict)
    ):
        return []
    stops = properties.get("stops") or []
    if len(stops) < 2 or not all(isinstance(stop, dict) for stop in (stops[0], stops[-1])):
        return []
    origin_name = leg["origin_stop"].get("official_stop_name") or leg["origin_stop"].get("name")
    destination_name = leg["destination_stop"].get("official_stop_name") or leg["destination_stop"].get("name")
    if not (_same_name(stops[0].get("name"), origin_name) and
            _same_name(stops[-1].get("name"), destination_name)):
        return []
    points = []
    for raw in (step.get("path") or {}).get("points") or []:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return []
        point = _coordinate({"lat": raw[1], "lon": raw[0]})
        if point is None:
            return []
        points.append(point)
    if len(points) < 2 or _distance_m(origin, points[0]) > 350 or _distance_m(destination, points[-1]) > 350:
        return []
    return [{"lat": lat, "lon": lon} for lat, lon in points]


def select_bus_match(payload: dict[str, Any], leg: dict[str, Any]) -> BusMatch | None:
    """Tie geometry and timing to one validated BUS step in the same response."""
    if payload.get("status") != "OK":
        return None
    fallback_match = None
    for route in payload.get("routes") or []:
        steps = route.get("steps") or []
        bus_steps = [step for step in steps if (step.get("properties") or {}).get("type") == "BUS"]
        for step in bus_steps:
            points = _match_step(step, leg)
            if points:
                total = _duration((route.get("properties") or {}).get("totalTime")) if len(bus_steps) == 1 else None
                match = BusMatch(points, _duration((step.get("properties") or {}).get("time")), total)
                if total is not None:
                    return match
                if fallback_match is None:
                    fallback_match = match
    return fallback_match


def select_bus_path(payload: dict[str, Any], leg: dict[str, Any]) -> list[dict[str, float]]:
    match = select_bus_match(payload, leg)
    return match.points if match else []


def select_transfer_time(payload: dict[str, Any], legs: list[dict[str, Any]], transfer: dict[str, Any]) -> int | None:
    """Accept a whole-route ETA only for the exact two validated BUS legs."""
    if payload.get("status") != "OK" or len(legs) != 2 or not transfer.get("official_stop_id"):
        return None
    first_end, second_start = legs[0].get("destination_stop") or {}, legs[1].get("origin_stop") or {}
    transfer_name = transfer.get("official_stop_name")
    if not (_same_name(first_end.get("official_stop_name"), transfer_name) and
            _same_name(second_start.get("official_stop_name"), transfer_name)):
        return None
    if not (first_end.get("official_stop_id") == transfer["official_stop_id"] ==
            second_start.get("official_stop_id")):
        return None
    for route in payload.get("routes") or []:
        bus_steps = [step for step in route.get("steps") or []
                     if (step.get("properties") or {}).get("type") == "BUS"]
        if len(bus_steps) != 2 or not all(_match_step(step, leg) for step, leg in zip(bus_steps, legs)):
            continue
        total = _duration((route.get("properties") or {}).get("totalTime"))
        if total is not None:
            return total
    return None


class KakaoTransitGeometry:
    def __init__(self, key: str, session: Any = requests):
        self.key = key.strip()
        self.session = session

    def _request(self, origin_stop: dict[str, Any], destination_stop: dict[str, Any]) -> dict[str, Any] | None:
        if not self.key:
            return None
        origin = _coordinate(origin_stop)
        destination = _coordinate(destination_stop)
        if not origin or not destination:
            return None
        try:
            response = self.session.get(
                URL,
                headers={"Authorization": f"KakaoAK {self.key}"},
                params={"start_x": origin[1], "start_y": origin[0],
                        "end_x": destination[1], "end_y": destination[0],
                        "input_coord": "WGS84", "output_coord": "WGS84"},
                timeout=4,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            LOG.info("Kakao transit geometry unavailable: %s", type(exc).__name__)
            return None

    def for_leg_match(self, leg: dict[str, Any]) -> BusMatch | None:
        payload = self._request(leg.get("origin_stop") or {}, leg.get("destination_stop") or {})
        return select_bus_match(payload, leg) if payload else None

    def for_leg(self, leg: dict[str, Any]) -> list[dict[str, float]]:
        match = self.for_leg_match(leg)
        return match.points if match else []

    def for_transfer_time(self, legs: list[dict[str, Any]], transfer: dict[str, Any]) -> int | None:
        if len(legs) != 2:
            return None
        payload = self._request(legs[0].get("origin_stop") or {}, legs[1].get("destination_stop") or {})
        return select_transfer_time(payload, legs, transfer) if payload else None
