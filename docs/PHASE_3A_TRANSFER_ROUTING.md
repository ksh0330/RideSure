# Phase 3A — one-transfer routing

RideSure can return a direct historical route as before. If no direct route exists, the service can return up to three one-transfer itineraries. Each itinerary has exactly two ride legs. It uses ordered historical `NEXT` relationships for both rides and connects them only through two `VERIFIED_OFFICIAL_STOP` relationships that point to the same official `Stop.official_node_id`.

Transfer matching does not use historical stop names, aliases, or coordinate proximity. Both mapping relationships must have status `EXACT` or `SEQUENCE_MATCH`, point to an `OFFICIAL_NODE_ID` stop, and agree on the official node ID. Each path stays within its own `RoutePattern`, follows outgoing `NEXT` relationships, and advances by exactly one sequence number per hop. The two patterns must differ.

## Response compatibility

`POST /api/predict` keeps its existing `routes` field. Direct results continue to populate `routes` and return an empty `itineraries` list. One-transfer results leave `routes` empty and populate the new additive `itineraries` field. Each itinerary contains `transfer_count=1`, the official stop ID and mapping provenance from each leg, historical transfer names and occurrence IDs on each leg, and two ordered leg objects.

Each transfer leg carries its own `onboard_count`, `relative_percentile`, `congestion_level`, `evidence_source`, and `congestion_status`. The two onboard counts are never combined. An explanation is generated deterministically without EXAONE for transfer results. It names the transfer and describes each leg's own evidence; it does not claim a travel time or congestion-optimal route.

Direct routes always take precedence. If no direct route exists, one-transfer candidates are ordered by total historical hop count, then first-leg and second-leg hops, line names, pattern IDs, and official stop ID. Hop count measures ordered stop-to-stop edges, not elapsed time or road distance.

## Live graph validation examples

All three pairs below had zero direct routes in `V2TransitRepository`. Both ride legs followed increasing historical sequence numbers and joined at the identical official stop ID.

| Origin → destination | First leg | Verified transfer | Second leg | Hops |
|---|---|---|---|---:|
| 대평동(해들마을) → 첫마을3단지 | 1000, 1 hop | `SJB293055058` 세종고속시외버스터미널 | 1004, 1 hop | 2 |
| 두루초.중학교 → 세종시문화예술회관 | 1000, 1 hop | `SJB293064097` 가락마을22단지 | 1003, 6 hops | 7 |
| 가락마을17.18단지 → 조치원역뒤편 | 1003, 1 hop | `SJB293064097` 가락마을22단지 | 1000, 6 hops | 7 |

At 08:00 on 2025-11-08, the first example's legs independently returned historical onboard counts 4 and 1. The second returned counts 0 and 5; the third returned 9 and 5. Their relative percentiles and congestion labels also remain per leg.

The historical line `1000-1` currently has no verified official-stop mappings. A live check confirmed it cannot take part in transfer discovery through its shared historical names alone. It may still be used for direct routing under the existing historical topology.

The API schema change is additive: clients that consume `routes` keep working for direct results. Clients must read `itineraries` to display one-transfer results. The frontend does not yet render those itineraries.
