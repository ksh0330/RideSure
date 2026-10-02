"""Apply reviewed multi-route TAGO bindings without changing historical topology."""

from __future__ import annotations

import json
import hashlib
from collections import Counter
from pathlib import Path

import config
from neo4j import GraphDatabase
from official_stop_mapping import (
    MappingPlan, apply_mapping_plan, build_mapping_plan, read_historical_pattern, read_official_route,
)
from public_data import import_records, load_tago_fixture


ROOT = Path("data/public/tago/multi_route_2026-10-02")


def validate_alignment_structure(plan: MappingPlan) -> None:
    """Reject one-way history that would consume both official directions."""
    directions = {decision.official.direction_code for decision in plan.verified
                  if decision.official is not None and decision.official.direction_code is not None}
    if len(directions) > 1 and plan.decisions[0].historical.name != plan.decisions[-1].historical.name:
        raise ValueError("One-way historical pattern crosses official outbound/return directions.")


def main() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    bindings = json.loads((ROOT / "selected_bindings.json").read_text(encoding="utf-8"))
    driver = GraphDatabase.driver(config.NEO4J_V2_URI,
                                  auth=(config.NEO4J_USER, config.NEO4J_PASS))
    report = []
    try:
        with driver.session() as session:
            b1_before = session.run("""
                MATCH (o:StopOccurrence)-[:VERIFIED_OFFICIAL_STOP]->(:Stop)
                MATCH (:Line {name:'B1'})-[:HAS_PATTERN]->(:RoutePattern)-[:HAS_OCCURRENCE]->(o)
                RETURN count(*) AS n
            """).single()["n"]
            for line, binding in bindings.items():
                candidates = [candidate for candidate in manifest["candidates"][line]
                              if candidate["route_id"] == binding["route_id"]
                              and candidate["city_code"] == binding["city_code"]]
                if len(candidates) != 1:
                    raise ValueError(f"Selected {line} binding has no unique TAGO discovery evidence.")
                candidate = candidates[0]
                stop_path = ROOT / candidate["stops_file"]
                manifest_files = [entry for entry in manifest["files"]
                                  if entry["file"] == candidate["stops_file"]]
                if len(manifest_files) != 1 or hashlib.sha256(stop_path.read_bytes()).hexdigest() != manifest_files[0]["sha256"]:
                    raise ValueError(f"Selected {line} TAGO snapshot checksum changed.")
                records = load_tago_fixture(stop_path)
                if len(records) != candidate["stop_count"]:
                    raise ValueError(f"Selected {line} route-stop response changed.")
                patterns = session.run("""
                    MATCH (:Line {name:$line})-[:HAS_PATTERN]->(p:RoutePattern)
                    RETURN p.pattern_id AS pattern_id
                """, line=line).data()
                if len(patterns) != 1:
                    raise ValueError(f"Selected {line} historical pattern is not unique.")
                pattern_id = patterns[0]["pattern_id"]
                import_records(records, 2000)
                staged = read_official_route(session, binding["route_id"])
                if len(staged) != len(records):
                    raise ValueError(f"Selected {line} staging count is incomplete.")
                history = read_historical_pattern(session, pattern_id, line)
                plan = build_mapping_plan(
                    history, staged, line_name=line, official_route_id=binding["route_id"],
                    route_binding_source=(
                        f"TAGO getRouteNoList+getRouteInfoIem+getRouteAcctoThrghSttnList "
                        f"cityCode={binding['city_code']} routeNo={line}; snapshot {manifest['retrieved_on']}"
                    ),
                )
                validate_alignment_structure(plan)
                counts = Counter(decision.status for decision in plan.decisions)
                if len(plan.verified) != binding["expected_verified"]:
                    raise ValueError(f"Selected {line} mapping no longer matches reviewed preview.")
                before = session.run("""
                    MATCH (p:RoutePattern {pattern_id:$pattern_id})-[:HAS_OCCURRENCE]->
                          (o:StopOccurrence)-[:VERIFIED_OFFICIAL_STOP]->(:Stop)
                    RETURN count(*) AS n
                """, pattern_id=pattern_id).single()["n"]
                first = session.execute_write(apply_mapping_plan, plan)
                middle = session.run("""
                    MATCH (p:RoutePattern {pattern_id:$pattern_id})-[:HAS_OCCURRENCE]->
                          (o:StopOccurrence)-[:VERIFIED_OFFICIAL_STOP]->(:Stop)
                    RETURN count(*) AS n
                """, pattern_id=pattern_id).single()["n"]
                second = session.execute_write(apply_mapping_plan, plan)
                after = session.run("""
                    MATCH (p:RoutePattern {pattern_id:$pattern_id})-[:HAS_OCCURRENCE]->
                          (o:StopOccurrence)-[:VERIFIED_OFFICIAL_STOP]->(:Stop)
                    RETURN count(*) AS n
                """, pattern_id=pattern_id).single()["n"]
                if first != len(plan.verified) or second != first or middle != after:
                    raise AssertionError(f"Selected {line} mapping is incomplete or not idempotent.")
                report.append({"line": line, "route_id": binding["route_id"],
                               "city_code": binding["city_code"], "imported_records": len(records),
                               "staged_records": len(staged), "historical": len(history),
                               "counts": dict(counts), "edges_before": before,
                               "edges_after_first": middle, "edges_after_second": after})
            b1_after = session.run("""
                MATCH (o:StopOccurrence)-[:VERIFIED_OFFICIAL_STOP]->(:Stop)
                MATCH (:Line {name:'B1'})-[:HAS_PATTERN]->(:RoutePattern)-[:HAS_OCCURRENCE]->(o)
                RETURN count(*) AS n
            """).single()["n"]
            if b1_before != b1_after:
                raise AssertionError("B1 mapping count changed.")
    finally:
        driver.close()
    print(json.dumps({"routes": report, "b1_before": b1_before, "b1_after": b1_after},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
