# RideSure Runbook

## 1. Prerequisites

Use Git, Python 3.12, and Docker Desktop (Windows) or Docker Engine with Compose (Linux). Node.js is needed only for frontend syntax and contract checks. The standard historical demo does not download EXAONE or require a GPU; the app uses a grounded fallback explanation when its optional local LLM server is unavailable.

## 2. Clone and environment

```sh
git clone https://github.com/ksh0330/RideSure.git
cd RideSure
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-demo.txt
```

On Windows PowerShell, replace the venv creation/activation lines with `py -3.12 -m venv .venv` and `.\.venv\Scripts\Activate.ps1`. In Git Bash use `source .venv/Scripts/activate`. If PowerShell blocks activation, call `.\.venv\Scripts\python.exe` in place of `python` below.

## 3. Environment variables

Copy `.env.example` to `.env` (`Copy-Item .env.example .env` in PowerShell or `cp .env.example .env` in Bash). Set `NEO4J_USER` (normally `neo4j`) and a local `NEO4J_PASS` of at least eight characters. The included `NEO4J_V2_URI=bolt://127.0.0.1:7688` points to the Compose v2 service. Keep `LLM_BASE_URL` set to its sample local address even when the optional server is off; it is only contacted for wording and failure falls back to deterministic text.

For the map, set `KAKAO_MAP_JAVASCRIPT_KEY` to your Kakao **JavaScript** key and register `http://127.0.0.1:8000` as an allowed web origin. `KAKAO_REST_API_KEY` is the separate optional server-side REST key used to fetch and validate transit BUS geometry. With no REST key or no verified endpoint coordinates, the app shows available stop markers and no route line. The JavaScript key is sent to the browser; the REST key stays server-side. Never commit `.env`.

`DATA_GO_KR_SERVICE_KEY` is optional for future/live TAGO refresh and is not used by demo preparation. `MODEL_ID`, `MODEL_PATH`, `HF_HOME`, `HF_HUB_OFFLINE`, `MAX_NEW_TOKENS`, and CUDA/PyTorch are optional EXAONE development settings. `requirements.txt`, `requirements-cuda.txt`, `scripts/setup.ps1`, and `scripts/start.ps1` belong to the full model workflow; they are not needed here.

## 4. Start Neo4j

```sh
docker compose --profile v2 up -d --wait neo4j-v2
docker compose --profile v2 ps
```

The `neo4j-v2` row should show `healthy` and port 7688. Start Docker Desktop/Engine first if Compose cannot reach its daemon.

## 5. Prepare demo data

```sh
python -m scripts.prepare_demo_data
```

This command uses the three shipped historical CSVs and reviewed TAGO snapshots. It imports history only into an empty v2 graph, accepts an already complete graph, checks reviewed mappings, and is safe to repeat. It refuses partial or inconsistent data for manual inspection. Expected output: 148 Lines, 154 RoutePatterns, 2,049 historical Stops, 11,375 StopOccurrences, 273,000 LoadObservations, 210 RouteStopStaging records, and 162 `VERIFIED_OFFICIAL_STOP` edges (160 automatic plus two separately reviewed B1 occurrences). It does not call TAGO or require a data.go.kr key.

## 6. Start RideSure

```sh
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. Use this exact origin in Kakao Developers; `localhost:8000` is a separate origin if you also want to use it. The API docs are at <http://127.0.0.1:8000/docs>.

## 7. Manual smoke tests

Choose stops from RideSure's stop suggestions; use date `2025-11-08` and time `08:00`.

| Case | Input | Expected |
|---|---|---|
| Direct | 반석역 → 한국농어촌공사 | Historical line 1000 route and congestion evidence; map line only when both verified endpoint coordinates and validated Kakao BUS geometry are available. |
| One transfer | 대평동(해들마을) → 첫마을3단지 | 1000 then 1004 via 세종고속시외버스터미널; each leg has its own evidence and validated line when available. |
| Missing geometry | 대전역 → 세종시청.교육청.시의회 | B1 route and historical evidence remain; the unverified destination receives no `도착` marker and no fabricated line. |
| No supported route | Select two valid suggested stops for which no direct or verified one-transfer path is returned | Explicit no-supported-route state; no invented route or line. |

The last case depends on the selected graph directions. Use `/api/predict` to confirm `result_status=NO_SUPPORTED_ROUTE` when choosing a pair; it is not an API error.

## 8. Automated tests

```sh
python -m pytest -q
python -m scripts.checks javascript
node tests/test_transfer_frontend.cjs
```

Install `pytest` separately for the optional test suite (`python -m pip install pytest`). Optional deeper checks: `python data_insert_v2.py verify` (running v2 database), `python -m compileall -q app.py config.py service.py prediction_v2.py scripts`, and `git diff --check`. The JavaScript check extracts inline scripts through the repository's stable `scripts.checks` command; no manual extraction is needed.

## 9. Stop services

Stop FastAPI with Ctrl+C, then:

```sh
docker compose --profile v2 down
```

`down` preserves the named Neo4j data volume. **Destructive local reset, only when intentionally discarding all Compose volumes:** `docker compose --profile v2 down -v`. This is not part of normal use and removes local graph data.

## 10. Troubleshooting

- Docker daemon unavailable: start Docker Desktop/Engine, then repeat the Compose command.
- Neo4j rejects a short password: use at least eight characters before creating the volume.
- Container removed but data remains: Compose's named volume survives `down`; rerun preparation against it.
- Port 8000 occupied: stop the old FastAPI process. If changing the port, register the new full origin with Kakao.
- Kakao `wrong appKey format`: use the JavaScript key in `KAKAO_MAP_JAVASCRIPT_KEY`, not the REST key.
- Kakao SDK domain error: register the exact browser origin, including host and port; `127.0.0.1` and `localhost` differ.
- Changed `.env` but app still uses the old key: stop and restart FastAPI.
- Route has no geometry: a selected endpoint may lack verified coordinates or the Kakao BUS segment may fail validation. Available endpoint markers remain; no line is invented.
