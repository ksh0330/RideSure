# RideSure — 복구된 버스 경로 데모

RideSure는 대전역 → 세종시청 예시 경로와 혼잡 수치를 화면에 표시하고, 로컬 EXAONE 모델이 그 결과의 설명문을 생성하는 FastAPI 데모입니다.

> **현재 버전은 실제 예측 완성본이 아닙니다.** `service.py`가 두 경로, 승객 수, 탑승 확률, 정류장, 소요 시간을 고정 반환합니다. 사용자가 입력한 위치는 카카오 지도 마커에는 반영되지만 예측 결과에는 반영되지 않습니다. Neo4j 데이터도 현재 예측 계산에는 사용되지 않습니다. EXAONE은 혼잡도를 계산하지 않고 고정 결과를 자연어로 설명합니다.

## 빠른 시작

필요한 프로그램은 Windows PowerShell 5.1+, Python 3.12, Docker Desktop/Compose, Node.js(검증용), NVIDIA 드라이버와 CUDA 사용 가능한 GPU입니다. 최초 설치에는 Python 패키지·PyTorch·EXAONE 다운로드를 위한 인터넷과 여유 디스크 공간이 필요합니다.

```powershell
# 최초 설치: .env가 없으면 예제만 복사하고 중단하므로 값을 채운 뒤 다시 실행
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1

# 평상시 전체 시작
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1

# 구성·GPU·DB·서버·지도 설정·최소 추론·예측 API 검증
powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1

# 이 프로젝트가 시작한 Python 프로세스와 Neo4j 컨테이너만 안전하게 종료
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
```

`setup.ps1`은 성공하면 전체 서비스를 시작한 상태로 둡니다. 사용자는 `app.py`와 `llm_server.py`를 직접 실행할 필요가 없습니다.

접속 주소:

- 웹: http://127.0.0.1:8000
- API 문서: http://127.0.0.1:8000/docs
- 전체 상태: http://127.0.0.1:8000/health
- EXAONE 상태: http://127.0.0.1:8001/health
- Neo4j Browser: http://127.0.0.1:7474

문제가 생기면 먼저 `scripts\verify.ps1`을 실행하고 `logs\`의 해당 서버 stderr 로그를 확인합니다.

## 최초 `.env` 설정

처음 실행하면 `.env.example`이 `.env`로 복사됩니다. 최소한 아래 플레이스홀더를 실제 로컬 값으로 교체한 뒤 `setup.ps1`을 다시 실행합니다.

```dotenv
NEO4J_PASS=본인_로컬_DB_비밀번호
KAKAO_MAP_JAVASCRIPT_KEY=본인_카카오_JavaScript_키
```

실제 키·비밀번호는 `.env`에만 두며 `.env`는 Git에서 제외됩니다. 카카오 JavaScript 키는 브라우저 SDK가 사용하므로 개발자 도구에서 보이는 공개 식별자입니다. 보안 경계는 Kakao Developers의 JavaScript SDK 허용 도메인 설정이며, 로컬에서는 `http://localhost:8000`과 `http://127.0.0.1:8000`만 허용하는 것이 좋습니다.

Neo4j 기존 볼륨의 비밀번호는 `.env`나 `NEO4J_AUTH`를 바꾼다고 변경되지 않습니다. 인증 불일치가 발생하면 스크립트가 중단하며 볼륨을 삭제하지 않습니다.

## 설치 스크립트가 하는 일

`setup.ps1`은 프로그램/버전을 점검하고 Python 3.12 가상환경을 만든 뒤 고정 버전 의존성과 CUDA 12.8용 PyTorch를 설치합니다. 그 다음 Neo4j를 healthy 상태까지 시작하고 DB를 분류합니다.

- 빈 DB: CSV를 적재하고 정확한 그래프 개수를 검증
- 완전 DB: 적재를 건너뜀
- 일부 또는 예상 외 DB: 아무것도 삭제·덮어쓰지 않고 오류로 중단

이후 EXAONE 스냅샷이 없으면 `.cache/huggingface/ridesure-exaone`에 일반 파일로 다운로드하고 실제 CUDA 모델 로드, 8001 health, 최소 추론, FastAPI와 예측 API까지 검증합니다. 일반 파일 디렉터리를 쓰므로 Windows 개발자 모드나 관리자 심볼릭 링크 권한이 필요하지 않으며 모델은 Git 대상이 아닙니다.

검증된 주요 환경은 Python 3.12.9, PyTorch 2.11.0+cu128, Transformers 4.57.1, Accelerate 1.14.0, FastAPI 0.141.1, Neo4j 드라이버 6.2.0, pandas 3.0.5, Neo4j 5.26 Community입니다. 직접 의존성은 `requirements.txt`, 콜드 스타트에서 해석된 전체 의존성은 `requirements-lock.txt`, GPU 패키지는 `requirements-cuda.txt`에 기록돼 있습니다.

## Git에 없는 것과 공개 제한

`.env`, `.venv`, Hugging Face 모델/캐시, 로그, PID, Python 캐시와 Docker 볼륨은 재생성 가능하거나 비밀/대용량 로컬 상태이므로 Git에서 제외됩니다. `.env.example`, 코드, 스크립트, 문서는 포함됩니다.

CSV 3개의 원 출처와 재배포 라이선스는 현재 파일과 문서만으로 확인되지 않았습니다. 자세한 내용은 `docs/DATA_SOURCES.md`에 기록했습니다. 라이선스를 확인하기 전에는 이 저장소와 CSV를 공개 저장소로 push하지 마세요.

전체 구조, DB 스키마, 새 PC 설치와 장애 대응은 [프로젝트 가이드](docs/PROJECT_GUIDE.md)를 참고하세요.
