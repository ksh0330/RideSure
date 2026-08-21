# llm_server.py
"""
EXAONE 4.0 1.2B 설명문 생성 전용 서버
포트: 8001
"""
import config  # .env를 Transformers 초기화 전에 로드해야 한다.

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
import logging
import os
from pathlib import Path

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 전역 변수
model = None
tokenizer = None
device = None
latest_output = None

class GenerateRequest(BaseModel):
    prompt: str
    max_new_tokens: Optional[int] = config.MAX_NEW_TOKENS
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.9

class GenerateResponse(BaseModel):
    success: bool
    result: str
    reasoning: Optional[str] = None

async def load_model():
    """서버 시작 시 모델 로드"""
    global model, tokenizer, device

    try:
        config.validate_required_config(("model", "llm_server"))
        logger.info("🚀 EXAONE 모델 로딩 시작...")

        model_source = (
            config.MODEL_PATH
            if (Path(config.MODEL_PATH) / "config.json").is_file()
            else config.MODEL_ID
        )

        # 디바이스 설정
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info(f"📱 디바이스: {device}")

        # 토크나이저 로드
        tokenizer = AutoTokenizer.from_pretrained(
            model_source,
            local_files_only=config.HF_OFFLINE,
        )
        logger.info("✓ 토크나이저 로드 완료")

        # 모델 로드
        model = AutoModelForCausalLM.from_pretrained(
            model_source,
            dtype=(
                torch.bfloat16
                if torch.cuda.is_available() and torch.cuda.is_bf16_supported()
                else torch.float16 if torch.cuda.is_available() else torch.float32
            ),
            device_map="auto" if torch.cuda.is_available() else None,
            low_cpu_mem_usage=True,
            local_files_only=config.HF_OFFLINE,
        )

        if not torch.cuda.is_available():
            model = model.to(device)

        model.eval()
        logger.info("✓ 모델 로드 완료")
        logger.info(f"💾 메모리 사용량: {torch.cuda.memory_allocated() / 1e9:.2f} GB" if torch.cuda.is_available() else "CPU 모드")

        logger.info("✅ EXAONE 모델 준비 완료!")

    except Exception as e:
        logger.error(f"❌ 모델 로딩 실패: {e}", exc_info=True)
        raise


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """서버 수명 주기에 맞춰 모델을 한 번 로드하고 종료 시 해제한다."""
    global model, tokenizer
    await load_model()
    yield
    model = None
    tokenizer = None
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


app = FastAPI(
    title="EXAONE LLM Server",
    description="고정된 RideSure 데모 결과의 자연어 설명을 생성하는 서버",
    version="0.1-demo",
    lifespan=lifespan,
)

@app.get("/")
async def root():
    """서버 상태 확인"""
    return {
        "status": "running",
        "model": config.MODEL_ID,
        "device": str(device) if device else "unknown",
        "ready": model is not None
    }

@app.get("/health")
async def health_check():
    """헬스체크"""
    if model is None or tokenizer is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {
        "status": "healthy",
        "model_loaded": True,
        "device": str(device)
    }

@app.post("/generate", response_model=GenerateResponse)
async def generate(request: GenerateRequest):
    """
    LLM 추론 엔드포인트

    프롬프트를 받아서 EXAONE 모델로 추론하고 결과 반환
    """
    if model is None or tokenizer is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    try:
        logger.info(f"📝 추론 요청 수신 (길이: {len(request.prompt)} 문자)")

        # 채팅 템플릿 적용
        messages = [
            {
                "role": "system",
                "content": "당신은 대전-세종 광역버스 탑승 예측 전문가입니다. 주어진 정보를 바탕으로 분석하고 추천해주세요."
            },
            {
                "role": "user",
                "content": request.prompt
            }
        ]

        # 토크나이징
        input_text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True
        )

        inputs = tokenizer(
            input_text,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=2048
        ).to(device)

        logger.info(f"🔢 입력 토큰 수: {inputs['input_ids'].shape[1]}")

        # 추론
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=request.max_new_tokens,
                temperature=request.temperature,
                top_p=request.top_p,
                do_sample=True,
                pad_token_id=tokenizer.eos_token_id,
                eos_token_id=tokenizer.eos_token_id
            )

        # 디코딩
        generated_text = tokenizer.decode(
            outputs[0][inputs['input_ids'].shape[1]:],
            skip_special_tokens=True
        ).strip()

        logger.info(f"✅ 추론 완료 (출력 길이: {len(generated_text)} 문자)")
        logger.info(f"📄 생성된 텍스트 미리보기: {generated_text[:100]}...")

        # 🔹 전체 출력 저장
        global latest_output
        latest_output = generated_text

        return GenerateResponse(
            success=True,
            result=generated_text,
            reasoning=generated_text
        )


    except Exception as e:
        logger.error(f"❌ 추론 실패: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/test")
async def test_generation():
    """테스트용 간단한 추론"""
    test_prompt = """
    출발지: 대전역
    도착지: 세종시청
    시각: 09:00

    121번 노선: 승객 45명, 탑승 확률 85%
    991번 노선: 승객 68명, 탑승 확률 60%

    어느 노선을 추천하시겠습니까?
    """

    request = GenerateRequest(
        prompt=test_prompt,
        max_new_tokens=150
    )

    return await generate(request)

@app.get("/debug")
async def debug_last():
    """
    마지막 LLM 출력 전체를 확인하기 위한 디버그 엔드포인트
    웹 브라우저에서 http://localhost:8001/debug_last 로 접속하면 됨
    """
    if latest_output is None:
        return {
            "has_output": False,
            "latest_output": None,
            "length": 0
        }
    return {
        "has_output": True,
        "length": len(latest_output),
        "latest_output": latest_output
    }


if __name__ == "__main__":
    import uvicorn

    # 환경 변수 설정 (선택)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    uvicorn.run(
        app,
        host=config.LLM_HOST,
        port=config.LLM_PORT,
        log_level="info"
    )
