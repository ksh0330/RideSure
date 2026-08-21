# service.py
"""
Neo4j + LLM 통합 서비스 레이어
FastAPI와 기존 query_utils, LLM 코드를 연결
"""
from typing import List, Dict, Optional, Tuple
from neo4j import GraphDatabase
import logging
from datetime import datetime

import config

logger = logging.getLogger(__name__)

class BusPredictionService:
    def __init__(self):
        config.validate_required_config(("neo4j", "llm_client"))
        self.driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USER, config.NEO4J_PASS)
        )
        # TODO: LLM 초기화
        # self.llm = initialize_llm()

    def close(self):
        self.driver.close()

    def check_connection(self) -> bool:
        """Neo4j에 실제로 간단한 쿼리를 실행해 연결 상태를 확인한다."""
        try:
            with self.driver.session() as session:
                session.run("RETURN 1 AS ok").single(strict=True)
            return True
        except Exception as exc:
            logger.warning("Neo4j 연결 확인 실패: %s", exc)
            return False

    def find_nearest_stop(self, location_name: str) -> Optional[Dict]:
        """
        위치 이름으로 가장 가까운 정류장 찾기

        Args:
            location_name: 위치 이름 (예: "대전역", "세종시청")

        Returns:
            {"stop_id": "...", "stop_name": "...", "lat": ..., "lon": ...}
        """
        query = """
        MATCH (s:Stop)
        WHERE s.name CONTAINS $location
        RETURN s.id AS stop_id, s.name AS stop_name
        LIMIT 5
        """

        with self.driver.session() as session:
            result = session.run(query, location=location_name)
            stops = [dict(record) for record in result]

            if stops:
                logger.info(f"'{location_name}' 검색 결과: {len(stops)}개")
                return stops[0]  # 첫 번째 매칭 결과 반환
            else:
                logger.warning(f"'{location_name}'에 해당하는 정류장을 찾을 수 없음")
                return None

    def find_routes_between_stops(
        self,
        origin_stop_id: str,
        dest_stop_id: str
    ) -> List[Dict]:
        """
        두 정류장 사이의 버스 노선 찾기
        (현재는 데모용 하드코딩을 쓰기 때문에 사용하지 않음)
        """
        query = """
        MATCH (origin:Stop {id: $origin_id})<-[:HAS_STOP]-(line:Line)-[:HAS_STOP]->(dest:Stop {id: $dest_id})
        MATCH (line)-[r:HAS_STOP]->(stop:Stop)
        WITH line, origin, dest, r, stop
        ORDER BY r.seq
        RETURN
            line.id AS line_id,
            line.name AS line_name,
            collect(stop.name) AS stops,
            collect(stop.id) AS stop_ids
        LIMIT 10
        """

        with self.driver.session() as session:
            result = session.run(
                query,
                origin_id=origin_stop_id,
                dest_id=dest_stop_id
            )
            routes = [dict(record) for record in result]
            logger.info(f"경로 탐색 결과: {len(routes)}개")
            return routes

    def get_route_load_data(
        self,
        line_id: str,
        stop_id: str,
        date: str,
        hour: int
    ) -> Dict:
        """
        특정 노선/정류장의 시간대별 승객 데이터 조회
        (현재는 데모용 하드코딩을 쓰기 때문에 사용하지 않음)
        """
        query = """
        MATCH (ld:Load)-[:AT]->(s:Stop {id: $stop_id})
        MATCH (ld)-[:AFFECTS]->(l:Line {id: $line_id})
        WHERE ld.date = $date AND ld.hour = $hour
        RETURN ld.count AS count
        """

        with self.driver.session() as session:
            result = session.run(
                query,
                stop_id=stop_id,
                line_id=line_id,
                date=date,
                hour=hour
            )
            record = result.single()

            if record:
                return {
                    "line_id": line_id,
                    "stop_id": stop_id,
                    "date": date,
                    "hour": hour,
                    "count": record["count"]
                }
            else:
                # 데이터가 없는 경우 0 반환
                return {
                    "line_id": line_id,
                    "stop_id": stop_id,
                    "date": date,
                    "hour": hour,
                    "count": 0
                }

    def calculate_boarding_probability(
        self,
        route: Dict,
        load_data: Dict,
        time_of_day: int
    ) -> float:
        """
        탑승 확률 계산 (간단한 휴리스틱)
        """
        passenger_count = load_data.get("count", 0)

        # 간단한 규칙 기반 계산
        # 승객 수가 적을수록 탑승 확률 높음
        if passenger_count < 30:
            return 90.0
        elif passenger_count < 50:
            return 75.0
        elif passenger_count < 70:
            return 50.0
        else:
            return 25.0

    def predict_with_llm(
        self,
        routes: List[Dict],
        load_data_list: List[Dict],
        origin: str,
        destination: str,
        departure_time: Optional[str] = None
    ) -> Dict:
        """
        LLM을 사용한 경로 추천 및 추론

        LLM 서버(localhost:8001)에 요청을 보내서 추론 결과를 받아옴
        """
        import requests
        import json

        # 프롬프트 구성
        route_info = "\n".join([
            f"- {r['line_name']}: 승객 {ld.get('count', 0)}명 (탑승 확률 {r.get('boarding_probability', 0):.0f}%)"
            for r, ld in zip(routes, load_data_list)
        ])

        prompt = f"""
다음은 버스 탑승 예측 분석 요청입니다.

출발지: {origin}
도착지: {destination}
출발 시각: {departure_time if departure_time else '현재 시각'}

이용 가능한 노선(대전 → 세종시청):

{route_info}

질문:
B1번 노선을 추천하고 그 이유를 말하세요

200자 이내로 간단명료하게 답변해주세요.
"""

        try:
            # LLM 서버에 요청
            logger.info("🤖 LLM 서버에 추론 요청 중...")

            response = requests.post(
                f"{config.LLM_BASE_URL}/generate",
                json={
                    "prompt": prompt,
                    "max_new_tokens": 200,
                    "temperature": 0.7,
                    "top_p": 0.9
                },
                timeout=120
            )

            if response.status_code == 200:
                result = response.json()
                reasoning = result.get("result", "").strip()

                logger.info(f"✅ LLM 응답 수신: {reasoning[:100]}...")

                # 대안 경로 추출 (간단한 휴리스틱)
                alternatives = []
                if "환승" in reasoning:
                    alternatives.append("환승 경로를 고려해보세요")
                if len(routes) > 1:
                    alternatives.append(f"대안: {routes[1]['line_name']}")

                return {
                    "reasoning": reasoning,
                    "alternatives": alternatives if alternatives else None
                }
            else:
                logger.warning(f"⚠️ LLM 서버 응답 오류: {response.status_code}")
                # Fallback: 규칙 기반
                return self._fallback_reasoning(routes, origin, destination)

        except requests.exceptions.Timeout:
            logger.error("⏱️ LLM 서버 타임아웃")
            return self._fallback_reasoning(routes, origin, destination)
        except Exception as e:
            logger.error(f"❌ LLM 요청 실패: {e}")
            return self._fallback_reasoning(routes, origin, destination)

    def _fallback_reasoning(self, routes: List[Dict], origin: str, destination: str) -> Dict:
        """LLM 실패 시 Fallback 로직"""
        if not routes:
            return {
                "reasoning": "이용 가능한 노선을 찾을 수 없습니다.",
                "alternatives": None
            }

        best_route = routes[0]
        reasoning = (
            f"{best_route['line_name']}을(를) 추천합니다. "
            f"탑승 확률이 {best_route.get('boarding_probability', 0):.0f}%로 가장 높고, "
            f"예상 승객 수는 {best_route.get('expected_load', 0)}명입니다."
        )

        alternatives = []
        if len(routes) > 1:
            alternatives.append(f"대안: {routes[1]['line_name']}")

        return {
            "reasoning": reasoning,
            "alternatives": alternatives if alternatives else None
        }

    def predict_boarding(
        self,
        origin: str,
        destination: str,
        departure_time: Optional[str] = None,
        date: str = "2025-11-08"
    ) -> Dict:
        """
        전체 예측 파이프라인 (복구된 데모의 하드코딩 버전)

        - 사용자 출발지/도착지/날짜는 계산에 사용하지 않음
        - 출발 시각은 EXAONE 설명 프롬프트에만 전달됨
        - 경로, 승객 수, 확률, 소요 시간은 두 경로로 고정됨
        - Neo4j 조회 함수는 존재하지만 이 메서드에서 호출하지 않음
        """
        # ⏰ 시간 파싱 (UI에서 입력 받는 값 그대로 사용)
        hour = 9  # 기본값
        if departure_time:
            try:
                dt = datetime.strptime(departure_time, "%H:%M")
                hour = dt.hour
            except ValueError:
                logger.warning(f"시간 파싱 실패: {departure_time}, 기본값 사용")

        # ✅ 프롬프트용 출발/도착지 하드코딩
        origin_fixed = "대전역"
        destination_fixed = "세종시청,시의회,교육청"

        # ✅ UI에 보여줄 경로 3개 하드코딩 (B1 포함)
        routes = [
            {
                "line_id": "B1",
                "line_name": "B1",
                "boarding_probability": 70.0,
                "expected_load": 20,
                "stops": [
  "대전역",
  "한밭자이아파트",
  "솔랑마을아파트",
  "대덕구청",
  "한남오거리(BRT)",
  "오정동행정복지센터",
  "오정농수산오거리",
  "오정농수산시장",
  "대덕산업단지",
  "한국개발연구원(KDI)",
  "소담동(새샘마을)",
  "세종시청,시의회,교육청"
],
                "travel_time": 41,   # 분
            },
            {
                "line_id": "202/613+1002",
                "line_name": "101 / 1002 (환승 1회)",
                "boarding_probability": 90.0,
                "expected_load": 14,
                "stops": [
  "대전역",
  "목척교",
  "중앙로역6번출구",
  "중구청역",
  "대전청남부우정",
  "서대전네거리역5번출구",
  "중도일보",
  "오룡역5번출구",
  "용문역7번출구",
  "용문역5번출구",
  "서부농협본점",
  "개나리아파트",
  "대전삼성화의소",
  "큰마을네거리",
  "갈마육교",
  "갈마네거리",
  "대전일보사",
  "월평삼거리",
  "대전교통공사",
  "만보교",
  "유성온천역7번출구",
  "온천교",
  "충남대학교",
  "장대네거리",
  "죽동네거리",
  "노은농수산물시장",
  "월드컵경기장역",
  "노은역",
  "엑스포5.6단지",
  "유성정류장",
  "유성장애인복지관",
  "지족역",
  "송림마을1단지",
  "반석역",

  "반석마을입구(환승)",

  "외삼삼거리",
  "세종고속시외버스터미널",
  "대평동(해들마을)",
  "보람동(해들마을)",
  "세종시청,시의회,교육청,세무서"
]
,
                "travel_time": 82,
            },
        ]

        # LLM 프롬프트에 넣을 승객 수 정보
        load_data_list = [
            {"count": r["expected_load"]} for r in routes
        ]

        # LLM 호출 (프롬프트에는 항상 대전역 → 세종시청)
        llm_result = self.predict_with_llm(
            routes,
            load_data_list,
            origin_fixed,
            destination_fixed,
            departure_time,
        )

        # FastAPI 응답 구조 맞춰서 리턴
        return {
            "success": True,
            # 클라이언트에는 실제 입력값 대신 고정 출발/도착지를 보여주고 싶으면 origin_fixed 사용
            "origin": origin_fixed,
            "destination": destination_fixed,
            "routes": routes,
            "reasoning": llm_result["reasoning"],
            "alternatives": llm_result.get("alternatives"),
        }

# 싱글톤 인스턴스
_service_instance = None

def get_service() -> BusPredictionService:
    """서비스 인스턴스 가져오기 (싱글톤)"""
    global _service_instance
    if _service_instance is None:
        _service_instance = BusPredictionService()
    return _service_instance
