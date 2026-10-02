from __future__ import annotations

import unittest

from official_stop_mapping import (
    HistoricalOccurrence, MappingDecision, MappingPlan, OfficialRouteOccurrence,
)
from phase2d_apply import validate_alignment_structure
from public_data import TAGO_SOURCE


def plan(names: tuple[str, str], directions: tuple[str, str]) -> MappingPlan:
    decisions = []
    for index, (name, direction) in enumerate(zip(names, directions), 1):
        historical = HistoricalOccurrence(f"hist-{index}", "pattern", index, name)
        official = OfficialRouteOccurrence(
            f"stage-{index}", "route", f"node-{index}", f"stop-{index}",
            index, name, direction, TAGO_SOURCE, TAGO_SOURCE, 36.0, 127.0,
        )
        decisions.append(MappingDecision(historical, "SEQUENCE_MATCH", "TEST", official))
    return MappingPlan("pattern", "line", "route", "test binding", None, tuple(decisions))


class StructuralBindingTests(unittest.TestCase):
    def test_one_way_pattern_must_not_cross_official_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "crosses official"):
            validate_alignment_structure(plan(("출발", "도착"), ("0", "1")))

    def test_round_trip_pattern_can_span_both_directions(self) -> None:
        validate_alignment_structure(plan(("출발", "출발"), ("0", "1")))

    def test_one_way_pattern_with_single_direction_is_allowed(self) -> None:
        validate_alignment_structure(plan(("출발", "도착"), ("0", "0")))
