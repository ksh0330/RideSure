"""Value-safe configuration, environment, database, and live-service checks."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

import config


class InlineScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capturing = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script" and not dict(attrs).get("src"):
            self._capturing = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self.scripts.append("".join(self._parts))
            self._capturing = False


def emit(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, sort_keys=True))


def check_config() -> None:
    config.validate_required_config(
        ("neo4j", "neo4j_v2", "llm_client", "model", "app", "llm_server", "kakao")
    )
    emit({"check": "config", "status": "ok"})


def settings() -> None:
    """Return only non-secret values needed by PowerShell orchestration."""
    emit(
        {
            "app_port": config.APP_PORT,
            "llm_port": config.LLM_PORT,
            "llm_base_url": config.LLM_BASE_URL,
            "neo4j_uri": config.NEO4J_URI,
            "neo4j_v2_uri": config.NEO4J_V2_URI,
        }
    )


def check_environment(require_cuda: bool) -> None:
    import accelerate
    import fastapi
    import neo4j
    import pandas
    import torch
    import transformers
    import uvicorn
    from huggingface_hub import snapshot_download

    failures = []
    if sys.version_info[:2] != (3, 12):
        failures.append(f"Python 3.12 required; found {sys.version.split()[0]}")
    if torch.version.cuda is None:
        failures.append("Installed PyTorch is not a CUDA build")
    if require_cuda and not torch.cuda.is_available():
        failures.append("NVIDIA CUDA is not available to PyTorch")

    config.validate_required_config(("model",))
    try:
        model_path = Path(config.MODEL_PATH)
        if model_path.is_dir():
            model_cached = all(
                (model_path / name).is_file()
                for name in ("config.json", "tokenizer_config.json", "tokenizer.json")
            ) and any(model_path.glob("*.safetensors"))
            if not model_cached:
                raise FileNotFoundError(
                    "Configured MODEL_PATH does not contain a complete model snapshot."
                )
        else:
            # Backward-compatible check for a cache created before MODEL_PATH existed.
            snapshot_download(config.MODEL_ID, local_files_only=True)
            model_cached = True
    except Exception:
        model_cached = False
        failures.append("Configured model is not complete in the configured local cache")

    result = {
        "check": "environment",
        "status": "failed" if failures else "ok",
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "torch_cuda_build": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "transformers": transformers.__version__,
        "accelerate": accelerate.__version__,
        "fastapi": fastapi.__version__,
        "uvicorn": uvicorn.__version__,
        "neo4j": neo4j.__version__,
        "pandas": pandas.__version__,
        "model_cached": model_cached,
        "failures": failures,
    }
    emit(result)
    if failures:
        raise SystemExit(1)


def check_database() -> None:
    from data_insert import EXPECTED_COUNTS, inspect_database, open_driver

    driver = open_driver()
    try:
        snapshot = inspect_database(driver)
    finally:
        driver.close()
    result = {
        "check": "database",
        "status": "ok" if snapshot.state == "complete" else "failed",
        "state": snapshot.state,
        "counts": snapshot.counts,
        "expected": EXPECTED_COUNTS,
        "orphan_loads": snapshot.orphan_loads,
        "sample_query_succeeded": snapshot.sample is not None,
    }
    emit(result)
    if snapshot.state != "complete":
        raise SystemExit(1)


def check_database_v2() -> None:
    from data_insert_v2 import inspect_database, open_driver, verify_b1

    driver = open_driver()
    try:
        snapshot = inspect_database(driver)
        b1 = verify_b1(driver) if snapshot.state == "complete" else None
    finally:
        driver.close()
    ok = snapshot.state == "complete" and b1 is not None and b1["status"] == "ok"
    emit(
        {
            "check": "database_v2",
            "status": "ok" if ok else "failed",
            "state": snapshot.state,
            "counts": snapshot.counts,
            "orphan_count": snapshot.orphan_count,
            "sequence_error_count": snapshot.sequence_error_count,
            "provenance_error_count": snapshot.provenance_error_count,
            "b1": b1,
        }
    )
    if not ok:
        raise SystemExit(1)


def _get(url: str, timeout: float = 10) -> requests.Response:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response


def check_services(run_inference: bool) -> None:
    config.validate_required_config(("llm_client", "app", "kakao"))
    app_base = f"http://127.0.0.1:{config.APP_PORT}"
    failures: list[str] = []
    details: dict[str, Any] = {}

    try:
        llm_health = _get(f"{config.LLM_BASE_URL}/health").json()
        details["llm_health"] = llm_health.get("status") == "healthy"
        details["llm_device"] = llm_health.get("device")
    except Exception as exc:
        failures.append(f"LLM health failed: {exc}")
        details["llm_health"] = False

    if run_inference and details.get("llm_health"):
        try:
            response = requests.post(
                f"{config.LLM_BASE_URL}/generate",
                json={
                    "prompt": "한 문장으로 'RideSure 설명 서버 정상'이라고 답하세요.",
                    "max_new_tokens": 12,
                    "temperature": 0.1,
                    "top_p": 0.9,
                },
                timeout=120,
            )
            response.raise_for_status()
            payload = response.json()
            details["llm_inference"] = bool(payload.get("success") and payload.get("result"))
            if not details["llm_inference"]:
                failures.append("LLM inference returned no generated result")
        except Exception as exc:
            failures.append(f"LLM inference failed: {exc}")
            details["llm_inference"] = False

    try:
        app_health = _get(f"{app_base}/health").json()
        details["app_health"] = app_health.get("status") == "healthy"
        if not details["app_health"]:
            failures.append(f"FastAPI health is {app_health.get('status')}")
    except Exception as exc:
        failures.append(f"FastAPI health failed: {exc}")
        details["app_health"] = False

    try:
        details["docs"] = _get(f"{app_base}/docs").status_code == 200
    except Exception as exc:
        failures.append(f"FastAPI docs failed: {exc}")
        details["docs"] = False

    try:
        frontend = _get(f"{app_base}/api/frontend-config").json()
        details["frontend_config"] = bool(frontend.get("kakao_map_javascript_key"))
        if not details["frontend_config"]:
            failures.append("frontend-config did not return the Kakao JavaScript setting")
    except Exception as exc:
        failures.append(f"frontend-config failed: {exc}")
        details["frontend_config"] = False

    try:
        html = _get(f"{app_base}/").text
        details["map_sdk_path"] = (
            "/api/frontend-config" in html
            and "https://dapi.kakao.com/v2/maps/sdk.js" in html
        )
        if not details["map_sdk_path"]:
            failures.append("index.html does not contain the runtime Kakao SDK configuration path")
    except Exception as exc:
        failures.append(f"index.html check failed: {exc}")
        details["map_sdk_path"] = False

    try:
        response = requests.post(
            f"{app_base}/api/predict",
            json={
                "origin": "대전역",
                "destination": "세종시청",
                "departure_time": "08:00",
                "date": "2025-11-08",
            },
            timeout=120,
        )
        response.raise_for_status()
        prediction = response.json()
        details["prediction_smoke"] = bool(prediction.get("success") and prediction.get("routes"))
        first_route = prediction.get("routes", [{}])[0]
        details["prediction_uses_v2"] = (
            prediction.get("origin") == "대전역"
            and prediction.get("destination") == "세종시청"
            and first_route.get("line_name") == "B1"
            and first_route.get("onboard_count") == 17
            and first_route.get("evidence_source") == "HISTORICAL_OBSERVATION"
        )
        details["no_fake_boarding_probability"] = all(
            route.get("boarding_probability") is None
            for route in prediction.get("routes", [])
        )
        if not details["prediction_smoke"]:
            failures.append("Prediction smoke test returned no routes")
        if not details["prediction_uses_v2"]:
            failures.append("Prediction did not return expected Neo4j v2 B1 historical evidence")
        if not details["no_fake_boarding_probability"]:
            failures.append("Prediction returned a fabricated boarding probability")
    except Exception as exc:
        failures.append(f"Prediction smoke test failed: {exc}")
        details["prediction_smoke"] = False

    emit(
        {
            "check": "services",
            "status": "failed" if failures else "ok",
            "details": details,
            "failures": failures,
        }
    )
    if failures:
        raise SystemExit(1)


def check_javascript() -> None:
    html_path = config.PROJECT_ROOT / "static" / "index.html"
    parser = InlineScriptParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    if not parser.scripts:
        raise RuntimeError("No inline JavaScript found in static/index.html")
    for index, source in enumerate(parser.scripts, start=1):
        result = subprocess.run(
            ["node", "--check", "-"],
            input=source,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Inline JavaScript block {index} failed syntax check: {result.stderr}")
    emit({"check": "javascript", "status": "ok", "inline_blocks": len(parser.scripts)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("config")
    subparsers.add_parser("settings")
    environment_parser = subparsers.add_parser("environment")
    environment_parser.add_argument("--require-cuda", action="store_true")
    subparsers.add_parser("database")
    subparsers.add_parser("database-v2")
    services_parser = subparsers.add_parser("services")
    services_parser.add_argument("--skip-inference", action="store_true")
    subparsers.add_parser("javascript")
    args = parser.parse_args()

    commands = {
        "config": check_config,
        "settings": settings,
        "environment": lambda: check_environment(args.require_cuda),
        "database": check_database,
        "database-v2": check_database_v2,
        "services": lambda: check_services(not args.skip_inference),
        "javascript": check_javascript,
    }
    commands[args.command]()


if __name__ == "__main__":
    main()
