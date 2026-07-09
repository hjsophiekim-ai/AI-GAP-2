"""
dynamic_exit_engine.py — Dynamic Exit AI: 익절/손절/트레일링/Profit Lock을 실시간으로 관리.

기존 고정 익절 3% / 손절 1.5%(및 하이닉스 자동매매의 기존 tiered TP/SL)는 삭제하지
않고 이 엔진이 판단할 수 없을 때의 fallback으로만 사용한다. 이 엔진은 매수 판단에는
개입하지 않으며 오직 "이미 보유한 포지션을 언제/얼마나 청산할지"만 판단한다.

`compute_hynix_tech_indicators`/`hynix_technical_score`의 지표 계산 로직(RSI/MACD/
볼린저/Williams%R/Stochastic/VWAP)을 그대로 재사용하고, 여기서는 시장유형 분류 ·
Profit Lock · Trailing Stop · Exit Score 판단만 추가한다.
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

LOW_VOLATILITY = "LOW_VOLATILITY"
NORMAL = "NORMAL"
HIGH_VOLATILITY = "HIGH_VOLATILITY"
TREND_UP = "TREND_UP"
TREND_DOWN = "TREND_DOWN"
PANIC = "PANIC"
SHORT_SQUEEZE = "SHORT_SQUEEZE"

_INVERSE_FLIP = {TREND_UP: TREND_DOWN, TREND_DOWN: TREND_UP}

_DEFAULT_CONFIG = {
    "market_profiles": {
        LOW_VOLATILITY: {"tp_pct": 2.0, "sl_pct": 1.0, "trailing_pct": 1.0, "uses_trailing": False},
        NORMAL: {"tp_pct": 3.0, "sl_pct": 1.5, "trailing_pct": 1.0, "uses_trailing": False},
        HIGH_VOLATILITY: {"tp_pct": 4.5, "sl_pct": 2.2, "trailing_pct": 2.0, "uses_trailing": False},
        TREND_UP: {"tp_pct": 6.0, "sl_pct": 2.5, "trailing_pct": 2.0, "uses_trailing": True},
        TREND_DOWN: {"tp_pct": 2.5, "sl_pct": 1.2, "trailing_pct": 1.0, "uses_trailing": False},
        PANIC: {"tp_pct": 2.0, "sl_pct": 0.8, "trailing_pct": 1.0, "uses_trailing": False},
        SHORT_SQUEEZE: {"tp_pct": 6.0, "sl_pct": 2.5, "trailing_pct": 3.0, "uses_trailing": True},
    },
    "fallback": {"tp_pct": 3.0, "sl_pct": 1.5},
    "profit_lock_steps": [[1.0, 0.0], [2.0, 1.0], [3.0, 2.0], [4.0, 3.0], [5.0, 4.0]],
    "time_stop": {"stagnant_minutes": 20, "stagnant_band_pct": 0.5, "max_minutes": 30, "trend_max_minutes": 60},
    "panic_detection": {"return_3m_pct": -1.5, "relative_volume": 2.0},
    "short_squeeze_detection": {"return_3m_pct": 1.5, "relative_volume": 2.0, "prior_oversold_rsi": 35},
    "trend_detection": {"trend_score_up": 65, "trend_score_down": 35},
    "volatility_detection": {"high": 65, "low": 35},
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
            merged.update({k: v for k, v in data.items() if k != "market_profiles"})
            if "market_profiles" in data:
                merged["market_profiles"] = {**_DEFAULT_CONFIG["market_profiles"], **data["market_profiles"]}
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

    # ── 2. 시장유형 분류 (7종) ────────────────────────────────────────────────
    def classify_market(self, snapshot: dict) -> str:
        panic_cfg = self.config["panic_detection"]
        squeeze_cfg = self.config["short_squeeze_detection"]
        ret3m = snapshot.get("return_3m_pct")
        rel_vol = snapshot.get("relative_volume")

        if ret3m is not None and rel_vol is not None:
            if ret3m <= panic_cfg["return_3m_pct"] and rel_vol >= panic_cfg["relative_volume"]:
                return PANIC
            if (ret3m >= squeeze_cfg["return_3m_pct"] and rel_vol >= squeeze_cfg["relative_volume"]
                    and (snapshot.get("rsi_14") or 50) <= squeeze_cfg["prior_oversold_rsi"] + 15):
                return SHORT_SQUEEZE

        trend_score = self._trend_score(snapshot)
        trend_cfg = self.config["trend_detection"]
        if trend_score >= trend_cfg["trend_score_up"]:
            return TREND_UP
        if trend_score <= trend_cfg["trend_score_down"]:
            return TREND_DOWN

        volatility_score = self._volatility_score(snapshot)
        vol_cfg = self.config["volatility_detection"]
        if volatility_score >= vol_cfg["high"]:
            return HIGH_VOLATILITY
        if volatility_score <= vol_cfg["low"]:
            return LOW_VOLATILITY
        return NORMAL

    def _volatility_score(self, snapshot: dict) -> float:
        parts = []
        atr = snapshot.get("atr_14_pct")
        if atr is not None:
            parts.append(50 + max(-50, min(50, (atr - 1.8) / 1.5 * 50)))
        bb = snapshot.get("bollinger_width_pct")
        if bb is not None:
            parts.append(50 + max(-50, min(50, (bb - 4.0) / 3.0 * 50)))
        ret5 = snapshot.get("return_5m_pct")
        if ret5 is not None:
            parts.append(50 + max(-50, min(50, (abs(ret5) - 0.35) / 0.4 * 50)))
        return sum(parts) / len(parts) if parts else 50.0

    def _trend_score(self, snapshot: dict) -> float:
        parts = []
        hist = snapshot.get("macd_histogram")
        if hist is not None:
            atr_ref = max(snapshot.get("atr_14_pct") or 1.5, 0.5)
            parts.append(50 + max(-50, min(50, hist / atr_ref * 200)))
        vwap_dist = snapshot.get("vwap_distance_pct")
        if vwap_dist is not None:
            parts.append(50 + max(-50, min(50, vwap_dist / 1.0 * 50)))
        rsi = snapshot.get("rsi_14")
        if rsi is not None:
            parts.append(rsi)
        ret5 = snapshot.get("return_5m_pct")
        if ret5 is not None:
            parts.append(50 + max(-50, min(50, ret5 / 1.5 * 50)))
        return sum(parts) / len(parts) if parts else 50.0

    # ── 3. 프로파일(시장유형별 TP/SL/Trailing) ───────────────────────────────
    def get_profile(self, market_type: str, position_symbol: Optional[str]) -> dict:
        from app.data_sources.hynix_inverse_collector import INVERSE_SYMBOL

        applied_type = market_type
        if position_symbol == INVERSE_SYMBOL and market_type in _INVERSE_FLIP:
            applied_type = _INVERSE_FLIP[market_type]

        profile = dict(self.config["market_profiles"].get(applied_type, self.config["fallback"]))
        profile["market_type"] = market_type
        profile["applied_profile"] = applied_type
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
    def check_time_stop(self, snapshot: dict, market_type: str) -> Optional[str]:
        cfg = self.config["time_stop"]
        held = snapshot.get("held_minutes")
        profit_pct = snapshot.get("profit_pct")
        if held is None:
            return None

        is_strong_trend = market_type in (TREND_UP, TREND_DOWN, SHORT_SQUEEZE)
        hard_cap = cfg["trend_max_minutes"] if is_strong_trend else cfg["max_minutes"]
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
    ) -> dict:
        """포지션 청산 여부를 종합 판단한다. position dict는 트레일링/최고저가 추적을 위해 in-place 갱신됨."""
        snapshot = self.build_snapshot(position, df_daily, df_1min, current_price, now, tick_strength)
        position["highest_price"] = snapshot["highest_price"]
        position["lowest_price"] = snapshot["lowest_price"]
        if snapshot.get("profit_pct") is not None:
            position["profit_lock_peak_pct"] = max(position.get("profit_lock_peak_pct", 0.0), snapshot["profit_pct"])

        market_type = self.classify_market(snapshot)
        profile = self.get_profile(market_type, position.get("symbol"))
        profit_pct = snapshot.get("profit_pct")

        thresholds = self.config["exit_score_thresholds"]
        base_result = {
            "market_type": market_type, "profile": profile, "snapshot": snapshot,
            "tp_pct": profile["tp_pct"], "sl_pct": profile["sl_pct"],
            "trailing_pct": profile["trailing_pct"], "trailing_enabled": profile["uses_trailing"],
            "trailing_armed": bool(position.get("trailing_armed")),
            "profit_lock_floor_pct": self.compute_profit_lock_floor(position.get("profit_lock_peak_pct", 0.0)),
        }

        if profit_pct is None:
            return {**base_result, "action": "HOLD", "ratio": 0.0, "exit_score": 0.0, "score_breakdown": {}, "reason": "현재가/매수가 정보 부족"}

        # ① 시간손절 (최우선)
        time_stop_reason = self.check_time_stop(snapshot, market_type)
        if time_stop_reason:
            return {**base_result, "action": "SELL_ALL", "ratio": 1.0, "exit_score": 100.0, "score_breakdown": {}, "reason": time_stop_reason}

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

        # ④ 시장유형별 기본 익절/손절 (트레일링 미사용 시에만 익절 즉시청산)
        if profit_pct >= profile["tp_pct"] and not profile["uses_trailing"]:
            return {**base_result, "action": "SELL_ALL", "ratio": 1.0, "exit_score": 100.0, "score_breakdown": {},
                    "reason": f"익절(+{profit_pct:.2f}%≥{profile['tp_pct']}%, {market_type})"}
        if profit_pct <= -profile["sl_pct"]:
            return {**base_result, "action": "SELL_ALL", "ratio": 1.0, "exit_score": 100.0, "score_breakdown": {},
                    "reason": f"손절({profit_pct:.2f}%≤-{profile['sl_pct']}%, {market_type})"}

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
