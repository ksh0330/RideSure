# RideSure 예측·경로 설계

## 구현된 요청 흐름

`/api/predict`는 로컬에 적재된 Neo4j v2 topology와 historical 관측을 조회한다. 새 클론에는 DB가 포함되지 않으므로 ETL을 먼저 실행해야 한다.

```text
origin/destination text + optional Kakao coordinates + date/time
  -> routeable stop candidates (nearby when mapped, name fallback otherwise)
  -> RoutePattern 안의 directed NEXT traversal
  -> 최대 3개 direct pattern 후보
  -> fresh realtime / exact historical / LoadProfile / UNKNOWN
  -> structured route and congestion facts
  -> grounded EXAONE explanation or deterministic fallback
  -> Kakao markers and optional stop-to-stop polyline
```

기본 ETL의 historical Stop에는 좌표가 없으므로 정류장명 검색 fallback을 사용한다. 좌표가 전달되어도 매핑된 routeable Stop이 없으면 좌표를 조작하지 않고 텍스트 후보를 찾는다.

## API 계약

`POST /api/predict` 입력:

- 필수: `origin`, `destination`
- 선택: `departure_time` (`HH:MM`, 기본 09시), `date` (`YYYY-MM-DD`, 기본 `TARGET_DATE`)
- 선택: `origin_lat`/`origin_lon`, `destination_lat`/`destination_lon`
- 선택: 검색 결과에서 고른 `origin_stop_id`, `destination_stop_id`
- 각 좌표 쌍은 함께 제공해야 하며 위도 `-90..90`, 경도 `-180..180`을 검증한다.

`GET /api/stops/search?q=<정류장명>&limit=10`은 경로에 연결된 Stop만 최대 20개까지 반환한다. 부분 이름 검색을 허용하고 정확 일치, 접두 일치, 나머지 부분 일치 순으로 정렬한다. 응답에는 `stop_id`, 이름, 노선명, occurrence 수, 실제로 존재하는 좌표가 포함된다. 동명 Stop ID가 여러 개면 각각 별도 후보로 반환한다. UI에서 후보를 고르면 해당 ID를 `/api/predict`에 보내며, 입력 텍스트를 수정하면 선택을 해제한다.

선택한 ID가 있으면 해당 Stop ID로 조회한다. 없는 ID는 오류로 처리하며 입력 이름으로 조용히 대체하지 않는다. ID를 보내지 않은 기존 호출은 이름 기반 후보 검색을 유지한다. Historical ETL 자체가 이름만으로 Stop을 만든다는 한계는 이 API로 해결되지 않으며 공식 ID 매핑이 필요하다.

응답의 각 `routes[]`는 line/pattern, occurrence별 정류장, 선택 좌표, geometry 종류, historical/realtime 근거와 상대 혼잡 안내를 담는다. 핵심 혼잡 필드는 다음과 같다.

- `onboard_count`: 원본 또는 realtime의 차내 재차인원
- `relative_percentile`: 같은 RoutePattern·service date·hour의 유효 historical 관측 대비 mid-rank percentile
- `congestion_level`: `LOW`, `MEDIUM`, `HIGH`, `VERY_HIGH`, `UNKNOWN`
- `boarding_guidance`: `여유`, `보통`, `혼잡`, `매우 혼잡`, `데이터 부족`
- `evidence_source`: 예: `HISTORICAL_OBSERVATION`, `REALTIME_OBSERVATION`, `HISTORICAL_PROFILE`, `NONE`
- `congestion_status`: 관측의 가용 상태
- `sample_size`: 상대 비교에 사용한 표본 수

`boarding_probability`, `expected_load`, `travel_time`은 이전 계약 호환을 위한 deprecated nullable 필드다. 현재 근거로 계산할 수 없으므로 모두 `null`이며 임의 숫자를 채우지 않는다.

직행 경로가 없거나 정류장을 찾지 못하면 서비스는 임의 경로 대신 명시적 400 오류를 반환한다. 프런트는 이 메시지를 표시한다.

## 직접 경로와 반복 정류장

`V2TransitRepository.find_direct_routes()`는 다음 topology를 조회한다.

```text
origin Stop
  <- AT_STOP - origin StopOccurrence
  <- HAS_OCCURRENCE - RoutePattern
  <- HAS_PATTERN - Line
origin StopOccurrence - NEXT* -> destination StopOccurrence
  - AT_STOP -> destination Stop
```

경로는 `seq < seq`만 비교하지 않고 실제 `NEXT` 관계를 통과하며, 모든 relationship이 같은 pattern에서 연속 seq인지 확인한다. 반복 정류장이 있으면 occurrence 조합별 경로 중 짧은 후보를 먼저 반환하고 서비스는 pattern별 최선 하나만 유지한다.

B1 기준은 대전역 `seq=2` → 세종시청 `seq=13`의 11-hop, 반대 방향 세종시청 `seq=42` → 대전역 `seq=52` 경로다. `오송역2.3.4`의 `seq=27`, `seq=28`도 독립 occurrence로 남는다.

현재 대안은 다른 direct RoutePattern 후보뿐이며 최대 3개 경로 중 첫 번째가 추천 경로다. 근접 정류장 후보와 hop 수로 정렬하며 혼잡 값을 경로 순위에 반영하지 않는다. 환승 탐색과 소요시간 계산은 구현하지 않았다.

## 혼잡과 fallback

`onboard_count`는 탑승 성공 확률이나 정원 대비 혼잡률이 아니다. exact historical 관측이 있으면 같은 pattern·날짜·시간의 유효 값 분포에서 mid-rank percentile을 계산한다.

| percentile | level | guidance |
|---:|---|---|
| `<= 33` | `LOW` | 여유 |
| `<= 67` | `MEDIUM` | 보통 |
| `<= 90` | `HIGH` | 혼잡 |
| `> 90` | `VERY_HIGH` | 매우 혼잡 |
| 없음 | `UNKNOWN` | 데이터 부족 |

근거 우선순위:

```text
fresh RealtimeObservation
  -> exact LoadObservation
  -> existing LoadProfile
  -> UNKNOWN / INSUFFICIENT_DATA
```

Realtime 조회와 stale 판정은 준비되어 있으나 importer가 없으므로 현재 실제 동작은 historical 또는 `UNKNOWN`이다. `LoadProfile` 조회도 지원하지만 생성 batch는 없다. 값을 찾지 못했을 때 0으로 바꾸지 않는다.

예시 데이터에서 B1 대전역은 08시 17명, 09시 24명이며 08시 pattern 내 상대 percentile은 약 56.6, `MEDIUM/보통`이다.

## 좌표와 지도

Historical CSV에는 공식 stop ID와 좌표가 없다. 기본 ETL의 historical Stop은 `UNMAPPED_NAME_ONLY`이고 좌표가 없다. `public_data.py`가 official Stop과 `RouteStopStaging`을 적재할 수 있지만 검증된 historical 매핑을 자동 생성하지 않는다.

선택한 경로의 모든 StopOccurrence가 실제 좌표를 가질 때만 API는 다음을 반환한다.

```text
geometry_kind = STOP_TO_STOP_APPROXIMATION
geometry = occurrence 순서의 lat/lon 목록
```

하나라도 좌표가 없거나 좌표가 2개 미만이면 `geometry_kind=UNAVAILABLE`, `geometry=[]`다. stop-to-stop 선은 정류장 좌표를 직선으로 이은 근사이며 도로 shape나 실제 차량 궤적이 아니다. 현재 공식 route shape는 구현하지 않았다.

## EXAONE 경계

서비스는 추천 경로의 line, 정류장, 날짜·시간, 재차인원, 상대 percentile/등급, evidence source, 대안 line만 JSON facts로 EXAONE에 전달한다. 프롬프트는 노선·정류장·수치·좌표를 추가하지 말고 탑승 확률을 만들지 말라고 명시한다.

생성문은 다음 grounding 조건을 통과해야 한다.

- 500자 이하이며 `%`나 `탑승 확률` 표현이 없음
- 추천 line 이름과 `boarding_guidance`를 포함
- 구조화 facts에 없던 숫자를 추가하지 않음

LLM HTTP 오류, 빈 응답 또는 grounding 실패 시에도 route/congestion 구조화 결과는 그대로 반환하며 서버의 결정론적 설명을 사용한다. EXAONE은 topology나 혼잡 수치를 계산하지 않는다.

## 남은 범위

1. 공식 B1 route/stop sequence를 확인하고 historical occurrence와 검증된 mapping을 생성한다.
2. mapping된 좌표 커버리지를 확보해 지도 stop-to-stop approximation을 실제로 검증한다.
3. 필요할 때 환승 탐색, 공식 route shape, 소요시간 근거를 추가한다.
4. 실제 realtime source가 확보된 경우에만 importer와 freshness 운영 정책을 추가한다.
5. 탑승 확률은 차량 정원·대기열·탑승 성공 ground truth가 확보되기 전까지 구현하지 않는다.
