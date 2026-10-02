"""Conservative, occurrence-level mapping to imported TAGO stop topology.

An explicit historical line / official route binding is required. Names alone
never create a verified edge. Preview and apply use the same deterministic plan.
Neither operation changes historical Stop, StopOccurrence, or NEXT topology.
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Iterable

import config
from data_insert_v2 import SCHEMA_VERSION, SOURCE
from public_data import MAPPING_STATUSES, TAGO_SOURCE, load_tago_fixture


@dataclass(frozen=True)
class HistoricalOccurrence:
    occurrence_id: str
    pattern_id: str
    seq: int
    name: str


@dataclass(frozen=True)
class OfficialRouteOccurrence:
    staging_id: str
    official_route_id: str
    official_node_id: str
    stop_id: str
    node_order: int
    name: str
    direction_code: str | None
    source: str
    coordinate_source: str | None
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class MappingDecision:
    historical: HistoricalOccurrence
    status: str
    method: str
    official: OfficialRouteOccurrence | None = None

    def __post_init__(self) -> None:
        if self.status not in MAPPING_STATUSES:
            raise ValueError(f"Unknown mapping status: {self.status}")
        if (self.status in {"EXACT", "SEQUENCE_MATCH"}) != (self.official is not None):
            raise ValueError("Verified status must have exactly one official occurrence.")


@dataclass(frozen=True)
class MappingPlan:
    pattern_id: str
    line_name: str
    official_route_id: str
    route_binding_source: str
    direction_code: str | None
    decisions: tuple[MappingDecision, ...]

    @property
    def verified(self) -> tuple[MappingDecision, ...]:
        return tuple(d for d in self.decisions if d.official is not None)


def _name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def build_mapping_plan(
    historical: Iterable[HistoricalOccurrence],
    official: Iterable[OfficialRouteOccurrence],
    *,
    line_name: str,
    official_route_id: str,
    route_binding_source: str,
    direction_code: str | None = None,
) -> MappingPlan:
    """Map only a bound route's unique full sequence or unique neighbor context.

    The binding source records how a human checked that the opaque TAGO route ID
    belongs to the historical line. If multiple official directions exist, the
    direction must also be selected explicitly. Changed segments remain unmapped.
    """
    if not line_name.strip() or not official_route_id.strip() or not route_binding_source.strip():
        raise ValueError("line_name, official_route_id, and route_binding_source are required.")
    history = sorted(historical, key=lambda row: row.seq)
    if not history or len({row.pattern_id for row in history}) != 1:
        raise ValueError("A single nonempty historical pattern is required.")
    if len({row.occurrence_id for row in history}) != len(history):
        raise ValueError("Historical occurrence IDs must be unique.")
    if [row.seq for row in history] != list(range(history[0].seq, history[-1].seq + 1)):
        raise ValueError("Historical sequence must be contiguous.")
    routes = list(official)
    if any(row.official_route_id != official_route_id or row.source != TAGO_SOURCE for row in routes):
        raise ValueError("Official topology must come from the bound TAGO route.")
    if len({row.staging_id for row in routes}) != len(routes):
        raise ValueError("Official staging IDs must be unique.")
    if direction_code is not None:
        routes = [row for row in routes if row.direction_code == direction_code]
        if not routes:
            raise ValueError("Selected direction has no official route-stop topology.")
    # TAGO can number a round trip continuously while changing updowncd at
    # the turn-around stop. In that case the historical pattern is compared
    # with the full globally ordered sequence, not two artificial halves.
    global_order_is_unique = len({row.node_order for row in routes}) == len(routes)
    groups: dict[str | None, list[OfficialRouteOccurrence]] = {}
    if direction_code is None and routes and global_order_is_unique:
        groups[None] = routes
    else:
        for row in routes:
            groups.setdefault(row.direction_code, []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda row: row.node_order)
        if len({row.node_order for row in rows}) != len(rows):
            raise ValueError("Official route has duplicate node order in one direction.")
        if [row.node_order for row in rows] != list(range(rows[0].node_order, rows[-1].node_order + 1)):
            raise ValueError("Official route has a gap in node order.")
    pattern_id = history[0].pattern_id

    def plan(status: str, method: str, chosen: list[OfficialRouteOccurrence | None]) -> MappingPlan:
        return MappingPlan(
            pattern_id, line_name, official_route_id, route_binding_source,
            direction_code,
            tuple(
                MappingDecision(row, status, method, candidate)
                for row, candidate in zip(history, chosen)
            ),
        )

    if not groups:
        return plan("UNMATCHED", "NO_OFFICIAL_TOPOLOGY", [None] * len(history))
    if len(groups) > 1:
        official_names_all = {_name(row.name) for row in routes}
        return MappingPlan(
            pattern_id, line_name, official_route_id, route_binding_source,
            direction_code,
            tuple(
                MappingDecision(
                    row,
                    "AMBIGUOUS" if _name(row.name) in official_names_all else "UNMATCHED",
                    "DIRECTION_NOT_SELECTED" if _name(row.name) in official_names_all else "NO_NAME_ON_OFFICIAL_ROUTE",
                )
                for row in history
            ),
        )
    route = next(iter(groups.values()))
    historical_names = [_name(row.name) for row in history]
    official_names = [_name(row.name) for row in route]
    if len(history) >= 3 and historical_names == official_names:
        return plan("EXACT", "BOUND_FULL_ORDERED_SEQUENCE", route)

    candidates: list[list[tuple[int, OfficialRouteOccurrence]]] = []
    for index, name in enumerate(historical_names):
        matches: list[tuple[int, OfficialRouteOccurrence]] = []
        for off_index, off_name in enumerate(official_names):
            if name != off_name:
                continue
            left = (
                index > 0 and off_index > 0
                and historical_names[index - 1] == official_names[off_index - 1]
            )
            right = (
                index + 1 < len(history) and off_index + 1 < len(route)
                and historical_names[index + 1] == official_names[off_index + 1]
            )
            # Interior stops need both neighbors; endpoints need their one neighbor.
            if len(history) >= 3 and (
                (index == 0 and right)
                or (index == len(history) - 1 and left)
                or (0 < index < len(history) - 1 and left and right)
            ):
                matches.append((off_index, route[off_index]))
        candidates.append(matches)

    decisions: list[MappingDecision] = []
    for index, row in enumerate(history):
        matches = candidates[index]
        if len(matches) == 1:
            decisions.append(MappingDecision(row, "SEQUENCE_MATCH", "UNIQUE_ADJACENT_SEQUENCE", matches[0][1]))
        else:
            same_name_count = official_names.count(historical_names[index])
            status = "AMBIGUOUS" if len(matches) > 1 or same_name_count > 1 else "UNMATCHED"
            method = "MULTIPLE_OR_UNPROVEN_CANDIDATES" if status == "AMBIGUOUS" else "NO_ADJACENT_EVIDENCE"
            decisions.append(MappingDecision(row, status, method))

    # A route occurrence cannot represent two historical occurrences; all
    # verified matches must also preserve the official order.
    positions = [
        (i, candidates[i][0][0]) for i, decision in enumerate(decisions)
        if decision.official is not None
    ]
    if any(right[1] <= left[1] for left, right in zip(positions, positions[1:])):
        decisions = [
            MappingDecision(decision.historical, "AMBIGUOUS", "NON_MONOTONIC_OR_REUSED_ORDER")
            if decision.official is not None else decision
            for decision in decisions
        ]
    return MappingPlan(
        pattern_id, line_name, official_route_id, route_binding_source,
        direction_code, tuple(decisions),
    )


def read_historical_pattern(session: Any, pattern_id: str, line_name: str) -> list[HistoricalOccurrence]:
    rows = session.run(
        """
        MATCH (line:Line {name: $line_name, source: $historical_source})
              -[:HAS_PATTERN]->(pattern:RoutePattern {pattern_id: $pattern_id, source: $historical_source})
              -[:HAS_OCCURRENCE]->(occurrence:StopOccurrence {source: $historical_source})
              -[:AT_STOP]->(:Stop {source: $historical_source})
        RETURN occurrence.occurrence_id AS occurrence_id,
               occurrence.pattern_id AS pattern_id,
               occurrence.seq AS seq,
               occurrence.raw_stop_name AS name
        ORDER BY seq
        """,
        line_name=line_name,
        pattern_id=pattern_id,
        historical_source=SOURCE,
    )
    return [HistoricalOccurrence(**dict(row)) for row in rows]


def read_official_route(session: Any, official_route_id: str) -> list[OfficialRouteOccurrence]:
    rows = session.run(
        """
        MATCH (staging:RouteStopStaging {official_route_id: $route_id, source: $tago_source})
              -[:STAGES_STOP]->(stop:Stop)
        WHERE stop.official_node_id = staging.official_node_id
          AND stop.id_kind = 'OFFICIAL_NODE_ID'
          AND staging.stop_name IS NOT NULL
        RETURN staging.staging_id AS staging_id,
               staging.official_route_id AS official_route_id,
               staging.official_node_id AS official_node_id,
               stop.stop_id AS stop_id,
               staging.node_order AS node_order,
               staging.stop_name AS name,
               staging.direction_code AS direction_code,
               staging.source AS source,
               stop.coordinate_source AS coordinate_source,
               stop.lat AS lat, stop.lon AS lon
        ORDER BY direction_code, node_order
        """,
        route_id=official_route_id,
        tago_source=TAGO_SOURCE,
    )
    return [OfficialRouteOccurrence(**dict(row)) for row in rows]


def read_official_route_json(path: str) -> list[OfficialRouteOccurrence]:
    """Read a saved raw TAGO response for preview without touching Neo4j."""
    return [
        OfficialRouteOccurrence(
            record.staging_id, record.official_route_id, record.official_node_id,
            record.stop_id, record.node_order, record.name, record.direction_code,
            record.source, record.source, record.lat, record.lon,
        )
        for record in load_tago_fixture(path)
    ]


def apply_mapping_plan(tx: Any, mapping: MappingPlan) -> int:
    """Add verified edges only; reject conflicting or stale graph state atomically."""
    rows = []
    for decision in mapping.verified:
        official = decision.official
        assert official is not None
        rows.append({
            "occurrence_id": decision.historical.occurrence_id,
            "pattern_id": mapping.pattern_id,
            "seq": decision.historical.seq,
            "raw_stop_name": decision.historical.name,
            "staging_id": official.staging_id,
            "official_stop_id": official.stop_id,
            "official_node_id": official.official_node_id,
            "official_route_id": mapping.official_route_id,
            "node_order": official.node_order,
            "stop_name": official.name,
            "status": decision.status,
            "method": decision.method,
            "coordinate_source": official.coordinate_source,
            "direction_code": official.direction_code,
        })
    if not rows:
        return 0
    existing = list(tx.run(
        """
        UNWIND $rows AS r
        MATCH (occurrence:StopOccurrence {occurrence_id: r.occurrence_id})
              -[mapping:VERIFIED_OFFICIAL_STOP]->(other:Stop)
        WHERE other.stop_id <> r.official_stop_id
           OR coalesce(mapping.staging_id, '') <> r.staging_id
           OR coalesce(mapping.official_route_id, '') <> r.official_route_id
        RETURN occurrence.occurrence_id AS occurrence_id
        """,
        rows=rows,
    ))
    if existing:
        raise ValueError("A historical occurrence already maps to a different official stop or route occurrence.")
    # Validate every endpoint and its provenance before any mutation. A plan
    # made against stale TAGO data must not silently create a different edge.
    validated = list(tx.run(
        """
        UNWIND $rows AS r
        MATCH (pattern:RoutePattern {pattern_id: r.pattern_id, source: $historical_source})
              -[:HAS_OCCURRENCE]->(occurrence:StopOccurrence {
                occurrence_id: r.occurrence_id, source: $historical_source,
                seq: r.seq, raw_stop_name: r.raw_stop_name})
        MATCH (staging:RouteStopStaging {
                staging_id: r.staging_id, source: $tago_source,
                official_route_id: r.official_route_id,
                official_node_id: r.official_node_id,
                node_order: r.node_order, stop_name: r.stop_name})
              -[:STAGES_STOP]->(stop:Stop {
                stop_id: r.official_stop_id, official_node_id: r.official_node_id,
                id_kind: 'OFFICIAL_NODE_ID'})
        WHERE coalesce(staging.direction_code, '') = coalesce(r.direction_code, '')
        RETURN occurrence.occurrence_id AS occurrence_id
        """,
        rows=rows, historical_source=SOURCE, tago_source=TAGO_SOURCE,
    ))
    if len(validated) != len(rows) or len({row["occurrence_id"] for row in validated}) != len(rows):
        raise ValueError("Historical or official topology changed since the mapping plan was built.")
    written = list(tx.run(
        """
        UNWIND $rows AS r
        MATCH (occurrence:StopOccurrence {occurrence_id: r.occurrence_id})
        MATCH (stop:Stop {stop_id: r.official_stop_id})
        MERGE (occurrence)-[mapping:VERIFIED_OFFICIAL_STOP]->(stop)
        ON CREATE SET mapping.created_at = datetime()
        SET mapping.mapping_status = r.status,
            mapping.mapping_method = r.method,
            mapping.official_source = $tago_source,
            mapping.official_route_id = r.official_route_id,
            mapping.official_node_id = r.official_node_id,
            mapping.staging_id = r.staging_id,
            mapping.direction_code = r.direction_code,
            mapping.node_order = r.node_order,
            mapping.coordinate_source = r.coordinate_source,
            mapping.route_binding_source = $route_binding_source,
            mapping.schema_version = $schema_version
        RETURN occurrence.occurrence_id AS occurrence_id
        """,
        rows=rows, tago_source=TAGO_SOURCE,
        route_binding_source=mapping.route_binding_source,
        schema_version=SCHEMA_VERSION,
    ))
    if len(written) != len(rows):
        raise ValueError("Verified mapping write was incomplete.")
    return len(written)


def verified_coordinates(session: Any, occurrence_id: str) -> dict[str, Any] | None:
    """Coordinates are exposed only through a verified occurrence relationship."""
    row = session.run(
        """
        MATCH (:StopOccurrence {occurrence_id: $occurrence_id})
              -[mapping:VERIFIED_OFFICIAL_STOP]->(official:Stop)
        WHERE mapping.mapping_status IN ['EXACT', 'SEQUENCE_MATCH']
          AND official.id_kind = 'OFFICIAL_NODE_ID'
          AND mapping.official_node_id = official.official_node_id
          AND official.coordinate_source IS NOT NULL
          AND official.lat IS NOT NULL AND official.lon IS NOT NULL
          AND official.lat >= -90 AND official.lat <= 90
          AND official.lon >= -180 AND official.lon <= 180
        RETURN official.official_node_id AS official_node_id,
               official.lat AS lat, official.lon AS lon,
               official.coordinate_source AS coordinate_source,
               mapping.mapping_status AS mapping_status
        """,
        occurrence_id=occurrence_id,
    ).single()
    return dict(row) if row is not None else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument("--pattern-id", required=True)
    parser.add_argument("--line-name", required=True)
    parser.add_argument("--official-route-id", required=True)
    parser.add_argument("--route-binding-source", required=True)
    parser.add_argument("--direction-code")
    parser.add_argument("--tago-json", help="raw TAGO route-stop response for read-only preview")
    args = parser.parse_args(argv)
    if args.tago_json and args.command != "plan":
        parser.error("--tago-json is available only for plan; apply requires imported official stops.")
    from neo4j import GraphDatabase

    config.validate_required_config(("neo4j_v2",))
    driver = GraphDatabase.driver(
        config.NEO4J_V2_URI, auth=(config.NEO4J_USER, config.NEO4J_PASS),
        connection_timeout=10,
    )
    try:
        driver.verify_connectivity()
        with driver.session() as session:
            plan = build_mapping_plan(
                read_historical_pattern(session, args.pattern_id, args.line_name),
                read_official_route_json(args.tago_json) if args.tago_json else read_official_route(session, args.official_route_id),
                line_name=args.line_name,
                official_route_id=args.official_route_id,
                route_binding_source=args.route_binding_source,
                direction_code=args.direction_code,
            )
            applied = session.execute_write(apply_mapping_plan, plan) if args.command == "apply" else 0
        output = {
            "line_name": plan.line_name,
            "pattern_id": plan.pattern_id,
            "official_route_id": plan.official_route_id,
            "direction_code": plan.direction_code,
            "route_binding_source": plan.route_binding_source,
            "counts": {status: sum(d.status == status for d in plan.decisions) for status in sorted(MAPPING_STATUSES)},
            "applied": applied,
            "decisions": [asdict(decision) for decision in plan.decisions],
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
