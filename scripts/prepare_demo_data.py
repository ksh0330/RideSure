"""Prepare the portfolio Neo4j v2 graph from repository-shipped snapshots only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import config
from data_insert_v2 import inspect_database, open_driver, process_files, verify_b1
from official_stop_mapping import (
    apply_mapping_plan, build_mapping_plan, read_historical_pattern, read_official_route,
)
from phase2d_apply import validate_alignment_structure
from public_data import import_records, load_tago_fixture
from scripts.review_b1_mappings import build_reviewed_plan

ROOT = Path(__file__).resolve().parents[1]
B1_DIR = ROOT / "data/public/tago/b1_2026-10-02"
MULTI_DIR = ROOT / "data/public/tago/multi_route_2026-10-02"
EXPECTED_STAGING = 210
AUTOMATIC_MAPPINGS = 160
EXPECTED_MAPPINGS = 162


def _scalar(session, query: str) -> int:
    return int(session.run(query).single(strict=True)["n"])


def _reference_counts(session) -> tuple[int, int]:
    return (
        _scalar(session, "MATCH (s:RouteStopStaging) RETURN count(s) AS n"),
        _scalar(session, "MATCH (:StopOccurrence)-[r:VERIFIED_OFFICIAL_STOP]->(:Stop) RETURN count(r) AS n"),
    )


def _sources():
    """Validate local route bindings and SHA-256 before writing anything."""
    manifest = json.loads((MULTI_DIR / "manifest.json").read_text(encoding="utf-8"))
    bindings = json.loads((MULTI_DIR / "selected_bindings.json").read_text(encoding="utf-8"))
    b1_info = json.loads((B1_DIR / "b1_route_basic_info.json").read_text(encoding="utf-8"))
    b1_route = b1_info["response"]["body"]["items"]["item"]
    if b1_route["routeno"] != "B1":
        raise ValueError("Saved B1 basic information does not identify B1.")
    b1_records = load_tago_fixture(B1_DIR / "b1_route_stops.json")
    if len(b1_records) != 55 or {row.official_route_id for row in b1_records} != {b1_route["routeid"]}:
        raise ValueError("Saved B1 stop sequence disagrees with basic information.")
    sources = [("B1", b1_route["routeid"], 41, b1_records,
                "TAGO B1 route-number and basic-info snapshot 2026-10-02")]
    for line, binding in bindings.items():
        matches = [item for item in manifest["candidates"][line]
                   if item["route_id"] == binding["route_id"]
                   and item["city_code"] == binding["city_code"]]
        if len(matches) != 1:
            raise ValueError(f"No unique saved TAGO discovery evidence for {line}.")
        candidate = matches[0]
        path = MULTI_DIR / candidate["stops_file"]
        checksums = [item["sha256"] for item in manifest["files"]
                     if item["file"] == candidate["stops_file"]]
        if len(checksums) != 1 or hashlib.sha256(path.read_bytes()).hexdigest() != checksums[0]:
            raise ValueError(f"Saved TAGO stop snapshot checksum failed for {line}.")
        records = load_tago_fixture(path)
        if len(records) != candidate["stop_count"]:
            raise ValueError(f"Saved TAGO stop count changed for {line}.")
        sources.append((line, binding["route_id"], binding["expected_verified"], records,
                        f"TAGO reviewed binding cityCode={binding['city_code']} routeNo={line}; "
                        f"snapshot {manifest['retrieved_on']}"))
    if sum(len(item[3]) for item in sources) != EXPECTED_STAGING:
        raise ValueError("Saved TAGO snapshots no longer contain 210 route-stop records.")
    if sum(item[2] for item in sources) != AUTOMATIC_MAPPINGS:
        raise ValueError("Automatic verified mapping totals no longer equal 160.")
    return sources


def prepare(driver) -> dict:
    sources = _sources()
    snapshot = inspect_database(driver)
    if snapshot.state == "partial":
        raise RuntimeError("Historical v2 graph is partial/inconsistent; inspect it before retrying.")
    with driver.session() as session:
        staging, mappings = _reference_counts(session)
    if staging not in (0, EXPECTED_STAGING) or mappings not in (0, AUTOMATIC_MAPPINGS, EXPECTED_MAPPINGS):
        raise RuntimeError(f"Official reference graph is incomplete (staging={staging}, mappings={mappings}); inspect before retrying.")
    if mappings and not staging:
        raise RuntimeError("Verified mapping edges exist without route staging; inspect before retrying.")
    if snapshot.state == "empty":
        process_files(driver, config.INPUT_FILES)
        snapshot = inspect_database(driver)
    if snapshot.state != "complete":
        raise RuntimeError("Historical import did not pass v2 integrity checks.")
    if verify_b1(driver)["status"] != "ok":
        raise RuntimeError("Historical B1 checks failed; official mapping was not started.")

    for line, route_id, expected, records, evidence in sources:
        import_records(records, 2_000)
        with driver.session() as session:
            patterns = session.run("""
                MATCH (:Line {name:$line})-[:HAS_PATTERN]->(p:RoutePattern)
                RETURN p.pattern_id AS pattern_id
                """, line=line).data()
            if len(patterns) != 1:
                raise RuntimeError(f"Historical pattern for {line} is not unique.")
            history = read_historical_pattern(session, patterns[0]["pattern_id"], line)
            official = read_official_route(session, route_id)
            if len(official) != len(records):
                raise RuntimeError(f"Official staging for {line} is incomplete.")
            plan = build_mapping_plan(history, official, line_name=line,
                                      official_route_id=route_id, route_binding_source=evidence)
            if line != "B1":
                validate_alignment_structure(plan)
            if len(plan.verified) != expected:
                raise RuntimeError(f"Reviewed mapping count changed for {line}.")
            session.execute_write(apply_mapping_plan, plan)
            if line == "B1":
                reviewed = build_reviewed_plan(plan, official)
                if len(reviewed.verified) != EXPECTED_MAPPINGS - AUTOMATIC_MAPPINGS:
                    raise RuntimeError("Reviewed B1 manifest count changed.")
                session.execute_write(apply_mapping_plan, reviewed)

    snapshot = inspect_database(driver)
    with driver.session() as session:
        staging, mappings = _reference_counts(session)
        distinct_mappings = _scalar(session, """
            MATCH (o:StopOccurrence)-[:VERIFIED_OFFICIAL_STOP]->(:Stop)
            RETURN count(DISTINCT o) AS n
            """)
    if (snapshot.state, staging, mappings, distinct_mappings) != (
        "complete", EXPECTED_STAGING, EXPECTED_MAPPINGS, EXPECTED_MAPPINGS
    ) or verify_b1(driver)["status"] != "ok":
        raise RuntimeError("Final historical or official-reference integrity check failed.")
    return {"historical": {key: snapshot.counts[key] for key in
                           ("Line", "RoutePattern", "Stop", "StopOccurrence", "LoadObservation")},
            "route_stop_staging": staging, "verified_official_stop": mappings}


def main() -> None:
    driver = open_driver()
    try:
        print(json.dumps(prepare(driver), ensure_ascii=False, sort_keys=True))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
