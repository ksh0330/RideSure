"""Neo4j v2 direct-route and defensible congestion lookup helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Sequence

from official_stop_mapping import normalize_presentation_name


@dataclass(frozen=True)
class CongestionResult:
    source: str
    status: str
    onboard_count: int | None
    relative_percentile: float | None
    congestion_level: str
    boarding_guidance: str
    service_date: str | None = None
    hour: int | None = None
    observation_id: str | None = None
    sample_size: int = 0


def relative_percentile(value: int, population: Sequence[int]) -> float | None:
    values = [int(item) for item in population]
    if not values:
        return None
    lower = sum(item < value for item in values)
    equal = sum(item == value for item in values)
    return round(100.0 * (lower + 0.5 * equal) / len(values), 1)


def congestion_labels(percentile: float | None) -> tuple[str, str]:
    if percentile is None:
        return "UNKNOWN", "데이터 부족"
    if percentile <= 33.0:
        return "LOW", "여유"
    if percentile <= 67.0:
        return "MEDIUM", "보통"
    if percentile <= 90.0:
        return "HIGH", "혼잡"
    return "VERY_HIGH", "매우 혼잡"


class V2TransitRepository:
    """Read-only repository; EXAONE receives results from this layer, not vice versa."""

    def __init__(self, driver: Any):
        self.driver = driver

    @staticmethod
    def _bounded_limit(limit: int, *, maximum: int = 50) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if limit < 1 or limit > maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        return limit

    def find_nearby_stops(
        self,
        lat: float,
        lon: float,
        radius_m: float = 1_000,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """Return routeable stops with real coordinates inside ``radius_m``.

        This searches coordinates on historical Stop nodes only. Occurrence
        mappings do not turn a name-based historical Stop into one physical
        location, so this lookup may remain empty after B1 enrichment.
        """
        coordinates = (lat, lon, radius_m)
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in coordinates
        ):
            raise ValueError("lat, lon, and radius_m must be numbers")
        if not all(isfinite(float(value)) for value in coordinates):
            raise ValueError("lat, lon, and radius_m must be finite")
        if not -90 <= float(lat) <= 90:
            raise ValueError("lat must be between -90 and 90")
        if not -180 <= float(lon) <= 180:
            raise ValueError("lon must be between -180 and 180")
        if radius_m <= 0:
            raise ValueError("radius_m must be greater than 0")
        limit = self._bounded_limit(max_results, maximum=100)
        query = """
        MATCH (stop:Stop)<-[:AT_STOP]-(occurrence:StopOccurrence)
              <-[:HAS_OCCURRENCE]-(pattern:RoutePattern)
        WHERE properties(stop)['location'] IS NOT NULL
          AND properties(stop)['lat'] IS NOT NULL
          AND properties(stop)['lon'] IS NOT NULL
        WITH stop, count(DISTINCT occurrence) AS occurrence_count,
             collect(DISTINCT pattern.pattern_id) AS pattern_ids,
             point.distance(
                 properties(stop)['location'],
                 point({latitude: $lat, longitude: $lon})
             ) AS distance_m
        WHERE distance_m <= $radius_m
        RETURN stop.stop_id AS stop_id,
               stop.name AS stop_name,
               properties(stop)['lat'] AS lat,
               properties(stop)['lon'] AS lon,
               stop.source AS source,
               occurrence_count,
               pattern_ids,
               distance_m
        ORDER BY distance_m, stop.name, stop.stop_id
        LIMIT $limit
        """
        with self.driver.session() as session:
            return session.run(
                query,
                lat=float(lat),
                lon=float(lon),
                radius_m=float(radius_m),
                limit=limit,
            ).data()

    def find_stops_by_name(
        self, query_text: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search historical stops and verified official aliases conservatively."""
        if not isinstance(query_text, str) or not query_text.strip():
            raise ValueError("query_text must not be empty")
        limit = self._bounded_limit(limit)
        raw = query_text.strip()
        normalized = normalize_presentation_name(raw).casefold()
        historical_query = """
        MATCH (stop:Stop)<-[:AT_STOP]-(occurrence:StopOccurrence)
              <-[:HAS_OCCURRENCE]-(pattern:RoutePattern)
              <-[:HAS_PATTERN]-(line:Line)
        WITH stop,
             count(DISTINCT occurrence) AS occurrence_count,
             collect(DISTINCT pattern.pattern_id) AS pattern_ids,
             collect(DISTINCT line.name) AS line_names
        RETURN stop.stop_id AS stop_id,
               stop.name AS stop_name,
               properties(stop)['lat'] AS lat,
               properties(stop)['lon'] AS lon,
               stop.source AS source,
               occurrence_count,
               pattern_ids,
               line_names
        """
        alias_query = """
        MATCH (occurrence:StopOccurrence)-[:AT_STOP]->(historical:Stop)
        MATCH (pattern:RoutePattern)-[:HAS_OCCURRENCE]->(occurrence)
        MATCH (line:Line)-[:HAS_PATTERN]->(pattern)
        MATCH (occurrence)-[verified:VERIFIED_OFFICIAL_STOP]->(official:Stop)
        WHERE verified.mapping_status IN ['EXACT', 'SEQUENCE_MATCH']
          AND verified.official_node_id = official.official_node_id
          AND official.id_kind = 'OFFICIAL_NODE_ID'
        RETURN DISTINCT historical.stop_id AS stop_id,
               historical.name AS stop_name,
               occurrence.occurrence_id AS occurrence_id,
               official.name AS official_stop_name,
               official.official_node_id AS official_stop_id,
               official.lat AS lat, official.lon AS lon,
               line.name AS line_name, pattern.pattern_id AS pattern_id
        """
        with self.driver.session() as session:
            historical = session.run(historical_query).data()
            aliases = session.run(alias_query).data()

        def rank(name: str) -> int | None:
            folded = normalize_presentation_name(name).casefold()
            if name.casefold() == raw.casefold():
                return 0
            if folded == normalized:
                return 1
            if folded.startswith(normalized):
                return 2
            if normalized in folded:
                return 3
            return None

        scored: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        for stop in historical:
            priority = rank(stop["stop_name"])
            if priority is None:
                continue
            candidate = dict(stop)
            candidate["line_names"] = sorted(
                candidate.get("line_names") or [],
                key=lambda line: (not str(line).startswith("B"), str(line)),
            )
            candidate["exact_match"] = priority == 0
            candidate["match_kind"] = ("RAW_EXACT", "NORMALIZED_EXACT", "NORMALIZED_PREFIX", "PARTIAL")[priority]
            scored.append(((priority, 0, stop["stop_name"], stop["stop_id"], ""), candidate))

        for alias in aliases:
            if alias["official_stop_name"] == alias["stop_name"]:
                continue
            priority = rank(alias["official_stop_name"])
            if priority is None:
                continue
            # A verified occurrence is the identity of this alias. Its parent
            # historical Stop may have other, unverified occurrences.
            candidate = {
                "stop_id": alias["stop_id"], "stop_name": alias["stop_name"],
                "occurrence_id": alias["occurrence_id"],
                "official_stop_name": alias["official_stop_name"],
                "official_stop_id": alias["official_stop_id"],
                "line_names": [alias["line_name"]], "pattern_ids": [alias["pattern_id"]],
                "occurrence_count": 1, "lat": alias["lat"], "lon": alias["lon"],
                "exact_match": False, "match_kind": "VERIFIED_OFFICIAL_ALIAS",
            }
            alias_priority = 1 if priority == 0 else priority
            scored.append(((alias_priority, 1, alias["stop_name"], alias["stop_id"], alias["occurrence_id"]), candidate))
        scored.sort(key=lambda item: item[0])
        return [candidate for _, candidate in scored[:limit]]

    def find_verified_stop_occurrence_by_id(self, occurrence_id: str) -> dict[str, Any] | None:
        """Resolve an alias selection only through its verified occurrence."""
        if not isinstance(occurrence_id, str) or not occurrence_id.strip():
            raise ValueError("occurrence_id must not be empty")
        query = """
        MATCH (occurrence:StopOccurrence {occurrence_id: $occurrence_id})-[:AT_STOP]->(historical:Stop)
        MATCH (pattern:RoutePattern)-[:HAS_OCCURRENCE]->(occurrence)
        MATCH (line:Line)-[:HAS_PATTERN]->(pattern)
        MATCH (occurrence)-[verified:VERIFIED_OFFICIAL_STOP]->(official:Stop)
        WHERE verified.mapping_status IN ['EXACT', 'SEQUENCE_MATCH']
          AND verified.official_node_id = official.official_node_id
          AND official.id_kind = 'OFFICIAL_NODE_ID'
        RETURN historical.stop_id AS stop_id, historical.name AS stop_name,
               occurrence.occurrence_id AS occurrence_id,
               official.name AS official_stop_name,
               official.official_node_id AS official_stop_id,
               official.lat AS lat, official.lon AS lon,
               line.name AS line_name, pattern.pattern_id AS pattern_id
        """
        with self.driver.session() as session:
            record = session.run(query, occurrence_id=occurrence_id.strip()).single()
        return dict(record) if record else None

    def find_stop_by_id(self, stop_id: str) -> dict[str, Any] | None:
        """Resolve one routeable Stop by identity, without a name fallback."""
        if not isinstance(stop_id, str) or not stop_id.strip():
            raise ValueError("stop_id must not be empty")
        query = """
        MATCH (stop:Stop {stop_id: $stop_id})<-[:AT_STOP]-(occurrence:StopOccurrence)
              <-[:HAS_OCCURRENCE]-(pattern:RoutePattern)
              <-[:HAS_PATTERN]-(line:Line)
        RETURN stop.stop_id AS stop_id,
               stop.name AS stop_name,
               properties(stop)['lat'] AS lat,
               properties(stop)['lon'] AS lon,
               stop.source AS source,
               count(DISTINCT occurrence) AS occurrence_count,
               collect(DISTINCT pattern.pattern_id) AS pattern_ids,
               collect(DISTINCT line.name) AS line_names
        """
        with self.driver.session() as session:
            record = session.run(query, stop_id=stop_id.strip()).single()
        return dict(record) if record else None

    def find_direct_routes(
        self,
        origin_stop_name: str | None,
        destination_stop_name: str | None,
        line_name: str | None = None,
        limit: int = 10,
        *,
        origin_stop_id: str | None = None,
        destination_stop_id: str | None = None,
        origin_occurrence_id: str | None = None,
        destination_occurrence_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = self._bounded_limit(limit)
        if origin_stop_id is None and not origin_stop_name:
            raise ValueError("origin_stop_name or origin_stop_id is required")
        if destination_stop_id is None and not destination_stop_name:
            raise ValueError("destination_stop_name or destination_stop_id is required")
        query = """
        MATCH (origin:Stop)<-[:AT_STOP]-(origin_occurrence:StopOccurrence)
              <-[:HAS_OCCURRENCE]-(pattern:RoutePattern)<-[:HAS_PATTERN]-(line:Line)
        WHERE (($origin_stop_id IS NOT NULL AND origin.stop_id = $origin_stop_id)
               OR ($origin_stop_id IS NULL AND origin.name = $origin_name))
          AND ($origin_occurrence_id IS NULL OR origin_occurrence.occurrence_id = $origin_occurrence_id)
          AND ($line_name IS NULL OR line.name = $line_name)
        MATCH path=(origin_occurrence)-[:NEXT*1..500]->(destination_occurrence:StopOccurrence)
        MATCH (destination_occurrence)-[:AT_STOP]->(destination:Stop)
        WHERE (($destination_stop_id IS NOT NULL AND destination.stop_id = $destination_stop_id)
               OR ($destination_stop_id IS NULL AND destination.name = $destination_name))
          AND ($destination_occurrence_id IS NULL OR destination_occurrence.occurrence_id = $destination_occurrence_id)
          AND all(occurrence IN nodes(path)
                  WHERE occurrence.pattern_id = pattern.pattern_id)
          AND all(next_relationship IN relationships(path)
                  WHERE endNode(next_relationship).seq = startNode(next_relationship).seq + 1)
        WITH line, pattern, path, origin, destination,
             origin_occurrence, destination_occurrence
        UNWIND range(0, size(nodes(path)) - 1) AS path_index
        WITH line, pattern, path, origin, destination,
             origin_occurrence, destination_occurrence, path_index,
             nodes(path)[path_index] AS occurrence
        MATCH (occurrence)-[:AT_STOP]->(stop:Stop)
        OPTIONAL MATCH (occurrence)-[verified:VERIFIED_OFFICIAL_STOP]->(official:Stop)
        WHERE verified.mapping_status IN ['EXACT', 'SEQUENCE_MATCH']
          AND verified.official_node_id = official.official_node_id
          AND official.id_kind = 'OFFICIAL_NODE_ID'
        WITH line, pattern, path, origin, destination,
             origin_occurrence, destination_occurrence, path_index, occurrence, stop, official
        ORDER BY path_index
        WITH line, pattern, path, origin, destination,
             origin_occurrence, destination_occurrence,
             collect({
                 occurrence_id: occurrence.occurrence_id,
                 seq: occurrence.seq,
                 stop_id: stop.stop_id,
                 stop_name: stop.name,
                 official_stop_name: official.name,
                 lat: official.lat,
                 lon: official.lon
             }) AS stops
        RETURN line.line_id AS line_id,
               line.name AS line_name,
               pattern.pattern_id AS pattern_id,
               pattern.terminal_description AS terminal_description,
               origin.stop_id AS origin_stop_id,
               origin.name AS origin_stop_name,
               origin_occurrence.occurrence_id AS origin_occurrence_id,
               destination.stop_id AS destination_stop_id,
               destination.name AS destination_stop_name,
               destination_occurrence.occurrence_id AS destination_occurrence_id,
               origin_occurrence.seq AS origin_seq,
               destination_occurrence.seq AS destination_seq,
               length(path) AS hops,
               stops,
               CASE
                   WHEN size(stops) >= 2
                        AND all(route_stop IN stops
                                WHERE route_stop.lat IS NOT NULL
                                  AND route_stop.lon IS NOT NULL)
                   THEN 'STOP_TO_STOP_APPROXIMATION'
                   ELSE 'UNAVAILABLE'
               END AS geometry_kind
        ORDER BY hops, line.name, pattern.pattern_id, origin_seq
        LIMIT $limit
        """
        with self.driver.session() as session:
            return session.run(
                query,
                origin_name=origin_stop_name,
                destination_name=destination_stop_name,
                origin_stop_id=origin_stop_id,
                destination_stop_id=destination_stop_id,
                origin_occurrence_id=origin_occurrence_id,
                destination_occurrence_id=destination_occurrence_id,
                line_name=line_name,
                limit=limit,
            ).data()

    def find_one_transfer_routes(
        self,
        origin_stop_name: str | None,
        destination_stop_name: str | None,
        limit: int = 10,
        *,
        origin_stop_id: str | None = None,
        destination_stop_id: str | None = None,
        origin_occurrence_id: str | None = None,
        destination_occurrence_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find two forward historical ride legs joined by one verified physical stop."""
        limit = self._bounded_limit(limit)
        if origin_stop_id is None and not origin_stop_name:
            raise ValueError("origin_stop_name or origin_stop_id is required")
        if destination_stop_id is None and not destination_stop_name:
            raise ValueError("destination_stop_name or destination_stop_id is required")
        query = """
        MATCH (origin:Stop)<-[:AT_STOP]-(origin_occurrence:StopOccurrence)
              <-[:HAS_OCCURRENCE]-(pattern1:RoutePattern)<-[:HAS_PATTERN]-(line1:Line)
        WHERE (($origin_stop_id IS NOT NULL AND origin.stop_id = $origin_stop_id)
               OR ($origin_stop_id IS NULL AND origin.name = $origin_name))
          AND ($origin_occurrence_id IS NULL OR origin_occurrence.occurrence_id = $origin_occurrence_id)
        MATCH path1=(origin_occurrence)-[:NEXT*1..500]->(transfer1:StopOccurrence)
        MATCH (pattern1)-[:HAS_OCCURRENCE]->(transfer1)
        MATCH (transfer1)-[mapping1:VERIFIED_OFFICIAL_STOP]->(physical:Stop)
        WHERE all(node IN nodes(path1) WHERE node.pattern_id = pattern1.pattern_id)
          AND all(rel IN relationships(path1)
                  WHERE endNode(rel).seq = startNode(rel).seq + 1)
          AND mapping1.mapping_status IN ['EXACT', 'SEQUENCE_MATCH']
          AND physical.id_kind = 'OFFICIAL_NODE_ID'
          AND mapping1.official_node_id = physical.official_node_id
        MATCH (physical)<-[mapping2:VERIFIED_OFFICIAL_STOP]-(transfer2:StopOccurrence)
              <-[:HAS_OCCURRENCE]-(pattern2:RoutePattern)<-[:HAS_PATTERN]-(line2:Line)
        WHERE pattern2.pattern_id <> pattern1.pattern_id
          AND mapping2.mapping_status IN ['EXACT', 'SEQUENCE_MATCH']
          AND mapping2.official_node_id = physical.official_node_id
        MATCH path2=(transfer2)-[:NEXT*1..500]->(destination_occurrence:StopOccurrence)
        MATCH (pattern2)-[:HAS_OCCURRENCE]->(destination_occurrence)
        MATCH (destination_occurrence)-[:AT_STOP]->(destination:Stop)
        WHERE (($destination_stop_id IS NOT NULL AND destination.stop_id = $destination_stop_id)
               OR ($destination_stop_id IS NULL AND destination.name = $destination_name))
          AND ($destination_occurrence_id IS NULL OR destination_occurrence.occurrence_id = $destination_occurrence_id)
          AND all(node IN nodes(path2) WHERE node.pattern_id = pattern2.pattern_id)
          AND all(rel IN relationships(path2)
                  WHERE endNode(rel).seq = startNode(rel).seq + 1)
        RETURN DISTINCT
               line1.line_id AS line1_id, line1.name AS line1_name,
               pattern1.pattern_id AS pattern1_id,
               pattern1.terminal_description AS pattern1_terminal,
               origin.stop_id AS origin_stop_id, origin.name AS origin_stop_name,
               origin_occurrence.occurrence_id AS origin_occurrence_id,
               transfer1.occurrence_id AS transfer1_occurrence_id,
               transfer1.raw_stop_name AS transfer1_name, transfer1.seq AS transfer1_seq,
               length(path1) AS leg1_hops,
               properties(mapping1) AS transfer1_mapping,
               line2.line_id AS line2_id, line2.name AS line2_name,
               pattern2.pattern_id AS pattern2_id,
               pattern2.terminal_description AS pattern2_terminal,
               transfer2.occurrence_id AS transfer2_occurrence_id,
               transfer2.raw_stop_name AS transfer2_name, transfer2.seq AS transfer2_seq,
               destination.stop_id AS destination_stop_id,
               destination.name AS destination_stop_name,
               destination_occurrence.occurrence_id AS destination_occurrence_id,
               destination_occurrence.seq AS destination_seq,
               length(path2) AS leg2_hops,
               physical.official_node_id AS transfer_official_stop_id,
               physical.name AS transfer_official_stop_name,
               properties(mapping2) AS transfer2_mapping
        ORDER BY leg1_hops + leg2_hops, leg1_hops, leg2_hops,
                 line1_name, line2_name, pattern1_id, pattern2_id,
                 origin_occurrence_id, transfer1_occurrence_id, transfer2_occurrence_id
        LIMIT $limit
        """
        with self.driver.session() as session:
            candidates = session.run(
                query,
                origin_name=origin_stop_name,
                destination_name=destination_stop_name,
                origin_stop_id=origin_stop_id,
                destination_stop_id=destination_stop_id,
                origin_occurrence_id=origin_occurrence_id,
                destination_occurrence_id=destination_occurrence_id,
                limit=limit,
            ).data()

        itineraries: list[dict[str, Any]] = []
        for candidate in candidates:
            leg1_geometry = self.get_route_geometry(
                candidate["pattern1_id"], candidate["origin_occurrence_id"],
                candidate["transfer1_occurrence_id"],
            )
            leg2_geometry = self.get_route_geometry(
                candidate["pattern2_id"], candidate["transfer2_occurrence_id"],
                candidate["destination_occurrence_id"],
            )
            itineraries.append({
                "transfer_count": 1,
                "total_hops": int(candidate["leg1_hops"]) + int(candidate["leg2_hops"]),
                "transfer": {
                    "official_stop_id": candidate["transfer_official_stop_id"],
                    "official_stop_name": candidate["transfer_official_stop_name"],
                    "historical_name_leg1": candidate["transfer1_name"],
                    "historical_name_leg2": candidate["transfer2_name"],
                    "leg1_occurrence_id": candidate["transfer1_occurrence_id"],
                    "leg2_occurrence_id": candidate["transfer2_occurrence_id"],
                    "leg1_seq": candidate["transfer1_seq"],
                    "leg2_seq": candidate["transfer2_seq"],
                    "mapping_leg1": self._mapping_provenance(candidate["transfer1_mapping"]),
                    "mapping_leg2": self._mapping_provenance(candidate["transfer2_mapping"]),
                },
                "legs": [
                    {
                        "line_id": candidate["line1_id"],
                        "line_name": candidate["line1_name"],
                        "pattern_id": candidate["pattern1_id"],
                        "terminal_description": candidate["pattern1_terminal"],
                        "origin_occurrence_id": candidate["origin_occurrence_id"],
                        "destination_occurrence_id": candidate["transfer1_occurrence_id"],
                        "hops": candidate["leg1_hops"],
                        **leg1_geometry,
                    },
                    {
                        "line_id": candidate["line2_id"],
                        "line_name": candidate["line2_name"],
                        "pattern_id": candidate["pattern2_id"],
                        "terminal_description": candidate["pattern2_terminal"],
                        "origin_occurrence_id": candidate["transfer2_occurrence_id"],
                        "destination_occurrence_id": candidate["destination_occurrence_id"],
                        "hops": candidate["leg2_hops"],
                        **leg2_geometry,
                    },
                ],
            })
        return itineraries

    @staticmethod
    def _mapping_provenance(properties: dict[str, Any]) -> dict[str, Any]:
        """Return serializable verification evidence without Neo4j timestamps."""
        allowed = (
            "mapping_status", "mapping_method", "official_route_id",
            "official_node_id", "route_binding_source", "name_match_kind",
            "normalized_name", "node_order", "staging_id", "coordinate_source",
            "official_source",
        )
        return {key: properties[key] for key in allowed if key in properties}

    def find_direct_routes_by_stop_ids(
        self,
        origin_stop_id: str,
        destination_stop_id: str,
        line_name: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not origin_stop_id or not destination_stop_id:
            raise ValueError("origin_stop_id and destination_stop_id are required")
        return self.find_direct_routes(
            None,
            None,
            line_name=line_name,
            limit=limit,
            origin_stop_id=origin_stop_id,
            destination_stop_id=destination_stop_id,
        )

    def get_route_stops(
        self,
        pattern_id: str,
        origin_occurrence_id: str | None = None,
        destination_occurrence_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return ordered occurrence-specific stops for one route pattern."""
        if not pattern_id:
            raise ValueError("pattern_id is required")
        if (origin_occurrence_id is None) != (destination_occurrence_id is None):
            raise ValueError("both occurrence IDs must be provided together")
        query = """
        MATCH (pattern:RoutePattern {pattern_id: $pattern_id})
              -[:HAS_OCCURRENCE]->(occurrence:StopOccurrence)
              -[:AT_STOP]->(stop:Stop)
        OPTIONAL MATCH (occurrence)-[verified:VERIFIED_OFFICIAL_STOP]->(official:Stop)
        WHERE verified.mapping_status IN ['EXACT', 'SEQUENCE_MATCH']
          AND verified.official_node_id = official.official_node_id
          AND official.id_kind = 'OFFICIAL_NODE_ID'
        RETURN occurrence.occurrence_id AS occurrence_id,
               occurrence.seq AS seq,
               stop.stop_id AS stop_id,
               stop.name AS stop_name,
               official.name AS official_stop_name,
               official.lat AS lat,
               official.lon AS lon
        ORDER BY occurrence.seq, occurrence.occurrence_id
        """
        with self.driver.session() as session:
            stops = session.run(query, pattern_id=pattern_id).data()
        if origin_occurrence_id is None:
            return stops

        positions = {
            stop.get("occurrence_id"): index for index, stop in enumerate(stops)
        }
        origin_index = positions.get(origin_occurrence_id)
        destination_index = positions.get(destination_occurrence_id)
        if (
            origin_index is None
            or destination_index is None
            or destination_index < origin_index
        ):
            return []
        return stops[origin_index : destination_index + 1]

    def get_route_geometry(
        self,
        pattern_id: str,
        origin_occurrence_id: str | None = None,
        destination_occurrence_id: str | None = None,
    ) -> dict[str, Any]:
        """Build a stop-to-stop approximation only when every stop has coordinates."""
        stops = self.get_route_stops(
            pattern_id, origin_occurrence_id, destination_occurrence_id
        )
        missing_coordinates = sum(
            stop.get("lat") is None or stop.get("lon") is None for stop in stops
        )
        complete = len(stops) >= 2 and missing_coordinates == 0
        coordinates = (
            [
                {"lat": float(stop["lat"]), "lon": float(stop["lon"])}
                for stop in stops
            ]
            if complete
            else []
        )
        return {
            "pattern_id": pattern_id,
            "geometry_kind": (
                "STOP_TO_STOP_APPROXIMATION" if complete else "UNAVAILABLE"
            ),
            "coordinates": coordinates,
            "stops": stops,
            "missing_coordinates": missing_coordinates,
        }

    def _historical_observation(
        self, pattern_id: str, occurrence_id: str, service_date: str, hour: int
    ) -> dict[str, Any] | None:
        query = """
        MATCH (pattern:RoutePattern {pattern_id: $pattern_id})
              -[:HAS_OCCURRENCE]->(occurrence:StopOccurrence {occurrence_id: $occurrence_id})
              <-[:OBSERVED_AT]-(observation:LoadObservation)
        WHERE observation.service_date = $service_date
          AND observation.hour = $hour
          AND observation.parse_status = 'VALID'
        MATCH (pattern)-[:HAS_OCCURRENCE]->(:StopOccurrence)
              <-[:OBSERVED_AT]-(peer:LoadObservation)
        WHERE peer.service_date = $service_date
          AND peer.hour = $hour
          AND peer.parse_status = 'VALID'
        RETURN observation.observation_id AS observation_id,
               observation.onboard_count AS onboard_count,
               collect(peer.onboard_count) AS population
        LIMIT 1
        """
        with self.driver.session() as session:
            record = session.run(
                query,
                pattern_id=pattern_id,
                occurrence_id=occurrence_id,
                service_date=service_date,
                hour=hour,
            ).single()
        return dict(record) if record else None

    def _realtime_observation(
        self,
        occurrence_id: str,
        now: datetime,
        max_age: timedelta,
    ) -> dict[str, Any] | None:
        cutoff = (now - max_age).astimezone(timezone.utc).isoformat()
        query = """
        MATCH (observation:RealtimeObservation)-[:OBSERVED_AT]->
              (:StopOccurrence {occurrence_id: $occurrence_id})
        WHERE observation.parse_status = 'VALID'
          AND datetime(observation.observed_at) >= datetime($cutoff)
        RETURN observation.observation_id AS observation_id,
               observation.onboard_count AS onboard_count,
               observation.observed_at AS observed_at
        ORDER BY datetime(observation.observed_at) DESC
        LIMIT 1
        """
        with self.driver.session() as session:
            record = session.run(
                query, occurrence_id=occurrence_id, cutoff=cutoff
            ).single()
        return dict(record) if record else None

    def _historical_profile(
        self, occurrence_id: str, hour: int
    ) -> dict[str, Any] | None:
        query = """
        MATCH (profile:LoadProfile {occurrence_id: $occurrence_id})
        WHERE profile.hour = $hour AND profile.status = 'READY'
        WITH properties(profile) AS props
        RETURN props['median_onboard_count'] AS onboard_count,
               props['relative_percentile'] AS relative_percentile,
               props['sample_size'] AS sample_size
        LIMIT 1
        """
        with self.driver.session() as session:
            record = session.run(query, occurrence_id=occurrence_id, hour=hour).single()
        return dict(record) if record else None

    def resolve_congestion(
        self,
        pattern_id: str,
        occurrence_id: str,
        service_date: str,
        hour: int,
        *,
        now: datetime | None = None,
        realtime_max_age: timedelta = timedelta(minutes=10),
    ) -> dict[str, Any]:
        if hour < 0 or hour > 23:
            raise ValueError("hour must be between 0 and 23")
        now = now or datetime.now(timezone.utc)
        realtime = self._realtime_observation(occurrence_id, now, realtime_max_age)
        if realtime:
            result = CongestionResult(
                source="REALTIME_OBSERVATION",
                status="AVAILABLE",
                onboard_count=int(realtime["onboard_count"]),
                relative_percentile=None,
                congestion_level="UNCLASSIFIED",
                boarding_guidance="실시간 재차인원 확인",
                observation_id=realtime["observation_id"],
                sample_size=1,
            )
            return asdict(result)

        historical = self._historical_observation(
            pattern_id, occurrence_id, service_date, hour
        )
        if historical:
            percentile = relative_percentile(
                int(historical["onboard_count"]), historical["population"]
            )
            level, guidance = congestion_labels(percentile)
            result = CongestionResult(
                source="HISTORICAL_OBSERVATION",
                status="AVAILABLE",
                onboard_count=int(historical["onboard_count"]),
                relative_percentile=percentile,
                congestion_level=level,
                boarding_guidance=guidance,
                service_date=service_date,
                hour=hour,
                observation_id=historical["observation_id"],
                sample_size=len(historical["population"]),
            )
            return asdict(result)

        profile = self._historical_profile(occurrence_id, hour)
        if profile:
            percentile = profile.get("relative_percentile")
            level, guidance = congestion_labels(percentile)
            result = CongestionResult(
                source="HISTORICAL_PROFILE",
                status="FALLBACK",
                onboard_count=int(profile["onboard_count"]),
                relative_percentile=percentile,
                congestion_level=level,
                boarding_guidance=guidance,
                hour=hour,
                sample_size=int(profile.get("sample_size") or 0),
            )
            return asdict(result)

        return asdict(
            CongestionResult(
                source="UNKNOWN",
                status="INSUFFICIENT_DATA",
                onboard_count=None,
                relative_percentile=None,
                congestion_level="UNKNOWN",
                boarding_guidance="데이터 부족",
                service_date=service_date,
                hour=hour,
            )
        )
