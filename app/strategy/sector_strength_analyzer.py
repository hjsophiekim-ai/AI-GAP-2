"""
sector_strength_analyzer.py

NXT 거래대금 수집 결과에서 섹터별 강도를 계산한다.

섹터별 계산 항목:
  sector_total_trading_value   : 섹터 내 거래대금 합계
  sector_avg_change_rate       : 섹터 내 평균 상승률
  sector_top_stock_trading_value: 섹터 내 1위 종목 거래대금
  sector_leader_concentration  : 1위 종목 TV / 섹터 전체 TV
  sector_stock_count           : 섹터 내 후보 종목 수
  volume_spike_overlap_count   : 거래량 급증 페이지 중복 종목 수
  us_sector_match              : 미국장 강세 섹터 일치 여부 (strong/moderate/none)
  sector_strength_score        : 최종 섹터 강도 점수 (max 35)

sector_strength_score 산식:
  거래대금 순위: 1위 +10, 2위 +8, 3위 +6, 4위 +4, 5위+ +2        (max 10)
  평균 상승률:   5%이상 +8, 3%이상 +5, 1%이상 +2, 0%이하 0         (max 8)
  종목 수:       5개이상 +5, 3개이상 +3, 1개이상 +1                  (max 5)
  거래량급증:    2개이상 +5, 1개이상 +2                              (max 5)
  미국장 매칭:   strong +7, moderate +3                             (max 7)
  합계 max = 35

대장주(leader) 선정 기준:
  - 섹터 내 거래대금 1위
  - current_price >= 20,000원
  - 2% <= change_rate <= 15%
  - ETF/ETN/우선주/스팩/리츠 아님

leader_score:
  sector_top_by_tv      : +15
  overall_top20         : +5  (NXT 전체 rank <= 20)
  volume_spike_overlap  : +5
  ma_alignment          : +5  (placeholder — 외부에서 MA 데이터 주입 시 활성화)
  us_sector_match       : +5
"""

from __future__ import annotations

from typing import Optional

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# ── 대장주 필터 기준 ───────────────────────────────────────────────────────────
_LEADER_MIN_PRICE = 20_000
_LEADER_MIN_CHANGE_RATE = 2.0
_LEADER_MAX_CHANGE_RATE = 15.0

# ── 섹터 강도 점수 배점 ────────────────────────────────────────────────────────
_TV_RANK_SCORES = {1: 10, 2: 8, 3: 6, 4: 4}   # 5위+ → 2
_CR_THRESHOLDS = [(5.0, 8), (3.0, 5), (1.0, 2)]  # (임계값, 점수)
_COUNT_THRESHOLDS = [(5, 5), (3, 3), (1, 1)]
_VS_THRESHOLDS = [(2, 5), (1, 2)]
_US_SCORES = {"strong": 7, "moderate": 3}

# ── 대장주 점수 배점 ───────────────────────────────────────────────────────────
_LEADER_SCORE_TV = 15
_LEADER_SCORE_TOP20 = 5
_LEADER_SCORE_VS = 5
_LEADER_SCORE_MA = 5          # placeholder
_LEADER_SCORE_US = 5


def _is_excluded(stock: dict) -> bool:
    return bool(
        stock.get("is_etf") or stock.get("is_etn")
        or stock.get("is_preferred") or stock.get("is_spac")
        or stock.get("is_reit") or stock.get("is_suspended")
        or stock.get("is_halt")
    )


def _tv_rank_score(rank: int) -> int:
    return _TV_RANK_SCORES.get(rank, 2)


def _cr_score(avg_cr: float) -> int:
    for threshold, score in _CR_THRESHOLDS:
        if avg_cr >= threshold:
            return score
    return 0


def _count_score(count: int) -> int:
    for threshold, score in _COUNT_THRESHOLDS:
        if count >= threshold:
            return score
    return 0


def _vs_score(overlap: int) -> int:
    for threshold, score in _VS_THRESHOLDS:
        if overlap >= threshold:
            return score
    return 0


def _us_score(match_level: str) -> int:
    return _US_SCORES.get(match_level, 0)


class SectorStrengthAnalyzer:
    """
    섹터별 강도 계산기.

    Usage
    -----
    analyzer = SectorStrengthAnalyzer()
    sector_results = analyzer.analyze(nxt_stocks, volume_spike_symbols, us_sector_results)
    top5 = analyzer.get_top_sectors(5)
    leader = analyzer.get_sector_leader("semiconductor")
    """

    def __init__(self):
        self._sector_data: dict[str, dict] = {}   # sector → aggregated info

    # ── 메인 분석 ──────────────────────────────────────────────────────────────

    def analyze(
        self,
        nxt_stocks: list[dict],
        volume_spike_symbols: Optional[set[str]] = None,
        us_sector_results: Optional[dict] = None,
    ) -> dict[str, dict]:
        """
        Parameters
        ----------
        nxt_stocks : list[dict]
            NaverNxtTurnoverCollector + SectorMapper 적용 결과.
            각 dict에 'sector', 'trading_value', 'change_rate', 'current_price',
            'symbol', 'rank' 필드가 있어야 한다.
        volume_spike_symbols : set[str] | None
            거래량 급증 페이지에 등장한 symbol 집합.
        us_sector_results : dict | None
            USSectorStrengthService 결과. 형태:
            {"semiconductor": {"match_level": "strong"|"moderate"|"none"}, ...}

        Returns
        -------
        dict[str, dict]  — {sector_key: sector_info_dict}
        """
        vs_symbols = volume_spike_symbols or set()
        raw_us = us_sector_results or {}

        # USSectorStrengthService 출력 포맷 ({strong_sectors, moderate_sectors, ...}) 정규화
        if "strong_sectors" in raw_us or "moderate_sectors" in raw_us:
            us_results: dict = {}
            for sec in raw_us.get("strong_sectors", []):
                us_results[sec] = {"match_level": "strong"}
            for sec in raw_us.get("moderate_sectors", []):
                if sec not in us_results:
                    us_results[sec] = {"match_level": "moderate"}
        else:
            us_results = raw_us

        # ── 1. 섹터별 종목 그룹화 ──────────────────────────────────────────────
        sector_buckets: dict[str, list[dict]] = {}
        for stock in nxt_stocks:
            sector = stock.get("sector", "unknown")
            if sector not in sector_buckets:
                sector_buckets[sector] = []
            sector_buckets[sector].append(stock)

        # ── 2. 섹터별 지표 계산 ───────────────────────────────────────────────
        sector_summaries: list[dict] = []
        for sector, stocks in sector_buckets.items():
            if not stocks:
                continue

            # 거래대금 기준 정렬 (내림차순)
            stocks_sorted = sorted(
                stocks,
                key=lambda s: s.get("trading_value", 0),
                reverse=True,
            )

            total_tv = sum(s.get("trading_value", 0) for s in stocks)
            avg_cr = (
                sum(s.get("change_rate", 0) for s in stocks) / len(stocks)
                if stocks else 0.0
            )
            top_stock = stocks_sorted[0]
            top_tv = top_stock.get("trading_value", 0)
            concentration = top_tv / total_tv if total_tv > 0 else 0.0
            count = len(stocks)
            vs_overlap = sum(1 for s in stocks if s.get("symbol") in vs_symbols)

            # 미국장 매칭 수준
            us_match_info = us_results.get(sector, {})
            us_match_level = us_match_info.get("match_level", "none")
            us_match_bool = us_match_level in ("strong", "moderate")

            # 대장주 선정
            leader = self._select_leader(stocks_sorted, vs_symbols, us_match_level)

            summary = {
                "sector": sector,
                "sector_stock_count": count,
                "sector_total_trading_value": total_tv,
                "sector_avg_change_rate": round(avg_cr, 2),
                "sector_top_stock_trading_value": top_tv,
                "sector_leader_concentration": round(concentration, 3),
                "volume_spike_overlap_count": vs_overlap,
                "us_sector_match": us_match_bool,
                "us_sector_match_level": us_match_level,
                "leader": leader,
                "stocks": stocks_sorted,
            }
            sector_summaries.append(summary)

        # ── 3. 거래대금 순위 부여 후 섹터 강도 점수 계산 ────────────────────
        sector_summaries.sort(
            key=lambda s: s["sector_total_trading_value"],
            reverse=True,
        )
        for tv_rank, summary in enumerate(sector_summaries, start=1):
            score = (
                _tv_rank_score(tv_rank)
                + _cr_score(summary["sector_avg_change_rate"])
                + _count_score(summary["sector_stock_count"])
                + _vs_score(summary["volume_spike_overlap_count"])
                + _us_score(summary["us_sector_match"])
            )
            summary["tv_rank"] = tv_rank
            summary["sector_strength_score"] = score

        # ── 4. 결과 저장 ─────────────────────────────────────────────────────
        self._sector_data = {s["sector"]: s for s in sector_summaries}
        self._sector_data.pop("unknown", None)  # unknown 섹터는 Top 순위에서 제외

        logger.info(
            "[SectorStrengthAnalyzer] 분석 완료: %d섹터, top=%s(score=%s)",
            len(self._sector_data),
            sector_summaries[0]["sector"] if sector_summaries else "N/A",
            sector_summaries[0].get("sector_strength_score", 0) if sector_summaries else 0,
        )
        return self._sector_data

    # ── 대장주 선정 ────────────────────────────────────────────────────────────

    def _select_leader(
        self,
        stocks_sorted_by_tv: list[dict],
        vs_symbols: set[str],
        us_match_level: str,
    ) -> Optional[dict]:
        """
        섹터 내 대장주 선정 (거래대금 1위 + 조건 충족).
        조건 불충족 시 다음 순위 종목으로 순차 시도.
        """
        for stock in stocks_sorted_by_tv:
            if _is_excluded(stock):
                continue
            price = stock.get("current_price", 0)
            cr = stock.get("change_rate", 0.0)
            if price < _LEADER_MIN_PRICE:
                continue
            if not (_LEADER_MIN_CHANGE_RATE <= cr <= _LEADER_MAX_CHANGE_RATE):
                continue

            # 조건 충족 → 점수 계산
            is_top_in_sector = True   # 이미 거래대금 정렬 후 첫 번째로 통과한 종목
            overall_rank = stock.get("rank", 9999)
            in_vs = stock.get("symbol", "") in vs_symbols

            leader_score = (
                (_LEADER_SCORE_TV if is_top_in_sector else 0)
                + (_LEADER_SCORE_TOP20 if overall_rank <= 20 else 0)
                + (_LEADER_SCORE_VS if in_vs else 0)
                + 0                              # ma_alignment placeholder
                + (_LEADER_SCORE_US if us_match_level in ("strong", "moderate") else 0)
            )

            return {
                **stock,
                "leader_score": leader_score,
                "leader_score_breakdown": {
                    "sector_top_by_tv": _LEADER_SCORE_TV if is_top_in_sector else 0,
                    "overall_top20": _LEADER_SCORE_TOP20 if overall_rank <= 20 else 0,
                    "volume_spike_overlap": _LEADER_SCORE_VS if in_vs else 0,
                    "ma_alignment": 0,
                    "us_sector_match": _LEADER_SCORE_US if us_match_level in ("strong", "moderate") else 0,
                },
            }
        return None  # 조건 충족 종목 없음

    # ── 조회 메서드 ────────────────────────────────────────────────────────────

    def get_top_sectors(self, n: int = 5) -> list[dict]:
        """섹터 강도 점수 내림차순 상위 n개 반환."""
        sorted_sectors = sorted(
            self._sector_data.values(),
            key=lambda s: s.get("sector_strength_score", 0),
            reverse=True,
        )
        return sorted_sectors[:n]

    def get_sector_leader(self, sector: str) -> Optional[dict]:
        """특정 섹터의 대장주 반환. 없으면 None."""
        info = self._sector_data.get(sector)
        if info is None:
            return None
        return info.get("leader")

    def get_sector_info(self, sector: str) -> Optional[dict]:
        """특정 섹터의 전체 정보 반환."""
        return self._sector_data.get(sector)

    def get_all_sector_data(self) -> dict[str, dict]:
        """전체 섹터 데이터 반환."""
        return dict(self._sector_data)
