# RideSure

> **B1 BRT의 노선·정류장·시간대별 차내 재차인원 데이터를 Knowledge Graph로 구조화해, 데이터 근거가 있는 혼잡 안내와 경로 추천을 제공하는 대중교통 프로토타입**

RideSure는 **2025 DSC 공유대학 KT 기업연계 오픈데이터 활용 스타트업 챌린지**에서 시작한 프로젝트입니다.  
초기 대회 버전은 제한된 데이터와 구현 기간으로 인해 일부 경로·혼잡 안내가 단순화되어 있었고, 이후 포트폴리오 프로젝트로 재구성하면서 **Neo4j 데이터 모델과 ETL을 다시 설계하고 실제 관측 데이터에 근거한 경로 탐색·혼잡 안내 구조로 개선**했습니다.

---

## Problem

B1 BRT는 대전·세종·오송을 연결하는 광역 교통수단이지만, 사용자는 탑승 전에 특정 시간대와 정류장에서의 혼잡 정도를 판단하기 어렵습니다.

기존 지도 서비스는 이동 경로와 도착 정보는 제공하지만, 프로젝트에서 확보한 데이터 기준으로는 다음 정보를 직접 활용하기 어려웠습니다.

- 특정 시간대의 차내 재차인원
- 혼잡 상황을 고려한 탑승 판단
- 혼잡한 경우의 대안 경로
- 데이터 근거를 설명하는 자연어 안내

RideSure는 단순히 버스 노선을 보여주는 것이 아니라, **노선 구조와 과거 승객 관측 데이터를 함께 활용해 혼잡을 고려한 이동 판단을 지원하는 것**을 목표로 했습니다.

---

## Solution

사용자가 출발지, 도착지, 날짜와 시간을 입력하면 RideSure는 다음 순서로 결과를 생성합니다.

```text
User Input
    ↓
FastAPI
    ↓
Neo4j Knowledge Graph
    ↓
Route Search + Congestion Logic
    ↓
EXAONE Local SLM
    ↓
Kakao Map + User Guidance
```

1. Neo4j에서 방향성을 가진 직행 경로를 탐색합니다.
2. 해당 RoutePattern과 StopOccurrence의 시간대별 차내 재차인원을 조회합니다.
3. 관측값을 같은 조건의 분포와 비교해 상대 혼잡 수준을 계산합니다.
4. EXAONE은 이미 계산된 구조화 결과만 전달받아 짧은 자연어 안내를 생성합니다.
5. Kakao Map에서 출발지·도착지와 사용 가능한 경로 정보를 시각화합니다.

**EXAONE이 노선이나 혼잡도를 계산하지 않습니다.**  
경로와 혼잡 정보는 Neo4j 및 Python 로직에서 결정하고, SLM은 확정된 사실을 설명하는 역할만 담당합니다.

---

## Key Engineering Challenges

### 1. 정류장 이름만으로는 실제 노선 순서를 표현하기 어려웠다

초기 그래프에서는 동일한 정류장명이 하나의 Stop으로 병합되면서 노선 방향과 반복 등장하는 정류장을 구분하기 어려웠습니다.

이를 해결하기 위해 그래프를 다음 구조로 재설계했습니다.

```text
Line
 └─ RoutePattern
      └─ StopOccurrence
           ├─ AT_STOP → Stop
           └─ NEXT → StopOccurrence
```

`StopOccurrence`를 별도로 두어 같은 이름의 정류장이 한 노선에서 여러 번 등장해도 각 occurrence와 순서를 독립적으로 보존했습니다.  
또한 `NEXT` 관계를 통해 실제 방향성을 가진 경로 탐색이 가능하도록 구성했습니다.

### 2. 기존 Observation Key에서 데이터 충돌 가능성이 있었다

초기 ETL은 노선·정류장·날짜·시간 중심의 key를 사용해 반복 occurrence나 중복 원본 행에서 관측값이 덮어써질 수 있었습니다.

v2 ETL에서는 **원본 파일 hash + source row + hour**를 기반으로 안정적인 Observation ID를 생성했습니다.  
이를 통해 각 시간대 관측값을 원본 위치까지 추적할 수 있도록 개선했습니다.

### 3. 실시간 데이터가 없어도 서비스가 동작해야 했다

RideSure는 실시간 혼잡 API가 항상 존재한다는 것을 전제로 하지 않습니다.

```text
Realtime Observation
        ↓
Exact Historical Observation
        ↓
Historical Profile
        ↓
UNKNOWN
```

실시간 값이 없으면 historical evidence를 사용하고, 근거가 없으면 임의의 결과를 생성하지 않고 `UNKNOWN / 데이터 부족` 상태를 반환합니다.

### 4. SLM이 교통 정보를 만들어내지 않도록 했다

EXAONE에는 노선, 재차인원, 혼잡 수준 등 애플리케이션이 계산한 사실만 전달합니다.

모델이 지원되지 않는 숫자나 탑승 확률 등을 생성할 경우 해당 결과를 사용하지 않고, 결정론적인 안내 문장으로 fallback하도록 구성했습니다.

---

## Engineering Improvements

| 초기 프로토타입 | 개선 버전 |
|---|---|
| 일부 고정된 데모 결과 | Neo4j 기반 실제 경로 조회 |
| 정류장 중심 그래프 | `RoutePattern` + `StopOccurrence` 구조 |
| 반복 정류장 구분 어려움 | occurrence 단위 독립 보존 |
| Observation key 충돌 가능 | 원본 provenance 기반 ID |
| 단순 혼잡 수치 | 차내 재차인원 + 상대 percentile |
| 데이터가 없어도 결과 제공 가능 | `UNKNOWN / 데이터 부족` 반환 |
| SLM 중심 설명 | 계산과 자연어 설명 계층 분리 |
| 제한적인 정류장 정보 | 공공데이터 기반 공식 ID·좌표 연동 구조 추가 |

---

## Results

### 데이터 구조 개선

원본 historical 데이터:

- 3개 CSV
- **11,375개** 노선·정류장 행
- 각 행의 24개 시간대 관측

Neo4j v2 변환 결과:

- **148 Lines**
- **154 RoutePatterns**
- **2,049 Stops**
- **11,375 StopOccurrences**
- **273,000 LoadObservations**

원본 11,375개 행의 시간대 데이터를 273,000개 Observation으로 보존해 각 관측값의 provenance를 추적할 수 있도록 구성했습니다.

### B1 경로 검증

B1 노선에서 다음 방향의 경로 탐색을 검증했습니다.

```text
대전역 → 세종시청
세종시청 → 대전역
```

방향에 따라 서로 다른 StopOccurrence sequence를 사용하며, `오송역2.3.4`처럼 동일 정류장이 연속해서 등장하는 경우도 각각 독립 occurrence로 보존합니다.

또한 동일 정류장에서도 시간대가 달라지면 서로 다른 historical onboard count를 조회하도록 구현했습니다.

### 데이터 기반 혼잡 안내

`onboard_count`는 **차내 재차인원**으로 사용합니다.

이를 차량 정원 대비 혼잡률이나 실제 탑승 성공 확률로 임의 변환하지 않고, 같은 RoutePattern과 시간대의 관측값과 비교해 상대 percentile과 혼잡 수준을 계산합니다.

---

## Public Data

### 차내 재차인원

- 제공기관: **국토교통부**
- 데이터: 노선별 재차인원 / 노선·정류장 지표(노선별 차내 재차인원)
- 수집 시스템: **교통카드빅데이터시스템(STCIS)**
- 활용 항목: 노선, 기종점, 정류장 순번, 정류장명, 시간대별 차내 재차인원

관련 데이터:
- [공공데이터포털 - 국토교통부 노선별 재차인원 현황](https://www.data.go.kr/data/15071617/fileData.do)
- [교통카드빅데이터시스템(STCIS)](https://www.stcis.go.kr/)

### 전국 버스정류장 위치정보

국토교통부 전국 버스정류장 위치정보를 활용해 공식 정류장 ID와 WGS84 좌표를 적재할 수 있도록 adapter를 구현했습니다.

- [공공데이터포털 - 전국 버스정류장 위치정보](https://www.data.go.kr/data/15067528/fileData.do)

### TAGO 버스노선정보

TAGO 버스노선정보 API를 통해 공식 route ID, 정류장 ID, 정류장 순서, 방향 및 좌표를 조회할 수 있도록 구성했습니다.

- [공공데이터포털 - TAGO 버스노선정보](https://www.data.go.kr/data/15098529/openapi.do)

공식 데이터와 기존 historical 데이터는 **이름만으로 자동 병합하지 않고, 검증된 경우에만 연결**하는 것을 원칙으로 합니다.

---

## My Role

- 프로젝트 팀장
- 문제 정의 및 서비스 기획
- 노선·정류장·시간대 데이터의 관계 구조 정의
- Neo4j Knowledge Graph 구조 설계 및 개선 방향 수립
- 로컬 SLM 활용 구조 설계
- 기능 검증 및 결과 비교
- 팀 작업 조율 및 최종 발표

---

## Tech Stack

**Backend**  
`Python` · `FastAPI` · `Pydantic`

**Data / Graph**  
`Neo4j` · `pandas` · `Public Data API`

**AI**  
`EXAONE 4.0 1.2B`

**Frontend**  
`JavaScript` · `Kakao Maps JavaScript SDK`

**Infrastructure**  
`Docker Compose` · `PowerShell`

---

## Limitations

현재 버전은 전국 단위 상용 대중교통 내비게이션이 아니라 **포트폴리오용 프로토타입**입니다.

- Neo4j에서 탐색 가능한 직행 경로 중심이며 전체 환승 경로 탐색은 지원하지 않습니다.
- 실시간 Observation을 사용할 수 있는 구조는 준비되어 있지만 production realtime importer는 구현 범위에 포함하지 않았습니다.
- 차내 재차인원만으로 실제 탑승 성공 확률을 계산할 수 없기 때문에 `boarding_probability`를 임의 생성하지 않습니다.
- 공식 road shape가 없는 경우 지도 경로는 정류장 좌표를 연결한 근사 경로만 사용할 수 있습니다.
- 공공데이터와 historical 데이터의 정류장 매핑은 검증된 경우에만 적용합니다.

---

## Run Locally

### Requirements

- Python 3.12
- Docker Desktop / Docker Compose
- Windows PowerShell
- Node.js
- Kakao Maps JavaScript Key
- NVIDIA CUDA 환경 (EXAONE GPU 추론 사용 시)

### Environment

`.env.example`을 `.env`로 복사한 뒤 최소한 다음 값을 설정합니다.

```dotenv
NEO4J_PASS=<local Neo4j password>
KAKAO_MAP_JAVASCRIPT_KEY=<Kakao JavaScript key>
```

실제 API Key와 비밀번호가 포함된 `.env`는 Git에 포함하지 않습니다.

### Start

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

### Verify

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1
```

### Stop

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
```

### Local Services

```text
Demo UI        http://127.0.0.1:8000
API Docs       http://127.0.0.1:8000/docs
Health Check   http://127.0.0.1:8000/health
Neo4j v2       http://127.0.0.1:7475
```

---

## Documentation

세부 설계와 데이터 구조는 `docs/`에서 확인할 수 있습니다.

- [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) — 데이터 출처 및 활용 범위
- [`docs/NEO4J_V2.md`](docs/NEO4J_V2.md) — Knowledge Graph 구조
- [`docs/PREDICTION_DESIGN.md`](docs/PREDICTION_DESIGN.md) — 경로 및 혼잡 안내 설계
- [`docs/PROJECT_GUIDE.md`](docs/PROJECT_GUIDE.md) — 실행 및 개발 가이드
