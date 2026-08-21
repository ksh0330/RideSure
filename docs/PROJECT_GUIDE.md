# RideSure 프로젝트 가이드

## 1. 프로젝트의 실제 상태

RideSure v0.1-demo는 복구된 시연 코드입니다. 데이터 적재와 조회용 함수, 로컬 LLM 서버, 웹 UI는 동작하지만 사용자 입력을 이용해 경로와 혼잡도를 계산하는 예측 파이프라인은 아직 연결되지 않았습니다.

현재 고정된 값은 `service.py`의 `BusPredictionService.predict_boarding()`에 있습니다.

- 출발/도착: 대전역 → 세종시청,시의회,교육청
- 경로: B1과 `101 / 1002 (환승 1회)` 두 개
- 각 경로의 정류장 목록, 승객 수, 탑승 확률, 소요 시간
- 사용자 날짜: 사용되지 않음
- 사용자 출발 시각: 계산에는 사용되지 않고 EXAONE 설명 프롬프트에만 포함

`find_nearest_stop()`, `find_routes_between_stops()`, `get_route_load_data()`, `calculate_boarding_probability()` 함수는 존재하지만 현재 `predict_boarding()`에서 호출되지 않습니다. 이 경계를 바꾸는 작업은 별도의 `feature/neo4j-prediction` 기능으로 설계·검증해야 하며, 데모 복구 작업에 임의 예측 알고리즘을 추가하지 않았습니다.

## 2. 전체 실행 흐름

```text
사용자 브라우저 (static/index.html)
  ├─ GET /api/frontend-config ──> app.py ──> .env의 Kakao JavaScript 키
  ├─ Kakao SDK ──> 입력 텍스트 지오코딩/지도 마커
  └─ POST /api/predict ──> app.py ──> service.py
                                      ├─ 고정 경로/확률/승객 수 구성
                                      ├─ POST /generate ──> llm_server.py ──> EXAONE
                                      └─ 고정 결과 + 생성된 설명 반환

CSV 3개 ──> data_insert.py ──> Neo4j
app.py /health ──> service.py ──> Neo4j 연결 확인
service.py의 조회 함수 ──> Neo4j (현재 예측 경로에서는 호출되지 않음)
```

버튼을 누르면 브라우저는 두 작업을 시작합니다. 카카오 SDK는 입력한 위치를 검색해 지도 마커를 옮깁니다. 동시에 `/api/predict`가 입력값을 서버로 보내지만, 서비스는 현재 이를 고정 결과 계산에 사용하지 않습니다. EXAONE은 고정 경로·승객 수·확률을 담은 프롬프트를 받아 설명문을 생성합니다. LLM이 실패하면 같은 고정 결과에 대한 규칙 기반 문장이 대체됩니다.

## 3. 주요 Python 파일

- `app.py`: 웹 정적 파일, `/api/frontend-config`, `/health`, `/api/predict`, Swagger 문서를 제공하는 경량 FastAPI 서버입니다.
- `service.py`: FastAPI와 Neo4j/LLM을 잇는 서비스 계층입니다. Neo4j 조회 함수와 휴리스틱 함수가 있지만 현재 예측 응답은 이 파일에 고정돼 있습니다.
- `config.py`: 프로젝트 루트 `.env`를 로드하고 설정 그룹을 값 노출 없이 검증합니다. Neo4j 비밀번호 기본값은 없습니다.
- `llm_server.py`: EXAONE 토크나이저/모델을 한 번 GPU에 로드하고 `/health`, `/generate`를 제공하는 별도 FastAPI 서버입니다. 혼잡도 예측 모델이 아니라 설명문 생성기입니다.
- `data_insert.py`: 세 CSV의 스키마를 검사하고 wide 형식의 24시간 열을 Load 노드로 펼쳐 Neo4j에 `MERGE`합니다. 빈/완전/부분 DB를 판정하고 기준 개수와 관계를 검증합니다.

`app.py`와 `llm_server.py`가 별도 프로세스인 이유는 EXAONE의 수 GB 모델과 GPU 수명주기를 웹/API 프로세스와 분리하기 위해서입니다. 앱을 재시작해도 모델 프로세스를 별도로 관리할 수 있고, 앱은 HTTP로 설명 생성을 요청하며 health 상태를 독립 확인합니다. 두 프로세스가 논리적으로 필수라기보다 현재 구조에서 무거운 GPU 모델을 한 번만 로드하고 장애 경계를 분리하기 위한 선택입니다.

## 4. Neo4j, 컨테이너, 볼륨, CSV

Neo4j는 노드와 관계로 데이터를 저장하는 그래프 데이터베이스입니다. 이 프로젝트는 다음 스키마를 사용합니다.

```text
(Line)-[:HAS_STOP {seq}]->(Stop)
(Load {date, hour, count})-[:AT]->(Stop)
(Load)-[:AFFECTS]->(Line)
```

`Line.id`, `Stop.id`, `Load.id`에는 고유 제약이 있습니다. Load ID는 `노선|정류장|날짜|시간`으로 안정적으로 만들어져 같은 CSV를 다시 처리해도 새 노드나 관계가 중복 생성되지 않습니다.

- `compose.yaml`: Neo4j 컨테이너를 어떻게 만들지 정의하는 재현 가능한 설정입니다.
- Docker 컨테이너: Neo4j 프로그램이 실행되는 교체 가능한 프로세스/파일시스템입니다.
- Docker 명명 볼륨: 실제 `/data`와 `/logs`가 남는 별도 저장소입니다. 컨테이너를 멈추거나 재생성해도 볼륨은 유지됩니다.

Compose는 고정 `container_name`이나 고정 외부 볼륨 이름을 쓰지 않습니다. 볼륨의 실제 이름은 Compose 프로젝트명에 따라 `<project>_neo4j_data`처럼 만들어집니다. 따라서 별도 디렉터리 또는 `COMPOSE_PROJECT_NAME`을 사용하면 기존 환경과 테스트 환경이 공유되지 않습니다. 어떤 스크립트도 `docker compose down -v`를 실행하지 않습니다.

기존 볼륨은 최초 생성 시의 Neo4j 인증정보를 보존합니다. `.env`의 `NEO4J_PASS`를 바꾸고 컨테이너 환경을 다시 주입해도 기존 DB 사용자의 비밀번호는 바뀌지 않습니다. health 실패 시 볼륨을 삭제하지 말고 기존 인증정보를 복구하거나 Neo4j 공식 비밀번호 변경 절차를 사용해야 합니다.

CSV 상세와 라이선스 상태는 `docs/DATA_SOURCES.md`에 있습니다.

## 5. 설정 파일과 비밀정보

- `.env`: 현재 PC에서만 쓰는 실제 키, 비밀번호, 포트, 모델/캐시 설정입니다. Git 제외 대상입니다.
- `.env.example`: 필요한 변수 이름과 안전한 기본/플레이스홀더만 보여 주는 추적 파일입니다.
- `.gitignore`: `.env`, 가상환경, 모델, 캐시, 로그, PID, 임시 파일을 Git 대상에서 제외합니다.

카카오 JavaScript 키는 브라우저가 받아야 하므로 비밀값으로 숨길 수 없습니다. 소스/Git에 직접 넣지 않고 런타임에 전달하되 Kakao Developers에서 허용 도메인을 제한합니다. Neo4j 비밀번호는 브라우저로 전달되지 않습니다.

주요 환경변수:

| 그룹 | 변수 | 사용 위치 |
|---|---|---|
| Neo4j | `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASS` | Compose, service, ETL, 검증 |
| Neo4j 포트 | `NEO4J_HTTP_PORT`, `NEO4J_BOLT_PORT` | Compose 호스트 포트 |
| 모델 | `MODEL_ID`, `HF_HOME`, `MODEL_PATH`, `HF_HUB_OFFLINE`, `MAX_NEW_TOKENS` | 모델 다운로드/LLM 서버 |
| 서버 | `LLM_BASE_URL`, `APP_HOST`, `APP_PORT`, `LLM_HOST`, `LLM_PORT` | 두 FastAPI 프로세스/검증 |
| 프런트 | `KAKAO_MAP_JAVASCRIPT_KEY` | `/api/frontend-config`와 지도 SDK |
| 데이터 | `TARGET_DATE` | API 기본 요청 날짜(현재 계산에는 미사용) |

## 6. 새 PC 설치

1. 저장소를 내려받고 PowerShell에서 프로젝트 루트로 이동합니다.
2. Docker Desktop을 실행합니다.
3. `powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1`을 실행합니다.
4. `.env`가 새로 생기면 비밀번호와 카카오 JavaScript 키를 채웁니다. 기존 `.env`는 덮어쓰지 않습니다.
5. 같은 setup 명령을 다시 실행합니다.

setup은 Python 3.12 격리 가상환경, 고정 패키지, CUDA 12.8 PyTorch, Neo4j healthy 대기, DB 상태 분류/빈 DB 적재, 모델 다운로드, CUDA 모델 로드, 최소 추론과 전체 API 검증을 수행합니다. 완전 DB에서는 적재를 생략하고 부분 DB에서는 중단합니다. 성공하면 서비스가 실행 중입니다.

모델을 이미 내려받아야 하는 폐쇄 환경에서는 `.env`의 `HF_HUB_OFFLINE=1`을 사용합니다. 캐시가 불완전하면 검증이 실패합니다. CPU 실행은 매우 느리며 의도적으로 사용할 때만 setup/verify에 `-AllowCpu`를 전달합니다.

## 7. 평상시 시작과 종료

`scripts/start.ps1`은 Neo4j를 먼저 healthy까지 시작하고 EXAONE 서버, 앱 서버 순으로 별도 숨김 프로세스를 띄웁니다. `.run`의 PID와 실제 명령줄을 함께 검사해 중복 실행과 다른 프로세스 인수를 방지하며, stdout/stderr는 `logs`에 분리합니다.

`scripts/stop.ps1`은 PID와 스크립트 경로가 일치하는 이 프로젝트 프로세스만 종료합니다. PID 파일이 없는 임의 Python 프로세스는 건드리지 않습니다. 마지막에 `docker compose stop neo4j`만 실행하므로 컨테이너와 모든 명명 볼륨은 남습니다.

## 8. 검증과 테스트

전체 통합 검증:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1
```

검증 항목은 `.env` 필수 변수(값 미출력), Python 3.12, 패키지 버전, PyTorch CUDA 빌드, GPU, 로컬 모델 스냅샷, Neo4j 컨테이너/데이터/관계/대표 조회, LLM health와 최소 추론, FastAPI health/docs, frontend-config, 지도 SDK 설정 경로, 고정 데모 예측 스모크, Node 기반 인라인 JavaScript 문법입니다. 실패하면 종료 코드 1입니다.

외부 서버 없이 실행하는 단위 테스트:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app.py service.py config.py llm_server.py data_insert.py scripts tests
```

실제 Neo4j/LLM/FastAPI를 쓰는 검증은 `verify.ps1`이 담당합니다. `data_insert.py --status`는 DB를 읽기만 하며 완전 상태가 아니면 0이 아닌 종료 코드를 반환합니다. `data_insert.py` 기본 실행도 완전 DB에서는 적재를 건너뛰고, 부분 상태에서는 중단합니다.

## 9. 장애 확인 순서

1. `scripts\verify.ps1`의 첫 실패 단계와 종료 코드를 확인합니다.
2. `docker info`, `docker compose ps`, `docker compose logs neo4j`로 Docker/Neo4j를 확인합니다.
3. 기존 볼륨 인증 오류라면 `.env` 변경으로 DB 비밀번호가 바뀌지 않는다는 점을 확인합니다. 볼륨 삭제로 우회하지 않습니다.
4. `logs\llm_server.stderr.log`에서 CUDA/모델 캐시 오류를 확인합니다.
5. `logs\app.stderr.log`에서 Neo4j/LLM 연결 오류를 확인합니다.
6. 지도만 실패하면 `/api/frontend-config`, 카카오 허용 도메인, 인터넷 연결을 확인합니다. 키 값을 로그나 이슈에 붙이지 않습니다.
7. 부분 DB로 판정되면 자동 재적재/삭제하지 말고 별도 백업 후 원인을 조사합니다.

## 10. 파일 트리

```text
RideSure/
├─ app.py                         웹/API FastAPI 서버
├─ service.py                     고정 데모 응답, Neo4j 조회 함수, LLM 호출
├─ config.py                      .env 로드와 값 미노출 검증
├─ llm_server.py                  EXAONE 설명문 생성 서버
├─ data_insert.py                 CSV ETL, DB 상태 분류와 검증
├─ compose.yaml                   Neo4j 컨테이너/명명 볼륨
├─ requirements.txt               검증된 Python 패키지 버전
├─ requirements-lock.txt          콜드 스타트에서 고정한 전체 의존성
├─ requirements-cuda.txt          CUDA 12.8 PyTorch 버전
├─ .env.example                   안전한 설정 예시
├─ .gitignore                     비밀/대용량/런타임 파일 제외
├─ README.md                      사용자 빠른 시작
├─ doit.txt                       네 명령 요약 메모
├─ static/
│  └─ index.html                  입력 UI, 결과 카드, 카카오 지도
├─ scripts/
│  ├─ setup.ps1                   최초 설치와 최종 통합 검증
│  ├─ start.ps1                   전체 서비스 시작
│  ├─ stop.ps1                    프로젝트 소유 프로세스 안전 종료
│  ├─ verify.ps1                  상태/구성/스모크 검증
│  ├─ common.ps1                  PowerShell 공통 PID/health 함수
│  ├─ checks.py                   값 미노출 Python 검증 CLI
│  └─ download_model.py           Hugging Face 모델 준비
├─ tests/
│  └─ test_unit.py                외부 서버 없는 단위 테스트
├─ docs/
│  ├─ PROJECT_GUIDE.md            이 문서
│  └─ DATA_SOURCES.md             CSV 스키마/해시/라이선스 상태
└─ 노선·정류장 지표… (1~3).csv    복구된 원본 데이터 분할 파일
```

`.venv`, `.cache`, `.run`, `logs`, `.env`, Docker 볼륨은 실행 중 생성되지만 Git 파일 트리에는 포함되지 않습니다.

## 11. 실제 예측 구현의 다음 단계

후속 `feature/neo4j-prediction`에서는 요구사항과 정답 기준을 먼저 정한 뒤 다음을 별도 테스트와 함께 연결해야 합니다.

1. 입력 위치를 정류장 ID에 안정적으로 매핑하고 동명이인/검색 순서를 처리합니다.
2. HAS_STOP의 방향과 정류장 순번으로 실제 이동 가능한 노선을 찾습니다.
3. 요청 날짜/시간의 Load를 노선·정류장에 결합합니다.
4. 탑승 확률의 의미, 학습/휴리스틱 근거, 평가 지표를 정의합니다.
5. 누락 데이터와 환승을 처리하고 결과가 입력에 따라 달라지는 테스트를 추가합니다.
6. 계산 결과만 EXAONE에 전달하고, LLM이 수치를 발명하지 못하게 응답을 검증합니다.

그 전까지 UI/API/문서는 반드시 “고정 데모”라고 표시해야 합니다.
