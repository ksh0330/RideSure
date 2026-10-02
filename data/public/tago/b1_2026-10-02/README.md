# B1 TAGO snapshot — 2026-10-02

These are **raw JSON responses** from the [official TAGO bus-route API](https://www.data.go.kr/data/15098529/openapi.do), plus a local read-only mapping preview. The responses contain no service key. They are small and source-attributed, so this directory is intentionally **not gitignored**; it remains uncommitted during review.

| File | Operation / request parameters (service key omitted) | SHA-256 |
|---|---|---|
| `city_codes.json` | `getCtyCodeList`, `_type=json`, `numOfRows=1000`, `pageNo=1` | `dedf65a7faf48f422026116c3da8d4d0a2623297c47257400ad012cae567d0f4` |
| `b1_route_number_city_25.json` | `getRouteNoList`, `cityCode=25`, `routeNo=B1`, `_type=json`, `numOfRows=1000`, `pageNo=1` | `5a25f5321a47159b6455fd4c2df09a8c9a7a640e197f4b9ca90e5083d52d252f` |
| `b1_route_basic_info.json` | `getRouteInfoIem`, `cityCode=25`, `routeId=DJB30300128`, `_type=json`, `numOfRows=1000`, `pageNo=1` | `5a99a693ac4f2bcd61ebc4fa46cd85ae3969cbf591be6b18b939e87207f6d880` |
| `b1_route_stops.json` | `getRouteAcctoThrghSttnList`, `cityCode=25`, `routeId=DJB30300128`, `_type=json`, `numOfRows=1000`, `pageNo=1` | `eb59fe3032532de00f4fc22281c3125ddaae671a8bc5317fec55a927cbf9332e` |
| `b1_mapping_preview.json` | Read-only `official_stop_mapping.py plan` against Neo4j historical B1 and the raw stop JSON | `8e2292592f8bde07223bdb17f1e68d0a2ca050485104c36493a07acef69d1f9c` |

The city list contains 세종특별시 `12`, 대전광역시/계룡시 `25`, and 청주시 `33010`. `getRouteNoList` for B1 returned zero records for `12` and `33010`, and one for `25`. The selected route's `getRouteInfoIem` confirms `routeno=B1`, `routeid=DJB30300128`, 기점 `대전역동광장`, 종점 `오송역2.3.4`, and `routetp=광역버스`.

The stop response contains 55 records, matching `totalCount=55`, with unique `nodeord` values 1–55. Direction codes are `0` for 29 records and `1` for 26, while order continues across the turn-around. All 55 records have valid coordinates according to the existing TAGO normalizer.

## Historical comparison

The historical RideSure B1 pattern has 53 occurrences. Ordered name alignment identifies two current-only stops: `국제과학비즈니스벨트` at official orders 11 and 46. They are **not inserted into historical topology** and receive no historical onboard observations.

Punctuation/spacing variants include `보람동.대평동` → `보람동,대평동`, `새롬동.나성동` → `새롬동,나성동`, and `정부세종청사남측/북측` → names with a space before `남측/북측`. Other changed labels need separate identity review: `소담동` → `소담동(새샘마을)`, `세종터미널` → `세종고속시외버스터미널(지하)`, `해밀리` → `해밀동,산울동`, `한별리` → `한별동`, `누리리` → `누리동`. `세종시청.교육청.시의회` → `세종시청,시의회,교육청` also changes the listed component order, so it is not treated as punctuation only. Name alignment alone does not prove physical-stop identity.

Historical `오송역2.3.4` occurs at sequences **27 and 28**. Current TAGO has two consecutive records at orders **28 and 29**, with distinct node IDs `DJB8007055` and `DJB9007055`, both marked direction `0`. The **Phase 2A preview** deliberately left both `AMBIGUOUS`; its local-neighbor matcher lacked enough adjacent evidence to choose an ID for either occurrence.

Phase 2A preview counts: `EXACT=0`, `SEQUENCE_MATCH=17`, `AMBIGUOUS=16`, `UNMATCHED=20`; `applied=0`. All 17 proposed verified mappings have TAGO coordinates, which would cover 17/53 historical B1 occurrences if later approved.

Reproduce the preview after loading the local historical Neo4j v2 graph:

```powershell
.\.venv\Scripts\python.exe .\official_stop_mapping.py plan --line-name B1 --pattern-id hist-pattern-2cedb0cf61fcded40dfe81618ddc4a32d12cdb336e7a0b92054568d7eafc20a6 --official-route-id DJB30300128 --route-binding-source "TAGO getRouteNoList cityCode=25 routeNo=B1 plus getRouteInfoIem; captured 2026-10-02" --tago-json .\data\public\tago\b1_2026-10-02\b1_route_stops.json
```

`plan --tago-json` reads the fixture directly and does not import official nodes or write mapping edges. The historical graph and the routing service remain unchanged.

## Phase 2B full-route alignment preview

`b1_mapping_preview_phase2b.json` is the read-only preview after replacing the
local-neighbor matcher with a full-route monotonic one-to-one alignment. Its
SHA-256 is `f60ed222adacbe5838d4f2d66bcba5020578c90e4aaf026a7d3c6398f4feb721`.
The older `b1_mapping_preview.json` is retained for comparison.

| Status | Phase 2A | Phase 2B |
|---|---:|---:|
| EXACT | 0 | 0 |
| SEQUENCE_MATCH | 17 | 41 |
| AMBIGUOUS | 16 | 0 |
| UNMATCHED | 20 | 12 |

All 17 earlier proposed mappings remain. The 24 newly proposed mappings use
the global order constraint; eight of the 41 accepted names differ only in
presentation punctuation or spacing. Historical `오송역2.3.4` seq 27/28 map
separately in the **preview** to official orders 28/29 and node IDs
`DJB8007055`/`DJB9007055`. Official orders 11/46
(`국제과학비즈니스벨트`) remain unused gaps. The 12 unmatched historical
occurrences have changed labels that this deterministic name matcher does not
equate. Proposed coordinate coverage is 41/53 historical occurrences. No
official nodes or verified edges were applied to Neo4j.
