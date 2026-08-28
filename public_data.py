"""Official public-transit reference-data adapters and import CLI.

The national stop file and TAGO API are optional. Missing files or credentials
are reported as ``NOT_CONFIGURED`` and never prevent historical-only operation.
Official records are stored separately from historical name-only stops; this
module deliberately performs no name-only merge.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

import config
from data_insert_v2 import SCHEMA_VERSION, stable_id


TAGO_SOURCE = "data.go.kr:15098529"
NATIONAL_STOP_SOURCE = "data.go.kr:15067528"
TAGO_ROUTE_STOPS_URL = (
    "https://apis.data.go.kr/1613000/BusRouteInfoInqireService/"
    "getRouteAcctoThrghSttnList"
)
NATIONAL_STOP_CSV_GLOB = "*.csv"
NATIONAL_STOP_COLUMN_ALIASES = {
    "정류장번호": "NODE_ID",
    "정류장명": "NODE_NM",
    "위도": "GPS_LATI",
    "경도": "GPS_LONG",
    "정보수집일": "COLLECTD_TIME",
    "모바일단축번호": "NODE_MOBILE_ID",
    "도시코드": "CITY_CD",
    "도시명": "CITY_NAME",
    "관리도시명": "ADMIN_NM",
}
VALID_COORDINATE = "VALID"
MISSING_COORDINATE = "MISSING_COORDINATE"
SUSPECT_SWAPPED_COORDINATE = "SUSPECT_SWAPPED_COORDINATE"
OTHER_INVALID_COORDINATE = "OTHER_INVALID_COORDINATE"
MAPPING_STATUSES = frozenset({"EXACT", "SEQUENCE_MATCH", "AMBIGUOUS", "UNMATCHED"})


class PublicDataNotConfigured(RuntimeError):
    """An optional external source has not been configured locally."""


class PublicDataResponseError(RuntimeError):
    """An official API or file returned data that cannot be used safely."""


@dataclass(frozen=True)
class OfficialStopRecord:
    stop_id: str
    official_node_id: str
    name: str
    lat: float
    lon: float
    city_code: str | None
    source: str
    official_route_id: str | None = None
    stop_number: str | None = None
    node_order: int | None = None
    direction_code: str | None = None
    staging_id: str | None = None
    city_name: str | None = None
    admin_name: str | None = None
    collected_at: str | None = None


@dataclass(frozen=True)
class NationalStopCoordinateIssue:
    classification: str
    source_file: str
    row_number: int
    official_node_id: str
    name: str
    raw_lat: str | None
    raw_lon: str | None
    raw_row: Mapping[str, Any]


@dataclass(frozen=True)
class NationalStopCsvResult:
    source_file: str
    total_rows: int
    records: tuple[OfficialStopRecord, ...]
    coordinate_issues: tuple[NationalStopCoordinateIssue, ...]


def _optional(item: Mapping[str, Any], key: str) -> str | None:
    value = item.get(key)
    return str(value).strip() if value is not None and str(value).strip() else None


def _required(item: Mapping[str, Any], key: str) -> str:
    value = _optional(item, key)
    if value is None:
        raise ValueError(f"Official record is missing {key}.")
    return value


def _coordinate(value: Any, key: str, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Official record has invalid {key}.") from exc
    if result < low or result > high:
        raise ValueError(f"Official record has out-of-range {key}.")
    return result


def _official_stop_id(node_id: str) -> str:
    # NODE_ID/nodeid is the authority identifier. Keeping it source-independent
    # lets TAGO route topology enrich the corresponding national stop record.
    return stable_id("official-stop", node_id)


def normalize_tago_route_stops(items: Iterable[Mapping[str, Any]]) -> list[OfficialStopRecord]:
    """Normalize TAGO route-stop items without inventing direction or IDs."""
    records: list[OfficialStopRecord] = []
    for item in items:
        node_id = _required(item, "nodeid")
        route_id = _required(item, "routeid")
        node_order_raw = _required(item, "nodeord")
        try:
            node_order = int(node_order_raw)
        except ValueError as exc:
            raise ValueError("Official record has invalid nodeord.") from exc
        if node_order < 1:
            raise ValueError("Official record has invalid nodeord.")
        direction_code = _optional(item, "updowncd")
        records.append(
            OfficialStopRecord(
                stop_id=_official_stop_id(node_id),
                official_node_id=node_id,
                name=_required(item, "nodenm"),
                lat=_coordinate(item.get("gpslati"), "gpslati", -90, 90),
                lon=_coordinate(item.get("gpslong"), "gpslong", -180, 180),
                city_code=_optional(item, "citycode"),
                source=TAGO_SOURCE,
                official_route_id=route_id,
                stop_number=_optional(item, "nodeno"),
                node_order=node_order,
                direction_code=direction_code,
                staging_id=stable_id(
                    "tago-route-stop",
                    route_id,
                    direction_code or "UNSPECIFIED",
                    node_order,
                    node_id,
                ),
            )
        )
    return records


def normalize_national_stop_rows(rows: Iterable[Mapping[str, Any]]) -> list[OfficialStopRecord]:
    """Normalize nationwide stop rows whose columns use canonical names."""
    records: list[OfficialStopRecord] = []
    for row in rows:
        node_id = _required(row, "NODE_ID")
        records.append(
            OfficialStopRecord(
                stop_id=_official_stop_id(node_id),
                official_node_id=node_id,
                name=_required(row, "NODE_NM"),
                lat=_coordinate(row.get("GPS_LATI"), "GPS_LATI", -90, 90),
                lon=_coordinate(row.get("GPS_LONG"), "GPS_LONG", -180, 180),
                city_code=_optional(row, "CITY_CD"),
                city_name=_optional(row, "CITY_NAME"),
                admin_name=_optional(row, "ADMIN_NM"),
                collected_at=_optional(row, "COLLECTD_TIME"),
                source=NATIONAL_STOP_SOURCE,
                stop_number=_optional(row, "NODE_MOBILE_ID"),
            )
        )
    return records


def _clean_csv_row(row: Mapping[str | None, Any]) -> dict[str, Any]:
    return {
        NATIONAL_STOP_COLUMN_ALIASES.get(
            str(key).strip().lstrip("\ufeff"), str(key).strip().lstrip("\ufeff")
        ): value.strip() if isinstance(value, str) else value
        for key, value in row.items()
        if key is not None
    }


def _classify_national_stop_coordinate(row: Mapping[str, Any]) -> str:
    raw_lat = _optional(row, "GPS_LATI")
    raw_lon = _optional(row, "GPS_LONG")
    if raw_lat is None or raw_lon is None:
        return MISSING_COORDINATE
    try:
        lat = float(raw_lat)
        lon = float(raw_lon)
    except ValueError:
        return OTHER_INVALID_COORDINATE
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return VALID_COORDINATE
    if -180 <= lat <= 180 and -90 <= lon <= 90:
        return SUSPECT_SWAPPED_COORDINATE
    return OTHER_INVALID_COORDINATE


def inspect_national_stop_csv(path: str | Path) -> NationalStopCsvResult:
    """Read valid stops and retain invalid-coordinate rows without repairing them."""
    csv_path = Path(path)
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            with csv_path.open("r", encoding=encoding, newline="") as stream:
                reader = csv.DictReader(stream)
                headers = {
                    NATIONAL_STOP_COLUMN_ALIASES.get(
                        str(name).strip().lstrip("\ufeff"),
                        str(name).strip().lstrip("\ufeff"),
                    )
                    for name in (reader.fieldnames or [])
                    if name is not None
                }
                required = {"NODE_ID", "NODE_NM", "GPS_LATI", "GPS_LONG"}
                missing = sorted(required - headers)
                if missing:
                    raise PublicDataResponseError(
                        "National stop CSV is missing required column(s): " + ", ".join(missing)
                    )
                records: list[OfficialStopRecord] = []
                coordinate_issues: list[NationalStopCoordinateIssue] = []
                total_rows = 0
                for row_number, raw_row in enumerate(reader, start=2):
                    total_rows += 1
                    row = _clean_csv_row(raw_row)
                    node_id = _required(row, "NODE_ID")
                    name = _required(row, "NODE_NM")
                    classification = _classify_national_stop_coordinate(row)
                    if classification == VALID_COORDINATE:
                        records.extend(normalize_national_stop_rows([row]))
                        continue
                    coordinate_issues.append(
                        NationalStopCoordinateIssue(
                            classification=classification,
                            source_file=str(csv_path),
                            row_number=row_number,
                            official_node_id=node_id,
                            name=name,
                            raw_lat=_optional(row, "GPS_LATI"),
                            raw_lon=_optional(row, "GPS_LONG"),
                            raw_row=dict(row),
                        )
                    )
            return NationalStopCsvResult(
                source_file=str(csv_path),
                total_rows=total_rows,
                records=tuple(records),
                coordinate_issues=tuple(coordinate_issues),
            )
        except UnicodeDecodeError as exc:
            last_error = exc
        except (OSError, csv.Error) as exc:
            raise PublicDataResponseError(f"Unreadable CSV file: {csv_path.name}") from exc
    raise PublicDataResponseError(f"Unsupported CSV encoding: {csv_path.name}") from last_error


def read_national_stop_csv(path: str | Path) -> list[OfficialStopRecord]:
    """Return importable records, excluding invalid-coordinate source rows."""
    return list(inspect_national_stop_csv(path).records)


def resolve_national_stop_csvs(path: str | Path | None = None) -> list[Path]:
    """Resolve an explicit CSV or all CSVs in the configured manual-download directory."""
    target = Path(path or config.NATIONAL_BUS_STOP_CSV_DIR).expanduser()
    if target.is_file():
        if target.suffix.lower() != ".csv":
            raise PublicDataResponseError(f"Expected a CSV file: {target}")
        return [target.resolve()]
    if not target.is_dir():
        raise PublicDataNotConfigured(
            f"National bus-stop CSV directory is missing: {target.resolve()}"
        )
    files = sorted(candidate.resolve() for candidate in target.glob(NATIONAL_STOP_CSV_GLOB))
    if not files:
        raise PublicDataNotConfigured(f"No CSV files found in: {target.resolve()}")
    return files


def iter_batches(values: Iterable[OfficialStopRecord], size: int) -> Iterator[list[OfficialStopRecord]]:
    if size < 1:
        raise ValueError("Batch size must be at least 1.")
    batch: list[OfficialStopRecord] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def upsert_official_stop_batch(tx: Any, records: Iterable[OfficialStopRecord]) -> None:
    """Upsert official stops/topology without matching historical name-only stops."""
    rows = [asdict(record) for record in records]
    tx.run(
        """
        UNWIND $rows AS r
        MERGE (stop:Stop {stop_id: r.stop_id})
        SET stop.official_node_id = r.official_node_id,
            stop.name = r.name,
            stop.lat = r.lat,
            stop.lon = r.lon,
            stop.location = point({latitude: r.lat, longitude: r.lon}),
            stop.city_code = coalesce(r.city_code, stop.city_code),
            stop.city_name = coalesce(r.city_name, stop.city_name),
            stop.admin_name = coalesce(r.admin_name, stop.admin_name),
            stop.stop_number = coalesce(r.stop_number, stop.stop_number),
            stop.collected_at = coalesce(r.collected_at, stop.collected_at),
            stop.coordinate_source = r.source,
            stop.reference_source = r.source,
            stop.id_kind = 'OFFICIAL_NODE_ID',
            stop.mapping_status = coalesce(stop.mapping_status, 'UNMATCHED'),
            stop.schema_version = $schema_version,
            stop.reference_updated_at = datetime()
        FOREACH (_ IN CASE
          WHEN r.staging_id IS NULL OR r.official_route_id IS NULL OR r.node_order IS NULL
          THEN [] ELSE [1] END |
          MERGE (staging:RouteStopStaging {staging_id: r.staging_id})
          SET staging.official_route_id = r.official_route_id,
              staging.official_node_id = r.official_node_id,
              staging.node_order = r.node_order,
              staging.direction_code = r.direction_code,
              staging.stop_number = r.stop_number,
              staging.source = r.source,
              staging.mapping_status = coalesce(staging.mapping_status, 'UNMATCHED'),
              staging.schema_version = $schema_version
          MERGE (staging)-[:STAGES_STOP]->(stop)
        )
        """,
        rows=rows,
        schema_version=SCHEMA_VERSION,
    ).consume()


def extract_tago_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Extract route stops from either a real TAGO envelope or a test fixture."""
    if isinstance(payload.get("items"), list):
        return payload["items"]  # type: ignore[return-value]
    response = payload.get("response")
    if not isinstance(response, Mapping):
        raise PublicDataResponseError("TAGO response is missing response.")
    header = response.get("header")
    if not isinstance(header, Mapping):
        raise PublicDataResponseError("TAGO response is missing header.")
    code = str(header.get("resultCode", "")).strip()
    if code not in {"00", "0"}:
        message = str(header.get("resultMsg", "UNKNOWN_ERROR")).strip()
        raise PublicDataResponseError(f"TAGO request failed ({code or 'UNKNOWN'}: {message}).")
    body = response.get("body")
    if not isinstance(body, Mapping):
        return []
    items_container = body.get("items")
    if not isinstance(items_container, Mapping):
        return []
    items = items_container.get("item")
    if items in (None, ""):
        return []
    if isinstance(items, Mapping):
        return [items]
    if isinstance(items, list) and all(isinstance(item, Mapping) for item in items):
        return items
    raise PublicDataResponseError("TAGO response has an invalid item collection.")


def load_tago_fixture(path: str | Path) -> list[OfficialStopRecord]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return normalize_tago_route_stops(extract_tago_items(payload))


class TagoClient:
    """Small TAGO route-stop client using the shared data.go.kr service key."""

    def __init__(
        self,
        service_key: str | None = None,
        *,
        timeout: float = 10.0,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        configured_key = service_key if service_key is not None else config.DATA_GO_KR_SERVICE_KEY
        self._service_key = configured_key.strip()
        self._timeout = timeout
        self._opener = opener

    @property
    def configured(self) -> bool:
        return bool(self._service_key) and self._service_key.lower() not in config.PLACEHOLDER_VALUES

    def route_stops(self, city_code: str, route_id: str) -> list[OfficialStopRecord]:
        if not self.configured:
            raise PublicDataNotConfigured("DATA_GO_KR_SERVICE_KEY is not configured.")
        if not city_code.strip() or not route_id.strip():
            raise ValueError("city_code and route_id are required.")
        # Preserve already percent-encoded portal keys while safely encoding decoded keys.
        key = quote(self._service_key, safe="%")
        query = urlencode(
            {
                "_type": "json",
                "cityCode": city_code.strip(),
                "routeId": route_id.strip(),
                "numOfRows": 1000,
                "pageNo": 1,
            }
        )
        url = f"{TAGO_ROUTE_STOPS_URL}?serviceKey={key}&{query}"
        try:
            with self._opener(url, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            # Do not include the URL: it contains the service key.
            raise PublicDataResponseError(f"TAGO request could not be completed ({type(exc).__name__}).") from exc
        return normalize_tago_route_stops(extract_tago_items(payload))


def configuration_status(path: str | Path | None = None) -> dict[str, Any]:
    client = TagoClient()
    try:
        files = resolve_national_stop_csvs(path)
    except PublicDataNotConfigured:
        files = []
    return {
        "data_go_kr": "READY" if client.configured else "NOT_CONFIGURED",
        "national_stop_csv": "READY" if files else "NOT_CONFIGURED",
        "national_stop_csv_dir": str(Path(path or config.NATIONAL_BUS_STOP_CSV_DIR).resolve()),
        "national_stop_csv_files": [file.name for file in files],
    }


def _open_v2_driver() -> Any:
    from neo4j import GraphDatabase

    config.validate_required_config(("neo4j_v2",))
    driver = GraphDatabase.driver(
        config.NEO4J_V2_URI,
        auth=(config.NEO4J_USER, config.NEO4J_PASS),
        connection_timeout=10,
    )
    driver.verify_connectivity()
    return driver


def import_records(records: Iterable[OfficialStopRecord], batch_size: int) -> int:
    driver = _open_v2_driver()
    count = 0
    try:
        with driver.session() as session:
            for batch in iter_batches(records, batch_size):
                session.execute_write(upsert_official_stop_batch, batch)
                count += len(batch)
    finally:
        driver.close()
    return count


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _national_validation_summary(
    files: list[Path], results: list[NationalStopCsvResult]
) -> dict[str, Any]:
    total_rows = sum(result.total_rows for result in results)
    valid_rows = sum(len(result.records) for result in results)
    issues = [issue for result in results for issue in result.coordinate_issues]
    missing_rows = sum(issue.classification == MISSING_COORDINATE for issue in issues)
    suspect_swapped_rows = sum(
        issue.classification == SUSPECT_SWAPPED_COORDINATE for issue in issues
    )
    other_invalid_rows = sum(
        issue.classification == OTHER_INVALID_COORDINATE for issue in issues
    )
    if total_rows == 0 or valid_rows * 2 < total_rows:
        status = "INVALID"
    elif issues:
        status = "VALID_WITH_WARNINGS"
    else:
        status = "VALID"
    return {
        "status": status,
        "files": [file.name for file in files],
        "total_rows": total_rows,
        "valid_rows": valid_rows,
        "missing_coordinate_rows": missing_rows,
        "suspect_swapped_rows": suspect_swapped_rows,
        "other_invalid_coordinate_rows": other_invalid_rows,
        # Keep the old count available to callers; only valid records are importable.
        "records": valid_rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status", help="show optional-source configuration")
    status_parser.add_argument("--path")
    validate_parser = subparsers.add_parser("validate-national", help="validate downloaded CSVs")
    validate_parser.add_argument("--path")
    import_parser = subparsers.add_parser("import-national", help="import downloaded CSVs into Neo4j v2")
    import_parser.add_argument("--path")
    import_parser.add_argument("--batch-size", type=int, default=2_000)
    tago_parser = subparsers.add_parser("fetch-tago", help="fetch an official route-stop sequence")
    tago_parser.add_argument("--city-code", required=True)
    tago_parser.add_argument("--route-id", required=True)
    tago_parser.add_argument("--import-to-neo4j", action="store_true")
    tago_parser.add_argument("--batch-size", type=int, default=2_000)
    args = parser.parse_args(argv)

    if args.command == "status":
        _emit(configuration_status(args.path))
        return 0
    try:
        if args.command in {"validate-national", "import-national"}:
            files = resolve_national_stop_csvs(args.path)
            file_results = [inspect_national_stop_csv(file) for file in files]
            result = _national_validation_summary(files, file_results)
            if result["status"] == "INVALID":
                _emit(result)
                return 1
            if args.command == "import-national":
                records = [record for file_result in file_results for record in file_result.records]
                result["imported"] = import_records(records, args.batch_size)
            _emit(result)
            return 0
        records = TagoClient().route_stops(args.city_code, args.route_id)
        imported = import_records(records, args.batch_size) if args.import_to_neo4j else 0
        _emit(
            {
                "status": "READY",
                "source": TAGO_SOURCE,
                "route_id": args.route_id,
                "records": len(records),
                "imported": imported,
            }
        )
        return 0
    except PublicDataNotConfigured as exc:
        _emit({"status": "NOT_CONFIGURED", "reason": str(exc)})
        return 2
    except (PublicDataResponseError, ValueError) as exc:
        _emit({"status": "INVALID", "reason": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
