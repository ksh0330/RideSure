"""
버스 탑승 예측 FastAPI 서버
- HTML 정적 파일 서빙
- Neo4j + LLM 연동 (service.py 사용)
- 포트: 8000
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import logging
from pathlib import Path

from service import get_service  # 🔹 Neo4j + LLM 서비스 레이어
import config  # 🔹 기본 날짜 등 설정 사용

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="RideSure Recovered Demo",
    description=(
        "Recovered demonstration API. Route/load/probability values are fixed in "
        "service.py; EXAONE only writes an explanation for those values."
    ),
    version="0.1-demo"
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

origins = [
    "http://localhost",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://127.0.0.1:8000",
    "http://localhost:5500",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Pydantic 모델 =====
class PredictionRequest(BaseModel):
    origin: str
    destination: str
    departure_time: Optional[str] = None  # "HH:MM" 형식
    date: Optional[str] = config.TARGET_DATE  # 기본 날짜는 config에서

class RouteInfo(BaseModel):
    line_id: str
    line_name: str
    boarding_probability: float
    expected_load: int
    stops: List[str]
    travel_time: Optional[int] = None

class PredictionResponse(BaseModel):
    success: bool
    origin: str
    destination: str
    routes: List[RouteInfo]
    reasoning: str
    alternatives: Optional[List[str]] = None

class FrontendConfig(BaseModel):
    kakao_map_javascript_key: str

# 정적 파일 서빙
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
async def root():
    """메인 페이지"""
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/api/frontend-config", response_model=FrontendConfig)
async def frontend_config():
    """브라우저에서 필요한 공개 런타임 설정을 전달한다."""
    if not config.KAKAO_MAP_JAVASCRIPT_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "카카오 지도 설정이 없습니다. 프로젝트 루트 .env에 "
                "KAKAO_MAP_JAVASCRIPT_KEY를 설정한 뒤 FastAPI 서버를 다시 시작하세요."
            ),
        )

    return FrontendConfig(
        kakao_map_javascript_key=config.KAKAO_MAP_JAVASCRIPT_KEY,
    )

@app.get("/health")
async def health():
    """헬스체크: LLM 서버 + (간단) Neo4j 연결 확인"""
    import requests

    # LLM 서버 확인
    try:
        r = requests.get(f"{config.LLM_BASE_URL}/health", timeout=2)
        llm_ok = r.status_code == 200
    except Exception:
        llm_ok = False

    # Neo4j 확인 (간단하게 서비스 인스턴스 생성만 시도)
    try:
        service = get_service()
        neo4j_ok = service.check_connection()
    except Exception:
        neo4j_ok = False

    status = "healthy" if (llm_ok and neo4j_ok) else "degraded"

    return {
        "status": status,
        "llm_server": "ok" if llm_ok else "error",
        "neo4j": "ok" if neo4j_ok else "error",
        "demo_mode": True,
    }

@app.post("/api/predict", response_model=PredictionResponse)
async def predict_boarding(request: PredictionRequest):
    """
    버스 탑승 예측 API (DB + Neo4j + LLM 사용)

    1. 출발지/도착지/시간 입력 받기
    2. service.py의 BusPredictionService를 통해:
       - Neo4j에서 노선/정류장/승객수 조회
       - 탑승 확률 계산
       - LLM으로 설명/추천 생성
    3. 결과 반환
    """
    logger.info(
        f"📍 예측 요청: {request.origin} → {request.destination} "
        f"@ {request.departure_time}, date={request.date}"
    )

    try:
        service = get_service()
        result = service.predict_boarding(
            origin=request.origin,
            destination=request.destination,
            departure_time=request.departure_time,
            date=request.date or config.TARGET_DATE,
        )
        # result 구조:
        # {
        #   "success": True,
        #   "origin": ...,
        #   "destination": ...,
        #   "routes": [...],
        #   "reasoning": "...",
        #   "alternatives": [...]
        # }

        logger.info("✅ 예측 완료 (Neo4j + LLM 사용)")
        # Pydantic이 dict → 모델로 자동 파싱
        return PredictionResponse(**result)

    except ValueError as e:
        # 출발지/도착지/경로를 못 찾은 경우 등
        logger.warning(f"⚠️ 잘못된 요청: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        logger.error(f"❌ 예측 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="서버 내부 오류가 발생했습니다.")

if __name__ == "__main__":
    import uvicorn

    logger.info("=" * 60)
    logger.info("🚀 버스 탑승 예측 서버 시작 (DB + LLM 버전)")
    logger.info("=" * 60)
    logger.info("📂 정적 파일: ./static/")
    logger.info("🌐 웹 페이지: http://localhost:8000")
    logger.info("📚 API 문서: http://localhost:8000/docs")
    logger.info("=" * 60)

    uvicorn.run(
        app,
        host=config.APP_HOST,
        port=config.APP_PORT,
        log_level="info",
    )
