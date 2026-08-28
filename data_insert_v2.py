"""Lossless, idempotent historical CSV importer for the RideSure Neo4j v2 graph.

The recovered v1 loader remains in :mod:`data_insert`.  This module targets the
separate v2 Neo4j service and never deletes or rewrites v1 nodes or volumes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import pandas as pd
from neo4j import GraphDatabase

import config


SOURCE = "ridesure_historical_onboard_csv"
SCHEMA_VERSION = "2"
REQUIRED_COLUMNS = ["노선", "기종점", "정류장순번", "정류장명"]
EXPECTED_V2_COUNTS = {
    "Line": 148,
    "RoutePattern": 154,
    "Stop": 2_049,
    "StopOccurrence": 11_375,
    "LoadObservation": 273_000,
    "ImportBatch": 3,
    "HAS_PATTERN": 154,
    "HAS_OCCURRENCE": 11_375,
    "AT_STOP": 11_375,
    "NEXT": 11_221,
    "OBSERVED_AT": 273_000,
    "ON_LINE": 273_000,
    "IMPORTED_IN": 273_000,
}


@dataclass(frozen=True)
class SourceFrame:
    path: Path
    file_hash: str
    service_date: str
    frame: pd.DataFrame


@dataclass(frozen=True)
class V2Snapshot:
    state: str
    counts: dict[str, int]
    orphan_count: int
    sequence_error_count: int
    invalid_coordinate_count: int
    incomplete_batch_count: int
    provenance_error_count: int


def normalized_text(value: Any, field: str) -> str:
    if value is None or pd.isna(value):
        raise ValueError(f"CSV row has no usable {field}.")
    text = unicodedata.normalize("NFC", str(value).strip())
    if not text or text.lower() == "nan":
        raise ValueError(f"CSV row has no usable {field}.")
    return text


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def date_from_filename(path: Path) -> str:
    import re

    match = re.search(r"(\d{8})", path.name)
    if not match:
        raise ValueError(f"CSV filename has no YYYYMMDD date: {path.name}")
    compact = match.group(1)
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def read_csv_korean(path: Path) -> pd.DataFrame:
    errors: list[str] = []
    for encoding in ("utf-8", "utf-8-sig", "cp949", "euc-kr"):
        try:
            frame = pd.read_csv(path, encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            errors.append(f"{encoding}: {exc}")
    else:
        raise RuntimeError(f"Could not read {path.name}: {'; '.join(errors)}")

    missing = [name for name in REQUIRED_COLUMNS if name not in frame.columns]
    missing_hours = [name for name in config.HOUR_COLS if name not in frame.columns]
    if missing or missing_hours:
        names = missing + missing_hours
        raise ValueError(f"CSV is missing required column(s): {', '.join(names)}")
    return frame


def load_sources(file_paths: Iterable[str | Path]) -> list[SourceFrame]:
    paths = [Path(path) for path in file_paths]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required CSV file(s) missing: {', '.join(missing)}")
    return [
        SourceFrame(
            path=path,
            file_hash=file_sha256(path),
            service_date=date_from_filename(path),
            frame=read_csv_korean(path),
        )
        for path in paths
    ]


def parse_stop_seq(value: Any) -> int:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid stop sequence: {value!r}") from exc
    # The source mixes zero-based and one-based patterns, so zero is valid.
    if not math.isfinite(parsed) or not parsed.is_integer() or parsed < 0:
        raise ValueError(f"Invalid stop sequence: {value!r}")
    return int(parsed)


def parse_onboard_count(value: Any) -> tuple[str | None, int | None, str]:
    """Return raw text, parsed non-negative integer, and provenance status."""
    if value is None or pd.isna(value):
        return None, None, "MISSING"
    raw_value = str(value).strip()
    if not raw_value:
        return raw_value, None, "MISSING"
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        return raw_value, None, "INVALID"
    if not math.isfinite(parsed) or not parsed.is_integer() or parsed < 0:
        return raw_value, None, "INVALID"
    return raw_value, int(parsed), "VALID"


def _raw_topology_rows(sources: Sequence[SourceFrame]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in sources:
        for offset, (_, row) in enumerate(source.frame.iterrows(), start=2):
            raw_line = normalized_text(row["노선"], "line")
            terminal = normalized_text(row["기종점"], "terminal description")
            stop_name = normalized_text(row["정류장명"], "stop name")
            seq = parse_stop_seq(row["정류장순번"])
            line_id = stable_id("hist-line", raw_line)
            pattern_id = stable_id("hist-pattern", raw_line, terminal)
            occurrence_id = stable_id("hist-occ", pattern_id, seq)
            rows.append(
                {
                    "line_id": line_id,
                    "raw_line": raw_line,
                    "pattern_id": pattern_id,
                    "terminal_description": terminal,
                    "seq": seq,
                    "stop_id": stable_id("hist-stop-name", stop_name),
                    "raw_stop_name": stop_name,
                    "occurrence_id": occurrence_id,
                    "source_file": source.path.name,
                    "source_row": offset,
                }
            )
    return rows


def build_topology_rows(sources: Sequence[SourceFrame]) -> list[dict[str, Any]]:
    rows = _raw_topology_rows(sources)
    by_occurrence: dict[str, dict[str, Any]] = {}
    for row in rows:
        existing = by_occurrence.get(row["occurrence_id"])
        if existing and existing["stop_id"] != row["stop_id"]:
            raise ValueError(
                "Conflicting stop names share a pattern/sequence key: "
                f"{row['raw_line']} / {row['terminal_description']} / {row['seq']}"
            )
        by_occurrence.setdefault(row["occurrence_id"], row)

    unique_rows = list(by_occurrence.values())
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in unique_rows:
        grouped.setdefault(row["pattern_id"], []).append(row)

    for pattern_rows in grouped.values():
        pattern_rows.sort(key=lambda item: item["seq"])
        expected = list(range(pattern_rows[0]["seq"], pattern_rows[-1]["seq"] + 1))
        actual = [row["seq"] for row in pattern_rows]
        if actual != expected:
            sample = pattern_rows[0]
            raise ValueError(
                "RoutePattern has a non-contiguous stop sequence: "
                f"{sample['raw_line']} / {sample['terminal_description']}"
            )
        signature = json.dumps(
            [(row["seq"], row["raw_stop_name"]) for row in pattern_rows],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        pattern_hash = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        for index, row in enumerate(pattern_rows):
            row["pattern_hash"] = pattern_hash
            row["previous_occurrence_id"] = (
                pattern_rows[index - 1]["occurrence_id"] if index else None
            )
    return sorted(unique_rows, key=lambda item: (item["pattern_id"], item["seq"]))


def topology_lookup(topology_rows: Sequence[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    return {
        (row["raw_line"], row["terminal_description"], row["seq"]): row
        for row in topology_rows
    }


def iter_observation_rows(
    source: SourceFrame,
    lookup: dict[tuple[str, str, int], dict[str, Any]],
    imported_at: str,
) -> Iterator[dict[str, Any]]:
    for source_row, (_, row) in enumerate(source.frame.iterrows(), start=2):
        raw_line = normalized_text(row["노선"], "line")
        terminal = normalized_text(row["기종점"], "terminal description")
        seq = parse_stop_seq(row["정류장순번"])
        topology = lookup[(raw_line, terminal, seq)]
        source_row_id = stable_id("hist-source-row", source.file_hash, source_row)
        for hour, hour_column in enumerate(config.HOUR_COLS):
            raw_value, onboard_count, parse_status = parse_onboard_count(row[hour_column])
            yield {
                "observation_id": stable_id(
                    "hist-load", source.file_hash, source_row, hour
                ),
                "source_file": source.path.name,
                "source_row": source_row,
                "source_row_id": source_row_id,
                "source_hour_column": hour_column,
                "service_date": source.service_date,
                "hour": hour,
                "raw_line": raw_line,
                "raw_terminal_description": terminal,
                "raw_stop_seq": seq,
                "raw_stop_name": topology["raw_stop_name"],
                "raw_value": raw_value,
                "onboard_count": onboard_count,
                "parse_status": parse_status,
                "imported_at": imported_at,
                "occurrence_id": topology["occurrence_id"],
                "line_id": topology["line_id"],
                "batch_id": stable_id("hist-batch", source.file_hash),
            }


def source_profile(sources: Sequence[SourceFrame]) -> dict[str, Any]:
    topology = build_topology_rows(sources)
    stop_ids = {row["stop_id"] for row in topology}
    line_ids = {row["line_id"] for row in topology}
    pattern_ids = {row["pattern_id"] for row in topology}
    observations = 0
    statuses = {"VALID": 0, "MISSING": 0, "INVALID": 0}
    lookup = topology_lookup(topology)
    for source in sources:
        for row in iter_observation_rows(source, lookup, "PROFILE_ONLY"):
            observations += 1
            statuses[row["parse_status"]] += 1
    return {
        "files": [
            {
                "name": source.path.name,
                "sha256": source.file_hash,
                "raw_rows": len(source.frame),
                "observations": len(source.frame) * 24,
            }
            for source in sources
        ],
        "raw_rows": len(topology),
        "observations": observations,
        "lines": len(line_ids),
        "route_patterns": len(pattern_ids),
        "source_local_stops": len(stop_ids),
        "stop_occurrences": len(topology),
        "next_relationships": sum(
            row["previous_occurrence_id"] is not None for row in topology
        ),
        "parse_status": statuses,
    }


def chunks(rows: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def create_constraints(driver: Any) -> None:
    statements = (
        "CREATE CONSTRAINT v2_line_id IF NOT EXISTS FOR (n:Line) REQUIRE n.line_id IS UNIQUE",
        "CREATE CONSTRAINT v2_pattern_id IF NOT EXISTS FOR (n:RoutePattern) REQUIRE n.pattern_id IS UNIQUE",
        "CREATE CONSTRAINT v2_stop_id IF NOT EXISTS FOR (n:Stop) REQUIRE n.stop_id IS UNIQUE",
        "CREATE CONSTRAINT v2_occurrence_id IF NOT EXISTS FOR (n:StopOccurrence) REQUIRE n.occurrence_id IS UNIQUE",
        "CREATE CONSTRAINT v2_observation_id IF NOT EXISTS FOR (n:LoadObservation) REQUIRE n.observation_id IS UNIQUE",
        "CREATE CONSTRAINT v2_import_batch_id IF NOT EXISTS FOR (n:ImportBatch) REQUIRE n.batch_id IS UNIQUE",
        "CREATE CONSTRAINT v2_realtime_observation_id IF NOT EXISTS FOR (n:RealtimeObservation) REQUIRE n.observation_id IS UNIQUE",
        "CREATE CONSTRAINT v2_load_profile_id IF NOT EXISTS FOR (n:LoadProfile) REQUIRE n.profile_id IS UNIQUE",
        "CREATE CONSTRAINT v2_route_stop_staging_id IF NOT EXISTS FOR (n:RouteStopStaging) REQUIRE n.staging_id IS UNIQUE",
        "CREATE INDEX v2_pattern_line IF NOT EXISTS FOR (n:RoutePattern) ON (n.line_id)",
        "CREATE INDEX v2_occurrence_pattern_seq IF NOT EXISTS FOR (n:StopOccurrence) ON (n.pattern_id, n.seq)",
        "CREATE INDEX v2_stop_name IF NOT EXISTS FOR (n:Stop) ON (n.name)",
        "CREATE INDEX v2_observation_time IF NOT EXISTS FOR (n:LoadObservation) ON (n.service_date, n.hour)",
        "CREATE INDEX v2_realtime_observed_at IF NOT EXISTS FOR (n:RealtimeObservation) ON (n.observed_at)",
        "CREATE INDEX v2_profile_occurrence_hour IF NOT EXISTS FOR (n:LoadProfile) ON (n.occurrence_id, n.hour)",
    )
    with driver.session() as session:
        for statement in statements:
            session.run(statement).consume()


def upsert_topology_batch(tx: Any, rows: list[dict[str, Any]]) -> None:
    tx.run(
        """
        UNWIND $rows AS r
        MERGE (line:Line {line_id: r.line_id})
        SET line.name = r.raw_line,
            line.raw_line = r.raw_line,
            line.source = $source,
            line.id_kind = 'HISTORICAL_SOURCE_LOCAL',
            line.schema_version = $schema_version
        MERGE (pattern:RoutePattern {pattern_id: r.pattern_id})
        SET pattern.line_id = r.line_id,
            pattern.terminal_description = r.terminal_description,
            pattern.direction_status = 'UNKNOWN',
            pattern.version = 1,
            pattern.pattern_hash = r.pattern_hash,
            pattern.source = $source,
            pattern.schema_version = $schema_version
        MERGE (line)-[:HAS_PATTERN]->(pattern)
        MERGE (occurrence:StopOccurrence {occurrence_id: r.occurrence_id})
        SET occurrence.pattern_id = r.pattern_id,
            occurrence.seq = r.seq,
            occurrence.raw_stop_name = r.raw_stop_name,
            occurrence.source = $source,
            occurrence.schema_version = $schema_version
        MERGE (pattern)-[:HAS_OCCURRENCE]->(occurrence)
        MERGE (stop:Stop {stop_id: r.stop_id})
        SET stop.name = r.raw_stop_name,
            stop.raw_name = r.raw_stop_name,
            stop.source = $source,
            stop.id_kind = 'HISTORICAL_NAME_HASH',
            stop.mapping_status = 'UNMAPPED_NAME_ONLY',
            stop.schema_version = $schema_version
        MERGE (occurrence)-[:AT_STOP]->(stop)
        """,
        rows=rows,
        source=SOURCE,
        schema_version=SCHEMA_VERSION,
    ).consume()


def upsert_next_batch(tx: Any, rows: list[dict[str, str]]) -> None:
    tx.run(
        """
        UNWIND $rows AS r
        MATCH (previous:StopOccurrence {occurrence_id: r.previous_occurrence_id})
        MATCH (current:StopOccurrence {occurrence_id: r.occurrence_id})
        MERGE (previous)-[:NEXT]->(current)
        """,
        rows=rows,
    ).consume()


def upsert_import_batch(tx: Any, row: dict[str, Any]) -> None:
    tx.run(
        """
        MERGE (batch:ImportBatch {batch_id: $row.batch_id})
        ON CREATE SET batch.imported_at = $row.imported_at
        SET batch.source = $source,
            batch.input_file = $row.input_file,
            batch.file_hash = $row.file_hash,
            batch.row_count = $row.row_count,
            batch.observation_count = $row.observation_count,
            batch.valid_count = $row.valid_count,
            batch.missing_count = $row.missing_count,
            batch.invalid_count = $row.invalid_count,
            batch.status = $row.status,
            batch.last_seen_at = $row.imported_at,
            batch.schema_version = $schema_version
        """,
        row=row,
        source=SOURCE,
        schema_version=SCHEMA_VERSION,
    ).consume()


def upsert_observation_batch(tx: Any, rows: list[dict[str, Any]]) -> None:
    tx.run(
        """
        UNWIND $rows AS r
        MATCH (occurrence:StopOccurrence {occurrence_id: r.occurrence_id})
        MATCH (line:Line {line_id: r.line_id})
        MATCH (batch:ImportBatch {batch_id: r.batch_id})
        MERGE (observation:LoadObservation {observation_id: r.observation_id})
        ON CREATE SET observation.imported_at = r.imported_at
        SET observation.source_file = r.source_file,
            observation.source_row = r.source_row,
            observation.source_row_id = r.source_row_id,
            observation.source_hour_column = r.source_hour_column,
            observation.service_date = r.service_date,
            observation.hour = r.hour,
            observation.raw_line = r.raw_line,
            observation.raw_terminal_description = r.raw_terminal_description,
            observation.raw_stop_seq = r.raw_stop_seq,
            observation.raw_stop_name = r.raw_stop_name,
            observation.raw_value = r.raw_value,
            observation.onboard_count = r.onboard_count,
            observation.parse_status = r.parse_status,
            observation.source = $source,
            observation.schema_version = $schema_version,
            observation.last_seen_at = r.imported_at
        MERGE (observation)-[:OBSERVED_AT]->(occurrence)
        MERGE (observation)-[:ON_LINE]->(line)
        MERGE (observation)-[:IMPORTED_IN]->(batch)
        """,
        rows=rows,
        source=SOURCE,
        schema_version=SCHEMA_VERSION,
    ).consume()


def process_files(driver: Any, file_paths: Iterable[str | Path], batch_size: int = 2_000) -> dict[str, Any]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    sources = load_sources(file_paths)
    topology = build_topology_rows(sources)
    lookup = topology_lookup(topology)
    imported_at = datetime.now(timezone.utc).isoformat()
    create_constraints(driver)

    with driver.session() as session:
        for batch in chunks(topology, batch_size):
            session.execute_write(upsert_topology_batch, batch)
        next_rows = [
            {
                "previous_occurrence_id": row["previous_occurrence_id"],
                "occurrence_id": row["occurrence_id"],
            }
            for row in topology
            if row["previous_occurrence_id"] is not None
        ]
        for batch in chunks(next_rows, batch_size):
            session.execute_write(upsert_next_batch, batch)

        for source in sources:
            batch_record = {
                "batch_id": stable_id("hist-batch", source.file_hash),
                "input_file": source.path.name,
                "file_hash": source.file_hash,
                "imported_at": imported_at,
                "row_count": len(source.frame),
                "observation_count": len(source.frame) * 24,
                "valid_count": 0,
                "missing_count": 0,
                "invalid_count": 0,
                "status": "RUNNING",
            }
            session.execute_write(upsert_import_batch, batch_record)
            try:
                for observation_batch in chunks(
                    iter_observation_rows(source, lookup, imported_at), batch_size
                ):
                    for observation in observation_batch:
                        key = f"{observation['parse_status'].lower()}_count"
                        batch_record[key] += 1
                    session.execute_write(upsert_observation_batch, observation_batch)
                batch_record["status"] = "COMPLETE"
                session.execute_write(upsert_import_batch, batch_record)
            except Exception:
                batch_record["status"] = "FAILED"
                session.execute_write(upsert_import_batch, batch_record)
                raise
    return source_profile(sources)


def _scalar(session: Any, query: str, **parameters: Any) -> int:
    record = session.run(query, **parameters).single(strict=True)
    return int(record["count"])


def inspect_database(driver: Any) -> V2Snapshot:
    node_queries = {
        "Line": "MATCH (n:Line {source: $source}) RETURN count(n) AS count",
        "RoutePattern": "MATCH (n:RoutePattern {source: $source}) RETURN count(n) AS count",
        "Stop": "MATCH (n:Stop {source: $source}) RETURN count(n) AS count",
        "StopOccurrence": "MATCH (n:StopOccurrence {source: $source}) RETURN count(n) AS count",
        "LoadObservation": "MATCH (n:LoadObservation {source: $source}) RETURN count(n) AS count",
        "ImportBatch": "MATCH (n:ImportBatch {source: $source}) RETURN count(n) AS count",
    }
    relationship_queries = {
        "HAS_PATTERN": "MATCH (:Line {source: $source})-[r:HAS_PATTERN]->(:RoutePattern {source: $source}) RETURN count(r) AS count",
        "HAS_OCCURRENCE": "MATCH (:RoutePattern {source: $source})-[r:HAS_OCCURRENCE]->(:StopOccurrence {source: $source}) RETURN count(r) AS count",
        "AT_STOP": "MATCH (:StopOccurrence {source: $source})-[r:AT_STOP]->(:Stop {source: $source}) RETURN count(r) AS count",
        "NEXT": "MATCH (:StopOccurrence {source: $source})-[r:NEXT]->(:StopOccurrence {source: $source}) RETURN count(r) AS count",
        "OBSERVED_AT": "MATCH (:LoadObservation {source: $source})-[r:OBSERVED_AT]->(:StopOccurrence {source: $source}) RETURN count(r) AS count",
        "ON_LINE": "MATCH (:LoadObservation {source: $source})-[r:ON_LINE]->(:Line {source: $source}) RETURN count(r) AS count",
        "IMPORTED_IN": "MATCH (:LoadObservation {source: $source})-[r:IMPORTED_IN]->(:ImportBatch {source: $source}) RETURN count(r) AS count",
    }
    with driver.session() as session:
        counts = {
            name: _scalar(session, query, source=SOURCE)
            for name, query in {**node_queries, **relationship_queries}.items()
        }
        orphan_count = _scalar(
            session,
            """
            MATCH (n)
            WHERE n.source = $source AND (
              (n:RoutePattern AND NOT (:Line)-[:HAS_PATTERN]->(n)) OR
              (n:StopOccurrence AND (NOT (:RoutePattern)-[:HAS_OCCURRENCE]->(n) OR NOT (n)-[:AT_STOP]->(:Stop))) OR
              (n:LoadObservation AND (NOT (n)-[:OBSERVED_AT]->(:StopOccurrence) OR NOT (n)-[:ON_LINE]->(:Line) OR NOT (n)-[:IMPORTED_IN]->(:ImportBatch)))
            )
            RETURN count(n) AS count
            """,
            source=SOURCE,
        )
        sequence_error_count = _scalar(
            session,
            """
            MATCH (a:StopOccurrence {source: $source})-[r:NEXT]->(b:StopOccurrence {source: $source})
            WHERE a.pattern_id <> b.pattern_id OR b.seq <> a.seq + 1
            RETURN count(r) AS count
            """,
            source=SOURCE,
        )
        invalid_coordinate_count = _scalar(
            session,
            """
            MATCH (s:Stop)
            WITH properties(s) AS props
            WHERE (props['lat'] IS NOT NULL OR props['lon'] IS NOT NULL)
              AND (props['lat'] IS NULL OR props['lon'] IS NULL
                   OR props['lat'] < -90 OR props['lat'] > 90
                   OR props['lon'] < -180 OR props['lon'] > 180)
            RETURN count(*) AS count
            """,
        )
        incomplete_batch_count = _scalar(
            session,
            "MATCH (b:ImportBatch {source: $source}) WHERE b.status <> 'COMPLETE' RETURN count(b) AS count",
            source=SOURCE,
        )
        provenance_error_count = _scalar(
            session,
            """
            MATCH (observation:LoadObservation {source: $source})
            WHERE observation.source_file IS NULL
               OR observation.source_row IS NULL
               OR observation.source_row_id IS NULL
               OR observation.source_hour_column IS NULL
               OR observation.service_date IS NULL
               OR observation.hour IS NULL
               OR observation.raw_line IS NULL
               OR observation.raw_terminal_description IS NULL
               OR observation.raw_stop_seq IS NULL
               OR observation.raw_stop_name IS NULL
               OR observation.parse_status IS NULL
               OR observation.imported_at IS NULL
               OR NOT (observation.parse_status IN ['VALID', 'MISSING', 'INVALID'])
               OR (observation.parse_status = 'VALID'
                   AND (observation.raw_value IS NULL OR observation.onboard_count IS NULL))
               OR (observation.parse_status = 'INVALID' AND observation.raw_value IS NULL)
               OR (observation.parse_status <> 'VALID' AND observation.onboard_count IS NOT NULL)
            RETURN count(observation) AS count
            """,
            source=SOURCE,
        )
    if all(value == 0 for value in counts.values()):
        state = "empty"
    elif (
        counts == EXPECTED_V2_COUNTS
        and orphan_count == 0
        and sequence_error_count == 0
        and invalid_coordinate_count == 0
        and incomplete_batch_count == 0
        and provenance_error_count == 0
    ):
        state = "complete"
    else:
        state = "partial"
    return V2Snapshot(
        state=state,
        counts=counts,
        orphan_count=orphan_count,
        sequence_error_count=sequence_error_count,
        invalid_coordinate_count=invalid_coordinate_count,
        incomplete_batch_count=incomplete_batch_count,
        provenance_error_count=provenance_error_count,
    )


def verify_b1(driver: Any) -> dict[str, Any]:
    with driver.session() as session:
        pattern = session.run(
            """
            MATCH (:Line {name: 'B1', source: $source})-[:HAS_PATTERN]->(p:RoutePattern)
            MATCH (p)-[:HAS_OCCURRENCE]->(o:StopOccurrence)
            RETURN p.pattern_id AS pattern_id, count(o) AS occurrences,
                   min(o.seq) AS min_seq, max(o.seq) AS max_seq
            """,
            source=SOURCE,
        ).single(strict=True)
        route = session.run(
            """
            MATCH (origin:Stop {name: '대전역'})<-[:AT_STOP]-(start:StopOccurrence)
                  <-[:HAS_OCCURRENCE]-(pattern:RoutePattern)<-[:HAS_PATTERN]-(:Line {name: 'B1'})
            MATCH path=(start)-[:NEXT*1..]->(finish:StopOccurrence)
            MATCH (finish)-[:AT_STOP]->(:Stop {name: '세종시청.교육청.시의회'})
            WHERE (pattern)-[:HAS_OCCURRENCE]->(finish)
            RETURN start.seq AS origin_seq, finish.seq AS destination_seq,
                   length(path) AS hops,
                   [item IN nodes(path) | item.raw_stop_name] AS stops
            ORDER BY hops
            LIMIT 1
            """
        ).single(strict=True)
        repeated = session.run(
            """
            MATCH (:Line {name: 'B1'})-[:HAS_PATTERN]->(p:RoutePattern)
                  -[:HAS_OCCURRENCE]->(o:StopOccurrence)-[:AT_STOP]->
                  (:Stop {name: '오송역2.3.4'})
            RETURN count(o) AS count, collect(o.seq) AS sequences
            """
        ).single(strict=True)
        hourly = session.run(
            """
            MATCH (:Line {name: 'B1'})-[:HAS_PATTERN]->(:RoutePattern)
                  -[:HAS_OCCURRENCE]->(o:StopOccurrence {seq: 2})
                  <-[:OBSERVED_AT]-(observation:LoadObservation)
            WHERE observation.service_date = '2025-11-08' AND observation.hour IN [8, 9]
            RETURN observation.hour AS hour, observation.onboard_count AS onboard_count
            ORDER BY hour
            """
        ).data()
        missing = session.run(
            """
            MATCH (:Line {name: 'B1'})-[:HAS_PATTERN]->(:RoutePattern)
                  -[:HAS_OCCURRENCE]->(o:StopOccurrence {seq: 2})
                  <-[:OBSERVED_AT]-(observation:LoadObservation)
            WHERE observation.service_date = '2099-01-01' AND observation.hour = 9
            RETURN count(observation) AS count
            """
        ).single(strict=True)
    from prediction_v2 import V2TransitRepository

    repository = V2TransitRepository(driver)
    direct_routes = repository.find_direct_routes(
        "대전역", "세종시청.교육청.시의회", line_name="B1"
    )
    reverse_routes = repository.find_direct_routes(
        "세종시청.교육청.시의회", "대전역", line_name="B1"
    )
    selected = direct_routes[0]
    congestion_08 = repository.resolve_congestion(
        selected["pattern_id"], selected["origin_occurrence_id"], "2025-11-08", 8
    )
    unknown = repository.resolve_congestion(
        selected["pattern_id"], selected["origin_occurrence_id"], "2099-01-01", 9
    )
    checks = {
        "pattern_occurrences": pattern["occurrences"] == 53,
        "pattern_sequence": pattern["min_seq"] == 1 and pattern["max_seq"] == 53,
        "direct_next_route": route["origin_seq"] == 2 and route["destination_seq"] == 13,
        "repeated_occurrence_preserved": repeated["count"] == 2,
        "hourly_evidence_changes": len(hourly) == 2
        and hourly[0]["onboard_count"] != hourly[1]["onboard_count"],
        "missing_is_unknown": missing["count"] == 0,
        "repository_direct_route": bool(direct_routes)
        and direct_routes[0]["origin_seq"] == 2
        and direct_routes[0]["destination_seq"] == 13,
        "repository_reverse_route": bool(reverse_routes)
        and reverse_routes[0]["origin_seq"] == 42
        and reverse_routes[0]["destination_seq"] == 52,
        "historical_congestion_lookup": congestion_08["source"] == "HISTORICAL_OBSERVATION"
        and congestion_08["onboard_count"] == 17,
        "fallback_unknown": unknown["source"] == "UNKNOWN"
        and unknown["boarding_guidance"] == "데이터 부족",
    }
    return {
        "status": "ok" if all(checks.values()) else "failed",
        "checks": checks,
        "route": dict(route),
        "repeated_stop_sequences": repeated["sequences"],
        "hourly_evidence": hourly,
        "congestion_08": congestion_08,
        "unknown_fallback": unknown,
    }


def open_driver() -> Any:
    config.validate_required_config(("neo4j_v2",))
    driver = GraphDatabase.driver(
        config.NEO4J_V2_URI,
        auth=(config.NEO4J_USER, config.NEO4J_PASS),
        connection_timeout=10,
    )
    driver.verify_connectivity()
    return driver


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("profile", help="profile CSVs without connecting to Neo4j")
    import_parser = subparsers.add_parser("import", help="idempotently import v2 data")
    import_parser.add_argument("--batch-size", type=int, default=2_000)
    subparsers.add_parser("status", help="inspect the v2 graph")
    subparsers.add_parser("verify", help="verify graph integrity and the B1 route")
    args = parser.parse_args()

    if args.command == "profile":
        emit(source_profile(load_sources(config.INPUT_FILES)))
        return 0

    driver = open_driver()
    try:
        if args.command == "import":
            profile = process_files(driver, config.INPUT_FILES, args.batch_size)
            snapshot = inspect_database(driver)
            emit({"profile": profile, "snapshot": asdict(snapshot)})
            return 0 if snapshot.state == "complete" else 1
        snapshot = inspect_database(driver)
        if args.command == "status":
            emit(asdict(snapshot))
            return 0 if snapshot.state == "complete" else 2
        b1 = verify_b1(driver) if snapshot.state == "complete" else None
        emit({"snapshot": asdict(snapshot), "b1": b1})
        return 0 if snapshot.state == "complete" and b1 and b1["status"] == "ok" else 1
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
