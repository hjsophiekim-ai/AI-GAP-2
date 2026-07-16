"""
dynamic_exit_engine.py — Dynamic Exit AI: 익절/손절/트레일링/Profit Lock을 실시간으로 관리.

기존 고정 익절 3% / 손절 1.5%(및 하이닉스 자동매매의 기존 tiered TP/SL)는 삭제하지
않고 이 엔진이 판단할 수 없을 때의 fallback으로만 사용한다. 이 엔진은 매수 판단에는
개입하지 않으며 오직 "이미 보유한 포지션을 언제/얼마나 청산할지"만 판단한다.

`compute_hynix_tech_indicators`/`hynix_technical_score`의 지표 계산 로직(RSI/MACD/
볼린저/Williams%R/Stochastic/VWAP)을 그대로 재사용하고, 여기서는 Profit Lock ·
Trailing Stop · Time Stop · Exit Score 판단만 추가한다.

요구사항(2026-07-16, ADAPTIVE_MARKET_REGIME 통합) — 장세 분류와 그에 따른
익절/손절/트레일링/최대보유시간/진입비중 프로필은 더 이상 이 엔진이 독자적으로
계산하지 않는다. 신규진입 게이트(hynix_switch_engine.evaluate_pullback_gate)와
Big Trend Holding(hynix_big_trend_engine)이 서로 다른 기준으로 장세를 판단해
진입·청산 기준이 충돌할 수 있었던 문제를, app.trading.adaptive_market_regime의
공용 분류(STRONG_UP/STRONG_DOWN/RANGE/HIGH_VOLATILITY/PANIC/REVERSAL/
DATA_INSUFFICIENT)로 통일한다. 예전 7종 시장유형(LOW_VOLATILITY/NORMAL/
HIGH_VOLATILITY/TREND_UP/TREND_DOWN/PANIC/SHORT_SQUEEZE)과 그 판정/프로필
로직은 중복 장세분류이므로 제거했다 — Profit Lock/Trailing/Time Stop/Exit
Score(장세와 무관한 공통 메커니즘)만 이 파일에 남아있다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from app.logger import logger
from app.models.hynix_technical_score import (
    _rsi_series, _macd_series, _bollinger_series, _williams_r_series,
    _stochastic_series, _vwap_from_1min, _resample,
)

ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = ROOT / "config" / "dynamic_exit_config.json"

_DEFAULT_CONFIG = {
    "profit_lock_steps": [[1.0, 0.0], [2.0, 1.0], [3.0, 2.0], [4.0, 3.0], [5.0, 4.0]],
    "time_stop": {"stagnant_minutes": 20, "stagnant_band_pct": 0.5, "max_minutes": 30, "trend_max_minutes": 60},
    "exit_score_weights": {
        "atr": 0.10, "macd": 0.15, "rsi": 0.10, "vwap": 0.10, "bollinger": 0.10,
        "williams": 0.10, "stochastic": 0.10, "volume": 0.10, "profit_lock": 0.10, "trailing": 0.15,
    },
    "exit_score_thresholds": {"sell_all": 90, "sell_partial": 70, "hold": 40},
}


def _load_config() -> dict:
    try:
        if _CONFIG_PATH.exists():
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            merged = dict(_DEFAULT_CONFIG)
            merged.update(data)
            return merged
    except Exception as exc:
        logger.debug("[DynamicExitEngine] 설정 로드 실패, 기본값 사용: %s", exc)
    return dict(_DEFAULT_CONFIG)


def _atr_pct(df_daily: pd.DataFrame, period: int) -> Optional[float]:
    if df_daily is None or len(df_daily) < period + 1:
        return None
    df = df_daily.sort_values("datetime")
    closes, highs, lows = df["close"], df["high"], df["low"]
    prev_close = closes.shift(1)
    tr = pd.concat([highs - lows, (highs - prev_close).abs(), (lows - prev_close).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(period).mean().iloc[-1])
    current = float(closes.iloc[-1])
    return round(atr / current * 100, 4) if current > 0 else None


def _relative_volume(df_1min: pd.DataFrame, lookback: int = 20) -> Optional[float]:
    if df_1min is None or len(df_1min) < 3:
        return None
    work = df_1min.sort_values("datetime")
    last_vol = float(work["volume"].iloc[-1])
    avg_vol = float(work["volume"].tail(min(lookback, len(work))).iloc[:-1].mean())
    if avg_vol <= 0:
        return None
    return round(last_vol / avg_vol, 3)


def _return_pct_over_minutes(df_1min: pd.DataFrame, minutes: int) -> Optional[float]:
    if df_1min is None or len(df_1min) < 2:
        return None
    work = df_1min.sort_values("datetime").tail(minutes + 1)
    if len(work) < 2:
        return None
    first = float(work.iloc[0]["close"])
    last = float(work.iloc[-1]["close"])
    return round((last / first - 1.0) * 100, 4) if first > 0 else None


class DynamicExitEngine:
    """실시간(1초 단위) 익절/손절/트레일링/Profit Lock 판단 엔진."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or _load_config()

    # ── 1. 지표 스냅샷 ────────────────────────────────────────────────────────
    def build_snapshot(
        self, position: dict, df_daily: Optional[pd.DataFrame], df_1min: Optional[pd.DataFrame],
        current_price: float, now: datetime, tick_strength: Optional[float] = None,
    ) -> dict:
        snapshot: dict = {
            "current_price": current_price,
            "entry_price": position.get("entry_price"),
            "highest_price": max(position.get("highest_price") or current_price, current_price),
            "lowest_price": min(position.get("lowest_price") or current_price, current_price),
            "held_minutes": None,
            "tick_strength": tick_strength,
        }
        if position.get("entry_time"):
            try:
                entry_time = datetime.fromisoformat(position["entry_time"])
                snapshot["held_minutes"] = max(0.0, (now - entry_time).total_seconds() / 60.0)
            except Exception:
                snapshot["held_minutes"] = None

        entry = snapshot["entry_price"]
        snapshot["profit_pct"] = round((current_price / entry - 1.0) * 100, 4) if entry else None

        if df_daily is not None and len(df_daily) >= 5:
            df = df_daily.sort_values("datetime")
            closes, highs, lows = df["close"], df["high"], df["low"]
            snapshot["atr_14_pct"] = _atr_pct(df_daily, 14)
            snapshot["atr_5_pct"] = _atr_pct(df_daily, 5)
            rsi_series = _rsi_series(closes)
            snapshot["rsi_14"] = round(float(rsi_series.iloc[-1]), 2) if len(rsi_series) and pd.notna(rsi_series.iloc[-1]) else None
            macd, signal, hist = _macd_series(closes)
            snapshot["macd"] = round(float(macd.iloc[-1]), 4) if len(macd) else None
            snapshot["macd_histogram"] = round(float(hist.iloc[-1]), 4) if len(hist) else None
            snapshot["macd_histogram_prev"] = round(float(hist.iloc[-2]), 4) if len(hist) > 1 else None
            upper, mid, lower = _bollinger_series(closes)
            if len(mid) and pd.notna(mid.iloc[-1]) and mid.iloc[-1]:
                snapshot["bollinger_width_pct"] = round(float((upper.iloc[-1] - lower.iloc[-1]) / mid.iloc[-1] * 100), 4)
                snapshot["bollinger_upper"] = round(float(upper.iloc[-1]), 2)
                snapshot["bollinger_lower"] = round(float(lower.iloc[-1]), 2)
            else:
                snapshot["bollinger_width_pct"] = None
            wr = _williams_r_series(highs, lows, closes)
            snapshot["williams_r"] = round(float(wr.iloc[-1]), 2) if len(wr) and pd.notna(wr.iloc[-1]) else None
            slow_k, slow_d = _stochastic_series(highs, lows, closes)
            snapshot["stochastic_k"] = round(float(slow_k.iloc[-1]), 2) if len(slow_k) and pd.notna(slow_k.iloc[-1]) else None
            snapshot["stochastic_d"] = round(float(slow_d.iloc[-1]), 2) if len(slow_d) and pd.notna(slow_d.iloc[-1]) else None
        else:
            for key in ("atr_14_pct", "atr_5_pct", "rsi_14", "macd", "macd_histogram", "macd_histogram_prev",
                        "bollinger_width_pct", "bollinger_upper", "bollinger_lower", "williams_r", "stochastic_k", "stochastic_d"):
                snapshot[key] = None

        if df_1min is not None and not df_1min.empty:
            df1 = df_1min.sort_values("datetime")
            snapshot["vwap"] = _vwap_from_1min(df1)
            snapshot["vwap_distance_pct"] = (
                round((current_price / snapshot["vwap"] - 1.0) * 100, 4) if snapshot.get("vwap") else None
            )
            snapshot["volume_last_bar"] = float(df1["volume"].iloc[-1])
            snapshot["relative_volume"] = _relative_volume(df1)
            snapshot["return_3m_pct"] = _return_pct_over_minutes(df1, 3)
            snapshot["return_5m_pct"] = _return_pct_over_minutes(df1, 5)
            df_3min = _resample(df1, 3)
            df_5min = _resample(df1, 5)
            snapshot["bar_1m_direction"] = _direction(df1)
            snapshot["bar_3m_direction"] = _direction(df_3min)
            snapshot["bar_5m_direction"] = _direction(df_5min)
        else:
            for key in ("vwap", "vwap_distance_pct", "volume_last_bar", "relative_volume",
                        "return_3m_pct", "return_5m_pct", "bar_1m_direction", "bar_3m_direction", "bar_5m_direction"):
                snapshot[key] = None

        return snapshot

    # ── 2. 시장유형(장세) 분류 — ADAPTIVE_MARKET_REGIME 공용 엔진에 위임 ──────
    # 요구사항(2026-07-16) — 신규진입 게이트/Big Trend Holding과 서로 다른
    # 기준으로 장세를 각자 판단하던 중복 로직을 제거하고, 하나의 공용 분류
    # (app.trading.adaptive_market_regime)만 사용한다.
    def classify_regime(
        self, df_1min: Optional[pd.DataFrame], df_daily: Optional[pd.DataFrame] = None,
        *, prev_close: Optional[float] = None, now: Optional[datetime] = None,
    ) -> dict:
        from app.trading.adaptive_market_regime import classify_raw_regime

        return classify_raw_regime(df_1min, df_daily, prev_close=prev_close, now=now)

    # 하위호환 별칭 — 예전 코드/테스트가 시장유형 문자열만 필요로 할 때.
    def classify_market(
        self, df_1min: Optional[pd.DataFrame] = None, df_daily: Optional[pd.DataFrame] = None,
        *, prev_close: Optional[float] = None, now: Optional[datetime] = None,
    ) -> str:
        return self.classify_regime(df_1min, df_daily, prev_close=prev_close, now=now)["regime"]

    # ── 3. 프로파일(장세별 TP/SL/Trailing) — 공용 리스크 프로필 사용 ─────────
    def get_profile(self, regime: str, position_symbol: Optional[str]) -> dict:
        """장세별 리스크 프로필을 공용 엔진(adaptive_market_regime.RISK_PROFILES)
        에서 조회한다. 인버스 보유 중에는 방향을 뒤집어 적용한다(하이닉스
        STRONG_DOWN=인버스 유리이므로 STRONG_UP 프로필을 적용하는 식)."""
        from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL
        from app.trading.adaptive_market_regime import get_risk_profile, STRONG_UP, STRONG_DOWN

        inverse_flip = {STRONG_UP: STRONG_DOWN, STRONG_DOWN: STRONG_UP}
        applied_regime = regime
        if position_symbol == INVERSE_SYMBOL and regime in inverse_flip:
            applied_regime = inverse_flip[regime]

        profile = get_risk_profile(applied_regime)
        profile["market_type"] = regime
        profile["regime"] = regime
        profile["applied_profile"] = applied_regime
        # decide()의 나머지 로직(Profit Lock/Trailing/Exit Score/기존 테스트)이
        # 참조하는 기존 필드명(tp_pct/sl_pct/trailing_pct/uses_trailing)을
        # 공용 프로필(tp1_pct/tp2_pct)에서 매핑해 채운다 — 익절은 "일부"가 있으면
        # 그 폭을, 없으면 최종 폭을 즉시청산 기준으로 쓴다.
        profile["tp_pct"] = profile.get("tp2_pct") if profile.get("tp2_pct") is not None else profile.get("tp1_pct")
        profile["sl_pct"] = profile.get("sl_pct")
        profile["trailing_pct"] = profile.get("trailing_pct") or 1.0
        profile["uses_trailing"] = bool(profile.get("uses_trailing"))
        return profile

    # ── 4. Profit Lock ───────────────────────────────────────────────────────
    def compute_profit_lock_floor(self, peak_profit_pct: float) -> Optional[float]:
        """수익 최고점 기준 손절선(%) 래칫. 한 번 올라간 손절선은 내려가지 않는다."""
        floor = None
        for milestone, lock_level in self.config["profit_lock_steps"]:
            if peak_profit_pct >= milestone:
                floor = lock_level
        return floor

    # ── 5. Trailing Stop ─────────────────────────────────────────────────────
    def update_trailing(self, position: dict, profile: dict, current_price: float, profit_pct: Optional[float]) -> dict:
        """트레일링 상태 갱신. position dict를 in-place 갱신하고 트리거 여부를 반환."""
        result = {"triggered": False, "reason": None}
        if not profile.get("uses_trailing") or profit_pct is None:
            return result

        if not position.get("trailing_armed") and profit_pct >= profile["tp_pct"]:
            position["trailing_armed"] = True
            position["trailing_peak_price"] = current_price
            return result

        if position.get("trailing_armed"):
            peak = max(position.get("trailing_peak_price") or current_price, current_price)
            position["trailing_peak_price"] = peak
            pullback_pct = (peak - current_price) / peak * 100 if peak > 0 else 0.0
            if pullback_pct >= profile["trailing_pct"]:
                result["triggered"] = True
                result["reason"] = f"Trailing Stop 발동(최고가 대비 -{pullback_pct:.2f}%, 트레일링폭 {profile['trailing_pct']}%)"
        return result

    # ── 6. Time Stop ─────────────────────────────────────────────────────────
    def check_time_stop(self, snapshot: dict, profile: dict) -> Optional[str]:
        """최대보유시간은 이제 장세별 리스크 프로필(profile["max_hold_minutes"])이
        결정한다(요구사항2 — RANGE 20분/PANIC 10분 등). 프로필이 명시하지 않으면
        (STRONG_UP/STRONG_DOWN/HIGH_VOLATILITY처럼 추세를 끝까지 태우는 장세)
        기존 안전망(trend_max_minutes, 기본 60분)을 그대로 적용해 무기한 보유를
        방지한다."""
        cfg = self.config["time_stop"]
        held = snapshot.get("held_minutes")
        profit_pct = snapshot.get("profit_pct")
        if held is None:
            return None

        hard_cap = profile.get("max_hold_minutes")
        if hard_cap is None:
            hard_cap = cfg["trend_max_minutes"]
        if held >= hard_cap:
            return f"시간손절(보유 {held:.0f}분 ≥ 최대 {hard_cap}분)"

        if held >= cfg["stagnant_minutes"] and profit_pct is not None and abs(profit_pct) <= cfg["stagnant_band_pct"]:
            return f"시간손절(보유 {held:.0f}분간 ±{cfg['stagnant_band_pct']}% 이내 정체)"
        return None

    # ── 7. Exit Score ────────────────────────────────────────────────────────
    def compute_exit_score(self, snapshot: dict, profile: dict, position: dict) -> dict:
        weights = self.config["exit_score_weights"]
        components: dict = {}

        atr = snapshot.get("atr_14_pct")
        atr5 = snapshot.get("atr_5_pct")
        if atr is not None and atr5 is not None and atr > 0:
            components["atr"] = max(0.0, min(100.0, 50 + (atr - atr5) / atr * 100))
        hist = snapshot.get("macd_histogram")
        hist_prev = snapshot.get("macd_histogram_prev")
        if hist is not None and hist_prev is not None:
            components["macd"] = max(0.0, min(100.0, 50 - (hist - hist_prev) * 300))
        rsi = snapshot.get("rsi_14")
        if rsi is not None:
            components["rsi"] = max(0.0, min(100.0, (rsi - 50) * 2)) if rsi >= 50 else max(0.0, min(100.0, (50 - rsi) * 2))
        vwap_dist = snapshot.get("vwap_distance_pct")
        if vwap_dist is not None:
            components["vwap"] = max(0.0, min(100.0, abs(vwap_dist) * 40))
        bb = snapshot.get("bollinger_width_pct")
        price = snapshot.get("current_price")
        upper = snapshot.get("bollinger_upper")
        if price is not None and upper is not None and upper > 0:
            components["bollinger"] = max(0.0, min(100.0, (price / upper) * 100)) if price >= upper * 0.9 else max(0.0, 50 - abs(bb or 0))
        wr = snapshot.get("williams_r")
        if wr is not None:
            components["williams"] = max(0.0, min(100.0, (wr + 100) if wr >= -20 else max(0.0, 100 + wr)))
        sk = snapshot.get("stochastic_k")
        if sk is not None:
            components["stochastic"] = max(0.0, min(100.0, sk)) if sk >= 80 else max(0.0, min(100.0, sk * 0.5))
        rel_vol = snapshot.get("relative_volume")
        if rel_vol is not None:
            components["volume"] = max(0.0, min(100.0, (rel_vol - 1.0) * 60))

        profit_pct = snapshot.get("profit_pct")
        lock_floor = self.compute_profit_lock_floor(max(position.get("profit_lock_peak_pct", 0.0), profit_pct or 0.0))
        if profit_pct is not None and lock_floor is not None:
            buffer_pct = max(0.0, profit_pct - lock_floor)
            components["profit_lock"] = max(0.0, min(100.0, 100 - buffer_pct * 40))
        else:
            components["profit_lock"] = 0.0

        if profile.get("uses_trailing") and position.get("trailing_armed"):
            peak = position.get("trailing_peak_price") or price
            current = price or peak
            if peak:
                used_pct = (peak - current) / peak * 100
                components["trailing"] = max(0.0, min(100.0, used_pct / profile["trailing_pct"] * 100))
        else:
            components["trailing"] = 0.0

        weighted, total_w = 0.0, 0.0
        for key, weight in weights.items():
            if key in components:
                weighted += components[key] * weight
                total_w += weight
        score = round(weighted / total_w, 2) if total_w > 0 else 0.0
        return {"exit_score": score, "components": {k: round(v, 2) for k, v in components.items()}}

    # ── 8. 종합 판단 ─────────────────────────────────────────────────────────
    def decide(
        self, position: dict, df_daily: Optional[pd.DataFrame], df_1min: Optional[pd.DataFrame],
        current_price: float, now: datetime, tick_strength: Optional[float] = None,
        prev_close: Optional[float] = None, opposite_signal_streak: int = 0,
        confirmed_regime: Optional[str] = None,
    ) -> dict:
        """포지션 청산 여부를 종합 판단한다. position dict는 트레일링/최고저가 추적을 위해 in-place 갱신됨."""
        snapshot = self.build_snapshot(position, df_daily, df_1min, current_price, now, tick_strength)
        position["highest_price"] = snapshot["highest_price"]
        position["lowest_price"] = snapshot["lowest_price"]
        if snapshot.get("profit_pct") is not None:
            position["profit_lock_peak_pct"] = max(position.get("profit_lock_peak_pct", 0.0), snapshot["profit_pct"])

        if prev_close is None and df_daily is not None and len(df_daily) >= 2:
            try:
                prev_close = float(df_daily.sort_values("datetime")["close"].iloc[-2])
            except Exception:
                prev_close = None
        regime_result = self.classify_regime(df_1min, df_daily, prev_close=prev_close, now=now)
        market_type = regime_result["regime"]
        profile = self.get_profile(market_type, position.get("symbol"))
        profit_pct = snapshot.get("profit_pct")

        thresholds = self.config["exit_score_thresholds"]
        base_result = {
            "market_type": market_type, "regime": market_type, "regime_confidence": regime_result.get("confidence"),
            "regime_reasons": regime_result.get("reasons"), "profile": profile, "snapshot": snapshot,
            "tp_pct": profile["tp_pct"], "sl_pct": profile["sl_pct"],
            "trailing_pct": profile["trailing_pct"], "trailing_enabled": profile["uses_trailing"],
            "trailing_armed": bool(position.get("trailing_armed")),
            "profit_lock_floor_pct": self.compute_profit_lock_floor(position.get("profit_lock_peak_pct", 0.0)),
        }

        if profit_pct is None:
            return {**base_result, "action": "HOLD", "ratio": 0.0, "exit_score": 0.0, "score_breakdown": {}, "reason": "현재가/매수가 정보 부족"}

        # ① 시간손절 (최우선)
        time_stop_reason = self.check_time_stop(snapshot, profile)
        if time_stop_reason:
            return {**base_result, "action": "SELL_ALL", "ratio": 1.0, "exit_score": 100.0, "score_breakdown": {}, "reason": time_stop_reason}

        # ①-b 반대 강신호 단계적 대응(VOLATILE_RANGE 요구사항 — 1회=50% 축소,
        # 2회=전량청산). TP/SL 도달 여부와 무관하게 시간손절 다음으로 최우선 적용된다.
        if opposite_signal_streak:
            from app.trading.adaptive_market_regime import opposite_signal_response

            opposite_action = opposite_signal_response(opposite_signal_streak, profile.get("applied_profile", market_type))
            if opposite_action:
                return {
                    **base_result, "action": opposite_action["action"], "ratio": opposite_action["ratio"],
                    "exit_score": 100.0, "score_breakdown": {}, "reason": opposite_action["reason"],
                }

        # ①-c 추세 반전 확정(큰 추세 수익 극대화 요구사항) — VWAP 이탈+15분반전+
        # 주요 스윙 붕괴가 2회 연속으로 확인돼 confirmed_regime이 보유 방향과
        # 반대되는 STRONG 추세로 이미 확정됐다면(2연속 확인은 caller가
        # compute_and_confirm_regime()/update_regime_confirmation()으로 수행 —
        # 이 함수는 순간 raw 재분류만 하므로 노이즈 방지를 위해 반드시
        # confirmed_regime을 명시적으로 받아야만 이 트리거가 동작한다), TP/SL
        # 도달 여부와 무관하게 즉시 전량청산한다. 작은 1·3·5분 반대신호만으로는
        # (confirmed_regime이 주어지지 않는 한) 청산하지 않는다.
        if confirmed_regime:
            from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL as _INVERSE_SYMBOL
            from app.trading.adaptive_market_regime import STRONG_UP as _STRONG_UP, STRONG_DOWN as _STRONG_DOWN

            position_symbol = position.get("symbol")
            reversal_confirmed = (
                (position_symbol == _INVERSE_SYMBOL and confirmed_regime == _STRONG_UP)
                or (position_symbol and position_symbol != _INVERSE_SYMBOL and confirmed_regime == _STRONG_DOWN)
            )
            if reversal_confirmed:
                return {
                    **base_result, "action": "SELL_ALL", "ratio": 1.0, "exit_score": 100.0, "score_breakdown": {},
                    "reason": f"추세 반전 확정({confirmed_regime}) — VWAP 이탈+15분 반전+스윙 붕괴 2회 확인, 전량청산",
                }

        # ② Profit Lock 손절선 붕괴
        lock_floor = base_result["profit_lock_floor_pct"]
        if lock_floor is not None and profit_pct <= lock_floor:
            return {**base_result, "action": "SELL_ALL", "ratio": 1.0, "exit_score": 100.0, "score_breakdown": {},
                    "reason": f"Profit Lock 발동(수익 {profit_pct:.2f}% ≤ 잠금 손절선 {lock_floor:.2f}%)"}

        # ③ Trailing Stop
        trailing = self.update_trailing(position, profile, current_price, profit_pct)
        base_result["trailing_armed"] = bool(position.get("trailing_armed"))
        if trailing["triggered"]:
            return {**base_result, "action": "SELL_ALL", "ratio": 1.0, "exit_score": 100.0, "score_breakdown": {}, "reason": trailing["reason"]}

        # ④ 손절 (장세별 리스크 프로필 기준)
        if profit_pct <= -profile["sl_pct"]:
            return {**base_result, "action": "SELL_ALL", "ratio": 1.0, "exit_score": 100.0, "score_breakdown": {},
                    "reason": f"손절({profit_pct:.2f}%≤-{profile['sl_pct']}%, {market_type})"}

        # ⑤ 익절 — tp2(전량)가 있으면 tp1(부분) 먼저, 없으면 tp1이 곧 전량 기준.
        # 트레일링을 쓰는 장세(STRONG_UP/STRONG_DOWN)는 tp1에서 일부만 확정하고
        # 나머지는 트레일링에 맡긴다(요구사항2 "+2% 일부익절, 나머지 ATR trailing").
        tp1_pct, tp2_pct = profile.get("tp1_pct"), profile.get("tp2_pct")
        if tp2_pct is not None and profit_pct >= tp2_pct:
            return {**base_result, "action": "SELL_ALL", "ratio": 1.0, "exit_score": 100.0, "score_breakdown": {},
                    "reason": f"익절(+{profit_pct:.2f}%≥{tp2_pct}%, {market_type})"}
        if tp1_pct is not None and profit_pct >= tp1_pct and not position.get("partial_tp1_done"):
            # ratio는 프로필의 tp1_ratio를 그대로 쓴다 — RANGE/HIGH_VOLATILITY/PANIC
            # 처럼 tp2가 있는 2단계 프로필은 tp1_ratio(보통 0.5)만큼만 부분매도하고,
            # STRONG_UP/STRONG_DOWN(tp2 없음, uses_trailing=True)도 마찬가지로
            # tp1_ratio만큼만 확정하고 나머지는 트레일링에 맡긴다(요구사항2 "+2%
            # 일부익절, 나머지 ATR trailing"). REVERSAL/DATA_INSUFFICIENT처럼
            # tp1_ratio=1.0인 단일단계 프로필만 이 시점에 전량 매도된다.
            ratio = profile.get("tp1_ratio", 0.5)
            position["partial_tp1_done"] = True
            return {**base_result, "action": "SELL_ALL" if ratio >= 1.0 else "SELL_PARTIAL", "ratio": ratio,
                    "exit_score": 100.0, "score_breakdown": {},
                    "reason": f"{'익절' if ratio >= 1.0 else '부분익절'}(+{profit_pct:.2f}%≥{tp1_pct}%, {market_type})"}

        # ⑤ Exit Score 기반 소프트 판단
        score_result = self.compute_exit_score(snapshot, profile, position)
        exit_score = score_result["exit_score"]
        if exit_score >= thresholds["sell_all"]:
            action, ratio, reason = "SELL_ALL", 1.0, f"Exit Score {exit_score:.0f} ≥ {thresholds['sell_all']} — 즉시 매도"
        elif exit_score >= thresholds["sell_partial"]:
            action, ratio, reason = "SELL_PARTIAL", 0.5, f"Exit Score {exit_score:.0f} ≥ {thresholds['sell_partial']} — 부분매도"
        elif exit_score >= thresholds["hold"]:
            action, ratio, reason = "HOLD", 0.0, f"Exit Score {exit_score:.0f} — 보유"
        else:
            action, ratio, reason = "HOLD", 0.0, f"Exit Score {exit_score:.0f} — 추가보유(약한 청산신호)"

        return {**base_result, "action": action, "ratio": ratio, "exit_score": exit_score,
                "score_breakdown": score_result["components"], "reason": reason}


def _direction(df: Optional[pd.DataFrame]) -> Optional[str]:
    if df is None or df.empty:
        return None
    try:
        last = df.iloc[-1]
        if float(last["close"]) > float(last["open"]):
            return "up"
        if float(last["close"]) < float(last["open"]):
            return "down"
        return "flat"
    except Exception:
        return None
