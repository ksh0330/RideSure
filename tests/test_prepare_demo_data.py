"""Offline guards for the single-command demo data preparation path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts import prepare_demo_data


def test_shipped_route_snapshots_match_reviewed_totals() -> None:
    sources = prepare_demo_data._sources()
    assert [line for line, *_ in sources] == ["B1", "1000", "1001", "1003", "1004"]
    assert sum(len(records) for _, _, _, records, _ in sources) == 210
    assert sum(expected for _, _, expected, _, _ in sources) == 160
    assert len({row.staging_id for _, _, _, records, _ in sources for row in records}) == 210


@pytest.mark.parametrize("state", ["partial", "empty"])
def test_refuses_inconsistent_graph_before_writes(state: str) -> None:
    driver = MagicMock()
    snapshot = SimpleNamespace(state=state)
    with patch.object(prepare_demo_data, "inspect_database", return_value=snapshot), \
         patch.object(prepare_demo_data, "_reference_counts", return_value=(3, 0)), \
         patch.object(prepare_demo_data, "process_files") as historical_import, \
         patch.object(prepare_demo_data, "import_records") as official_import:
        with pytest.raises(RuntimeError):
            prepare_demo_data.prepare(driver)
    historical_import.assert_not_called()
    official_import.assert_not_called()


def test_complete_graph_skips_history_and_can_repeat_reference_apply() -> None:
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    session.run.return_value.data.return_value = [{"pattern_id": "historical-pattern"}]
    snapshot = SimpleNamespace(state="complete", counts={
        "Line": 148, "RoutePattern": 154, "Stop": 2049,
        "StopOccurrence": 11375, "LoadObservation": 273000,
    })
    sources = prepare_demo_data._sources()
    by_route = {route_id: records for _, route_id, _, records, _ in sources}
    verified_by_line = {line: expected for line, _, expected, _, _ in sources}
    with patch.object(prepare_demo_data, "inspect_database", return_value=snapshot), \
         patch.object(prepare_demo_data, "_reference_counts", return_value=(210, 162)), \
         patch.object(prepare_demo_data, "_scalar", return_value=162), \
         patch.object(prepare_demo_data, "verify_b1", return_value={"status": "ok"}), \
         patch.object(prepare_demo_data, "read_historical_pattern", return_value=[]), \
         patch.object(prepare_demo_data, "read_official_route", side_effect=lambda _, route_id: by_route[route_id]), \
         patch.object(prepare_demo_data, "build_mapping_plan", side_effect=lambda *args, **kwargs:
                      SimpleNamespace(verified=[None] * verified_by_line[kwargs["line_name"]])), \
         patch.object(prepare_demo_data, "build_reviewed_plan", return_value=SimpleNamespace(verified=[None, None])), \
         patch.object(prepare_demo_data, "validate_alignment_structure"), \
         patch.object(prepare_demo_data, "apply_mapping_plan"), \
         patch.object(prepare_demo_data, "process_files") as historical_import, \
         patch.object(prepare_demo_data, "import_records") as official_import:
        assert prepare_demo_data.prepare(driver)["verified_official_stop"] == 162
        assert prepare_demo_data.prepare(driver)["verified_official_stop"] == 162
    historical_import.assert_not_called()
    assert official_import.call_count == 10
    assert session.execute_write.call_count == 12
