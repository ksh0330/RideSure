from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock

import config
from data_insert_v2 import build_topology_rows, load_sources
from official_stop_mapping import (
    HistoricalOccurrence,
    OfficialRouteOccurrence,
    apply_mapping_plan,
    build_mapping_plan,
    read_official_route_json,
    verified_coordinates,
)
from public_data import TAGO_SOURCE


def historical(*names: str) -> list[HistoricalOccurrence]:
    return [
        HistoricalOccurrence(f"hist-{index}", "pattern-1", index, name)
        for index, name in enumerate(names, start=1)
    ]


def official(*names: str, direction: str | None = None) -> list[OfficialRouteOccurrence]:
    return [
        OfficialRouteOccurrence(
            f"staging-{direction}-{index}", "route-1", f"node-{direction}-{index}",
            f"official-{direction}-{index}", index, name, direction, TAGO_SOURCE,
            TAGO_SOURCE, 36.3 + index / 100, 127.3 + index / 100,
        )
        for index, name in enumerate(names, start=1)
    ]


def mapping(history: list[HistoricalOccurrence], route: list[OfficialRouteOccurrence], **kwargs):
    return build_mapping_plan(
        history, route, line_name="B1", official_route_id="route-1",
        route_binding_source="operator route listing, checked manually", **kwargs,
    )


class MappingPlanTests(unittest.TestCase):
    def test_saved_tago_json_supports_preview_without_graph_import(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "tago_route_stops.json"
        route = read_official_route_json(str(fixture))
        self.assertEqual([row.node_order for row in route], [1, 2])
        self.assertEqual(route[0].official_node_id, "TEST_NODE_001")

    def test_real_b1_history_preserves_both_osong_occurrences(self) -> None:
        rows = [
            row for row in build_topology_rows(load_sources(config.INPUT_FILES))
            if row["raw_line"] == "B1"
        ]
        pattern_ids = {row["pattern_id"] for row in rows}
        self.assertEqual(len(pattern_ids), 1)
        history = [
            HistoricalOccurrence(row["occurrence_id"], row["pattern_id"], row["seq"], row["raw_stop_name"])
            for row in rows
        ]
        route = [
            OfficialRouteOccurrence(
                f"synthetic-stage-{index}", "route-1", f"synthetic-node-{index}",
                f"synthetic-stop-{index}", index, row.name, None, TAGO_SOURCE,
                TAGO_SOURCE, 36.3, 127.3,
            )
            for index, row in enumerate(history, start=1)
        ]
        plan = mapping(history, route)
        repeated = [d for d in plan.decisions if d.historical.name == "오송역2.3.4"]
        self.assertEqual([d.historical.seq for d in repeated], [27, 28])
        self.assertEqual([d.status for d in repeated], ["EXACT", "EXACT"])
        self.assertNotEqual(repeated[0].official.staging_id, repeated[1].official.staging_id)

    def test_current_b1_snapshot_maps_turnaround_without_filling_new_stops(self) -> None:
        root = Path(__file__).resolve().parents[1]
        route = read_official_route_json(
            str(root / "data/public/tago/b1_2026-10-02/b1_route_stops.json")
        )
        rows = [
            row for row in build_topology_rows(load_sources(config.INPUT_FILES))
            if row["raw_line"] == "B1"
        ]
        history = [
            HistoricalOccurrence(row["occurrence_id"], row["pattern_id"], row["seq"], row["raw_stop_name"])
            for row in rows
        ]
        plan = build_mapping_plan(
            history, route, line_name="B1", official_route_id="DJB30300128",
            route_binding_source="TAGO route-number and basic-info snapshot",
        )
        self.assertEqual((len(route), len(plan.decisions)), (55, 53))
        self.assertEqual([row.node_order for row in route], list(range(1, 56)))
        self.assertEqual(
            [(d.historical.seq, d.status, d.official.node_order, d.official.official_node_id)
             for d in plan.decisions if d.historical.name == "오송역2.3.4"],
            [(27, "SEQUENCE_MATCH", 28, "DJB8007055"),
             (28, "SEQUENCE_MATCH", 29, "DJB9007055")],
        )
        positions = [d.official.node_order for d in plan.verified]
        self.assertEqual(positions, sorted(set(positions)))
        self.assertNotIn(11, positions)
        self.assertNotIn(46, positions)
        self.assertEqual(len(plan.verified), 41)
        self.assertEqual(sum(d.status == "UNMATCHED" for d in plan.decisions), 12)
        self.assertTrue(plan.verified)
        self.assertTrue(all(d.official.lat is not None and d.official.lon is not None for d in plan.verified))

    def test_exact_full_sequence_uses_distinct_repeated_occurrences(self) -> None:
        plan = mapping(
            historical("A", "오송역2.3.4", "오송역2.3.4", "Z"),
            official("A", "오송역2.3.4", "오송역2.3.4", "Z"),
        )
        self.assertEqual([decision.status for decision in plan.decisions], ["EXACT"] * 4)
        self.assertNotEqual(plan.decisions[1].official.staging_id, plan.decisions[2].official.staging_id)
        self.assertNotEqual(plan.decisions[1].historical.occurrence_id, plan.decisions[2].historical.occurrence_id)

    def test_neighboring_sequence_matches_when_official_has_an_extra_stop(self) -> None:
        plan = mapping(historical("A", "B", "C"), official("NEW", "A", "B", "C"))
        self.assertEqual([decision.status for decision in plan.decisions], ["SEQUENCE_MATCH"] * 3)
        self.assertEqual([decision.official.node_order for decision in plan.decisions], [2, 3, 4])

    def test_presentation_separators_normalize_but_numeric_punctuation_does_not(self) -> None:
        plan = mapping(
            historical("정부세종청사남측", "보람동.대평동", "오송역2.3.4"),
            official("정부세종청사 남측", "보람동,대평동", "오송역234"),
        )
        self.assertEqual([d.status for d in plan.decisions], ["UNMATCHED"] * 3)
        exact = mapping(
            historical("정부세종청사남측", "보람동.대평동", "오송역2.3.4"),
            official("정부세종청사 남측", "보람동,대평동", "오송역2.3.4"),
        )
        self.assertEqual([d.status for d in exact.decisions], ["EXACT"] * 3)
        self.assertEqual(exact.decisions[1].evidence["name_match_kind"], "PRESENTATION_NORMALIZED")

    def test_semantic_rename_remains_unmatched_in_global_alignment(self) -> None:
        plan = mapping(
            historical("A", "B", "소담동", "C", "D"),
            official("A", "B", "소담동(새샘마을)", "C", "D"),
        )
        self.assertEqual([d.status for d in plan.decisions],
                         ["SEQUENCE_MATCH", "SEQUENCE_MATCH", "UNMATCHED", "SEQUENCE_MATCH", "SEQUENCE_MATCH"])
        self.assertEqual(plan.decisions[2].evidence["possible_official_orders"], [])

    def test_unique_candidate_that_can_be_skipped_stays_ambiguous(self) -> None:
        plan = mapping(historical("A", "B", "C", "D"), official("A", "C", "B", "D"))
        self.assertEqual(plan.decisions[1].status, "AMBIGUOUS")
        self.assertTrue(plan.decisions[1].evidence["can_skip_in_optimal_alignment"])
        self.assertEqual(plan.decisions[2].status, "AMBIGUOUS")

    def test_alignment_is_deterministic_under_input_order_changes(self) -> None:
        history = historical("A", "B", "B", "C")
        route = official("A", "NEW", "B", "B", "C")
        first = mapping(history, route)
        second = mapping(list(reversed(history)), list(reversed(route)))
        self.assertEqual(first, second)
        self.assertEqual([d.official.node_order for d in first.verified], [1, 3, 4, 5])

    def test_same_name_without_unique_neighbor_context_stays_ambiguous(self) -> None:
        plan = mapping(
            historical("A", "B", "C"),
            official("A", "B", "C", "X", "A", "B", "C"),
        )
        self.assertEqual([decision.status for decision in plan.decisions], ["AMBIGUOUS"] * 3)
        self.assertEqual(plan.verified, ())

    def test_unmatched_stop_remains_in_historical_plan(self) -> None:
        plan = mapping(historical("A", "B", "C", "D"), official("A", "B", "X", "D"))
        self.assertEqual(len(plan.decisions), 4)
        self.assertEqual(plan.decisions[2].status, "UNMATCHED")
        self.assertIsNone(plan.decisions[2].official)

    def test_name_only_evidence_never_maps(self) -> None:
        plan = mapping(historical("B"), official("A", "B", "C"))
        self.assertEqual(plan.decisions[0].status, "UNMATCHED")
        self.assertEqual(plan.verified, ())

    def test_multiple_directions_require_explicit_selection(self) -> None:
        history = historical("A", "B", "C")
        routes = official("A", "B", "C", direction="UP") + official("C", "B", "A", direction="DOWN")
        self.assertEqual(
            [decision.status for decision in mapping(history, routes).decisions],
            ["AMBIGUOUS"] * 3,
        )
        self.assertEqual(
            [decision.status for decision in mapping(history, routes, direction_code="UP").decisions],
            ["EXACT"] * 3,
        )

    def test_continuously_numbered_round_trip_crosses_direction_boundary(self) -> None:
        outbound = official("A", "B", "TURN", direction="0")
        inbound = [
            OfficialRouteOccurrence(**{**row.__dict__, "node_order": row.node_order + 3})
            for row in official("TURN", "B", "A", direction="1")
        ]
        plan = mapping(historical("A", "B", "TURN", "TURN", "B", "A"), outbound + inbound)
        self.assertEqual([decision.status for decision in plan.decisions], ["EXACT"] * 6)
        self.assertEqual([decision.official.node_order for decision in plan.decisions], list(range(1, 7)))

    def test_missing_official_topology_keeps_historical_pattern_usable(self) -> None:
        plan = mapping(historical("A", "B", "C"), [])
        self.assertEqual(len(plan.decisions), 3)
        self.assertEqual([decision.status for decision in plan.decisions], ["UNMATCHED"] * 3)
        self.assertEqual(plan.verified, ())

    def test_wrong_route_identity_or_unverified_binding_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "bound TAGO route"):
            mapping(historical("A", "B", "C"), [
                OfficialRouteOccurrence(
                    "stage", "other-route", "node", "stop", 1, "A", None,
                    TAGO_SOURCE, TAGO_SOURCE, 36.3, 127.3,
                )
            ])
        with self.assertRaisesRegex(ValueError, "route_binding_source"):
            build_mapping_plan(
                historical("A", "B", "C"), official("A", "B", "C"),
                line_name="B1", official_route_id="route-1", route_binding_source="",
            )

    def test_duplicate_official_order_is_rejected(self) -> None:
        route = official("A", "B", "C")
        route[2] = OfficialRouteOccurrence(**{**route[2].__dict__, "node_order": 2})
        with self.assertRaisesRegex(ValueError, "duplicate node order"):
            mapping(historical("A", "B", "C"), route)


class MappingPersistenceTests(unittest.TestCase):
    def test_unverified_plan_never_writes(self) -> None:
        tx = Mock()
        self.assertEqual(apply_mapping_plan(tx, mapping(historical("B"), official("B"))), 0)
        tx.run.assert_not_called()

    def test_verified_mapping_is_an_occurrence_edge_with_provenance(self) -> None:
        tx = Mock()
        tx.run.side_effect = [
            [],
            [{"occurrence_id": "hist-1"}, {"occurrence_id": "hist-2"}, {"occurrence_id": "hist-3"}],
            [{"occurrence_id": "hist-1"}, {"occurrence_id": "hist-2"}, {"occurrence_id": "hist-3"}],
        ]
        plan = mapping(historical("A", "B", "C"), official("A", "B", "C"))
        self.assertEqual(apply_mapping_plan(tx, plan), 3)
        query = tx.run.call_args_list[2].args[0]
        params = tx.run.call_args_list[2].kwargs
        self.assertIn("MERGE (occurrence)-[mapping:VERIFIED_OFFICIAL_STOP]->(stop)", query)
        self.assertNotIn("SET occurrence.", query)
        self.assertNotIn("SET stop.", query)
        self.assertEqual(params["rows"][0]["official_node_id"], "node-None-1")
        self.assertEqual(params["rows"][0]["coordinate_source"], TAGO_SOURCE)
        self.assertEqual(params["rows"][0]["normalized_name"], "A")
        self.assertEqual(params["rows"][0]["name_match_kind"], "RAW_EXACT")
        self.assertIn("mapping.name_match_kind = r.name_match_kind", query)
        self.assertEqual(params["route_binding_source"], plan.route_binding_source)

    def test_conflicting_existing_mapping_is_rejected(self) -> None:
        tx = Mock()
        tx.run.return_value = [{"occurrence_id": "hist-1"}]
        with self.assertRaisesRegex(ValueError, "different official stop"):
            apply_mapping_plan(tx, mapping(historical("A", "B", "C"), official("A", "B", "C")))
        self.assertEqual(tx.run.call_count, 1)

    def test_coordinates_are_read_only_through_verified_mapping(self) -> None:
        session = Mock()
        session.run.return_value.single.side_effect = [
            None,
            {"official_node_id": "node-1", "lat": 36.3, "lon": 127.3,
             "coordinate_source": TAGO_SOURCE, "mapping_status": "EXACT"},
        ]
        self.assertIsNone(verified_coordinates(session, "hist-1"))
        self.assertEqual(verified_coordinates(session, "hist-1")["lat"], 36.3)
        query = session.run.call_args.args[0]
        self.assertIn("VERIFIED_OFFICIAL_STOP", query)
        self.assertIn("['EXACT', 'SEQUENCE_MATCH']", query)


if __name__ == "__main__":
    unittest.main()
