# TAGO multi-route snapshot — 2026-10-02

Raw responses are from the [official TAGO bus-route API](https://www.data.go.kr/data/15098529/openapi.do). `manifest.json` records every operation, request parameter except the service key, and SHA-256 checksum. The JSON is small, contains no service key, and is intentionally **not gitignored**. Reproduce discovery with `tago_route_discovery.py` using route numbers `1000`, `1000-1`, `1001`, `1003`, `1004` and official city codes `12`, `25`, `33010`. The code first verifies those codes against `getCtyCodeList`.

The `getRouteNoList` responses are retained even when they have zero exact matches. `getRouteInfoIem` confirms the route number and metadata for every exact candidate; `getRouteAcctoThrghSttnList` supplies the full ordered stop response. `*_preview.json` files are read-only historical alignment summaries. `selected_bindings.json` records the reviewed four bindings. The historical data is unchanged.

| Historical line | Selected city / route ID | Official basic information | Official records | Preview EXACT / SEQUENCE_MATCH / AMBIGUOUS / UNMATCHED | Applied coverage |
|---|---|---|---:|---:|---:|
| 1000 | 12 / `SJB293000169` | 반석역 → 조형아파트, 광역버스 | 34 | 0 / 29 / 0 / 5 | 29/34 |
| 1000-1 | No exact route-number result in checked cities | — | — | — | 0/34 |
| 1001 | 25 / `DJB30300151` | 집현동 → 시청환승지, 광역버스 | 47 | 0 / 45 / 0 / 0 | 45/45 |
| 1003 | 12 / `SJB293000341` | 반석역 → 오송역파라곤센트럴시티1차, 광역버스 | 35 | 0 / 19 / 0 / 9 | 19/28 |
| 1004 | 12 / `SJB293000178` | 반석역 → 장기중학교 후문, 광역버스 | 39 | 0 / 26 / 2 / 11 | 26/39 |

The `1000` reverse candidate `SJB293000168` and `1004` reverse candidate `SJB293000179` each have zero forward sequence matches. The Cheongju `1003` candidate `CJB270029500` has 25 name-alignment matches, but its 70-stop response is a round trip and the last proposed match is official order 37 in the **return** direction (`updowncd=1`). The one-way historical pattern must not cross that turn. That candidate was rejected and its initially applied edges and reference staging were removed. The selected Sejong outbound candidate has 19 mandatory matches within its own 35-stop sequence. The `1003` reverse candidate `SJB293000342` has zero matches. No official route number `1000-1` was returned; the reverse `1000` route has a similar physical sequence, but route-number identity is insufficient to bind it to the historical `1000-1` line.

Unused official orders are **alignment gaps**, not automatically current-only stops. For `1001`, orders 1 and 47 are the official `집현동 기점지` at the ends of the round trip. The selected `1003` route has 16 unused official orders. Other gaps may reflect changed labels, added stops, or changed service; no historical observation is created for them. Unmatched historical occurrences also receive no name-based coordinate fallback. Notable unresolved cases include `한국농어촌공사` (1000), `가득초등학교정문` (1003), and the repeated `봉안리` at historical 1004 sequences 33/34. The latter two remain `AMBIGUOUS` rather than being collapsed.

The four selected imports added 155 `RouteStopStaging` records, bringing the live total to 210 including B1. Live official `Stop` nodes total 199 because some route records share node IDs. The 119 new verified edges bring the total to 160 including B1's unchanged 41. A second apply produced the same edge counts.

Live routing still follows historical `NEXT` topology. The sample 1001 and 1004 four-stop segments have complete verified coordinates and return `STOP_TO_STOP_APPROXIMATION`. The sample 1000 and 1003 segments have missing mapped stops and return `UNAVAILABLE`; 1000-1 remains historical-only. Eight official node IDs are verified across at least two historical patterns. For example, `SJB186004230` (반석역) is shared by 1000, 1003, and 1004; `SJB293055058` (세종고속시외버스터미널) by 1000 and 1004; and `SJB293064097` (가락마을22단지) by 1000 and 1003. This is a physical-stop identity observation only; transfer routing is not implemented.

Run `phase2d_analysis.py` to regenerate candidate previews against local Neo4j. Run `phase2d_apply.py` to import and idempotently apply the reviewed bindings. Run `phase2d_live_verify.py` for direct route, geometry, coordinate coverage, and shared-stop checks.
