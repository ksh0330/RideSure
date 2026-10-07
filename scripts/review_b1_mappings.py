"""Validate and apply explicitly reviewed B1 occurrence mappings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from data_insert_v2 import open_driver
from official_stop_mapping import (
    MappingDecision, MappingPlan, apply_mapping_plan, build_mapping_plan,
    read_historical_pattern, read_official_route,
)

MANIFEST = (Path(__file__).resolve().parents[1] /
            "data/public/tago/b1_2026-10-02/b1_reviewed_mappings.json")


def build_reviewed_plan(automatic: MappingPlan, official: list, manifest_path: Path = MANIFEST) -> MappingPlan:
    """Require every reviewed pair and its named neighboring context to match."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if automatic.line_name != "B1" or automatic.official_route_id != manifest["route_id"]:
        raise ValueError("Reviewed B1 route binding differs from the automatic plan.")
    history = [decision.historical for decision in automatic.decisions]
    by_order = {row.node_order: row for row in official}
    by_seq = {row.seq: row for row in history}
    by_id = {decision.historical.occurrence_id: decision for decision in automatic.decisions}
    reviewed = []
    for entry in manifest["mappings"]:
        decision = by_id.get(entry["historical_occurrence_id"])
        if decision is None or decision.status != "UNMATCHED":
            raise ValueError("Reviewed occurrence is absent or no longer unmatched.")
        historical = decision.historical
        current = by_order.get(entry["official_node_order"])
        if (historical.seq != entry["historical_seq"] or historical.name != entry["historical_name"]
                or current is None or current.official_route_id != manifest["route_id"]
                or current.official_node_id != entry["official_node_id"]
                or current.name != entry["official_name"]
                or current.lat is None or current.lon is None):
            raise ValueError("Reviewed B1 occurrence or official stop changed.")
        neighbors = (
            by_seq.get(historical.seq - 1), by_seq.get(historical.seq + 1),
            by_order.get(current.node_order - 1), by_order.get(current.node_order + 1),
        )
        if any(row is None for row in neighbors) or tuple(row.name for row in neighbors) != (
            entry["previous_historical"], entry["next_historical"],
            entry["previous_official"], entry["next_official"],
        ) or not entry["review_reason"].strip():
            raise ValueError("Reviewed B1 surrounding sequence evidence changed.")
        reviewed.append(MappingDecision(historical, "SEQUENCE_MATCH", "HUMAN_REVIEWED_SEQUENCE", current, {
            "review_reason": entry["review_reason"],
            "historical_previous_name": entry["previous_historical"],
            "historical_next_name": entry["next_historical"],
            "official_previous_name": entry["previous_official"],
            "official_next_name": entry["next_official"],
            "name_match_kind": "HUMAN_REVIEWED_WORD_ORDER",
        }))
    if len({row.historical.occurrence_id for row in reviewed}) != len(reviewed) or len({
        row.official.staging_id for row in reviewed
    }) != len(reviewed):
        raise ValueError("Reviewed B1 manifest repeats an occurrence or official position.")
    combined = sorted((*automatic.verified, *reviewed), key=lambda row: row.historical.seq)
    positions = [row.official.node_order for row in combined]
    if positions != sorted(set(positions)):
        raise ValueError("Reviewed B1 mapping would cross or reuse an official position.")
    return MappingPlan(automatic.pattern_id, "B1", automatic.official_route_id,
                       manifest["review_source"], None, tuple(reviewed))


def reviewed_plan_from_graph(session) -> MappingPlan:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    patterns = session.run("""
        MATCH (:Line {name:'B1'})-[:HAS_PATTERN]->(p:RoutePattern)
        RETURN p.pattern_id AS pattern_id
        """).data()
    if len(patterns) != 1:
        raise ValueError("Historical B1 pattern is not unique.")
    history = read_historical_pattern(session, patterns[0]["pattern_id"], "B1")
    official = read_official_route(session, manifest["route_id"])
    automatic = build_mapping_plan(history, official, line_name="B1",
                                   official_route_id=manifest["route_id"],
                                   route_binding_source=manifest["review_source"])
    if len(automatic.verified) != 41 or len(automatic.decisions) != 53:
        raise ValueError("B1 automatic baseline changed; reviewed mapping needs re-review.")
    return build_reviewed_plan(automatic, official)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "apply"))
    args = parser.parse_args()
    driver = open_driver()
    try:
        with driver.session() as session:
            plan = reviewed_plan_from_graph(session)
            applied = session.execute_write(apply_mapping_plan, plan) if args.command == "apply" else 0
            print(json.dumps({"reviewed": len(plan.verified), "applied": applied,
                              "sequences": [row.historical.seq for row in plan.verified]},
                             ensure_ascii=False))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
