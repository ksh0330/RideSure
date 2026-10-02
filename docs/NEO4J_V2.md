# Neo4j v2

## 목적과 격리

v2는 historical CSV의 pattern, 반복 정류장 occurrence, 모든 시간대 관측과 provenance를 손실 없이 보존하고 `/api/predict`의 실제 경로·혼잡 조회에 사용된다. 기존 `neo4j` 서비스와 volume은 유지하며 v2는 Compose profile `v2`, 포트 7475/7688, `neo4j_v2_data`/`neo4j_v2_logs`를 사용한다.

```powershell
docker compose --profile v2 up -d --wait neo4j-v2
.\.venv\Scripts\python.exe .\data_insert_v2.py profile
.\.venv\Scripts\python.exe .\data_insert_v2.py import
.\.venv\Scripts\python.exe .\data_insert_v2.py verify
```

재적재는 `MERGE` 기반이며 같은 file hash/row/hour에서 같은 ID를 만든다. 삭제/reset 명령은 제공하지 않는다.

## 그래프 구조

```text
(Line)-[:HAS_PATTERN]->(RoutePattern)
(RoutePattern)-[:HAS_OCCURRENCE]->(StopOccurrence)
(StopOccurrence)-[:AT_STOP]->(Stop)
(StopOccurrence)-[:NEXT]->(StopOccurrence)
(LoadObservation)-[:OBSERVED_AT]->(StopOccurrence)
(LoadObservation)-[:ON_LINE]->(Line)
(LoadObservation)-[:IMPORTED_IN]->(ImportBatch)
```

핵심 속성:

- `Line`: `line_id`, `name`, `raw_line`, `source`, `id_kind`
- `RoutePattern`: `pattern_id`, `line_id`, `terminal_description`, `direction_status`, `version`, `pattern_hash`, `source`
- `Stop`: `stop_id`, `name`, `raw_name`, `id_kind`, `mapping_status`, `source`; 공식 데이터에는 `official_node_id`, `lat`, `lon`, `location`, `city_code`
- `StopOccurrence`: `occurrence_id`, `pattern_id`, `seq`, `raw_stop_name`, `source`
- `LoadObservation`: `observation_id`, source file/row/hour, `service_date`, `hour`, raw fields/value, `onboard_count`, `parse_status`, `imported_at`
- `ImportBatch`: `batch_id`, source/file/hash, imported time, row/observation/status counts, `status`

Historical ID는 source-local hash이며 공식 ID처럼 보이지 않는다. `Line`은 raw line, `RoutePattern`은 raw line+terminal, `Stop`은 정확히 정규화한 raw name, `StopOccurrence`는 pattern+seq, observation은 file hash+row+hour에서 결정론적으로 만든다. 정류장명 구두점을 제거하지 않으며 반복 방문은 occurrence로 분리한다.

Historical source에 공식 방향값이 없어 `direction_status='UNKNOWN'`이며, Stop은 공식 매핑 전 `UNMAPPED_NAME_ONLY`다. 동명 정류장 구분은 검증된 공식 매핑이 필요하다.

## CSV에서 계산한 적재 예상 개수

다음은 저장소의 세 CSV에 `data_insert_v2.py profile`을 적용해 확인한 topology/관측 예상 개수다. 새 클론의 Neo4j는 비어 있으며 아래 개수가 실제 DB에 존재한다고 뜻하지 않는다. import 후 `verify`로 적재 상태를 확인한다.

| Node | 개수 | Relationship | 개수 |
|---|---:|---|---:|
| `Line` | 148 | `HAS_PATTERN` | 154 |
| `RoutePattern` | 154 | `HAS_OCCURRENCE` | 11,375 |
| `Stop` | 2,049 | `AT_STOP` | 11,375 |
| `StopOccurrence` | 11,375 | `NEXT` | 11,221 |
| `LoadObservation` | 273,000 | `OBSERVED_AT` | 273,000 |
| `ImportBatch` | 3 | `ON_LINE` | 273,000 |
| | | `IMPORTED_IN` | 273,000 |

`verify`는 orphan, pattern을 넘는 `NEXT`, 비연속 seq, provenance 누락, 비정상 좌표, 미완료 batch와 B1 대표 경로를 검사한다.

## B1 기준

- historical pattern 1개, occurrence `seq=1..53`
- `오송역2.3.4`가 seq 27과 28에 독립 occurrence로 존재
- 대전역 seq 2 → 세종시청 seq 13: 11-hop direct path
- 세종시청 seq 42 → 대전역 seq 52: 반대 방향 direct path
- 대전역 historical onboard count: 08시 17, 09시 24

경로 조회는 occurrence의 실제 `NEXT*`를 통과하고 같은 pattern·연속 seq를 확인하므로 단순 `origin.seq < destination.seq` 비교보다 반복 정류장에 안전하다.

## 공공데이터 staging과 좌표

`public_data.py`는 다음 두 선택적 입력을 지원한다.

- 전국 정류장 CSV: official ID·WGS84 좌표의 Stop을 upsert
- TAGO 노선별 정류장 API: official Stop과 `RouteStopStaging`의 route ID/order/direction을 upsert

```powershell
.\.venv\Scripts\python.exe .\public_data.py status
.\.venv\Scripts\python.exe .\public_data.py validate-national
.\.venv\Scripts\python.exe .\public_data.py import-national
.\.venv\Scripts\python.exe .\public_data.py fetch-tago --city-code <official-city-code> --route-id <official-route-id> --import-to-neo4j
```

공식 자료와 API 키는 저장소에 포함되지 않는다. 각 컴퓨터의 설정 여부는 `public_data.py status`로 확인한다. 제공된 ETL은 historical Stop에 좌표를 설정하거나 B1 공식 mapping을 만들지 않는다. Official Stop과 historical Stop/RoutePattern은 이름만으로 병합하지 않는다. `tests/fixtures/`는 실제 B1 응답이 아닌 synthetic schema fixture다.

## API에서의 사용

`prediction_v2.V2TransitRepository`가 다음을 제공한다.

- routeable Stop name/nearby 후보와 Stop ID 선택 조회
- occurrence ID를 보존하는 direct route와 ordered stops
- 모든 정류장에 실제 좌표가 있을 때만 stop-to-stop geometry
- fresh realtime → exact historical → profile → unknown 혼잡 근거

`service.py`는 최대 3개 direct pattern을 근접 후보·hop 수로 정렬해 첫 경로를 추천하고 나머지를 alternative로 반환한다. 현재 혼잡 수치는 ranking 점수에 들어가지 않는다. Historical 관측이 없으면 0을 만들지 않고 `UNKNOWN/데이터 부족`을 반환한다. `boarding_probability`는 nullable deprecated field이며 항상 `null`이다.

## 제한

- official B1 ID/sequence와 historical occurrence mapping 미완료
- 기본 historical ETL에는 좌표 매핑이 없어 geometry `UNAVAILABLE`
- official road/route shape와 환승 탐색 미구현
- realtime importer 및 `LoadProfile` 생성 batch 미구현
- 차량 정원·대기열·탑승 성공 label 부재
