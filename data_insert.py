"""Idempotent CSV-to-Neo4j loader for the recovered RideSure demo dataset."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd
from neo4j import GraphDatabase

import config


EXPECTED_COUNTS = {
    "Line": 148,
    "Stop": 2_047,
    "Load": 172_872,
    "HAS_STOP": 7_203,
    "AT": 172_872,
    "AFFECTS": 172_872,
}
REQUIRED_COLUMNS = ["노선", "기종점", "정류장순번", "정류장명"]


@dataclass(frozen=True)
class DatabaseSnapshot:
    state: str
    counts: Dict[str, int]
    orphan_loads: int
    sample: Optional[Dict]


def read_csv_korean(path: str) -> pd.DataFrame:
    """Read a Korean CSV without silently dropping malformed records."""
    errors: list[str] = []
    for encoding in ("utf-8", "utf-8-sig", "cp949", "euc-kr"):
        try:
            frame = pd.read_csv(path, encoding=encoding)
            print(f"CSV read: {Path(path).name} ({encoding}, {len(frame)} rows)")
            return frame
        except UnicodeDecodeError:
            continue
        except Exception as exc:  # retain a useful cause without skipping rows
            errors.append(f"{encoding}: {exc}")
    raise RuntimeError(f"Could not read CSV {path}. Attempts: {'; '.join(errors)}")


def slug(value: str) -> str:
    if value is None or pd.isna(value):
        return "UNKNOWN"
    text = str(value).strip().replace(" ", "_")
    text = re.sub(r"[^\w\-가-힣_]", "", text)
    return text or "UNKNOWN"


def hour_to_int(value: str) -> Optional[int]:
    if pd.isna(value):
        return None
    try:
        hour = int(str(value).replace("시", "").strip())
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour <= 23 else None


def date_from_filename(path: Path) -> str:
    match = re.search(r"(\d{8})", path.name)
    if not match:
        raise ValueError(f"CSV filename has no YYYYMMDD date: {path.name}")
    compact = match.group(1)
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def melt_hourly_data(frame: pd.DataFrame, date_str: str) -> pd.DataFrame:
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(missing)}")
    hour_columns = [column for column in config.HOUR_COLS if column in frame.columns]
    if len(hour_columns) != 24:
        missing_hours = [column for column in config.HOUR_COLS if column not in frame.columns]
        raise ValueError(f"CSV is missing hourly column(s): {', '.join(missing_hours)}")

    subset = frame[REQUIRED_COLUMNS + hour_columns].copy()
    melted = subset.melt(
        id_vars=REQUIRED_COLUMNS,
        value_vars=hour_columns,
        var_name="hour_kor",
        value_name="count",
    )
    melted["hour"] = melted["hour_kor"].apply(hour_to_int)
    if melted["hour"].isna().any():
        raise ValueError("CSV contains an invalid hourly column name.")
    melted["hour"] = melted["hour"].astype(int)
    melted["count"] = pd.to_numeric(melted["count"], errors="coerce").fillna(0).astype(int)
    melted["date"] = date_str
    melted["line_id"] = melted["노선"].astype(str).str.strip()
    melted["stop_id"] = melted["정류장명"].astype(str).apply(slug)
    melted["seq"] = pd.to_numeric(
        melted["정류장순번"], errors="coerce"
    ).fillna(-1).astype(int)

    invalid = melted[
        melted["line_id"].isin(("", "nan")) | (melted["stop_id"] == "UNKNOWN")
    ]
    if not invalid.empty:
        raise ValueError(f"CSV contains {len(invalid)} row(s) without a usable line/stop ID.")
    return melted[
        ["line_id", "노선", "기종점", "stop_id", "정류장명", "seq", "date", "hour", "count"]
    ]


def upsert_batch(tx, rows: List[Dict]) -> None:
    """MERGE stable IDs so replaying the same dataset cannot create duplicates."""
    tx.run(
        """
        UNWIND $rows AS r
        MERGE (l:Line {id: r.line_id})
          ON CREATE SET l.name = r.노선
          ON MATCH SET l.name = coalesce(l.name, r.노선)
        MERGE (s:Stop {id: r.stop_id})
          ON CREATE SET s.name = r.정류장명
          ON MATCH SET s.name = coalesce(s.name, r.정류장명)
        WITH l, s, r
        MERGE (l)-[hs:HAS_STOP]->(s)
          ON CREATE SET hs.seq = CASE WHEN r.seq >= 0 THEN r.seq ELSE NULL END,
                        hs.key = l.id + '::' + s.id
          ON MATCH SET hs.seq = coalesce(hs.seq, CASE WHEN r.seq >= 0 THEN r.seq ELSE NULL END)
        WITH l, s, r
        MERGE (ld:Load {id: r.line_id + '|' + r.stop_id + '|' + r.date + '|' + toString(r.hour)})
          ON CREATE SET ld.date = r.date, ld.hour = r.hour, ld.count = r.count,
                        ld.line_id = r.line_id, ld.stop_id = r.stop_id
          ON MATCH SET ld.count = r.count
        MERGE (ld)-[:AT]->(s)
        MERGE (ld)-[:AFFECTS]->(l)
        """,
        rows=rows,
    ).consume()


def create_constraints(driver) -> None:
    statements = (
        "CREATE CONSTRAINT line_id_unique IF NOT EXISTS FOR (n:Line) REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT stop_id_unique IF NOT EXISTS FOR (n:Stop) REQUIRE n.id IS UNIQUE",
        "CREATE CONSTRAINT load_id_unique IF NOT EXISTS FOR (n:Load) REQUIRE n.id IS UNIQUE",
        "CREATE INDEX load_date_idx IF NOT EXISTS FOR (n:Load) ON (n.date)",
        "CREATE INDEX load_hour_idx IF NOT EXISTS FOR (n:Load) ON (n.hour)",
    )
    with driver.session() as session:
        for statement in statements:
            session.run(statement).consume()


def _scalar(session, query: str, key: str = "count") -> int:
    return int(session.run(query).single(strict=True)[key])


def inspect_database(driver) -> DatabaseSnapshot:
    with driver.session() as session:
        total_nodes = _scalar(session, "MATCH (n) RETURN count(n) AS count")
        if total_nodes == 0:
            return DatabaseSnapshot(
                "empty",
                {name: 0 for name in EXPECTED_COUNTS},
                0,
                None,
            )
        counts = {
            "Line": _scalar(session, "MATCH (n:Line) RETURN count(n) AS count"),
            "Stop": _scalar(session, "MATCH (n:Stop) RETURN count(n) AS count"),
            "Load": _scalar(session, "MATCH (n:Load) RETURN count(n) AS count"),
            "HAS_STOP": _scalar(session, "MATCH ()-[r:HAS_STOP]->() RETURN count(r) AS count"),
            "AT": _scalar(session, "MATCH ()-[r:AT]->() RETURN count(r) AS count"),
            "AFFECTS": _scalar(session, "MATCH ()-[r:AFFECTS]->() RETURN count(r) AS count"),
        }
        orphan_loads = _scalar(
            session,
            """
            MATCH (ld:Load)
            WHERE NOT (ld)-[:AT]->(:Stop) OR NOT (ld)-[:AFFECTS]->(:Line)
            RETURN count(ld) AS count
            """,
        )
        record = session.run(
            """
            MATCH (l:Line)-[hs:HAS_STOP]->(s:Stop)<-[:AT]-(ld:Load)-[:AFFECTS]->(l)
            RETURN l.id AS line_id, l.name AS line_name,
                   s.id AS stop_id, s.name AS stop_name, hs.seq AS seq,
                   ld.date AS date, ld.hour AS hour, ld.count AS load
            ORDER BY l.id, hs.seq, ld.hour
            LIMIT 1
            """
        ).single()
        sample = dict(record) if record else None

    if all(value == 0 for value in counts.values()):
        state = "empty"
    elif counts == EXPECTED_COUNTS and orphan_loads == 0 and sample is not None:
        state = "complete"
    else:
        state = "partial"
    return DatabaseSnapshot(state, counts, orphan_loads, sample)


def open_driver():
    config.validate_required_config(("neo4j",))
    driver = GraphDatabase.driver(
        config.NEO4J_URI,
        auth=(config.NEO4J_USER, config.NEO4J_PASS),
        connection_timeout=10,
    )
    driver.verify_connectivity()
    return driver


def process_files(driver, file_paths: Iterable[str], batch_size: int = 1_000) -> None:
    paths = [Path(path) for path in file_paths]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Required CSV file(s) missing: {', '.join(missing)}")

    create_constraints(driver)
    for path in paths:
        frame = read_csv_korean(str(path))
        melted = melt_hourly_data(frame, date_from_filename(path))
        total = len(melted)
        print(f"Importing {path.name}: {total} expanded rows")
        with driver.session() as session:
            for start in range(0, total, batch_size):
                rows = melted.iloc[start : start + batch_size].to_dict("records")
                session.execute_write(upsert_batch, rows)
        print(f"Imported {path.name}")


def snapshot_json(snapshot: DatabaseSnapshot) -> str:
    return json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status",
        action="store_true",
        help="inspect only; do not import",
    )
    args = parser.parse_args()

    driver = open_driver()
    try:
        before = inspect_database(driver)
        print(f"Database state before import: {snapshot_json(before)}")
        if args.status:
            return 0 if before.state == "complete" else 2
        if before.state == "complete":
            print("Database is already complete; import skipped (idempotent setup).")
            return 0
        if before.state == "partial":
            raise RuntimeError(
                "Neo4j contains a partial or unexpected RideSure dataset. "
                "No data was overwritten or deleted; inspect/restore it manually."
            )

        process_files(driver, config.INPUT_FILES)
        after = inspect_database(driver)
        print(f"Database state after import: {snapshot_json(after)}")
        if after.state != "complete":
            raise RuntimeError("CSV import finished but graph verification did not pass.")
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
