# RideSure 데이터 소스와 설정 상태

## Historical 차내 재차인원 CSV

프로젝트의 세 CP949 CSV는 `노선·정류장 지표(노선별 차내 재차인원)` 분할 파일이다. 파일명의 `20251108`을 service date `2025-11-08`로 사용한다.

| 파일 | 원본 행 | 관측 셀 | SHA-256 |
|---|---:|---:|---|
| `…20251108 (1).csv` | 4,307 | 103,368 | `b9fd9a18a4dca0a21a99c82f23b398200e14951d051b30345e10fbbe311595a5` |
| `…20251108 (2).csv` | 5,156 | 123,744 | `8cc5e1f58bafc60e42a498d8471b01f2581bbdbcbb8347eeaed146bedb3ff72b` |
| `…20251108 (3).csv` | 1,912 | 45,888 | `4537a3e46b9e6e9299f6bbfc82e15c237c3fa2d7736b2c12320b9265e6f987a4` |
| 합계 | 11,375 | 273,000 | - |

필수 열은 `노선`, `기종점`, `정류장순번`, `정류장명`, 24개 시간대 열이다. 현재 273,000개 셀은 모두 0 이상 정수이며 실제 0도 관측값으로 보존한다. 향후 입력의 null과 파싱 실패는 각각 `MISSING`, `INVALID`로 저장한다.

`count`는 **차내 재차인원(onboard passenger count)** 이다. 승차 인원, 차량 정원 대비 혼잡률, 탑승 성공 확률, 실시간 값 또는 미래 예측 레이블이 아니다. `/api/predict`는 exact historical 값과 같은 pattern·날짜·시간 분포에서 계산한 상대 percentile을 사용한다.

원본 CSV의 제공 기관, 원문 URL, 수집·가공 과정, 재배포 라이선스는 현재 자료만으로 확인되지 않았다. 확인 전에는 이 세 파일을 공개 저장소에 push하지 않는다.

## 선택적 공식 참조 데이터

### 전국 버스정류장 위치정보

- 공식 서비스: [국토교통부_전국 버스정류장 위치정보](https://www.data.go.kr/data/15067528/fileData.do)
- 활용 열: `NODE_ID`, `NODE_NM`, `GPS_LATI`, `GPS_LONG`, `NODE_MOBILE_ID`, `CITY_CD`, `CITY_NAME`, `ADMIN_NM`, `COLLECTD_TIME`
- 좌표계: WGS84
- 구현: CSV 탐색·인코딩 처리·스키마/좌표 검증·official Stop batch upsert
- 실제 상태: `NOT_CONFIGURED` — 파일을 다운로드하거나 적재하지 않음

설정 및 검증:

CSV를 `D:\sun\data\public\national_bus_stops\*.csv`에 둔다. 다른 폴더를 쓸 때는 `D:\sun\.env`에 다음 값을 설정한다.

```dotenv
NATIONAL_BUS_STOP_CSV_DIR=D:\path\to\national_bus_stops
```

```powershell
.\.venv\Scripts\python.exe .\public_data.py status
.\.venv\Scripts\python.exe .\public_data.py validate-national
.\.venv\Scripts\python.exe .\public_data.py import-national
```

`import-national`은 공식 ID와 좌표를 가진 별도 Stop을 적재한다. historical name-only Stop 또는 StopOccurrence와 이름만으로 자동 연결하지 않으므로, 이 명령만으로 API 경로 좌표가 생기지는 않는다.

### TAGO 버스노선정보 API

- 공식 서비스: [국토교통부_(TAGO)_버스노선정보](https://www.data.go.kr/data/15098529/openapi.do)
- 활용 endpoint: 노선별 경유 정류소 목록
- 활용 열: `routeid`, `nodeid`, `nodenm`, `nodeno`, `nodeord`, `gpslati`, `gpslong`, `updowncd`
- 구현: JSON client, 오류/좌표 검증, official Stop과 `RouteStopStaging` upsert
- 실제 상태: `NOT_CONFIGURED` — `DATA_GO_KR_SERVICE_KEY`가 없고 live 응답을 검증·적재하지 않음

설정 및 검증:

```dotenv
# D:\sun\.env
DATA_GO_KR_SERVICE_KEY=<your data.go.kr service key>
```

```powershell
.\.venv\Scripts\python.exe .\public_data.py status
.\.venv\Scripts\python.exe .\public_data.py fetch-tago --city-code <official-city-code> --route-id <official-route-id>
.\.venv\Scripts\python.exe .\public_data.py fetch-tago --city-code <official-city-code> --route-id <official-route-id> --import-to-neo4j
```

공식 city/route ID는 포털 응답이나 공식 자료로 확인해야 한다. RideSure는 B1 ID를 추측하지 않는다. `updowncd`가 없으면 `None`으로 보존한다. staging도 검증된 historical 매핑을 자동 생성하지 않는다.

### 대전광역시 정류소정보조회 API

- 공식 서비스: [대전광역시_정류소정보조회](https://www.data.go.kr/data/15157895/openapi.do)
- 실제 상태: `NOT_IMPLEMENTED`

응답 필드와 필요성이 확인되기 전에는 필드명을 추측한 adapter를 추가하지 않는다. 현재 설정이 필요한 항목은 아니다.

## 적용 현황

| 데이터/기능 | 상태 |
|---|---|
| Historical RoutePattern/StopOccurrence | `READY` |
| Historical 273,000 LoadObservation | `READY` |
| `/api/predict` historical fallback | `READY` |
| 전국 정류장 CSV/TAGO adapter와 CLI | `IMPLEMENTED_NOT_VERIFIED` |
| 공공데이터 key·다운로드 파일 | `NOT_CONFIGURED` |
| B1 공식 route/stop 매핑 | `NOT_CONFIGURED` |
| Historical occurrence 좌표 커버리지 | 0 |
| 정확한 도로 route shape | `NOT_IMPLEMENTED` |
| Realtime importer | `NOT_IMPLEMENTED` |
| 차량 정원/탑승 성공 ground truth | 없음 |

`tests/fixtures/`의 public-data 응답은 adapter 계약을 검증하는 synthetic fixture이며 실제 공식 매핑 결과가 아니다.
