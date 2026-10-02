"""Neo4j v2 direct-route and defensible congestion lookup helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Sequence


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

        Historical stops currently have no coordinates, so an unenriched graph
        correctly returns an empty list rather than fabricated locations.
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
        """Return routeable name candidates, exact matches first.

        This method deliberately does not collapse homonyms or pick one result.
        Callers must use the returned stop ID to make an unambiguous selection.
        """
        if not isinstance(query_text, str) or not query_text.strip():
            raise ValueError("query_text must not be empty")
        limit = self._bounded_limit(limit)
        normalized_query = query_text.strip()
        query = """
        MATCH (stop:Stop)<-[:AT_STOP]-(occurrence:StopOccurrence)
              <-[:HAS_OCCURRENCE]-(pattern:RoutePattern)
              <-[:HAS_PATTERN]-(line:Line)
        WHERE toLower(stop.name) CONTAINS toLower($name_query)
        WITH stop,
             count(DISTINCT occurrence) AS occurrence_count,
             collect(DISTINCT pattern.pattern_id) AS pattern_ids,
             collect(DISTINCT line.name) AS line_names,
             CASE WHEN toLower(stop.name) = toLower($name_query) THEN true ELSE false END
                 AS exact_match,
             CASE WHEN toLower(stop.name) STARTS WITH toLower($name_query)
                  THEN true ELSE false END AS prefix_match
        RETURN stop.stop_id AS stop_id,
               stop.name AS stop_name,
               properties(stop)['lat'] AS lat,
               properties(stop)['lon'] AS lon,
               stop.source AS source,
               exact_match,
               occurrence_count,
               pattern_ids,
               line_names
        ORDER BY exact_match DESC, prefix_match DESC, stop.name, stop.stop_id
        LIMIT $limit
        """
        with self.driver.session() as session:
            return session.run(
                query, name_query=normalized_query, limit=limit
            ).data()

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
          AND ($line_name IS NULL OR line.name = $line_name)
        MATCH path=(origin_occurrence)-[:NEXT*1..500]->(destination_occurrence:StopOccurrence)
        MATCH (destination_occurrence)-[:AT_STOP]->(destination:Stop)
        WHERE (($destination_stop_id IS NOT NULL AND destination.stop_id = $destination_stop_id)
               OR ($destination_stop_id IS NULL AND destination.name = $destination_name))
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
        WITH line, pattern, path, origin, destination,
             origin_occurrence, destination_occurrence, path_index, occurrence, stop
        ORDER BY path_index
        WITH line, pattern, path, origin, destination,
             origin_occurrence, destination_occurrence,
             collect({
                 occurrence_id: occurrence.occurrence_id,
                 seq: occurrence.seq,
                 stop_id: stop.stop_id,
                 stop_name: stop.name,
                 lat: properties(stop)['lat'],
                 lon: properties(stop)['lon']
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
                line_name=line_name,
                limit=limit,
            ).data()

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
        RETURN occurrence.occurrence_id AS occurrence_id,
               occurrence.seq AS seq,
               stop.stop_id AS stop_id,
               stop.name AS stop_name,
               properties(stop)['lat'] AS lat,
               properties(stop)['lon'] AS lon
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
