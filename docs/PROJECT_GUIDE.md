# RideSure 프로젝트 가이드

## 1. 프로젝트 경계

RideSure는 2025 DSC 공유대학 KT 기업연계 오픈데이터 활용 스타트업 챌린지에서 제안한 B1 BRT 혼잡·탑승 불확실성 문제를 포트폴리오용으로 재구성한 FastAPI 데모다. 전국 단위 운영 서비스가 아니라, 제한된 공공데이터로도 추적 가능한 경로·혼잡 근거와 안전한 설명을 만드는 프로토타입이다.

원 competition prototype의 고정 응답은 현재 `main` 코드에서 Neo4j v2 조회로 교체됐다. v1 importer와 별도 Compose volume은 남아 있다. 새 클론에는 두 데이터베이스의 적재 상태가 포함되지 않는다.

## 2. 현재 아키텍처

```text
Browser / Kakao place search
  -> POST /api/predict (text, optional coordinates, date/time)
  -> service.py
       -> prediction_v2.py -> Neo4j v2 NEXT route + congestion evidence
       -> llm_server.py /generate -> EXAONE explanation
       -> deterministic explanation when EXAONE fails validation
  -> structured route cards + Kakao markers/polyline when coordinates exist

Historical CSV -> data_insert_v2.py -> isolated Neo4j v2
Optional official CSV/TAGO -> public_data.py -> official Stop/RouteStopStaging
```

역할 분리:

- Neo4j v2: line, pattern, ordered occurrence, direct topology, 관측 provenance
- Python repository/service: 후보 선택, 경로 ranking, percentile/등급, API 구조화
- EXAONE: 확정된 structured facts를 짧은 한국어로 표현
- Kakao Map: 사용자 위치 marker와 반환된 route geometry 시각화

EXAONE은 노선·정류장·혼잡 수치를 계산하거나 발명하지 않는다. LLM 서버를 별도 프로세스로 두어 수 GB 모델/GPU 수명주기를 웹 API와 분리한다.

## 3. API 사용

`POST /api/predict` 예시:

```json
{
  "origin": "대전역",
  "destination": "세종시청",
  "departure_time": "08:00",
  "date": "2025-11-08"
}
```

정류장 검색은 `GET /api/stops/search?q=대전&limit=10`을 사용한다. UI의 자동완성에서 정류장을 고르면 요청에 `origin_stop_id` 또는 `destination_stop_id`를 추가한다. 선택한 ID가 있으면 해당 Stop을 경로 정체성으로 사용하고, ID가 없으면 기존 이름 검색으로 조회한다. 텍스트를 다시 편집하면 UI는 저장한 ID를 해제한다.

위도/경도가 있을 때 `origin_lat`, `origin_lon`, `destination_lat`, `destination_lon`도 보낼 수 있다. 쌍의 한 값만 보내거나 범위를 벗어나면 422다. Text/date/time 검증을 통과했지만 정류장 또는 direct path가 없으면 400으로 명확히 실패한다.

응답은 최대 3개 direct route, occurrence 순서의 stops, historical/realtime evidence, `onboard_count`, 상대 percentile/등급, `boarding_guidance`, geometry와 explanation을 포함한다. 추천 외 direct pattern은 `alternatives`에도 요약된다.

Deprecated `boarding_probability`, `expected_load`, `travel_time`은 compatibility를 위해 남아 있지만 `null`이다. 이 데이터로 탑승 성공 확률이나 정원 대비 혼잡률을 만들지 않는다.

## 4. 데이터와 fallback

Neo4j v2는 세 historical CSV의 11,375행과 273,000개 시간대 셀을 모두 독립 `LoadObservation`으로 보존한다. 현재 셀은 모두 valid 0 이상 정수이며 loader는 향후 missing/invalid를 0과 구분한다.

혼잡 근거 우선순위:

```text
fresh RealtimeObservation
  -> exact historical LoadObservation
  -> existing LoadProfile
  -> UNKNOWN / 데이터 부족
```

Realtime importer와 profile 생성 batch는 아직 없으므로 실사용은 exact historical 또는 unknown이다. 예시로 B1 대전역 08시는 17명, 09시는 24명이다. 08시 상대 percentile 약 56.6은 `MEDIUM/보통`이며 탑승 확률이 아니다.

## 5. 좌표와 Kakao Map

브라우저는 Kakao place 검색 좌표를 API로 전달하고 출발·도착 marker를 표시한다. Backend는 좌표가 매핑된 routeable Stop이 있으면 2 km 이내 후보를 먼저 사용하고, 현재처럼 좌표 graph가 비어 있으면 정류장명 후보로 fallback한다.

Historical Stop에는 공식 ID/좌표 mapping이 아직 없어 기본 graph의 route geometry는 `UNAVAILABLE`이다. 선택 경로의 모든 occurrence에 검증된 좌표가 있을 때만 `STOP_TO_STOP_APPROXIMATION`을 반환하고 프런트가 polyline을 그린다. 이는 정류장 좌표를 순서대로 이은 선이지 공식 도로 shape나 실제 차량 궤적이 아니다.

전국 정류장 CSV/TAGO adapter는 구현되어 있다. 파일·키 유무는 `public_data.py status`로 각 컴퓨터에서 확인한다. Official 데이터를 적재해도 historical occurrence에 자동 이름 병합하지 않으므로 별도 검증 mapping이 필요하다.

## 6. 주요 파일

- `app.py`: FastAPI models, `/health`, `/api/predict`, frontend config/static 제공
- `service.py`: v2 route 후보와 결과 구성, EXAONE grounding/fallback
- `prediction_v2.py`: Neo4j read repository, direct route, geometry, congestion precedence
- `data_insert_v2.py`: 무손실 historical ETL, idempotent import/verify
- `public_data.py`: 선택적 전국 정류장 CSV/TAGO adapter와 CLI
- `llm_server.py`: 로컬 EXAONE `/health`, `/generate`
- `static/index.html`: 입력, Kakao 검색/지도, v2 결과 카드
- `compose.yaml`: 보존된 v1과 격리된 v2 Neo4j service/volume
- `scripts/setup.ps1`, `start.ps1`, `stop.ps1`, `verify.ps1`: 재현 가능한 운영·검증

Graph schema와 count는 [NEO4J_V2.md](NEO4J_V2.md), 계산 계약은 [PREDICTION_DESIGN.md](PREDICTION_DESIGN.md), 출처/설정은 [DATA_SOURCES.md](DATA_SOURCES.md)를 참고한다.

## 7. 새 PC 설치와 평상시 실행

```powershell
# 저장소 루트에서 실행
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

처음에는 `.env.example`을 `.env`로 복사하고 중단한다. 최소 `NEO4J_PASS`, `KAKAO_MAP_JAVASCRIPT_KEY`를 실제 값으로 바꾼 뒤 같은 setup 명령을 다시 실행한다. 기존 `.env`와 Docker volume은 덮어쓰거나 삭제하지 않는다.

Setup은 Python 3.12 venv, 고정 의존성/CUDA PyTorch, v1/v2 Neo4j, historical ETL, EXAONE snapshot/server, FastAPI와 통합 검증을 준비한다. CPU만 의도적으로 사용할 때는 `-AllowCpu`, 기존 model을 사용할 때는 필요에 따라 `-SkipModelDownload`를 쓴다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
```

서비스 주소는 웹/API docs/EXAONE이 각각 8000, 8000/docs, 8001이며 Neo4j v1/v2 Browser는 7474/7475다. `stop.ps1`은 이 프로젝트가 시작한 Python 프로세스와 컨테이너만 정지하며 volume을 삭제하지 않는다.

## 8. 선택적 공식 데이터 설정

상태 확인:

```powershell
.\.venv\Scripts\python.exe .\public_data.py status
```

TAGO는 data.go.kr에서 서비스 키와 공식 city/route ID를 얻고 저장소 루트의 `.env`에 아래 값을 넣는다.

```dotenv
DATA_GO_KR_SERVICE_KEY=<your key>
```

```powershell
.\.venv\Scripts\python.exe .\public_data.py fetch-tago --city-code <official-city-code> --route-id <official-route-id>
# 검증 후 staging 적재
.\.venv\Scripts\python.exe .\public_data.py fetch-tago --city-code <official-city-code> --route-id <official-route-id> --import-to-neo4j
```

전국 정류장 위치 CSV는 공식 data.go.kr 파일을 저장소 루트의 `data/public/national_bus_stops/*.csv`에 둔다.

```powershell
.\.venv\Scripts\python.exe .\public_data.py validate-national
.\.venv\Scripts\python.exe .\public_data.py import-national
```

상세 공식 링크와 구성 상태의 의미는 [DATA_SOURCES.md](DATA_SOURCES.md)에 있다. 실제 secret을 문서·로그·테스트에 넣지 않는다.

## 9. 테스트와 장애 확인

외부 서비스 없이 CSV와 단위 테스트만 확인하려면 저장소 루트에서 다음을 실행한다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-audit.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe .\data_insert_v2.py profile
```

전체 서비스 검증은 Neo4j, 모델, Kakao 설정을 완료한 뒤 실행한다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app.py service.py prediction_v2.py public_data.py tests
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1
```

`verify.ps1`은 config, Python/GPU/model, v1/v2 컨테이너와 graph count/B1 path, JavaScript, EXAONE/FastAPI/API를 검사한다. 외부 공식 data key/file 부재는 historical 동작을 막지 않는다.

장애 확인 순서:

1. `verify.ps1`의 첫 실패 단계와 `logs\*.stderr.log`를 확인한다.
2. `docker compose --profile v2 ps`와 각 Neo4j log를 확인한다.
3. 기존 volume 비밀번호가 `.env` 변경으로 바뀌지 않는 점을 확인하고 volume 삭제로 우회하지 않는다.
4. 지도만 실패하면 `/api/frontend-config`와 Kakao 허용 도메인을 확인한다.
5. 공식 데이터는 `public_data.py status`로 별도 확인한다.

## 10. 남은 제한

- B1 공식 route/stop ID와 historical occurrence mapping 미완료
- historical route coordinates 0, 기본 polyline 없음
- transfer routing, 공식 road shape, defensible travel time 미구현
- realtime importer/profile builder 미구현
- 탑승 확률 계산에 필요한 capacity/waiting/boarding ground truth 없음
- historical CSV 원 출처와 재배포 license 미확인

이 제한은 가짜 값을 만들어 숨기지 않고 API의 nullable/unknown 상태와 문서에 드러낸다.
