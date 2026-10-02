"""Read-only live verification for the Phase 2D selected routes."""

from __future__ import annotations

import json

import config
from neo4j import GraphDatabase
from prediction_v2 import V2TransitRepository


CASES = (
    ("1000", "반석역", "한국농어촌공사"),
    ("1000-1", "조형아파트", "홍익대학교"),
    ("1001", "새나루마을10단지", "국책연구단지동측"),
    ("1003", "반석역", "가득초등학교정문"),
    ("1004", "반석역", "한솔중학교(첫마을7단지)"),
)


def main() -> None:
    driver = GraphDatabase.driver(config.NEO4J_V2_URI,
                                  auth=(config.NEO4J_USER, config.NEO4J_PASS))
    repo = V2TransitRepository(driver)
    result: dict[str, object] = {"routes": []}
    try:
        for line, origin, destination in CASES:
            routes = repo.find_direct_routes(origin, destination, line_name=line)
            if not routes:
                raise AssertionError(f"Historical {line} direct route disappeared.")
            route = routes[0]
            geometry = repo.get_route_geometry(route["pattern_id"],
                                               route["origin_occurrence_id"],
                                               route["destination_occurrence_id"])
            stops = route["stops"]
            result["routes"].append({
                "line": line, "pattern_id": route["pattern_id"],
                "origin_seq": route["origin_seq"], "destination_seq": route["destination_seq"],
                "hops": route["hops"], "stop_count": len(stops),
                "mapped_stops": sum(s["lat"] is not None and s["lon"] is not None for s in stops),
                "geometry_kind": geometry["geometry_kind"],
                "geometry_points": len(geometry["coordinates"]),
                "names_preserved": stops[0]["stop_name"] == origin and stops[-1]["stop_name"] == destination,
            })
        with driver.session() as session:
            shared = session.run("""
                MATCH (line:Line)-[:HAS_PATTERN]->(pattern:RoutePattern)
                      -[:HAS_OCCURRENCE]->(occurrence:StopOccurrence)
                      -[:VERIFIED_OFFICIAL_STOP]->(official:Stop)
                WITH official, collect(DISTINCT pattern.pattern_id) AS patterns,
                     collect(DISTINCT line.name) AS lines
                WHERE size(patterns) >= 2
                RETURN official.official_node_id AS node_id, official.name AS name,
                       size(patterns) AS pattern_count, lines
                ORDER BY pattern_count DESC, name
            """).data()
            counts = session.run("""
                MATCH (line:Line)-[:HAS_PATTERN]->(p:RoutePattern)
                      -[:HAS_OCCURRENCE]->(o:StopOccurrence)
                WHERE line.name IN ['B1','1000','1000-1','1001','1003','1004']
                OPTIONAL MATCH (o)-[v:VERIFIED_OFFICIAL_STOP]->(s:Stop)
                RETURN line.name AS line,count(DISTINCT o) AS historical,
                       count(v) AS verified, count(DISTINCT s) AS official_nodes
                ORDER BY line
            """).data()
            total_edges = session.run("MATCH ()-[v:VERIFIED_OFFICIAL_STOP]->() RETURN count(v) AS n").single()["n"]
            official_nodes = session.run("""
                MATCH (s:Stop {id_kind:'OFFICIAL_NODE_ID'}) RETURN count(s) AS n
            """).single()["n"]
            staging_records = session.run("""
                MATCH (s:RouteStopStaging) RETURN count(s) AS n
            """).single()["n"]
            result.update({"coverage": counts, "verified_edges_total": total_edges,
                           "official_nodes_total": official_nodes,
                           "staging_records_total": staging_records,
                           "shared_official_stop_count": len(shared),
                           "shared_official_examples": shared[:20]})
    finally:
        driver.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
