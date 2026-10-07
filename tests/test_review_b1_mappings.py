"""Reviewed B1 mappings are explicit and never change automatic alignment."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import config
from data_insert_v2 import build_topology_rows, load_sources
from official_stop_mapping import (
    HistoricalOccurrence, apply_mapping_plan, build_mapping_plan, read_official_route_json,
)
from scripts.review_b1_mappings import MANIFEST, build_reviewed_plan
from service import BusPredictionService


def _plans():
    rows = [row for row in build_topology_rows(load_sources(config.INPUT_FILES))
            if row["raw_line"] == "B1"]
    history = [HistoricalOccurrence(row["occurrence_id"], row["pattern_id"],
                                    row["seq"], row["raw_stop_name"]) for row in rows]
    official = read_official_route_json(str(MANIFEST.parent / "b1_route_stops.json"))
    automatic = build_mapping_plan(history, official, line_name="B1",
                                   official_route_id="DJB30300128",
                                   route_binding_source="saved TAGO B1 route metadata")
    return automatic, official


def test_two_directional_reviews_preserve_automatic_baseline_and_evidence():
    automatic, official = _plans()
    assert len(automatic.verified) == 41
    assert [row.historical.seq for row in automatic.decisions if row.status == "UNMATCHED"] == [
        12, 13, 16, 24, 25, 26, 29, 30, 31, 39, 42, 43,
    ]
    reviewed = build_reviewed_plan(automatic, official)
    assert [(row.historical.seq, row.official.node_order, row.official.official_node_id)
            for row in reviewed.verified] == [
                (13, 14, "DJB8007295"), (42, 43, "DJB8007296"),
            ]
    assert all(row.method == "HUMAN_REVIEWED_SEQUENCE" and row.evidence["review_reason"]
               for row in reviewed.verified)
    assert len(automatic.verified) == 41


def test_insufficient_or_stale_review_evidence_creates_no_plan(tmp_path: Path):
    automatic, official = _plans()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["mappings"][0]["official_node_id"] = "wrong-node"
    path = tmp_path / "invalid-review.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        build_reviewed_plan(automatic, official, path)


def test_reviewed_apply_preserves_reason_and_uses_idempotent_edge_merge():
    automatic, official = _plans()
    plan = build_reviewed_plan(automatic, official)
    tx = Mock()
    ids = [{"occurrence_id": row.historical.occurrence_id} for row in plan.verified]
    tx.run.side_effect = [[], ids, ids]
    assert apply_mapping_plan(tx, plan) == 2
    query = tx.run.call_args_list[2].args[0]
    rows = tx.run.call_args_list[2].kwargs["rows"]
    assert "MERGE (occurrence)-[mapping:VERIFIED_OFFICIAL_STOP]->(stop)" in query
    assert "mapping.review_reason = r.review_reason" in query
    assert all(row["method"] == "HUMAN_REVIEWED_SEQUENCE" and row["review_reason"]
               for row in rows)


def test_reviewed_coordinate_enrichment_keeps_historical_name():
    stop = BusPredictionService._normalize_stop({
        "stop_id": "historical-stop", "occurrence_id": "reviewed-occurrence",
        "stop_name": "세종시청.교육청.시의회",
        "official_stop_name": "세종시청,시의회,교육청",
        "lat": 36.478752, "lon": 127.288994,
    })
    assert stop["name"] == "세종시청.교육청.시의회"
    assert stop["official_stop_name"] == "세종시청,시의회,교육청"
    assert (stop["lat"], stop["lon"]) == (36.478752, 127.288994)
