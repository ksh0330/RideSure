"""Phase 2D local audit of official snapshots against historical Neo4j patterns."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import config
from neo4j import GraphDatabase
from official_stop_mapping import build_mapping_plan, read_historical_pattern, read_official_route_json


LINES = ("1000", "1000-1", "1001", "1003", "1004")
SNAPSHOT = Path("data/public/tago/multi_route_2026-10-02")


def main() -> None:
    manifest = json.loads((SNAPSHOT / "manifest.json").read_text(encoding="utf-8"))
    driver = GraphDatabase.driver(config.NEO4J_V2_URI,
                                  auth=(config.NEO4J_USER, config.NEO4J_PASS))
    with driver.session() as session:
        rows = session.run("""
            MATCH (l:Line)-[:HAS_PATTERN]->(p:RoutePattern)-[:HAS_OCCURRENCE]->(o:StopOccurrence)
            WHERE l.name IN $lines
            WITH l,p,o ORDER BY o.seq
            WITH l,p,collect(o.raw_stop_name) AS names
            RETURN l.name AS line,p.pattern_id AS pattern,size(names) AS n,
                   names[0] AS first,names[-1] AS last
            ORDER BY line,pattern
        """, lines=list(LINES)).data()
        result = {"historical_patterns": rows, "previews": []}
        for row in rows:
            for candidate in manifest["candidates"][row["line"]]:
                path = SNAPSHOT / candidate["stops_file"]
                official = read_official_route_json(str(path))
                history = read_historical_pattern(session, row["pattern"], row["line"])
                plan = build_mapping_plan(
                    history, official, line_name=row["line"],
                    official_route_id=candidate["route_id"],
                    route_binding_source=(f"TAGO cityCode={candidate['city_code']} routeNo={row['line']} "
                                          f"getRouteInfoIem+getRouteAcctoThrghSttnList 2026-10-02"),
                )
                counts = Counter(d.status for d in plan.decisions)
                used = {d.official.node_order for d in plan.verified if d.official}
                official_orders = {o.node_order for o in official}
                preview = {
                    "line": row["line"], "pattern_id": row["pattern"],
                    "historical_count": len(history), "official_route_id": candidate["route_id"],
                    "city_code": candidate["city_code"], "official_count": len(official),
                    "counts": {s: counts[s] for s in ("EXACT", "SEQUENCE_MATCH", "AMBIGUOUS", "UNMATCHED")},
                    "verified_orders": sorted(used), "unused_official_orders": sorted(official_orders - used),
                    "unmatched_historical": [{"seq": d.historical.seq, "name": d.historical.name}
                                             for d in plan.decisions if not d.official],
                    "plan": plan,
                }
                result["previews"].append({k: v for k, v in preview.items() if k != "plan"})
                (SNAPSHOT / f"{row['line'].replace('-', '_')}_{candidate['route_id']}_preview.json").write_text(
                    json.dumps({k: v for k, v in preview.items() if k != "plan"},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
    driver.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
