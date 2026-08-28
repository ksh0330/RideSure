# RideSure — Neo4j v2 기반 대중교통 혼잡 안내

RideSure는 2025 DSC 공유대학 KT 기업연계 오픈데이터 활용 스타트업 챌린지에서 만든 B1 BRT 아이디어를 포트폴리오용으로 재구성한 프로젝트입니다. 사용자 입력을 Neo4j v2의 정류장·노선 패턴·`NEXT` 경로와 연결하고, 실제 CSV의 차내 재차인원으로 상대 혼잡 안내를 만듭니다. EXAONE은 계산기가 아니라 확정된 사실을 짧은 한국어로 설명하는 로컬 SLM입니다.

현재 `/api/predict`는 더 이상 고정 노선이나 가짜 탑승 확률을 반환하지 않습니다. 지원 범위는 Neo4j v2에서 찾을 수 있는 직행 경로이며, 같은 입력이라도 날짜·시간의 관측값에 따라 재차인원과 혼잡 안내가 달라질 수 있습니다. 직접 경로가 없거나 정류장을 찾지 못하면 임의 결과 대신 오류를 반환합니다.

## 현재 구현 범위

- `RoutePattern`별 occurrence와 `NEXT`를 이용한 방향성 있는 직행 경로 조회
- 원본 11,375행과 273,000개 시간대 관측을 보존한 historical fallback
- fresh realtime → exact historical → `LoadProfile` → `UNKNOWN` 조회 순서
- `onboard_count`, 상대 percentile, `LOW`~`VERY_HIGH`, 탑승 안내 반환
- 여러 직행 pattern이 있으면 최대 3개 후보와 대안 노선 표시
- 선택 좌표를 받는 API와 Kakao Map의 마커·경로 표시 준비
- 구조화된 사실만 사용하는 EXAONE 프롬프트, 검증 실패/서버 장애 시 결정론적 설명 fallback

`boarding_probability`, `expected_load`, `travel_time`은 이전 프런트와의 호환을 위한 deprecated 필드이며 항상 `null`입니다. `onboard_count`는 차내 재차인원이지 탑승 확률이나 정원 대비 혼잡률이 아닙니다.

Historical 정류장에는 공식 ID·좌표 매핑이 아직 없습니다. 따라서 현재 기본 데이터만으로는 경로가 `geometry_kind=UNAVAILABLE`이고 지도 polyline도 표시되지 않습니다. 공식 정류장/TAGO adapter와 적재 CLI는 구현했지만 실제 key·파일은 `NOT_CONFIGURED`이며, 이름만으로 historical 정류장에 자동 병합하지 않습니다. 좌표가 검증된 occurrence 전체에 연결된 경우에만 정류장 좌표를 잇는 `STOP_TO_STOP_APPROXIMATION`을 표시하며 정확한 차량 궤적이라고 주장하지 않습니다.

## 빠른 시작

Windows PowerShell 5.1+, Python 3.12, Docker Desktop/Compose, Node.js, 기본 GPU 실행에는 NVIDIA CUDA 환경이 필요합니다.

```powershell
# 최초 설치: .env가 없으면 예제를 복사한 뒤 설정을 요청하고 중단합니다.
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1

# 평상시 전체 시작
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1

# 구성·DB·서버·EXAONE·API·JavaScript 검증
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1

# 이 프로젝트가 시작한 프로세스와 컨테이너만 정지
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
```

처음 생성된 `.env`에서는 최소한 다음 값을 설정합니다. 실제 값은 Git에 넣지 않습니다.

```dotenv
NEO4J_PASS=<local Neo4j password>
KAKAO_MAP_JAVASCRIPT_KEY=<Kakao JavaScript key>
```

접속 주소:

- 웹: http://127.0.0.1:8000
- API 문서: http://127.0.0.1:8000/docs
- 통합 상태: http://127.0.0.1:8000/health
- EXAONE 상태: http://127.0.0.1:8001/health
- Neo4j v1/v2 Browser: http://127.0.0.1:7474 / http://127.0.0.1:7475

## Neo4j v2와 테스트

v2는 기존 v1 volume을 건드리지 않는 별도 Compose profile, 포트 7475/7688, 별도 named volume을 사용합니다.

```powershell
docker compose --profile v2 up -d --wait neo4j-v2
.\.venv\Scripts\python.exe .\data_insert_v2.py profile
.\.venv\Scripts\python.exe .\data_insert_v2.py import
.\.venv\Scripts\python.exe .\data_insert_v2.py verify
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

B1 검증 기준은 대전역 `seq=2` → 세종시청 `seq=13`, 반대 방향 `seq=42` → `seq=52`이며 반복되는 `오송역2.3.4` occurrence도 독립적으로 보존합니다. 예시 historical 근거는 B1 대전역 08시 17명, 09시 24명입니다.

## 선택 사항: 공식 공공데이터 설정

현재 상태 확인:

```powershell
.\.venv\Scripts\python.exe .\public_data.py status
```

### 공공데이터포털 서비스 키 — `NOT_CONFIGURED`

1. [국토교통부_(TAGO)_버스노선정보](https://www.data.go.kr/data/15098529/openapi.do) 활용 신청 후 data.go.kr 서비스 키를 발급받습니다.
2. `D:\sun\.env`에 `DATA_GO_KR_SERVICE_KEY=<your key>`를 설정합니다.
3. 공식 `cityCode`와 `routeId`를 확인한 뒤 다음 명령으로 응답을 검증합니다.

```powershell
.\.venv\Scripts\python.exe .\public_data.py fetch-tago --city-code <official-city-code> --route-id <official-route-id>
```

검증된 응답을 v2에 staging하려면 마지막에 `--import-to-neo4j`를 추가합니다. 프로젝트는 B1의 공식 route ID를 추측하거나 하드코딩하지 않습니다.

### 전국 버스정류장 위치정보 CSV — `NOT_CONFIGURED`

1. [국토교통부_전국 버스정류장 위치정보](https://www.data.go.kr/data/15067528/fileData.do)에서 CSV를 내려받습니다.
2. CSV 파일을 `D:\sun\data\public\national_bus_stops\`에 둡니다. 다른 경로를 쓸 때는 `.env`의 `NATIONAL_BUS_STOP_CSV_DIR`을 변경합니다.
3. 검증 후 v2에 적재합니다.

```powershell
.\.venv\Scripts\python.exe .\public_data.py validate-national
.\.venv\Scripts\python.exe .\public_data.py import-national
```

이 적재는 official Stop과 좌표를 보존하지만 historical occurrence와의 검증된 매핑을 자동 생성하지 않습니다. 현재 B1 공식 매핑과 historical 좌표 커버리지는 0입니다.

## 문서와 공개 제한

- [Neo4j v2 구조](docs/NEO4J_V2.md)
- [예측·경로 설계](docs/PREDICTION_DESIGN.md)
- [데이터 출처와 설정](docs/DATA_SOURCES.md)
- [프로젝트 실행 가이드](docs/PROJECT_GUIDE.md)

원본 historical CSV 3개의 제공 기관·원문 URL·재배포 라이선스는 현재 자료만으로 확인되지 않았습니다. 확인 전에는 CSV를 공개 저장소에 push하지 마세요. `.env`, 모델, 캐시, 로그, PID, 가상환경, Docker volume도 Git 대상이 아닙니다.
