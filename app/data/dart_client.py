"""
DartClient - DART(금융감독원 전자공시시스템) Open API 클라이언트.

API 키는 환경변수에서만 읽으며 절대 로그/출력에 노출하지 않습니다.
DART API 장애 시 빈 결과를 반환하고 프로그램 전체를 중단시키지 않습니다.
"""

import time
import requests
from datetime import datetime, timedelta
from app.logger import logger

DART_API_BASE = "https://opendart.fss.or.kr/api"


class DartClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session = requests.Session()

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def get_recent_disclosures(
        self,
        corp_name: str = "",
        bgn_de: str = "",
        end_de: str = "",
        page_count: int = 10,
    ) -> list[dict]:
        """
        최근 공시 목록 조회.

        Parameters
        ----------
        corp_name : str  회사명 (비어있으면 전체)
        bgn_de    : str  조회 시작일 YYYYMMDD
        end_de    : str  조회 종료일 YYYYMMDD
        page_count: int  페이지당 건수

        Returns
        -------
        list[dict]  공시 목록. 실패 시 [] 반환.
        각 항목: {corp_name, stock_code, report_nm, rcept_dt, flr_nm, rcept_no}
        """
        if not self.is_configured():
            logger.debug("[DART] API 키 없음 → 공시 조회 스킵")
            return []

        today = datetime.now().strftime("%Y%m%d")
        if not end_de:
            end_de = today
        if not bgn_de:
            bgn_de = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

        params = {
            "crtfc_key": self._api_key,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_no": "1",
            "page_count": str(page_count),
        }
        if corp_name:
            params["corp_name"] = corp_name

        try:
            resp = self._session.get(
                f"{DART_API_BASE}/list.json",
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "")
            if status not in ("000", "013"):  # 013 = no data
                logger.warning("[DART] API 응답 오류: status=%s message=%s", status, data.get("message", ""))
                return []
            items = data.get("list", [])
            logger.debug("[DART] %s 공시 %d건 조회", corp_name or "전체", len(items))
            return items
        except Exception as e:
            logger.warning("[DART] 공시 조회 실패 (%s): %s", corp_name, e)
            return []

    def get_disclosures_for_symbols(
        self,
        symbols_names: list[tuple[str, str]],
        lookback_days: int = 7,
    ) -> dict[str, list[dict]]:
        """
        종목 목록에 대해 최근 공시를 일괄 조회합니다.

        Parameters
        ----------
        symbols_names : list[(symbol, name)]
        lookback_days : int

        Returns
        -------
        dict[symbol -> list[공시항목]]
        """
        if not self.is_configured():
            return {}

        bgn_de = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
        end_de = datetime.now().strftime("%Y%m%d")
        result: dict[str, list[dict]] = {}

        for symbol, name in symbols_names:
            disclosures = self.get_recent_disclosures(
                corp_name=name,
                bgn_de=bgn_de,
                end_de=end_de,
                page_count=5,
            )
            result[symbol] = disclosures
            time.sleep(0.2)  # DART API rate limit 배려

        return result


def create_dart_client() -> DartClient:
    """환경변수에서 DART API 키를 읽어 DartClient를 생성합니다."""
    from app.config import get_dart_api_key
    key = get_dart_api_key()
    client = DartClient(api_key=key)
    if client.is_configured():
        logger.info("[DART] 클라이언트 초기화 완료")
    else:
        logger.warning("[DART] API 키 없음 — 공시 점수 비활성화")
    return client
