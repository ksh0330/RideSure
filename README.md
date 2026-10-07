# RideSure

**과거 버스 재차인원과 검증된 정류장 연결을 바탕으로 경로와 혼잡 근거를 함께 보여주는 대중교통 프로토타입**

RideSure는 2025 DSC 공유대학 KT 기업연계 오픈데이터 활용 스타트업 챌린지에서 출발했다. 당시 문제는 B1 BRT 승객이 탑승 전 혼잡을 판단하거나 대안을 찾기 어렵다는 것이었다. 대회 MVP의 개념을 보존하면서, 이후 포트폴리오 재구성에서 데이터 식별자·그래프 구조·경로 검증·지도 표현을 다시 만들었다. 현재 버전은 **2025-11-08로 표시된 historical 차내 재차인원 스냅샷**을 사용하는 데모이며 실시간 교통 서비스가 아니다.

## 문제와 원래 MVP

원래 MVP는 B1을 중심으로 Neo4j, 재차인원 기반 안내, 로컬 EXAONE 설명, 지도 화면을 결합한 서비스 프로토타입이었다. 제한된 공공데이터와 개발 기간 때문에 일부 경로·혼잡 결과는 단순화되어 있었다. 아래의 재현 가능한 ETL, occurrence 단위 경로, 공식 정류장 검증, 1회 환승, Kakao BUS geometry는 **대회 이후 포트폴리오 재구성 작업**이다.

## 데이터와 Knowledge Graph 재설계

세 historical CSV의 원본 11,375행은 각 행에 24개 시간대 값을 갖는다. 이를 손실 없이 적재해 `LoadObservation` **273,000개**를 만들었다. 초기 v1 키를 다시 분석했을 때 collision key **83,688개**, conflicting key **33,650개**가 확인되어, 정류장 이름과 시간만으로 관측을 식별하는 방식은 안전하지 않았다. v2 observation ID는 원본 파일 hash·행·시간에서 만들고 출처 필드를 보존한다.

정류장 이름은 방향, 순서, 반복 방문을 충분히 식별하지 못한다. 그래서 historical `Stop`과 특정 노선 패턴의 `StopOccurrence`를 분리하고, 이동 순서를 `NEXT`로 표현한다.

```text
(Line)-[:HAS_PATTERN]->(RoutePattern)
(RoutePattern)-[:HAS_OCCURRENCE]->(StopOccurrence)
(StopOccurrence)-[:AT_STOP]->(Stop)
(StopOccurrence)-[:NEXT]->(StopOccurrence)
(LoadObservation)-[:OBSERVED_AT]->(StopOccurrence)
(LoadObservation)-[:ON_LINE]->(Line)
(LoadObservation)-[:IMPORTED_IN]->(ImportBatch)
```

이 구조는 B1의 연속된 `오송역2.3.4` 두 occurrence도 서로 다른 순서·ID로 보존한다. 검증된 v2 historical graph는 `Line` **148**, `RoutePattern` **154**, historical `Stop` **2,049**, `StopOccurrence` **11,375**, `LoadObservation` **273,000**, `ImportBatch` **3**개다.

## 공식 정류장 검증과 경로

TAGO의 현재 정류장은 historical 이름 기반 `Stop`과 합치지 않는다. 노선 ID, 방향, 전체 순서, 앞뒤 정류장을 검토한 경우에만 historical occurrence에서 공식 `Stop`으로 `VERIFIED_OFFICIAL_STOP` 관계를 둔다. 해당 관계의 좌표만 지도와 경로 결과에 사용한다.

B1은 historical **53 occurrence**와 현재 TAGO **55 정류장**을 비교했다. 보수적인 자동 정렬은 **41개**를 검증했다. `세종시청.교육청.시의회`의 양방향 **2개**는 같은 기관명들의 표시 순서 차이와 방향·앞뒤 sequence를 사람 검토로 확인하고 `HUMAN_REVIEWED_SEQUENCE` 근거를 명시했다. 따라서 최종 **43/53**이 검증되었고, 근거가 부족한 **10개는 미해결 상태**로 남겼다. 현재 노선에만 있는 두 정류장에는 historical 관측을 만들지 않았다.

RideSure는 historical `NEXT`를 따라 직행 경로를 조회한다. 직행이 없으면 서로 다른 `RoutePattern`의 occurrence가 **동일한 공식 물리 정류장**에 검증된 경우에만 1회 환승을 연결한다. 예: `대평동(해들마을) → 1000 → 세종고속시외버스터미널 → 1004 → 첫마을3단지`. 이름 유사도, 좌표 거리, 도보 연결로 환승을 추정하지 않는다.

## Kakao 지도와 EXAONE의 역할

**노선 선택은 RideSure/Neo4j가 수행한다.** Kakao 대중교통 응답은 선택한 구간의 BUS 노선명, 승하차 정류장명, 검증된 양 끝 좌표의 위치가 일치할 때만 경로 선으로 사용한다. 검증되지 않거나 좌표가 없는 구간에는 가짜 직선을 그리지 않는다. 사용자가 보는 출발·도착 표식도 실제 선택된 occurrence의 좌표가 있을 때만 놓는다.

```text
Neo4j 경로·관측 조회 → Python의 결정론적 혼잡 계산
                    → 선택적 EXAONE 설명 또는 규칙 기반 설명
                    → 검증된 Kakao BUS geometry 표시
```

EXAONE은 **선택적 설명 계층**이다. 경로를 만들거나 혼잡 수치를 계산하지 않는다. 모델 서버가 없어도 구조화된 결과와 규칙 기반 안내가 동작한다.

## 검증 결과

Windows, Python 3.12, Docker Desktop, Neo4j 5.26 Community에서 **fresh clone → 새 venv → 빈 Neo4j volume → 저장소의 CSV/TAGO snapshot만으로 재구축**을 확인했다. `python -m scripts.prepare_demo_data`는 첫 실행 약 **2분 10초**, 재실행 약 **34초**였다. 소요 시간은 컴퓨터 환경에 따라 달라진다. 두 실행 모두 historical graph 개수와 `RouteStopStaging` **210**, `VERIFIED_OFFICIAL_STOP` **162**를 유지했다.

`data_insert_v2.py verify`는 `complete` 상태와 incomplete batch·비정상 좌표·orphan·provenance·sequence 오류 **각 0건**을 확인했다. 자동 검증은 **112 pytest tests와 9 subtests**가 통과했다. 브라우저에서는 B1 `대전역 → 세종시청.교육청.시의회`, 1000 `두루초.중학교 → 조형아파트`, 1000→1004 1회 환승에서 실제 Kakao BUS 도로 경로 선을 확인했다.

## 데이터 범위와 한계

- `onboard_count`는 해당 날짜·시간의 **차내 재차인원**이다. 이를 탑승 성공 확률이나 차량 정원 대비 혼잡률로 바꾸지 않는다.
- 임의 날짜의 혼잡 예측, 이동 시간 예측, 상용 수준의 최적 경로 추천은 제공하지 않는다. 현재 후보 순위는 정류장 후보와 hop 수 중심이다.
- 실시간 관측 importer와 도보 환승·2회 이상 환승은 없다. 근거가 없으면 `UNKNOWN / 데이터 부족`으로 표시한다.
- official mapping이 없는 historical occurrence는 좌표가 없다. B1에서도 10개가 미해결이다.
- 저장소 CSV의 개별 원 출처와 재배포 조건을 공공데이터포털 항목에 일대일로 연결하는 기록은 없다. 확인 범위는 [데이터 출처](docs/DATA_SOURCES.md)에 명시했다.

## 기술적 의의와 개선 방향

이 재구성의 핵심은 정류장명만으로는 잃기 쉬운 **방향·방문 순서·반복·관측 출처**를 식별자로 되살리고, 불확실한 공식 정류장 대응과 지도 경로를 검증 경계 밖에 두는 것이다. 빈 DB에서도 같은 저장소 데이터로 그래프를 재구축하고 무결성을 확인할 수 있다. 후속 개선은 미해결 occurrence의 물리 정류장 근거, CSV의 원 출처·재배포 조건, 신뢰할 수 있는 실시간 관측 및 탑승 결과 근거가 확보될 때에만 검토한다.

## Quick Start

Python 3.12와 Docker가 필요하다. `.env`에 로컬 Neo4j 비밀번호를 설정한다. Kakao JavaScript key는 지도, REST key는 검증된 대중교통 경로 선에 사용한다. 저장된 TAGO snapshot으로 데모를 준비할 때 `DATA_GO_KR_SERVICE_KEY`, EXAONE 모델, GPU는 필요하지 않다.

```powershell
git clone https://github.com/ksh0330/RideSure.git
cd RideSure
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-demo.txt
Copy-Item .env.example .env  # NEO4J_PASS와 사용할 Kakao 키 설정
docker compose --profile v2 up -d --wait neo4j-v2
.\.venv\Scripts\python.exe -m scripts.prepare_demo_data
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

브라우저 주소는 <http://127.0.0.1:8000/>이다. Kakao Developers에 **이 주소의 origin**을 등록해야 한다. 운영체제별 명령, 검증, 종료, 문제 해결은 [RUNBOOK](docs/RUNBOOK.md)에 있다.

## 프로젝트 기여와 문서

대회 당시 역할은 팀장으로서 문제 정의, 서비스 기획, 그래프 구조 설계, 로컬 SLM 활용 방향, 검증 및 발표 조율이었다. 이 저장소는 그 MVP를 바탕으로 한 **후속 포트폴리오 재구성 결과**다.

- [데이터 출처와 재현 범위](docs/DATA_SOURCES.md)
- [Neo4j v2 구조](docs/NEO4J_V2.md)
- [공식 정류장 mapping](docs/OFFICIAL_STOP_MAPPING.md)
- [경로·혼잡 설계](docs/PREDICTION_DESIGN.md)
- [실행 RUNBOOK](docs/RUNBOOK.md)
