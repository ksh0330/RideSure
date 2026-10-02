from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from public_data import PublicDataResponseError
from tago_route_discovery import TagoRouteDiscovery, snapshot_routes


def envelope(items: list[dict[str, object]], total: int | None = None) -> dict[str, object]:
    body: dict[str, object] = {"items": {"item": items}}
    if total is not None:
        body["totalCount"] = total
    return {"response": {"header": {"resultCode": "00", "resultMsg": "OK"}, "body": body}}


class Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class TagoRouteDiscoveryTests(unittest.TestCase):
    def test_snapshot_keeps_only_exact_route_number_and_key_free_raw_evidence(self) -> None:
        seen: list[tuple[str, dict[str, list[str]]]] = []

        def opener(url: str, *, timeout: float) -> Response:
            path = urlsplit(url)
            query = parse_qs(path.query)
            seen.append((path.path.rsplit("/", 1)[-1], query))
            operation = path.path.rsplit("/", 1)[-1]
            if operation == "getCtyCodeList":
                return Response(envelope([{"citycode": 12, "cityname": "테스트시"}]))
            if operation == "getRouteNoList":
                return Response(envelope([
                    {"routeid": "TEST1", "routeno": "10"},
                    {"routeid": "TEST2", "routeno": "100"},
                ], 2))
            if operation == "getRouteInfoIem":
                return Response(envelope([{"routeid": "TEST1", "routeno": "10",
                                           "startnodenm": "출발", "endnodenm": "도착",
                                           "routetp": "일반"}]))
            return Response(envelope([{"routeid": "TEST1", "nodeid": "NODE1",
                                       "nodeord": 1, "nodenm": "출발",
                                       "gpslati": 36.0, "gpslong": 127.0}], 1))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = snapshot_routes(["10"], ["12"], root,
                                     TagoRouteDiscovery("secret-key", opener=opener))
            self.assertEqual(len(result["candidates"]["10"]), 1)
            self.assertEqual(result["candidates"]["10"][0]["route_id"], "TEST1")
            self.assertFalse((root / "10_city_12_TEST2_info.json").exists())
            self.assertTrue((root / "10_city_12_TEST1_stops.json").exists())
            for file in root.iterdir():
                self.assertNotIn("secret-key", file.read_text(encoding="utf-8"))
            self.assertEqual(len(seen), 4)
            self.assertEqual(seen[1][1]["routeNo"], ["10"])

    def test_incomplete_page_is_rejected(self) -> None:
        client = TagoRouteDiscovery("key", opener=lambda *_args, **_kwargs:
                                    Response(envelope([{"routeid": "A"}], 2)))
        with self.assertRaisesRegex(PublicDataResponseError, "incomplete snapshot"):
            client.fetch("getRouteNoList", cityCode="12", routeNo="10")

    def test_disagreed_route_identity_is_rejected(self) -> None:
        client = TagoRouteDiscovery("key", opener=lambda *_args, **_kwargs:
                                    Response(envelope([{"routeid": "WRONG", "routeno": "10"}])))
        with self.assertRaisesRegex(PublicDataResponseError, "route ID"):
            client.route_info("12", "EXPECTED")

    def test_request_failure_does_not_expose_service_key(self) -> None:
        def fail(*_args: object, **_kwargs: object) -> Response:
            raise OSError("secret-key")

        client = TagoRouteDiscovery("secret-key", opener=fail)
        with self.assertRaises(PublicDataResponseError) as captured:
            client.fetch("getCtyCodeList")
        self.assertNotIn("secret-key", str(captured.exception))
