"""
candidate_quality_filter.py

설계 원칙:
  "위험 종목 하드 제외"와 "품질 감점"을 완전 분리.
  - 하드 제외: ETF/ETN/스팩/리츠/우선주/거래정지/관리/가격<1000/거래대금<3억/갭>20%(relaxed)
  - 감점(relaxed): 갭 9~20%, 거래대금 3억~10억, 과열/급락, 낙폭, 윗꼬리
  - fallback: 후보 < target_min 이면 overflow 풀에서 -5점 후 보충

처리 순서:
  1. 하드 제외 (excluded_reason 기록, hard_excluded=True)
  2. 소프트 감점 (penalty_reason 기록)
  3. MA/일봉 보너스 (heavy filter, 상위 N개)
  4. 테마 대장주 보너스
  5. final_score = existing_score + bonus - penalty
  6. 정렬 → 테마 cap (overflow 보존)
  7. fallback 보충 (선택 < target_min)
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger
from app.models import Candidate, StockData
from app.config import get_config

try:
    from app.services.us_theme_map import match_kr_stock_to_themes
except ImportError:
    def match_kr_stock_to_themes(name: str, sector: str = "") -> list[str]:  # type: ignore[misc]
        return []


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ETF_ETN_KEYWORDS = [
    "KODEX", "TIGER", "ACE", "SOL", "PLUS", "KBSTAR", "KOSEF",
    "HANARO", "ARIRANG", "ETN", "ETF", "레버리지", "인버스", "선물",
    "합성", "TR", "RISE", "FOCUS", "TREX", "TIMEFOLIO", "WOORI",
]

SPAC_KEYWORDS = ["스팩", "SPAC"]
REIT_KEYWORDS = ["리츠", "REIT", "REITS"]
RISK_KEYWORDS = ["관리", "거래정지", "상장폐지", "불성실", "정리매매", "투자주의"]

_PREFERRED_RE = re.compile(r'(?:우B?|\d+우B?)$')


# ---------------------------------------------------------------------------
# Main Class
# ---------------------------------------------------------------------------

class CandidateQualityFilter:

    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()
        self._qcfg: dict = self.cfg._raw.get("candidate_quality_filters", {})
        self._last_diagnostics: dict = {}

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def filter_and_score(
        self,
        candidates: list[Candidate],
        stock_data_by_symbol: Optional[dict[str, StockData]] = None,
        daily_prices_cache: Optional[dict[str, list[dict]]] = None,
    ) -> tuple[list[Candidate], list[dict]]:

        if not self._qcfg.get("enabled", True):
            return candidates, []

        qcfg = self._qcfg
        sdb = stock_data_by_symbol or {}
        dpc = daily_prices_cache or {}
        relaxed: bool = qcfg.get("relaxed_mode", False)
        speed_mode: bool = qcfg.get("speed_mode", True)
        heavy_limit: int = qcfg.get("max_candidates_for_heavy_filters", 30)
        target_min: int = qcfg.get("target_min_candidates", 10)
        max_positions: int = self.cfg.trading.get("max_positions", 15)

        # ── Step 1: 하드 제외 ─────────────────────────────────────────
        passed: list[Candidate] = []
        excluded: list[dict] = []

        for c in candidates:
            sd = sdb.get(c.symbol)
            reason = self._hard_exclude(c, sd)
            if reason:
                c.hard_excluded = True
                c.excluded_reason = reason
                excluded.append({
                    "code": c.symbol,
                    "name": c.name,
                    "gap_rate": round(c.gap_rate, 2),
                    "current_price": c.current_price,
                    "trading_value": c.trade_value,
                    "excluded_reason": reason,
                    "warning_reason": "",
                })
                logger.debug(f"[QFilter] 하드제외: {c.symbol} {c.name} — {reason}")
            else:
                if relaxed:
                    c.relaxed_mode_applied = True
                passed.append(c)

        # ── Step 2: 소프트 감점 + 보너스 ─────────────────────────────
        for i, c in enumerate(passed):
            sd = sdb.get(c.symbol)
            is_heavy = (not speed_mode) or (i < heavy_limit)
            daily = dpc.get(c.symbol, []) if is_heavy else []
            if relaxed:
                self._apply_soft_penalties_relaxed(c, sd, daily, qcfg)
            else:
                self._apply_score_adjustments(c, sd, daily)

        # ── Step 3: 테마 대장주 보너스 ───────────────────────────────
        self._apply_theme_leader_bonus(passed)

        # ── Step 4: final_score 재계산 ────────────────────────────────
        for c in passed:
            existing = c.final_score if c.final_score > 0 else c.rule_score
            adjusted = (
                existing
                + c.quality_bonus
                + c.momentum_bonus
                + c.ma_bonus
                + c.theme_leader_bonus
                - c.risk_penalty_q
                - c.liquidity_penalty
                - c.overheat_penalty
            )
            c.final_score = round(max(0.0, min(100.0, adjusted)), 4)

        passed.sort(key=lambda x: x.final_score, reverse=True)
        for idx, c in enumerate(passed, 1):
            c.rank = idx

        # ── Step 5: 테마 cap (overflow 보존) ─────────────────────────
        max_theme: int = qcfg.get("max_same_theme_in_top15", 5 if relaxed else 4)
        try:
            selected, overflow = self._apply_theme_cap_with_overflow(
                passed, max_theme, max_positions
            )
        except Exception as e:
            logger.warning(f"[QFilter] 테마 cap 오류(스킵): {e}")
            selected, overflow = passed[:max_positions], passed[max_positions:]

        # ── Step 6: fallback 보충 ─────────────────────────────────────
        n_before_fallback = len(selected)
        if relaxed and len(selected) < target_min:
            selected = self._fallback_restore(selected, overflow, target_min)

        n_fallback = sum(1 for c in selected if c.fallback_included)
        n_penalized = sum(1 for c in passed if c.penalty_reason)

        self._last_diagnostics = {
            "n_input": len(candidates),
            "n_hard_excluded": len(excluded),
            "n_soft_penalized": n_penalized,
            "n_passed": len(passed),
            "n_selected": len(selected),
            "n_fallback": n_fallback,
            "fallback_triggered": n_before_fallback < target_min,
            "relaxed_mode": relaxed,
            "top_exclude_reasons": _top5_reasons(
                [e["excluded_reason"] for e in excluded]
            ),
            "top_penalty_reasons": _top5_reasons(
                [c.penalty_reason for c in passed if c.penalty_reason]
            ),
        }
        logger.info(
            f"[QFilter] 입력{len(candidates)} → 하드제외{len(excluded)} "
            f"→ 감점{n_penalized} → 최종{len(selected)} "
            f"(fallback+{n_fallback})"
        )
        return selected, excluded

    def save_explain_csv(
        self,
        candidates: list[Candidate],
        excluded: list[dict],
        date_str: str = None,
        time_str: str = None,
    ) -> tuple[str, str]:
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")
        if time_str is None:
            time_str = datetime.now().strftime("%H%M")

        out_dir = Path("data") / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        explain_rows = []
        for c in candidates:
            explain_rows.append({
                "rank": c.rank,
                "code": c.symbol,
                "name": c.name,
                "theme": c.theme,
                "current_price": c.current_price,
                "gap_rate": round(c.gap_rate, 2),
                "trading_value": c.trade_value,
                "existing_score": round(
                    c.final_score if c.final_score > 0 else c.rule_score, 4
                ),
                "quality_bonus": round(c.quality_bonus, 4),
                "momentum_bonus": round(c.momentum_bonus, 4),
                "ma_bonus": round(c.ma_bonus, 4),
                "theme_leader_bonus": round(c.theme_leader_bonus, 4),
                "risk_penalty": round(c.risk_penalty_q, 4),
                "liquidity_penalty": round(c.liquidity_penalty, 4),
                "overheat_penalty": round(c.overheat_penalty, 4),
                "final_score": round(c.final_score, 4),
                "penalty_reason": c.penalty_reason,
                "warning_reason": c.warning_reason,
                "fallback_included": c.fallback_included,
                "hard_excluded": c.hard_excluded,
                "relaxed_mode_applied": c.relaxed_mode_applied,
            })

        explain_path = out_dir / f"top15_explain_{date_str}_{time_str}.csv"
        pd.DataFrame(explain_rows).to_csv(explain_path, index=False, encoding="utf-8-sig")
        logger.info(f"[QFilter] explain CSV 저장: {explain_path}")

        excl_path = out_dir / f"excluded_candidates_{date_str}_{time_str}.csv"
        pd.DataFrame(excluded).to_csv(excl_path, index=False, encoding="utf-8-sig")
        logger.info(f"[QFilter] 제외 CSV 저장: {excl_path} ({len(excluded)}개)")

        return str(explain_path), str(excl_path)

    # ------------------------------------------------------------------ #
    # Hard Exclusion                                                        #
    # ------------------------------------------------------------------ #

    def _hard_exclude(self, c: Candidate, sd: Optional[StockData]) -> Optional[str]:
        """ETF/ETN/스팩/리츠/우선주/거래정지 등 절대 제외 사유 반환. None이면 통과."""
        qcfg = self._qcfg
        relaxed = qcfg.get("relaxed_mode", False)
        name_upper = (c.name or "").upper()
        symbol = c.symbol or ""

        # ETF / ETN
        if sd and (sd.is_etf or sd.is_etn):
            return "ETF/ETN 플래그"
        for kw in ETF_ETN_KEYWORDS:
            if kw.upper() in name_upper:
                return f"ETF/ETN 키워드: {kw}"

        # 스팩
        if sd and sd.is_spac:
            return "스팩"
        for kw in SPAC_KEYWORDS:
            if kw in c.name:
                return f"스팩 키워드: {kw}"

        # 리츠
        if sd and sd.is_reit:
            return "리츠"
        for kw in REIT_KEYWORDS:
            if kw.upper() in name_upper:
                return f"리츠 키워드: {kw}"

        # 우선주
        if sd and sd.is_preferred:
            return "우선주 플래그"
        if _PREFERRED_RE.search(c.name or ""):
            return "우선주(이름패턴)"

        # 거래정지 / 투자경고
        if sd and (sd.is_warning or sd.is_halt):
            return "거래정지/투자경고"
        for kw in RISK_KEYWORDS:
            if kw in c.name:
                return f"위험키워드: {kw}"

        # 종목코드 이상
        if not (len(symbol) == 6 and symbol.isdigit()):
            return f"종목코드 이상: {symbol}"

        # 최저가
        min_price = qcfg.get("min_price", 1000)
        if c.current_price < min_price:
            return f"동전주(가격 {c.current_price} < {min_price})"

        # 거래대금 — relaxed vs 기존
        if relaxed:
            abs_min = qcfg.get("absolute_min_trading_value", 300_000_000)
            if c.trade_value < abs_min:
                return f"거래대금극소({c.trade_value:,.0f} < {abs_min:,.0f})"
        else:
            tv_min = qcfg.get("min_trading_value_0920", 3_000_000_000)
            if c.trade_value < tv_min:
                return f"거래대금부족({c.trade_value:,.0f} < {tv_min:,.0f})"

        # 갭 — relaxed vs 기존
        if relaxed:
            hard_gap = qcfg.get("hard_exclude_gap_rate", 20.0)
            if c.gap_rate > hard_gap:
                return f"갭과다({c.gap_rate:.1f}% > {hard_gap}%)"
        else:
            max_gap = qcfg.get("max_open_gap_rate", 12.0)
            if c.gap_rate > max_gap:
                return f"갭과다({c.gap_rate:.1f}% > {max_gap}%)"

        # 시초가 대비 낙폭 하드 제외 (기본 50% 이상 하락)
        max_drop_from_open = float(qcfg.get("max_drop_from_open_rate", 50.0))
        if c.open > 0 and max_drop_from_open > 0 and c.current_price * 0.5 <= c.open <= c.current_price * 2.0:
            drop_from_open = (c.current_price - c.open) / c.open * 100.0
            if drop_from_open <= -max_drop_from_open:
                return f"시초가대비낙폭({drop_from_open:.1f}% ≤ -{max_drop_from_open}%)"

        return None

    # ------------------------------------------------------------------ #
    # Relaxed Soft Penalties                                               #
    # ------------------------------------------------------------------ #

    def _apply_soft_penalties_relaxed(
        self,
        c: Candidate,
        sd: Optional[StockData],
        daily: list[dict],
        qcfg: dict,
    ) -> None:
        """relaxed_mode 전용: 갭/거래대금을 감점 처리, 하드 제외 없음."""
        penalties: list[str] = []
        warnings: list[str] = []

        # ── 거래대금 티어 감점 ───────────────────────────────────────
        tv = c.trade_value
        tv_threshold = self._get_tv_threshold(qcfg)
        tv_general = qcfg.get("min_trading_value_general", 700_000_000)
        tv_abs = qcfg.get("absolute_min_trading_value", 300_000_000)

        if tv < tv_abs:
            c.liquidity_penalty += 20.0
            penalties.append(f"거래대금극소({tv / 1e8:.1f}억)")
        elif tv < tv_general:
            ratio = (tv - tv_abs) / max(tv_general - tv_abs, 1)
            c.liquidity_penalty += round(7.0 * (1.0 - ratio), 2)
            penalties.append(f"거래대금부족({tv / 1e8:.1f}억)")
        elif tv < tv_threshold:
            ratio = (tv - tv_general) / max(tv_threshold - tv_general, 1)
            c.liquidity_penalty += round(3.0 * (1.0 - ratio), 2)
            penalties.append(f"거래대금주의({tv / 1e8:.1f}억)")

        # ── 갭 티어 bonus / penalty ──────────────────────────────────
        gap = c.gap_rate
        healthy_min = qcfg.get("healthy_gap_min", 1.0)
        healthy_max = qcfg.get("healthy_gap_max", 9.0)
        caution_max = qcfg.get("caution_gap_max", 15.0)
        hard_gap = qcfg.get("hard_exclude_gap_rate", 20.0)

        if healthy_min <= gap <= healthy_max:
            normalized = 1.0 - abs(gap - 5.0) / max(healthy_max - healthy_min, 1)
            c.momentum_bonus = round(max(0.0, normalized) * 8.0, 2)
        elif gap > healthy_max and gap <= caution_max:
            excess = gap - healthy_max
            c.momentum_bonus = round(-(excess / max(caution_max - healthy_max, 1) * 3.0), 2)
            penalties.append(f"갭주의({gap:.1f}%)")
        elif gap > caution_max:
            excess = min(gap - caution_max, hard_gap - caution_max)
            c.momentum_bonus = round(-(excess / max(hard_gap - caution_max, 1) * 8.0), 2)
            c.overheat_penalty += round(min(5.0, (gap - caution_max) * 0.5), 2)
            penalties.append(f"갭과대({gap:.1f}%)")

        # ── 장초반 낙폭 ──────────────────────────────────────────────
        max_drop = qcfg.get("max_intraday_drop_from_high", 4.0)
        if c.high > 0:
            drop = (c.current_price - c.high) / c.high * 100.0
            if drop < -max_drop:
                c.risk_penalty_q += 5.0
                penalties.append(f"장초반낙폭{drop:.1f}%")
            elif drop < -2.0:
                c.risk_penalty_q += 2.0

        # ── 윗꼬리 ───────────────────────────────────────────────────
        if c.open > 0 and c.high > 0:
            cr = c.high - c.low
            if cr > 0 and (c.high - c.current_price) / cr > 0.45:
                c.risk_penalty_q += 3.0
                penalties.append("윗꼬리")

        # ── 일봉 데이터 ──────────────────────────────────────────────
        if daily:
            self._apply_daily_based_filters(c, daily, warnings, qcfg)
        else:
            warnings.append("일봉 데이터 없음(MA/수익률 필터 스킵)")

        # ── 테마 분류 ────────────────────────────────────────────────
        sector = sd.sector if sd else ""
        themes = match_kr_stock_to_themes(c.name, sector)
        c.matched_themes = ",".join(themes)
        c.theme = themes[0] if themes else ""

        c.penalty_reason = "; ".join(penalties) if penalties else ""
        c.warning_reason = "; ".join(warnings) if warnings else ""

    # ------------------------------------------------------------------ #
    # Original Score Adjustments (non-relaxed, backward-compat)           #
    # ------------------------------------------------------------------ #

    def _apply_score_adjustments(
        self,
        c: Candidate,
        sd: Optional[StockData],
        daily: list[dict],
    ) -> None:
        qcfg = self._qcfg
        warnings: list[str] = []

        caution_gap = qcfg.get("caution_gap_rate", 7.0)
        if 2.0 <= c.gap_rate <= caution_gap:
            normalized = 1.0 - abs(c.gap_rate - 5.0) / 3.0
            c.momentum_bonus = round(max(0.0, normalized) * 8.0, 2)
        elif c.gap_rate > caution_gap:
            penalty = (c.gap_rate - caution_gap) / (12.0 - caution_gap) * 5.0
            c.momentum_bonus = round(-penalty, 2)
        else:
            c.momentum_bonus = 0.0

        max_intraday_drop = qcfg.get("max_intraday_drop_from_high", 4.0)
        if c.high > 0:
            drop_from_high = (c.current_price - c.high) / c.high * 100.0
            if drop_from_high < -max_intraday_drop:
                c.risk_penalty_q += 5.0
                warnings.append(f"장초반 낙폭 {drop_from_high:.1f}%")
            elif drop_from_high < -2.0:
                c.risk_penalty_q += 2.0

        if c.open > 0 and c.high > 0:
            candle_range = c.high - c.low
            if candle_range > 0:
                upper_shadow_ratio = (c.high - c.current_price) / candle_range
                if upper_shadow_ratio > 0.45:
                    c.risk_penalty_q += 3.0
                    warnings.append(f"윗꼬리 비율 {upper_shadow_ratio:.2f}")

        if daily:
            self._apply_daily_based_filters(c, daily, warnings, qcfg)
        else:
            warnings.append("일봉 데이터 없음(MA/수익률 필터 스킵)")

        sector = sd.sector if sd else ""
        themes = match_kr_stock_to_themes(c.name, sector)
        c.matched_themes = ",".join(themes)
        c.theme = themes[0] if themes else ""

        c.warning_reason = "; ".join(warnings) if warnings else ""

    # ------------------------------------------------------------------ #
    # Daily-Based Filters (MA bonus / overheat / crash)                   #
    # ------------------------------------------------------------------ #

    def _apply_daily_based_filters(
        self,
        c: Candidate,
        daily: list[dict],
        warnings: list[str],
        qcfg: dict,
    ) -> None:
        # VWAP 없으면 경고만, 제외 안 함
        if not any(d.get("vwap") is not None for d in daily):
            warnings.append("VWAP 데이터 없음(VWAP 필터 스킵)")

        closes = [d["close"] for d in daily if d.get("close", 0) > 0]
        if len(closes) < 5:
            warnings.append("일봉 데이터 부족(5일 미만)")
            return

        current = c.current_price or closes[0]

        ret3d  = (current / closes[2]  - 1) * 100 if len(closes) >= 3  else None
        ret5d  = (current / closes[4]  - 1) * 100 if len(closes) >= 5  else None
        ret20d = (current / closes[19] - 1) * 100 if len(closes) >= 20 else None

        max_3d = qcfg.get("max_3d_return", 25.0)
        max_5d = qcfg.get("max_5d_return", 35.0)

        if ret3d is not None and ret3d > max_3d:
            c.overheat_penalty += min(10.0, (ret3d - max_3d) / 5.0 * 5.0)
            warnings.append(f"3일 급등 {ret3d:.1f}%")
        if ret5d is not None and ret5d > max_5d:
            c.overheat_penalty += min(15.0, (ret5d - max_5d) / 5.0 * 5.0)
            warnings.append(f"5일 급등 {ret5d:.1f}%")
        if ret20d is not None and ret20d > 70.0:
            c.overheat_penalty += 5.0
            warnings.append(f"20일 급등 {ret20d:.1f}%")

        if ret5d is not None and ret5d < -18.0:
            c.risk_penalty_q += 5.0
            warnings.append(f"5일 급락 {ret5d:.1f}%")
        if ret20d is not None and ret20d < -30.0:
            c.risk_penalty_q += 5.0
            warnings.append(f"20일 급락 {ret20d:.1f}%")

        if len(closes) < 20:
            warnings.append("MA20 계산 불가(데이터 부족)")
            return

        ma5  = sum(closes[:5])  / 5
        ma10 = sum(closes[:10]) / 10
        ma20 = sum(closes[:20]) / 20

        ma5_prev  = sum(closes[1:6])  / 5  if len(closes) >= 6  else ma5
        ma10_prev = sum(closes[1:11]) / 10 if len(closes) >= 11 else ma10
        ma20_prev = sum(closes[1:21]) / 20 if len(closes) >= 21 else ma20

        ma_bonus = 0.0
        if ma5 > ma5_prev and ma10 > ma10_prev and ma20 > ma20_prev:
            ma_bonus += 5.0
        if current >= ma5 and current >= ma10 and current >= ma20:
            ma_bonus += 3.0
        if ma5 > ma10 > ma20:
            ma_bonus += 4.0

        max_ma20_ext = qcfg.get("max_ma20_extension_rate", 15.0)
        if ma20 > 0:
            ext = (current / ma20 - 1) * 100
            if ext > max_ma20_ext:
                ma_bonus -= 5.0
                warnings.append(f"MA20 대비 과열 {ext:.1f}%")

        c.ma_bonus = round(max(0.0, ma_bonus), 2)

    # ------------------------------------------------------------------ #
    # Theme Leader Bonus                                                   #
    # ------------------------------------------------------------------ #

    def _apply_theme_leader_bonus(self, candidates: list[Candidate]) -> None:
        theme_groups: dict[str, list[Candidate]] = {}
        for c in candidates:
            theme = c.theme or "__no_theme__"
            theme_groups.setdefault(theme, []).append(c)

        for theme, group in theme_groups.items():
            if theme == "__no_theme__" or len(group) < 2:
                continue
            leader = max(group, key=lambda x: x.trade_value)
            leader.theme_leader_bonus = 5.0
            logger.debug(f"[QFilter] 테마대장: {theme} → {leader.symbol} {leader.name}")

    # ------------------------------------------------------------------ #
    # Theme Cap with Overflow                                              #
    # ------------------------------------------------------------------ #

    def _apply_theme_cap_with_overflow(
        self,
        candidates: list[Candidate],
        max_theme: int,
        max_positions: int,
    ) -> tuple[list[Candidate], list[Candidate]]:
        selected: list[Candidate] = []
        overflow: list[Candidate] = []
        theme_counts: dict[str, int] = {}

        for c in candidates:
            theme = (c.theme or "").strip() or f"__unique_{c.symbol}__"
            count = theme_counts.get(theme, 0)
            if count < max_theme:
                selected.append(c)
                theme_counts[theme] = count + 1
            else:
                overflow.append(c)

        remaining = max_positions - len(selected)
        if remaining > 0 and overflow:
            selected.extend(overflow[:remaining])
            overflow = overflow[remaining:]

        result = selected[:max_positions]
        for idx, c in enumerate(result, 1):
            c.rank = idx
        return result, overflow

    # backward-compat wrapper
    def _apply_theme_cap(
        self,
        candidates: list[Candidate],
        max_theme: int,
        max_positions: int,
    ) -> list[Candidate]:
        selected, _ = self._apply_theme_cap_with_overflow(candidates, max_theme, max_positions)
        return selected

    # ------------------------------------------------------------------ #
    # Fallback Restore                                                     #
    # ------------------------------------------------------------------ #

    def _fallback_restore(
        self,
        selected: list[Candidate],
        overflow: list[Candidate],
        target_min: int,
    ) -> list[Candidate]:
        """overflow 풀에서 -5점 후 보충. 하드 제외 대상은 절대 복구 안 함."""
        if len(selected) >= target_min:
            return selected

        included = {c.symbol for c in selected}

        pool = sorted(
            [c for c in overflow if c.symbol not in included and not c.hard_excluded],
            key=lambda x: x.final_score,
            reverse=True,
        )
        for c in pool:
            if len(selected) >= target_min:
                break
            c.final_score = round(max(0.0, c.final_score - 5.0), 4)
            c.fallback_included = True
            pr = (c.penalty_reason + "; " if c.penalty_reason else "") + "fallback복구(-5)"
            c.penalty_reason = pr
            selected.append(c)
            included.add(c.symbol)
            logger.debug(f"[QFilter] fallback 복구: {c.symbol} {c.name} score={c.final_score}")

        selected.sort(key=lambda x: x.final_score, reverse=True)
        for idx, c in enumerate(selected, 1):
            c.rank = idx

        n_fb = sum(1 for c in selected if c.fallback_included)
        if len(selected) < target_min:
            logger.info(f"[QFilter] fallback 후 목표 미달: {len(selected)}/{target_min}")
        else:
            logger.info(f"[QFilter] fallback 완료: +{n_fb}개 → {len(selected)}개")
        return selected

    # ------------------------------------------------------------------ #
    # Time-Proportional TV Threshold                                       #
    # ------------------------------------------------------------------ #

    def _get_tv_threshold(self, qcfg: dict) -> float:
        """09:00~09:20 사이 경과 시간에 비례해 최소 거래대금 반환."""
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        open_t = 9 * 60        # 540
        cut_t  = 9 * 60 + 20   # 560
        standard = qcfg.get("min_trading_value_0920", 1_000_000_000)
        abs_min  = qcfg.get("absolute_min_trading_value", 300_000_000)

        if cur >= cut_t:
            return standard
        if cur <= open_t:
            return max(abs_min, standard * 0.25)

        ratio = (cur - open_t) / (cut_t - open_t)
        return max(abs_min, standard * ratio)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _top5_reasons(reasons: list[str]) -> list[tuple[str, int]]:
    """Returns top-5 (reason, count) pairs from a flat list of reason strings."""
    from collections import Counter
    counts: Counter = Counter()
    for r in reasons:
        for part in r.split(";"):
            part = part.strip()
            if part:
                # Trim trailing numeric details for grouping
                key = re.sub(r'\(.*?\)', '', part).strip()
                counts[key] += 1
    return counts.most_common(5)
