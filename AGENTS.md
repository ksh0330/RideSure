# RideSure — AGENTS.md

## Project Purpose

RideSure is a portfolio-oriented reconstruction and improvement of a project originally developed for the 2025 DSC 공유대학 KT 기업연계 오픈데이터 활용 스타트업 챌린지.

The original problem was:

> B1 BRT passengers cannot easily know how crowded an arriving bus will be, whether they are likely to board it, or what alternative route they should take when congestion is high.

The original target features were:

1. Bus congestion estimation
2. Boarding possibility guidance
3. Alternative / optimal route recommendation
4. Natural-language explanation using a local SLM
5. Map-based route visualization

The original competition prototype was limited by incomplete public data and therefore used a mixture of public CSV data, a Neo4j knowledge graph, simplified calculations, and hardcoded demo results.

The current goal is **not to rebuild the project as a production transit platform**.

The goal is to:

* preserve the original project concept,
* replace major hardcoded demo logic where reasonably possible,
* improve the data and Neo4j structure,
* make route and congestion results traceable to actual data,
* improve the map/navigation demo,
* use the local SLM appropriately,
* and leave the repository in a clean portfolio-quality state.

---

## Core Product Concept

Think of RideSure as a simplified public-transport navigation service similar to an existing map application's transit routing feature, with an additional congestion layer.

Conceptually:

```text
User Input
    ↓
Route / Stop / Historical Bus Data
    ↓
Neo4j Knowledge Graph
    ↓
Route Search + Congestion Calculation
    ↓
EXAONE Local SLM
    ↓
Natural-language Recommendation
    ↓
Kakao Map Visualization
```

The user provides:

* origin
* destination
* departure time

The system should return, where data permits:

* recommended route
* relevant stops
* route path
* estimated travel information
* historical or current congestion information
* boarding guidance
* alternative route when useful
* short natural-language explanation

---

## Role of Neo4j

Neo4j is the structured knowledge and routing layer.

It should represent concepts such as:

* bus lines
* route patterns / directions
* stops
* ordered stop occurrences
* historical onboard-passenger observations
* optional realtime observations

The graph should be usable for actual route queries rather than only as a visualization.

Do not manually enter large datasets into Neo4j.

All graph construction must be reproducible through ETL or seed scripts.

---

## Role of EXAONE / SLM

EXAONE is a local Small Language Model used primarily for explanation and recommendation wording.

The SLM should receive structured facts produced by the application, such as:

* selected route
* stops
* congestion value or level
* relevant time
* alternative route
* reason for the recommendation

The SLM should convert these facts into concise natural-language guidance.

Do not depend on the SLM to invent route topology or public-transit facts.

Do not require LangChain unless it provides a clear implementation benefit.

A simple architecture such as:

```text
FastAPI
→ Neo4j query
→ Python calculation
→ prompt construction
→ EXAONE
→ response
```

is acceptable and preferred when it is sufficient.

---

## Congestion and Boarding Guidance

The historical `count` data represents **차내 재차인원 (onboard passenger count)**.

Do not silently redefine it as an actual boarding probability.

It may be used for:

* raw onboard count
* historical comparison
* relative congestion level
* percentile or simple congestion categories
* congestion-aware route ranking

If a boarding probability is eventually displayed, its calculation must have a defined basis.

For the portfolio version, a clearly explained congestion or boarding-risk level is acceptable when a defensible probability cannot be produced.

Do not fabricate unsupported realtime values.

---

## Realtime Data

RideSure should not depend entirely on realtime APIs.

Realtime data may be used when available, but the architecture should support:

```text
Realtime data available
    → use realtime observation

Realtime data unavailable / delayed
    → use historical data

Historical data unavailable
    → return unknown / insufficient data
```

The original project specifically targets cases where useful realtime congestion information is unavailable.

---

## B1 BRT

B1 is the primary demonstration route and should receive strong test coverage.

However, avoid hardcoding the entire application around B1.

The data model should support:

* different route directions,
* multiple route patterns,
* repeated stop appearances,
* ordered stops,
* future expansion to other bus routes.

B1 may be used as the main demo and validation scenario.

---

## Map and Routing

Kakao Map is the visualization layer.

Where the data is available, the application should display:

* origin
* destination
* boarding stop
* alighting stop
* intermediate stops
* route polyline
* recommended route
* alternative route

A stop-to-stop polyline is acceptable for the portfolio demo if an official road/route geometry dataset is unavailable.

Do not present approximated geometry as an exact vehicle trajectory.

---

## Development Priorities

Prefer this order:

1. Preserve the working demo
2. Improve Neo4j data structure
3. Connect real route queries to Neo4j
4. Replace hardcoded congestion results
5. Improve map route visualization
6. Add historical-data fallback
7. Add realtime data only when practical
8. Improve EXAONE explanation
9. Improve UX and portfolio documentation

Do not over-engineer the project.

This is a portfolio-quality prototype, not a nationwide production transportation platform.

---

## Validation Philosophy

Use practical validation.

Important checks include:

* application starts successfully
* Neo4j loads correctly
* route order is sensible
* B1 demo route works
* repeated stops do not break routing
* different departure times can produce different congestion evidence
* missing data does not create obviously false results
* EXAONE explanation reflects the structured result
* Kakao Map renders the selected route

Avoid spending excessive implementation effort on exhaustive auditing unless a data problem directly affects the user-facing result.

---

## Repository Safety

Use the current checkout as the repository root. Inspect the current branch before edits; `main` contains the portfolio reconstruction. Create a focused branch when needed, and preserve the `v0.1-demo` tag.

Do not overwrite or rewrite the preserved demo history without explicit instruction.

Before large changes:

* inspect the current branch,
* inspect `git status`,
* understand existing code,
* make focused changes,
* run relevant tests,
* summarize what changed.

Do not deploy, merge to `main`, or delete preserved data unless explicitly requested.

---

## Existing Working Components

Treat the currently working system as a baseline.

The following are expected to remain functional:

* FastAPI
* EXAONE local inference
* Kakao Map integration
* Docker Neo4j
* demo startup / execution automation

Prefer incremental improvements over unnecessary rewrites.

---

## Documentation

The repository should eventually explain:

* what RideSure is,
* the original 2025 competition context,
* limitations of the original prototype,
* current architecture,
* public-data sources,
* Neo4j graph structure,
* EXAONE's role,
* how to run the demo,
* which results are real data, derived values, or fallback estimates.

Keep documentation concise and portfolio-friendly.

---

## Final Principle

The project should demonstrate the following engineering story:

> A public-transport problem was identified around B1 BRT congestion and boarding uncertainty.
> Limited public data prevented the original competition prototype from fully implementing the idea.
> The project was later reconstructed using reproducible public-data ETL, a Neo4j knowledge graph, route/congestion logic, Kakao Map, and a local EXAONE SLM to produce a more complete and explainable prototype.

Prefer a **working, understandable, defensible prototype** over an unnecessarily complex system.
