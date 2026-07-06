"""
sector_leader_top3_selector.py

주도섹터 Top3 집중매수 전략 선정기.

final_score = sector_strength_score + sector_leader_score + us_sector_match_score
              + volume_spike_confirm_score + ma_bonus - risk_penalty

하드 제외 (fallback에서도 절대 복구 금지):
- 현재가 20,000원 미만
- 거래대금 20억 미만
- 상승률 15% 초과 또는 2% 미만
- ETF/ETN/우선주/스팩/리츠
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    from app.logger import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent

# Score caps (defaults; overridden by config)
_SCORE_CAPS = {
    "sector_strength_score_max": 35,
    "sector_leader_score_max": 25,
    "us_sector_match_score_max": 20,
    "volume_spike_confirm_score_max": 10,
    "ma_bonus_max": 10,
    "risk_penalty_max": 30,
}

# Hard exclusion thresholds
_HARD_MIN_PRICE = 20_000
_HARD_MIN_TV = 2_000_000_000     # 20억
_HARD_MIN_CR = 2.0
_HARD_MAX_CR = 15.0

def _is_hard_excluded(stock: dict) -> tuple[bool, str]:
    """하드 제외 여부와 사유를 반환한다. fallback에서도 절대 복구 불가."""
    if stock.get("is_etf") or stock.get("is_etn"):
        return True, "etf_etn"
    if stock.get("is_preferred"):
        return True, "preferred_stock"
    if stock.get("is_spac"):
        return True, "spac"
    if stock.get("is_reit"):
        return True, "reit"
    if stock.get("is_suspended") or stock.get("is_halt"):
        return True, "suspended"
    if stock.get("sector") == "unknown":
        return True, "unknown_sector"
    price = stock.get("current_price", 0)
    if price < _HARD_MIN_PRICE:
        return True, f"price_below_{_HARD_MIN_PRICE}"
    tv = stock.get("trading_value", stock.get("trade_value", 0))
    if tv < _HARD_MIN_TV:
        return True, "trading_value_below_20b"
    cr = stock.get("change_rate", 0.0)
    if cr > _HARD_MAX_CR:
        return True, "change_rate_above_max"
    if cr <= 0:
        return True, "negative_change_rate"
    if cr < _HARD_MIN_CR:
        return True, "change_rate_below_min"
    return False, ""


class SectorLeaderTop3Selector:
    """주도섹터 Top3 집중매수 선정기."""

    def __init__(self, cfg=None):
        if cfg is None:
            try:
                from app.config import get_config
                cfg = get_config()
            except Exception:
                cfg = None
        self.cfg = cfg
        self._t3_cfg: dict = self._load_cfg()
        self._last_excluded: list[dict] = []
        self._last_sector_analysis: dict = {}

    def _load_cfg(self) -> dict:
        try:
            return self.cfg._raw.get("sector_leader_top3", {}) if self.cfg else {}
        except AttributeError:
            return {}

    def _cap(self, key: str) -> int:
        return int(self._t3_cfg.get(key, _SCORE_CAPS.get(key, 0)))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(
        self,
        nxt_stocks: list[dict],
        volume_spike_stocks: list[dict] = None,
        us_sector_result: dict = None,
    ) -> tuple[list[dict], dict, list[dict]]:
        """
        nxt_stocks: NXT 거래대금 상위 종목 리스트 (rank, symbol, name, current_price,
                    change_rate, trading_value, sector, subtheme 포함)
        volume_spike_stocks: 거래량 급증 종목 (보조 확인용)
        us_sector_result: USSectorStrengthService.get_us_sector_strength() 반환값

        Returns:
            top3: list of selected stocks with all score fields
            diag: diagnostic dict
            excluded: list of excluded stocks with reason
        """
        volume_spike_stocks = volume_spike_stocks or []
        us_sector_result = us_sector_result or {}

        vs_symbols = {s.get("symbol") for s in volume_spike_stocks}
        market_regime = us_sector_result.get("market_regime", "neutral")
        us_data_source = us_sector_result.get("data_source_used", "none")

        diag: dict = {
            "total_nxt": len(nxt_stocks),
            "hard_excluded": 0,
            "after_hard_filter": 0,
            "candidates_evaluated": 0,
            "sectors_found": 0,
            "top3_count": 0,
            "fallback_used": False,
            "market_regime": market_regime,
            "us_data_source": us_data_source,
        }
        excluded: list[dict] = []

        # ── Stage 1: 섹터 매핑 ────────────────────────────────────────────
        stocks = self._ensure_sector(nxt_stocks)

        # ── Stage 2: 하드 필터 적용 ──────────────────────────────────────
        passed: list[dict] = []
        for s in stocks:
            excl, reason = _is_hard_excluded(s)
            if excl:
                diag["hard_excluded"] += 1
                excluded.append({**s, "excluded_reason": reason})
            else:
                passed.append(s)
        diag["after_hard_filter"] = len(passed)
        diag["candidates_evaluated"] = len(passed)

        # eligible=0 → 즉시 반환 (fallback도 hard-excluded 복구 불가)
        if len(passed) == 0:
            logger.warning("[SectorLeaderTop3] 하드 필터 통과 종목 0개 → Top3 불가")
            diag["top3_count"] = 0
            return [], diag, excluded

        # ── Stage 3: 섹터 강도 계산 ──────────────────────────────────────
        # 반드시 하드 필터 이전 전체 stocks 기준으로 섹터 순위 산출.
        # passed(필터 후)로 계산하면 경쟁 섹터 종목이 제거되어 조선업 등이
        # 순위가 올라가는 오류 발생 (한화오션 버그의 근본 원인).
        sector_analysis = self._analyze_sectors(stocks, vs_symbols)
        self._last_sector_analysis = sector_analysis
        diag["sectors_found"] = len(sector_analysis)

        # ── Stage 3b: 섹터 순위 제한 필터 (상위 섹터 종목만) ─────────────
        max_sector_rank = int(self._t3_cfg.get("max_sector_rank", 5))
        sector_rank_excluded: list[dict] = []
        eligible: list[dict] = []
        for s in passed:
            sec = s.get("sector", "unknown")
            sec_rank = sector_analysis.get(sec, {}).get("sector_tv_rank", 99)
            if sec_rank > max_sector_rank:
                s2 = dict(s)
                s2["excluded_reason"] = f"sector_rank_too_low({sec_rank}>{max_sector_rank})"
                s2["sector_tv_rank"] = sec_rank
                sector_rank_excluded.append(s2)
            else:
                s["sector_tv_rank"] = sec_rank
                eligible.append(s)

        excluded.extend(sector_rank_excluded)
        diag["sector_rank_excluded"] = len(sector_rank_excluded)
        diag["eligible_after_sector_rank"] = len(eligible)
        passed = eligible

        if len(passed) == 0:
            logger.warning("[SectorLeaderTop3] 섹터 순위 필터 후 통과 종목 0개 → Top3 불가")
            diag["top3_count"] = 0
            return [], diag, excluded

        # ── Stage 4: 각 종목 점수 계산 ───────────────────────────────────
        scored: list[dict] = []
        for s in passed:
            scored_stock = self._score_stock(
                s, sector_analysis, vs_symbols, us_sector_result
            )
            scored.append(scored_stock)

        scored.sort(key=lambda x: x["final_score"], reverse=True)

        # ── Stage 5: Top3 선정 (동일 섹터 최대 2개) ──────────────────────
        top3 = self._pick_top3(scored)

        # ── Stage 6: Fallback (passed 목록 내에서만, hard 조건 복구 없음) ─
        if len(top3) < 3:
            diag["fallback_used"] = True
            top3 = self._fallback(top3, passed, vs_symbols, us_sector_result, sector_analysis)

        # ── Stage 7: rank 부여 및 메타데이터 ─────────────────────────────
        for i, s in enumerate(top3, 1):
            s["rank"] = i
            s["market_regime"] = market_regime
            s["us_data_source"] = us_data_source

        diag["top3_count"] = len(top3)
        self._last_excluded = excluded

        assert all("symbol" in s for s in top3), "top3 항목에 symbol 누락"
        assert all("final_score" in s for s in top3), "top3 항목에 final_score 누락"
        assert len(top3) <= 3, f"top3 개수 초과: {len(top3)}"

        logger.info(
            "[SectorLeaderTop3] 수집 %d → 하드제외 %d → 통과 %d → Top3 %d",
            diag["total_nxt"], diag["hard_excluded"], diag["after_hard_filter"], diag["top3_count"],
        )
        return top3, diag, excluded

    # ------------------------------------------------------------------
    # Sector analysis
    # ------------------------------------------------------------------

    def _ensure_sector(self, stocks: list[dict]) -> list[dict]:
        """sector 필드가 없으면 sector_mapper로 채운다."""
        try:
            from app.strategy.sector_mapper import SectorMapper
            mapper = SectorMapper()
        except ImportError:
            mapper = None

        result = []
        for s in stocks:
            s = dict(s)
            if not s.get("sector") and mapper:
                sec, sub = mapper.map(s.get("name", ""), s.get("sector_name", ""))
                s["sector"] = sec
                s["subtheme"] = sub
            elif not s.get("sector"):
                s["sector"] = "unknown"
                s["subtheme"] = ""
            result.append(s)
        return result

    def _analyze_sectors(self, stocks: list[dict], vs_symbols: set) -> dict:
        """섹터별 강도 지표를 계산한다."""
        sector_stocks: dict[str, list[dict]] = {}
        for s in stocks:
            sec = s.get("sector", "unknown")
            sector_stocks.setdefault(sec, []).append(s)

        analysis: dict[str, dict] = {}
        for sec, members in sector_stocks.items():
            total_tv = sum(m.get("trading_value", m.get("trade_value", 0)) for m in members)
            avg_cr = sum(m.get("change_rate", 0) for m in members) / len(members)
            members_by_tv = sorted(members, key=lambda x: x.get("trading_value", x.get("trade_value", 0)), reverse=True)
            top_tv = members_by_tv[0].get("trading_value", members_by_tv[0].get("trade_value", 0)) if members_by_tv else 0
            concentration = top_tv / total_tv if total_tv > 0 else 0.0
            vs_overlap = sum(1 for m in members if m.get("symbol") in vs_symbols)
            analysis[sec] = {
                "sector_total_trading_value": total_tv,
                "sector_avg_change_rate": avg_cr,
                "sector_top_stock_trading_value": top_tv,
                "sector_leader_concentration": concentration,
                "sector_stock_count": len(members),
                "volume_spike_overlap_count": vs_overlap,
                "members_by_tv": [m.get("symbol") for m in members_by_tv],
                "leader_symbol": members_by_tv[0].get("symbol") if members_by_tv else "",
            }

        # Rank sectors by total trading value
        ranked = sorted(analysis.items(), key=lambda x: x[1]["sector_total_trading_value"], reverse=True)
        for rank, (sec, _) in enumerate(ranked, 1):
            analysis[sec]["sector_tv_rank"] = rank

        return analysis

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score_stock(
        self,
        s: dict,
        sector_analysis: dict,
        vs_symbols: set,
        us_sector_result: dict,
    ) -> dict:
        s = dict(s)
        sector = s.get("sector", "unknown")
        sa = sector_analysis.get(sector, {})
        symbol = s.get("symbol", "")
        tv = s.get("trading_value", s.get("trade_value", 0))
        cr = s.get("change_rate", 0.0)
        nxt_rank = s.get("rank", 999)

        # ── sector_strength_score ────────────────────────────────────────
        ss_max = self._cap("sector_strength_score_max")
        tv_rank = sa.get("sector_tv_rank", 99)
        avg_cr = sa.get("sector_avg_change_rate", 0.0)
        vs_overlap = sa.get("volume_spike_overlap_count", 0)
        stock_count = sa.get("sector_stock_count", 1)

        ss_score = 0.0
        if tv_rank == 1:
            ss_score += ss_max * 0.50
        elif tv_rank == 2:
            ss_score += ss_max * 0.30
        elif tv_rank == 3:
            ss_score += ss_max * 0.15
        if avg_cr >= 5:
            ss_score += ss_max * 0.20
        elif avg_cr >= 3:
            ss_score += ss_max * 0.10
        if stock_count >= 3:
            ss_score += ss_max * 0.15
        elif stock_count == 2:
            ss_score += ss_max * 0.08
        if vs_overlap >= 2:
            ss_score += ss_max * 0.15
        elif vs_overlap == 1:
            ss_score += ss_max * 0.08
        ss_score = min(ss_max, ss_score)

        # ── sector_leader_score ──────────────────────────────────────────
        sl_max = self._cap("sector_leader_score_max")
        sl_score = 0.0
        # Sector leader (top TV in sector)
        is_sector_leader = (sa.get("leader_symbol") == symbol)
        if is_sector_leader:
            sl_score += 15
        # Overall NXT rank
        if nxt_rank <= 20:
            sl_score += 5
        # Volume spike overlap
        if symbol in vs_symbols:
            sl_score += 5
        # MA alignment placeholder (0 if no data)
        sl_score += 0
        sl_score = min(sl_max, sl_score)

        # ── us_sector_match_score ────────────────────────────────────────
        us_max = self._cap("us_sector_match_score_max")
        us_score = 0
        us_sector_reason = ""
        matched_us_sector = ""
        if us_sector_result and us_sector_result.get("data_source_used") != "none":
            try:
                from app.services.us_sector_strength_service import USSectorStrengthService
                svc = USSectorStrengthService(self.cfg)
                us_score, matched_us_sector, us_sector_reason = svc.get_us_sector_match_score(
                    sector, us_sector_result, us_max
                )
            except Exception:
                us_score = 0

        # ── volume_spike_confirm_score ────────────────────────────────────
        vs_max = self._cap("volume_spike_confirm_score_max")
        vs_score = vs_max if symbol in vs_symbols else 0

        # ── ma_bonus (placeholder) ────────────────────────────────────────
        ma_max = self._cap("ma_bonus_max")
        ma_bonus = 0  # 실시간 MA 데이터 없을 때 0

        # ── risk_penalty ──────────────────────────────────────────────────
        rp_max = self._cap("risk_penalty_max")
        risk_penalty = 0
        if cr > 12:
            risk_penalty += 5
        # Additional penalty placeholders
        risk_penalty = min(rp_max, risk_penalty)

        final_score = (
            ss_score + sl_score + us_score + vs_score + ma_bonus - risk_penalty
        )

        # Build selected_reason
        reasons = []
        if is_sector_leader:
            reasons.append(f"섹터1위({sector})")
        if tv_rank == 1:
            reasons.append("거래대금섹터1위")
        if symbol in vs_symbols:
            reasons.append("거래량급증중복")
        if us_score > 0:
            reasons.append(f"미국섹터매칭({matched_us_sector})")

        s.update({
            "sector_strength_score": round(ss_score, 1),
            "sector_leader_score": round(sl_score, 1),
            "us_sector_match_score": us_score,
            "volume_spike_confirm_score": vs_score,
            "ma_bonus": ma_bonus,
            "risk_penalty": risk_penalty,
            "final_score": round(final_score, 2),
            "selected_reason": " | ".join(reasons) if reasons else "점수기반선정",
            "matched_us_sector": matched_us_sector,
            "us_sector_reason": us_sector_reason,
        })
        return s

    # ------------------------------------------------------------------
    # Top3 selection with same-sector max-2 rule
    # ------------------------------------------------------------------

    def _pick_top3(self, scored: list[dict]) -> list[dict]:
        sector_count: dict[str, int] = {}
        top3: list[dict] = []
        for s in scored:
            sec = s.get("sector", "unknown")
            if sector_count.get(sec, 0) >= 2:
                continue
            top3.append(s)
            sector_count[sec] = sector_count.get(sec, 0) + 1
            if len(top3) == 3:
                break
        return top3

    def _fallback(
        self,
        current_top3: list[dict],
        passed: list[dict],
        vs_symbols: set,
        us_sector_result: dict,
        sector_analysis: dict,
    ) -> list[dict]:
        """Top3 미달 시 passed(하드 필터 통과) 내에서만 추가 선정.

        hard_excluded/unknown/change_rate 위반 종목은 절대 복구하지 않는다.
        소프트 조건(동일 섹터 제한)만 완화.
        """
        if len(current_top3) >= 3:
            return current_top3

        selected_symbols = {s["symbol"] for s in current_top3}
        sector_count: dict[str, int] = {}
        for s in current_top3:
            sec = s.get("sector", "unknown")
            sector_count[sec] = sector_count.get(sec, 0) + 1

        # Score remaining passed stocks (not yet selected)
        remaining: list[dict] = []
        for s in passed:
            sym = s.get("symbol", "")
            if sym in selected_symbols:
                continue
            scored = self._score_stock(s, sector_analysis, vs_symbols, us_sector_result)
            remaining.append(scored)

        remaining.sort(key=lambda x: x["final_score"], reverse=True)

        # Pick with same-sector max-2 constraint still enforced
        for s in remaining:
            if len(current_top3) >= 3:
                break
            sec = s.get("sector", "unknown")
            if sector_count.get(sec, 0) >= 2:
                continue
            s["selected_reason"] = (s.get("selected_reason", "") + "|fallback").lstrip("|")
            current_top3.append(s)
            sector_count[sec] = sector_count.get(sec, 0) + 1
            selected_symbols.add(s["symbol"])

        return current_top3

    # ------------------------------------------------------------------
    # CSV output
    # ------------------------------------------------------------------

    def save_top3_csv(self, top3: list[dict], date_str: str = None, time_str: str = None) -> str:
        if not date_str:
            date_str = datetime.now().strftime("%Y%m%d")
        if not time_str:
            time_str = datetime.now().strftime("%H%M")
        out_dir = _ROOT / "data" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        filepath = out_dir / f"sector_leader_top3_{date_str}_{time_str}.csv"
        columns = [
            "rank", "symbol", "name", "sector", "subtheme",
            "current_price", "change_rate", "trading_value",
            "sector_strength_score", "sector_leader_score", "us_sector_match_score",
            "volume_spike_confirm_score", "ma_bonus", "risk_penalty", "final_score",
            "hard_excluded", "eligible", "selected", "selected_reason",
            "sector_rank", "warning_reason", "excluded_reason",
            "matched_us_sector", "us_sector_reason",
            "us_data_source", "market_regime",
        ]
        rows = [{col: s.get(col, "") for col in columns} for s in top3]
        df = pd.DataFrame(rows, columns=columns)
        df.to_csv(str(filepath), index=False, encoding="utf-8-sig")
        logger.info("[SectorLeaderTop3] Top3 CSV 저장: %s", filepath)
        return str(filepath)

    def save_sector_strength_csv(self, sector_analysis: dict, date_str: str = None, time_str: str = None) -> str:
        if not date_str:
            date_str = datetime.now().strftime("%Y%m%d")
        if not time_str:
            time_str = datetime.now().strftime("%H%M")
        out_dir = _ROOT / "data" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        filepath = out_dir / f"sector_strength_{date_str}_{time_str}.csv"
        rows = []
        for sec, info in sorted(sector_analysis.items(), key=lambda x: x[1].get("sector_tv_rank", 99)):
            rows.append({
                "sector": sec,
                "sector_tv_rank": info.get("sector_tv_rank", ""),
                "sector_total_trading_value": info.get("sector_total_trading_value", 0),
                "sector_avg_change_rate": round(info.get("sector_avg_change_rate", 0), 2),
                "sector_stock_count": info.get("sector_stock_count", 0),
                "volume_spike_overlap_count": info.get("volume_spike_overlap_count", 0),
                "leader_symbol": info.get("leader_symbol", ""),
            })
        df = pd.DataFrame(rows)
        df.to_csv(str(filepath), index=False, encoding="utf-8-sig")
        logger.info("[SectorLeaderTop3] 섹터강도 CSV 저장: %s", filepath)
        return str(filepath)

    def save_excluded_csv(self, excluded: list[dict] = None, date_str: str = None, time_str: str = None) -> Optional[str]:
        excluded = excluded or self._last_excluded
        if not excluded:
            return None
        if not date_str:
            date_str = datetime.now().strftime("%Y%m%d")
        if not time_str:
            time_str = datetime.now().strftime("%H%M")
        out_dir = _ROOT / "data" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        filepath = out_dir / f"sector_leader_excluded_{date_str}_{time_str}.csv"
        df = pd.DataFrame(excluded)
        df.to_csv(str(filepath), index=False, encoding="utf-8-sig")
        logger.info("[SectorLeaderTop3] 제외 CSV 저장: %s (%d개)", filepath, len(excluded))
        return str(filepath)
