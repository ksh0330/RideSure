from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import public_data


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class PublicDataFileTests(unittest.TestCase):
    def test_missing_manual_download_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "national_bus_stops"
            with self.assertRaises(public_data.PublicDataNotConfigured):
                public_data.resolve_national_stop_csvs(missing)
            output = io.StringIO()
            with patch("sys.stdout", output):
                result = public_data.main(["validate-national", "--path", str(missing)])
            self.assertEqual(result, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "NOT_CONFIGURED")

    def test_utf8_csv_is_validated_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "synthetic.csv"
            path.write_text(
                "NODE_ID,NODE_NM,GPS_LATI,GPS_LONG,NODE_MOBILE_ID,CITY_CD,CITY_NAME,ADMIN_NM,COLLECTD_TIME\n"
                "SYNTHETIC_NODE,테스트 정류장,36.35,127.38,00001,TEST,테스트시,테스트구,2026-01-01\n",
                encoding="utf-8-sig",
            )
            records = public_data.read_national_stop_csv(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].official_node_id, "SYNTHETIC_NODE")
        self.assertEqual(records[0].city_name, "테스트시")
        self.assertEqual(records[0].source, public_data.NATIONAL_STOP_SOURCE)

    def test_korean_headers_classify_and_exclude_invalid_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "official.csv"
            path.write_text(
                "정류장번호,정류장명,위도,경도,정보수집일,모바일단축번호,도시코드,도시명,관리도시명\n"
                "VALID,정상,36.35,127.38,2026-01-01,00001,TEST,테스트시,테스트BIS\n"
                "MISSING,좌표없음,,,2026-01-01,00002,TEST,테스트시,테스트BIS\n"
                "SWAPPED,좌표의심,127.38,36.35,2026-01-01,00003,TEST,테스트시,테스트BIS\n"
                "INVALID,좌표오류,999,999,2026-01-01,00004,TEST,테스트시,테스트BIS\n",
                encoding="utf-8-sig",
            )
            result = public_data.inspect_national_stop_csv(path)
            output = io.StringIO()
            with patch("sys.stdout", output):
                exit_code = public_data.main(["validate-national", "--path", str(path)])

        self.assertEqual(result.total_rows, 4)
        self.assertEqual([record.official_node_id for record in result.records], ["VALID"])
        self.assertEqual(
            [issue.classification for issue in result.coordinate_issues],
            [
                public_data.MISSING_COORDINATE,
                public_data.SUSPECT_SWAPPED_COORDINATE,
                public_data.OTHER_INVALID_COORDINATE,
            ],
        )
        self.assertEqual(result.coordinate_issues[1].raw_lat, "127.38")
        self.assertEqual(result.coordinate_issues[1].raw_lon, "36.35")
        summary = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "INVALID")
        self.assertEqual(summary["valid_rows"], 1)
        self.assertEqual(summary["missing_coordinate_rows"], 1)
        self.assertEqual(summary["suspect_swapped_rows"], 1)
        self.assertEqual(summary["other_invalid_coordinate_rows"], 1)

    def test_wrong_csv_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "synthetic.csv"
            path.write_text("name,latitude\nstop,36.35\n", encoding="utf-8")
            with self.assertRaisesRegex(public_data.PublicDataResponseError, "NODE_ID"):
                public_data.read_national_stop_csv(path)


class TagoClientTests(unittest.TestCase):
    def test_missing_shared_key_is_not_configured(self) -> None:
        with self.assertRaises(public_data.PublicDataNotConfigured):
            public_data.TagoClient("").route_stops("25", "SYNTHETIC_ROUTE")

    def test_real_envelope_shape_is_normalized(self) -> None:
        payload = {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "OK"},
                "body": {
                    "items": {
                        "item": {
                            "routeid": "SYNTHETIC_ROUTE",
                            "nodeid": "SYNTHETIC_NODE",
                            "nodenm": "테스트 정류장",
                            "nodeord": 1,
                            "gpslati": 36.35,
                            "gpslong": 127.38,
                            "citycode": "25",
                        }
                    }
                },
            }
        }
        seen_url: list[str] = []

        def opener(url: str, **_: object) -> _Response:
            seen_url.append(url)
            return _Response(payload)

        records = public_data.TagoClient("synthetic/key=", opener=opener).route_stops(
            "25", "SYNTHETIC_ROUTE"
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].official_node_id, "SYNTHETIC_NODE")
        self.assertIn("serviceKey=synthetic%2Fkey%3D", seen_url[0])
        self.assertIn("getRouteAcctoThrghSttnList", seen_url[0])

    def test_api_error_does_not_include_service_key(self) -> None:
        secret = "must-not-appear"
        payload = {
            "response": {
                "header": {"resultCode": "30", "resultMsg": "UNREGISTERED"},
                "body": {},
            }
        }
        client = public_data.TagoClient(secret, opener=lambda *args, **kwargs: _Response(payload))
        with self.assertRaises(public_data.PublicDataResponseError) as caught:
            client.route_stops("25", "SYNTHETIC_ROUTE")
        self.assertNotIn(secret, str(caught.exception))

    def test_local_tago_json_can_be_imported_without_an_api_key(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "tago_route_stops.json"
        output = io.StringIO()
        with patch.object(public_data, "import_records", return_value=2) as imported:
            with patch("sys.stdout", output):
                exit_code = public_data.main(["import-tago-json", "--path", str(fixture)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["route_id"], "TEST_ROUTE_001")
        self.assertEqual(len(imported.call_args.args[0]), 2)


class OfficialUpsertTests(unittest.TestCase):
    def test_upsert_uses_mapping_vocabulary_and_never_name_matches(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "tago_route_stops.json"
        tx = Mock()
        tx.run.return_value.consume.return_value = None
        public_data.upsert_official_stop_batch(tx, public_data.load_tago_fixture(fixture))
        query = tx.run.call_args.args[0]
        self.assertIn("mapping_status = coalesce", query)
        self.assertIn("'UNMATCHED'", query)
        self.assertNotIn("MATCH (historical", query)
        self.assertEqual(
            public_data.MAPPING_STATUSES,
            {"EXACT", "SEQUENCE_MATCH", "AMBIGUOUS", "UNMATCHED"},
        )


if __name__ == "__main__":
    unittest.main()
