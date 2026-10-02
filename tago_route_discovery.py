"""Discover TAGO routes by official city code and exact route number.

The returned payloads are suitable for source-attributed, key-free snapshots.
No historical/official route binding is inferred from a route number alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

import config
from public_data import PublicDataNotConfigured, PublicDataResponseError, extract_tago_items


BASE_URL = "https://apis.data.go.kr/1613000/BusRouteInfoInqireService/"
OPERATIONS = frozenset({
    "getCtyCodeList", "getRouteNoList", "getRouteInfoIem", "getRouteAcctoThrghSttnList"
})


def total_count(payload: Mapping[str, Any]) -> int | None:
    body = payload.get("response", {}).get("body", {})
    value = body.get("totalCount") if isinstance(body, Mapping) else None
    return int(value) if value is not None else None


class TagoRouteDiscovery:
    def __init__(self, service_key: str | None = None, *, timeout: float = 15.0,
                 opener: Callable[..., Any] = urlopen) -> None:
        self._key = (service_key if service_key is not None else config.DATA_GO_KR_SERVICE_KEY).strip()
        self._timeout = timeout
        self._opener = opener

    def fetch(self, operation: str, **parameters: str | int) -> dict[str, Any]:
        if operation not in OPERATIONS:
            raise ValueError("Unsupported TAGO operation.")
        if not self._key or self._key.lower() in config.PLACEHOLDER_VALUES:
            raise PublicDataNotConfigured("DATA_GO_KR_SERVICE_KEY is not configured.")
        params = {"_type": "json", "numOfRows": 1000, "pageNo": 1, **parameters}
        url = f"{BASE_URL}{operation}?serviceKey={quote(self._key, safe='%')}&{urlencode(params)}"
        try:
            with self._opener(url, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            # URL and chained exception may expose the service key.
            raise PublicDataResponseError(f"TAGO {operation} request failed ({type(exc).__name__}).") from None
        if not isinstance(payload, dict):
            raise PublicDataResponseError("TAGO response is not a JSON object.")
        rows = extract_tago_items(payload)
        count = total_count(payload)
        if count is not None and count > len(rows):
            raise PublicDataResponseError("TAGO response was paginated; incomplete snapshot refused.")
        return payload

    def city_codes(self) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
        payload = self.fetch("getCtyCodeList")
        return payload, extract_tago_items(payload)

    def routes_by_number(self, city_code: str, route_number: str) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
        payload = self.fetch("getRouteNoList", cityCode=city_code, routeNo=route_number)
        return payload, [row for row in extract_tago_items(payload)
                         if str(row.get("routeno", "")).strip() == route_number]

    def route_info(self, city_code: str, route_id: str) -> tuple[dict[str, Any], Mapping[str, Any]]:
        payload = self.fetch("getRouteInfoIem", cityCode=city_code, routeId=route_id)
        rows = extract_tago_items(payload)
        if len(rows) != 1 or str(rows[0].get("routeid")) != route_id:
            raise PublicDataResponseError("TAGO route basic information does not confirm the route ID.")
        return payload, rows[0]

    def route_stops(self, city_code: str, route_id: str) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
        payload = self.fetch("getRouteAcctoThrghSttnList", cityCode=city_code, routeId=route_id)
        rows = extract_tago_items(payload)
        if not rows or any(str(row.get("routeid")) != route_id for row in rows):
            raise PublicDataResponseError("TAGO route stops do not confirm the route ID.")
        return payload, rows


def snapshot_routes(route_numbers: list[str], city_codes: list[str], output_dir: Path,
                    client: TagoRouteDiscovery | None = None) -> dict[str, Any]:
    """Save raw discovery evidence for every exact route-number candidate."""
    client = client or TagoRouteDiscovery()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "source": "https://www.data.go.kr/data/15098529/openapi.do",
        "retrieved_on": date.today().isoformat(),
        "city_codes": {},
        "files": [], "candidates": {},
    }

    def obtain(filename: str, operation: str, **params: str) -> dict[str, Any]:
        cached = output_dir / filename
        if cached.exists():
            payload = json.loads(cached.read_text(encoding="utf-8"))
            extract_tago_items(payload)
            return payload
        for attempt in range(4):
            try:
                return client.fetch(operation, **params)
            except PublicDataResponseError as exc:
                if "가용한 세션이 존재하지 않습니다" not in str(exc) or attempt == 3:
                    raise
                time.sleep(10 * (attempt + 1))
        raise AssertionError("unreachable")

    city_payload = obtain("city_codes.json", "getCtyCodeList")
    cities = extract_tago_items(city_payload)
    available = {str(row.get("citycode")): str(row.get("cityname")) for row in cities}
    if any(code not in available for code in city_codes):
        raise ValueError("Requested city code is absent from the official city list.")
    manifest["city_codes"] = {code: available[code] for code in city_codes}

    def save(filename: str, operation: str, params: dict[str, Any], payload: dict[str, Any]) -> None:
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if client._key.encode("utf-8") in body or quote(client._key, safe="%").encode("utf-8") in body:
            raise PublicDataResponseError("TAGO response unexpectedly contains a service key.")
        (output_dir / filename).write_bytes(body)
        manifest["files"].append({"file": filename, "operation": operation,
                                  "parameters": params, "sha256": hashlib.sha256(body).hexdigest()})

    save("city_codes.json", "getCtyCodeList", {}, city_payload)
    for number in route_numbers:
        manifest["candidates"][number] = []
        safe_number = number.replace("-", "_")
        for code in city_codes:
            params = {"cityCode": code, "routeNo": number}
            filename = f"{safe_number}_city_{code}_routes.json"
            payload = obtain(filename, "getRouteNoList", **params)
            exact = [row for row in extract_tago_items(payload)
                     if str(row.get("routeno", "")).strip() == number]
            save(filename, "getRouteNoList", params, payload)
            for row in exact:
                route_id = str(row.get("routeid", ""))
                if not route_id or not route_id.replace("-", "").isalnum():
                    raise PublicDataResponseError("TAGO route ID is missing or unsafe for snapshot filename.")
                stem = f"{safe_number}_city_{code}_{route_id}"
                info_payload = obtain(stem + "_info.json", "getRouteInfoIem",
                                      cityCode=code, routeId=route_id)
                info_rows = extract_tago_items(info_payload)
                if len(info_rows) != 1 or str(info_rows[0].get("routeid")) != route_id:
                    raise PublicDataResponseError("TAGO basic info does not confirm route ID.")
                info = info_rows[0]
                if str(info.get("routeno", "")).strip() != number:
                    raise PublicDataResponseError("TAGO basic information disagrees with the requested route number.")
                stops_payload = obtain(stem + "_stops.json", "getRouteAcctoThrghSttnList",
                                       cityCode=code, routeId=route_id)
                stops = extract_tago_items(stops_payload)
                if not stops or any(str(stop.get("routeid")) != route_id for stop in stops):
                    raise PublicDataResponseError("TAGO stops do not confirm route ID.")
                save(stem + "_info.json", "getRouteInfoIem", {"cityCode": code, "routeId": route_id}, info_payload)
                save(stem + "_stops.json", "getRouteAcctoThrghSttnList", {"cityCode": code, "routeId": route_id}, stops_payload)
                manifest["candidates"][number].append({"city_code": code, "route_id": route_id,
                    "start_stop": info.get("startnodenm"), "end_stop": info.get("endnodenm"),
                    "route_type": info.get("routetp"), "stop_count": len(stops),
                    "stops_file": stem + "_stops.json"})
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route-number", action="append", required=True)
    parser.add_argument("--city-code", action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = snapshot_routes(args.route_number, args.city_code, args.output_dir)
    print(json.dumps({"city_codes": result["city_codes"],
                      "candidates": result["candidates"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
