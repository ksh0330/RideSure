# Changelog

All notable changes to RideSure are recorded here. Dates use the commit dates in the git history.

## [1.0.0] - 2026-11-23

Portfolio rebuild of the original contest MVP on top of a traceable Neo4j v2 knowledge graph.

### Added

- Neo4j v2 graph with provenance: `Line`, `RoutePattern`, `Stop`, `StopOccurrence`, `NEXT`, `LoadObservation` and `ImportBatch`, rebuilt from the three 2025-11-08 historical CSVs (`data_insert_v2.py`, `prediction_v2.py`).
- Direct routing along historical `NEXT` order and stop search by name and stop ID.
- Conservative official stop mapping against TAGO snapshots (`official_stop_mapping.py`, `tago_route_discovery.py`), with the full B1 route aligned (43 of 53 occurrences verified) and reusable multi-route snapshots under `data/public/tago/`.
- Verified one-transfer routing, allowed only at the same `VERIFIED_OFFICIAL_STOP`.
- Kakao transit geometry and travel time, used only when the bus line, boarding stop, alighting stop and coordinates match (`kakao_transit_geometry.py`).
- Boarding-aware route comparison: categorical boarding likelihood from relative congestion, up to three `route_options`, and an alternative recommendation when the current route is rated low or very low.
- Reproducible demo setup: `python -m scripts.prepare_demo_data`, `compose.yaml` profile `v2`, PowerShell setup/start/stop/verify scripts, and `docs/RUNBOOK.md`.
- `requirements-dev.txt` for the test suite.
- GitHub Actions workflow that runs `python -m pytest -q` on push and pull request.
- Unit tests for configuration helpers (`tests/test_config_helpers.py`).

### Changed

- Historical ridership CSVs moved to `data/historical/` with their original file names.
- README rewritten for the portfolio, with an English summary, test and CI notes, and a future work list.

### Fixed

- JSON snapshot checksums are preserved across platforms (`.gitattributes` line-ending rules).

## [0.1-demo] - 2026-08-21

- Restored the original RideSure bus prediction demo.
- Fixed Windows managed process tree shutdown.
