"""RideSure runtime configuration loaded from the project-local ``.env`` file."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent
DOTENV_PATH = Path(os.getenv("RIDESURE_ENV_FILE", PROJECT_ROOT / ".env"))
load_dotenv(DOTENV_PATH, override=False)

# Windows pipes otherwise commonly fall back to CP949 and fail on Unicode logs.
for stream_name in ("stdout", "stderr"):
    stream = getattr(sys, stream_name, None)
    if stream is not None and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer (configured value was invalid).") from exc


@dataclass(frozen=True)
class Config:
    # Neo4j has no password default: credentials must come from .env/environment.
    NEO4J_URI: str = _env("NEO4J_URI")
    NEO4J_USER: str = _env("NEO4J_USER")
    NEO4J_PASS: str = _env("NEO4J_PASS")

    # Local EXAONE explanation server.
    MODEL_ID: str = _env("MODEL_ID")
    HF_HOME: str = _env("HF_HOME")
    MODEL_PATH: str = _env(
        "MODEL_PATH",
        str(Path(_env("HF_HOME", ".cache/huggingface")) / "ridesure-exaone"),
    )
    HF_OFFLINE: bool = env_flag("HF_HUB_OFFLINE", False)
    LLM_BASE_URL: str = _env("LLM_BASE_URL")

    TARGET_DATE: str = _env("TARGET_DATE", "2025-11-08")
    MAX_NEW_TOKENS: int = _env_int("MAX_NEW_TOKENS", 160)
    APP_HOST: str = _env("APP_HOST", "127.0.0.1")
    APP_PORT: int = _env_int("APP_PORT", 8000)
    LLM_HOST: str = _env("LLM_HOST", "127.0.0.1")
    LLM_PORT: int = _env_int("LLM_PORT", 8001)
    KAKAO_MAP_JAVASCRIPT_KEY: str = _env("KAKAO_MAP_JAVASCRIPT_KEY")


_config = Config()

NEO4J_URI = _config.NEO4J_URI
NEO4J_USER = _config.NEO4J_USER
NEO4J_PASS = _config.NEO4J_PASS
MODEL_ID = _config.MODEL_ID
HF_HOME = _config.HF_HOME
MODEL_PATH = _config.MODEL_PATH
HF_OFFLINE = _config.HF_OFFLINE
TARGET_DATE = _config.TARGET_DATE
MAX_NEW_TOKENS = _config.MAX_NEW_TOKENS
LLM_BASE_URL = _config.LLM_BASE_URL
APP_HOST = _config.APP_HOST
APP_PORT = _config.APP_PORT
LLM_HOST = _config.LLM_HOST
LLM_PORT = _config.LLM_PORT
KAKAO_MAP_JAVASCRIPT_KEY = _config.KAKAO_MAP_JAVASCRIPT_KEY


CONFIG_GROUPS = {
    "neo4j": ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASS"),
    "llm_client": ("LLM_BASE_URL",),
    "model": ("MODEL_ID", "HF_HOME", "MODEL_PATH"),
    "app": ("APP_HOST", "APP_PORT"),
    "llm_server": ("LLM_HOST", "LLM_PORT"),
    "kakao": ("KAKAO_MAP_JAVASCRIPT_KEY",),
}

PLACEHOLDER_VALUES = {
    "your_neo4j_password",
    "your_kakao_javascript_key",
    "change_me",
    "replace_me",
}


def validate_required_config(groups: Iterable[str]) -> None:
    """Raise a value-free error naming missing/placeholder configuration keys."""
    names: list[str] = []
    unknown: list[str] = []
    for group in groups:
        if group not in CONFIG_GROUPS:
            unknown.append(group)
            continue
        names.extend(CONFIG_GROUPS[group])
    if unknown:
        raise RuntimeError(f"Unknown configuration group(s): {', '.join(unknown)}")

    missing = []
    for name in dict.fromkeys(names):
        value = str(globals()[name]).strip()
        if not value or value.lower() in PLACEHOLDER_VALUES:
            missing.append(name)
    if missing:
        raise RuntimeError(
            "Required setting(s) are missing or still placeholders in .env: "
            + ", ".join(missing)
        )


HOUR_COLS = [f"{hour:02d}시" for hour in range(24)]
INPUT_FILES = [
    str(PROJECT_ROOT / "노선·정류장 지표(노선별 차내 재차인원)_20251108 (1).csv"),
    str(PROJECT_ROOT / "노선·정류장 지표(노선별 차내 재차인원)_20251108 (2).csv"),
    str(PROJECT_ROOT / "노선·정류장 지표(노선별 차내 재차인원)_20251108 (3).csv"),
]
