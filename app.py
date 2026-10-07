"""RideSure FastAPI server backed by Neo4j v2 historical transit data."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

import config
from service import get_service


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="RideSure Neo4j v2 Demo",
    description=(
        "Direct and one-transfer public-transit routes with per-leg relative congestion guidance from "
        "the RideSure Neo4j v2 historical knowledge graph."
    ),
    version="0.2-v2",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
        "http://localhost:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PredictionRequest(BaseModel):
    origin: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    departure_time: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    date: Optional[str] = Field(default=config.TARGET_DATE, pattern=r"^\d{4}-\d{2}-\d{2}$")
    origin_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    origin_lon: Optional[float] = Field(default=None, ge=-180, le=180)
    destination_lat: Optional[float] = Field(default=None, ge=-90, le=90)
    destination_lon: Optional[float] = Field(default=None, ge=-180, le=180)
    origin_stop_id: Optional[str] = Field(default=None, min_length=1, max_length=200)
    destination_stop_id: Optional[str] = Field(default=None, min_length=1, max_length=200)
    origin_occurrence_id: Optional[str] = Field(default=None, min_length=1, max_length=200)
    destination_occurrence_id: Optional[str] = Field(default=None, min_length=1, max_length=200)

    @field_validator("origin_stop_id", "destination_stop_id", "origin_occurrence_id", "destination_occurrence_id")
    @classmethod
    def stop_id_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("stop ID must not be blank")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def coordinates_are_paired(self) -> "PredictionRequest":
        if self.origin_occurrence_id and not self.origin_stop_id:
            raise ValueError("origin_occurrence_id requires origin_stop_id")
        if self.destination_occurrence_id and not self.destination_stop_id:
            raise ValueError("destination_occurrence_id requires destination_stop_id")
        pairs = (
            ("origin", self.origin_lat, self.origin_lon),
            ("destination", self.destination_lat, self.destination_lon),
        )
        for label, latitude, longitude in pairs:
            if (latitude is None) != (longitude is None):
                raise ValueError(f"{label} latitude and longitude must be provided together")
        return self


class StopInfo(BaseModel):
    stop_id: Optional[str] = None
    occurrence_id: Optional[str] = None
    name: str
    seq: Optional[int] = None
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    distance_m: Optional[float] = Field(default=None, ge=0)


class GeometryPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class RouteInfo(BaseModel):
    line_id: str
    line_name: str
    pattern_id: str
    terminal_description: Optional[str] = None
    origin_stop: StopInfo
    destination_stop: StopInfo
    stops: List[StopInfo]
    geometry: List[GeometryPoint] = Field(default_factory=list)
    geometry_kind: str = "UNAVAILABLE"
    map_geometry: List[GeometryPoint] = Field(default_factory=list)
    map_geometry_source: str = "UNAVAILABLE"
    onboard_count: Optional[int] = Field(default=None, ge=0)
    relative_percentile: Optional[float] = Field(default=None, ge=0, le=100)
    congestion_level: str
    boarding_guidance: str
    evidence_source: str
    congestion_status: str
    service_date: Optional[str] = None
    hour: Optional[int] = Field(default=None, ge=0, le=23)
    sample_size: int = Field(default=0, ge=0)
    # Deprecated compatibility fields. They remain null instead of fabricating values.
    boarding_probability: Optional[float] = None
    expected_load: Optional[int] = None
    travel_time: Optional[int] = None


class TransferLeg(RouteInfo):
    hops: int = Field(ge=1)
    origin_occurrence_id: str
    destination_occurrence_id: str


class TransferPoint(BaseModel):
    official_stop_id: str
    official_stop_name: str
    historical_name_leg1: str
    historical_name_leg2: str
    leg1_occurrence_id: str
    leg2_occurrence_id: str
    leg1_seq: int
    leg2_seq: int
    mapping_leg1: dict
    mapping_leg2: dict


class OneTransferItinerary(BaseModel):
    transfer_count: Literal[1]
    total_hops: int = Field(ge=2)
    transfer: TransferPoint
    legs: List[TransferLeg] = Field(min_length=2, max_length=2)


class PredictionResponse(BaseModel):
    success: bool
    result_status: Literal["ROUTE_FOUND", "NO_SUPPORTED_ROUTE"] = "ROUTE_FOUND"
    origin: str
    destination: str
    routes: List[RouteInfo]
    itineraries: List[OneTransferItinerary] = Field(default_factory=list)
    reasoning: str
    explanation: str
    alternatives: List[str] = Field(default_factory=list)
    data_mode: str = "NEO4J_V2_HISTORICAL"


class FrontendConfig(BaseModel):
    kakao_map_javascript_key: str


class StopSearchCandidate(BaseModel):
    stop_id: str
    stop_name: str
    occurrence_id: Optional[str] = None
    official_stop_name: Optional[str] = None
    official_stop_id: Optional[str] = None
    match_kind: str = "UNKNOWN"
    line_names: List[str] = Field(default_factory=list)
    occurrence_count: int = Field(ge=1)
    exact_match: bool
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)


class StopSearchResponse(BaseModel):
    q: str
    stops: List[StopSearchCandidate]


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/frontend-config", response_model=FrontendConfig)
async def frontend_config() -> FrontendConfig:
    if not config.KAKAO_MAP_JAVASCRIPT_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "카카오 지도 설정이 없습니다. 프로젝트 루트 .env에 "
                "KAKAO_MAP_JAVASCRIPT_KEY를 설정한 뒤 FastAPI 서버를 다시 시작하세요."
            ),
        )
    return FrontendConfig(kakao_map_javascript_key=config.KAKAO_MAP_JAVASCRIPT_KEY)


@app.get("/api/stops/search", response_model=StopSearchResponse)
async def search_stops(
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=10, ge=1, le=20),
) -> StopSearchResponse:
    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail="q must contain a stop name")
    try:
        return StopSearchResponse(q=query, stops=get_service().search_stops(query, limit))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Stop search failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="정류장 검색 중 서버 오류가 발생했습니다.") from exc


@app.get("/health")
async def health() -> dict:
    import requests

    try:
        response = requests.get(f"{config.LLM_BASE_URL}/health", timeout=2)
        llm_ok = response.status_code == 200
    except Exception:
        llm_ok = False

    try:
        neo4j_ok = get_service().check_connection()
    except Exception:
        neo4j_ok = False

    return {
        "status": "healthy" if llm_ok and neo4j_ok else "degraded",
        "llm_server": "ok" if llm_ok else "error",
        "neo4j_v2": "ok" if neo4j_ok else "error",
        "demo_mode": False,
        "data_mode": "NEO4J_V2_HISTORICAL",
    }


@app.post("/api/predict", response_model=PredictionResponse)
async def predict_boarding(request: PredictionRequest) -> PredictionResponse:
    logger.info(
        "Prediction request: %s -> %s @ %s date=%s",
        request.origin,
        request.destination,
        request.departure_time,
        request.date,
    )
    try:
        result = get_service().predict_boarding(
            origin=request.origin,
            destination=request.destination,
            departure_time=request.departure_time,
            date=request.date or config.TARGET_DATE,
            origin_lat=request.origin_lat,
            origin_lon=request.origin_lon,
            destination_lat=request.destination_lat,
            destination_lon=request.destination_lon,
            origin_stop_id=request.origin_stop_id,
            destination_stop_id=request.destination_stop_id,
            origin_occurrence_id=request.origin_occurrence_id,
            destination_occurrence_id=request.destination_occurrence_id,
        )
        return PredictionResponse(**result)
    except ValueError as exc:
        logger.warning("Invalid prediction request: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Prediction failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="서버 내부 오류가 발생했습니다.") from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.APP_HOST, port=config.APP_PORT, log_level="info")
