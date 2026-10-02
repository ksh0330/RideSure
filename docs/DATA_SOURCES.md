# RideSure 데이터 출처와 재현 범위

## 저장소에 포함된 historical CSV

저장소 루트의 `노선·정류장 지표(노선별 차내 재차인원)_20251108 (1..3).csv`는 v2 importer의 입력이다. 파일명의 `20251108`을 **서비스 날짜로 해석**한다. CSV 내부에 별도의 날짜 열은 없으므로 날짜의 원 출처는 추가 확인이 필요하다.

| 파일 | 원본 행 | 24시간 관측 셀 | SHA-256 |
|---|---:|---:|---|
| `(1).csv` | 4,307 | 103,368 | `b9fd9a18a4dca0a21a99c82f23b398200e14951d051b30345e10fbbe311595a5` |
| `(2).csv` | 5,156 | 123,744 | `8cc5e1f58bafc60e42a498d8471b01f2581bbdbcbb8347eeaed146bedb3ff72b` |
| `(3).csv` | 1,912 | 45,888 | `4537a3e46b9e6e9299f6bbfc82e15c237c3fa2d7736b2c12320b9265e6f987a4` |
| **합계** | **11,375** | **273,000** | |

`data_insert_v2.py profile`로 위 해시·행·셀 수와 topology 예상 개수를 로컬에서 재현할 수 있다. 이 값은 **CSV/ETL 프로파일**이다. Neo4j에 실제로 적재된 개수는 별도로 `data_insert_v2.py verify`를 실행해야 확인된다. 새 클론에는 Neo4j 데이터베이스나 Docker volume이 포함되지 않는다.

필수 열은 `노선`, `기종점`, `정류장순번`, `정류장명`, `00시`부터 `23시`까지다. 현재 프로파일의 273,000개 값은 모두 0 이상 정수다. 실제 0을 유효 관측으로 보존하며, 향후 null과 파싱 실패는 각각 `MISSING`, `INVALID`로 구분한다. `count`는 **차내 재차인원**이며 차량 정원 대비 혼잡률, 탑승 성공 확률, 실시간 값이 아니다.

관련 공식 자료: [공공데이터포털의 국토교통부 노선별 재차인원 현황](https://www.data.go.kr/data/15071617/fileData.do)은 수집 방법을 교통카드빅데이터시스템(STCIS)으로 설명한다. [STCIS](https://www.stcis.go.kr/)는 관련 조회 시스템이다. 다만 저장소의 **세 CSV와 해당 포털 항목을 일대일로 연결하는 다운로드 기록·원문 URL은 저장소에 없다**. 따라서 포털 항목의 이용 조건을 이 세 파일의 확인된 재배포 조건으로 단정하지 않는다. 파일별 원 출처와 재배포 조건은 추가 확인 과제다.

## 선택적 공식 참조 데이터

다음 데이터는 저장소에 포함되지 않는다. 실행 환경에 따라 `.env`의 파일 경로 또는 API 키로 설정하며, 기본 historical import에는 필요하지 않다.

| 자료 | 코드에서 구현한 범위 | 사용자 측 준비 |
|---|---|---|
| [국토교통부 전국 버스정류장 위치정보](https://www.data.go.kr/data/15067528/fileData.do) | CSV 인코딩·열·좌표 검증, 공식 ID/좌표 Stop 적재 | CSV 수동 다운로드, `NATIONAL_BUS_STOP_CSV_DIR` |
| [국토교통부 TAGO 버스노선정보](https://www.data.go.kr/data/15098529/openapi.do) | 노선별 경유 정류소 JSON 조회, 공식 Stop과 `RouteStopStaging` 적재 | `DATA_GO_KR_SERVICE_KEY`, 공식 city/route ID |

전국 정류장 CSV adapter는 `NODE_ID`, `NODE_NM`, `GPS_LATI`, `GPS_LONG` 및 선택적 위치·도시 필드를 읽는다. 기본 경로는 저장소 기준 `data/public/national_bus_stops/*.csv`이며 해당 파일은 `.gitignore` 대상이다. TAGO adapter는 `routeid`, `nodeid`, `nodenm`, `nodeord`, `gpslati`, `gpslong` 등을 읽고, `updowncd`가 없으면 방향을 추측하지 않는다. `tests/fixtures/`의 TAGO 응답은 synthetic test fixture다.

```powershell
.\.venv\Scripts\python.exe .\public_data.py status
.\.venv\Scripts\python.exe .\public_data.py validate-national
.\.venv\Scripts\python.exe .\public_data.py import-national
.\.venv\Scripts\python.exe .\public_data.py fetch-tago --city-code <official-city-code> --route-id <official-route-id>
```

`import-national`과 TAGO의 `--import-to-neo4j` 옵션은 **로컬 Neo4j v2**에 공식 ID를 가진 별도 노드를 적재한다. 이름만으로 historical Stop/StopOccurrence에 병합하지 않는다. 따라서 공식 자료 적재만으로 B1 historical 경로의 좌표나 지도 선이 생기지는 않는다. 먼저 공식 ID와 historical occurrence의 대응을 검증해야 한다. 외부 키·다운로드 파일·적재 결과의 유무는 각 실행 환경에서 `public_data.py status`와 Neo4j 조회로 확인한다.

Phase 2A의 선택적 occurrence 매핑은 [OFFICIAL_STOP_MAPPING.md](OFFICIAL_STOP_MAPPING.md)에 설명한다. 전국 정류장 CSV만으로는 노선별 정차 순서를 알 수 없어 안전한 매핑에 충분하지 않다. TAGO의 노선별 경유 정류소 순서와 검증된 노선 ID가 필요하며, 이미 받은 TAGO JSON 응답은 `public_data.py import-tago-json --path <file>`로 키 없이 적재할 수 있다.

## 현재 구현 경계

- 저장소 포함: 세 historical CSV, 재현 가능한 v2 importer와 단위 테스트.
- 선택적 로컬 상태: Neo4j v1/v2 volume, EXAONE 모델 가중치, `.env`의 Kakao/API 키, 전국 정류장 CSV.
- 구현된 fallback 조회: fresh realtime observation → exact historical observation → 이미 생성된 profile → `UNKNOWN`. 현재 저장소에는 realtime importer와 profile 생성 batch가 없다.
- 미구현: 검증된 B1 공식 정류장 매핑, 공식 도로 route shape, 차량 정원/대기열 근거의 탑승 확률.

설정 누락은 `public_data.py status`의 `NOT_CONFIGURED`로 표시된다. 이 값은 **실행한 컴퓨터의 상태**이며 저장소 기능의 영구 상태를 뜻하지 않는다.
