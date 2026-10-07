"""Optional Kakao BUS-step geometry for a route already selected by RideSure."""

from __future__ import annotations

import logging
import math
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


def select_bus_path(payload: dict[str, Any], leg: dict[str, Any]) -> list[dict[str, float]]:
    """Accept only a BUS step with exact line identity and both endpoint contexts."""
    origin = _coordinate(leg.get("origin_stop") or {})
    destination = _coordinate(leg.get("destination_stop") or {})
    if not origin or not destination or payload.get("status") != "OK":
        return []
    for route in payload.get("routes") or []:
        for step in route.get("steps") or []:
            properties = step.get("properties") or {}
            if properties.get("type") != "BUS":
                continue
            if not any(_same_name(vehicle.get("name"), leg.get("line_name"))
                       for vehicle in properties.get("vehicles") or [] if isinstance(vehicle, dict)):
                continue
            stops = properties.get("stops") or []
            if len(stops) < 2 or not all(isinstance(stop, dict) for stop in (stops[0], stops[-1])):
                continue
            # An occurrence may have a separately verified current official
            # name. Use it for API validation while keeping the historical
            # name for display and route identity.
            origin_name = leg["origin_stop"].get("official_stop_name") or leg["origin_stop"].get("name")
            destination_name = leg["destination_stop"].get("official_stop_name") or leg["destination_stop"].get("name")
            if not (_same_name(stops[0].get("name"), origin_name) and
                    _same_name(stops[-1].get("name"), destination_name)):
                continue
            raw_points = (step.get("path") or {}).get("points") or []
            points = []
            for raw in raw_points:
                if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                    points = []
                    break
                point = _coordinate({"lat": raw[1], "lon": raw[0]})
                if point is None:
                    points = []
                    break
                points.append(point)
            if len(points) < 2:
                continue
            # The bus step must board/alight near the verified occurrence positions.
            if _distance_m(origin, points[0]) > 350 or _distance_m(destination, points[-1]) > 350:
                continue
            return [{"lat": lat, "lon": lon} for lat, lon in points]
    return []


class KakaoTransitGeometry:
    def __init__(self, key: str, session: Any = requests):
        self.key = key.strip()
        self.session = session

    def for_leg(self, leg: dict[str, Any]) -> list[dict[str, float]]:
        if not self.key:
            return []
        origin = _coordinate(leg.get("origin_stop") or {})
        destination = _coordinate(leg.get("destination_stop") or {})
        if not origin or not destination:
            return []
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
            return select_bus_path(response.json(), leg)
        except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
            LOG.info("Kakao transit geometry unavailable: %s", type(exc).__name__)
            return []
